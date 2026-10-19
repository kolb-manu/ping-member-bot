"""Wrapper for the Telegram calls the jobs make on their own (DMs and paid-channel
admin actions), so the logic can be tested with a fake.

Handles the robustness rules: a global throttle (~20 messages/s), waiting out
429 flood control, retrying network errors with backoff, and treating a 403 on
a DM as "this user blocked us" (no retry). Admin actions are logged at INFO.

A response that isn't JSON at all (e.g. an HTML 502 page from a proxy) arrives
as aiogram's ClientDecodeError, which isn't a TelegramAPIError; it's handled like
a server error. A JSON response aiogram can't read means the call probably
worked, so that one is never retried (a DM must not be sent twice).
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import Bot
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup
from pydantic import ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T")

MESSAGES_PER_SECOND = 20
NETWORK_ATTEMPTS = 3
MAX_FLOOD_WAITS = 5
BACKOFF_SECONDS = 1.0
IN_CHANNEL_STATUSES = frozenset({"member", "administrator", "creator", "restricted"})

# Telegram's wording when the user isn't (or was never) in the chat.
_NOT_MEMBER_HINTS = ("user_not_participant", "not a member", "participant_id_invalid",
                     "user not found", "member not found")


class SendResult(enum.Enum):
    OK = "ok"
    BLOCKED = "blocked"      # 403: user blocked the bot or never started it. Don't retry.
    FAILED = "failed"        # network / flood trouble that outlasted the retries. Retry later.
    REJECTED = "rejected"    # any other API error (e.g. 400). Retrying won't help.


def _is_not_member(exc: TelegramAPIError) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _NOT_MEMBER_HINTS)


def _undecodable(exc: ClientDecodeError) -> bool:
    """True for a body that isn't JSON (proxy error page): the call didn't happen.
    False for JSON aiogram couldn't validate: Telegram probably did it."""
    return not isinstance(exc.original, ValidationError)


# Errors after which the outcome of an admin action or lookup is just unknown.
_API_FAILURES = (TelegramAPIError, ClientDecodeError)


def _describe(exc: Exception) -> str:
    # ClientDecodeError's str() includes the whole response body (an HTML page).
    return exc.message if isinstance(exc, ClientDecodeError) else str(exc)


class _Throttle:
    """Spaces out calls so the whole bot stays under the global send limit."""

    def __init__(self, per_second: float, sleep: Callable[[float], Awaitable[Any]]):
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._sleep = sleep

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await self._sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


class TelegramGateway:
    def __init__(
        self,
        bot: Bot,
        paid_channel_id: int,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        per_second: float = MESSAGES_PER_SECOND,
    ):
        self.bot = bot
        self.paid_channel_id = paid_channel_id
        self._sleep = sleep
        self._throttle = _Throttle(per_second, sleep)

    async def _call(self, what: str, make: Callable[[], Awaitable[T]]) -> T:
        """Run one API call with flood-control waits and network retries.
        Anything else (403, 400, ...) is raised straight away."""
        failures = 0
        flood_waits = 0
        while True:
            await self._throttle.wait()
            try:
                return await make()
            except TelegramRetryAfter as exc:
                flood_waits += 1
                if flood_waits > MAX_FLOOD_WAITS:
                    raise
                log.warning("Telegram flood control on %s, waiting %ss", what, exc.retry_after)
                await self._sleep(exc.retry_after)
            except (TelegramNetworkError, TelegramServerError, ClientDecodeError) as exc:
                if isinstance(exc, ClientDecodeError) and not _undecodable(exc):
                    raise
                failures += 1
                if failures >= NETWORK_ATTEMPTS:
                    raise
                log.warning("Telegram %s failed (%s), retry %d", what, type(exc).__name__, failures)
                await self._sleep(BACKOFF_SECONDS * 2 ** (failures - 1))

    async def send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> SendResult:
        try:
            await self._call("send_message", lambda: self.bot.send_message(chat_id, text, reply_markup=markup))
        except TelegramForbiddenError:
            log.info("DM to %s refused (blocked or never started the bot)", chat_id)
            return SendResult.BLOCKED
        except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as exc:
            log.warning("DM to %s failed after retries: %s", chat_id, type(exc).__name__)
            return SendResult.FAILED
        except ClientDecodeError as exc:
            if _undecodable(exc):
                log.warning("DM to %s failed after retries: Telegram's reply wasn't JSON", chat_id)
                return SendResult.FAILED
            # Probably delivered; treat as done so it's never sent twice.
            log.error("DM to %s: couldn't read Telegram's reply, not retrying: %s", chat_id, exc.message)
            return SendResult.REJECTED
        except TelegramAPIError as exc:
            log.warning("DM to %s rejected: %s", chat_id, exc)
            return SendResult.REJECTED
        return SendResult.OK

    async def approve_join(self, user_id: int) -> bool:
        return await self._join_action("approve", user_id)

    async def decline_join(self, user_id: int) -> bool:
        return await self._join_action("decline", user_id)

    async def _join_action(self, action: str, user_id: int) -> bool:
        method = self.bot.approve_chat_join_request if action == "approve" else self.bot.decline_chat_join_request
        log.info("Paid channel: %s join request from %s", action, user_id)
        try:
            await self._call(f"{action}_chat_join_request", lambda: method(self.paid_channel_id, user_id))
        except _API_FAILURES as exc:
            log.warning("Could not %s join request from %s: %s", action, user_id, _describe(exc))
            return False
        return True

    async def member_status(self, user_id: int) -> str | None:
        """'member', 'administrator', 'creator', 'restricted', 'left' or 'kicked'.
        A restricted user who isn't actually in the channel counts as 'left'.
        None means we couldn't find out, so callers must not act on it."""
        try:
            member = await self._call(
                "get_chat_member", lambda: self.bot.get_chat_member(self.paid_channel_id, user_id)
            )
        except TelegramBadRequest as exc:
            if _is_not_member(exc):
                return "left"
            log.warning("get_chat_member for %s failed: %s", user_id, exc)
            return None
        except _API_FAILURES as exc:
            log.warning("get_chat_member for %s failed: %s", user_id, _describe(exc))
            return None
        status = getattr(member.status, "value", member.status)
        if status == "restricted" and not getattr(member, "is_member", False):
            return "left"
        return str(status)

    async def kick(self, user_id: int, permanent: bool = False) -> bool:
        """Remove from the paid channel but allow rejoining later (ban + unban).
        permanent=True (users flagged banned) skips the unban. 'Not a member' counts
        as success."""
        log.info("Paid channel: removing %s%s", user_id, " (permanent ban)" if permanent else "")
        try:
            await self._call("ban_chat_member", lambda: self.bot.ban_chat_member(self.paid_channel_id, user_id))
        except TelegramBadRequest as exc:
            if _is_not_member(exc):
                log.info("%s was not in the paid channel", user_id)
                return True
            log.error("Could not remove %s: %s", user_id, exc)
            return False
        except _API_FAILURES as exc:
            log.error("Could not remove %s: %s", user_id, _describe(exc))
            return False
        if permanent:
            return True
        try:
            await self._call(
                "unban_chat_member",
                lambda: self.bot.unban_chat_member(self.paid_channel_id, user_id, only_if_banned=True),
            )
        except _API_FAILURES as exc:
            # They're out, but still banned, so they couldn't rejoin after paying.
            log.error("Removed %s but the unban failed, they stay banned until unbanned: %s",
                      user_id, _describe(exc))
        return True

    async def unban(self, user_id: int) -> bool:
        """Lift a permanent ban so they can request to join again (only_if_banned: a
        no-op for anyone who isn't banned)."""
        log.info("Paid channel: lifting any ban on %s", user_id)
        try:
            await self._call(
                "unban_chat_member",
                lambda: self.bot.unban_chat_member(self.paid_channel_id, user_id, only_if_banned=True),
            )
        except _API_FAILURES as exc:
            log.error("Could not lift the ban on %s: %s", user_id, _describe(exc))
            return False
        return True

    async def bot_rights_problems(self, bot_id: int) -> list[str]:
        """Startup check: is the bot an admin of the paid channel with the rights it needs?"""
        try:
            me = await self._call("get_chat_member", lambda: self.bot.get_chat_member(self.paid_channel_id, bot_id))
        except _API_FAILURES as exc:
            return [f"couldn't check the bot's rights in the paid channel ({_describe(exc)})"]
        if getattr(me.status, "value", me.status) != "administrator":
            return ["the bot is not an admin of the paid channel"]
        problems = []
        if not getattr(me, "can_invite_users", False):
            problems.append("the bot lacks the 'Invite users via link' right (needed to approve join requests)")
        if not getattr(me, "can_restrict_members", False):
            problems.append("the bot lacks the 'Ban users' right (needed to remove people)")
        return problems
