"""Paid-channel access: join requests (approve or decline via is_entitled) and
chat_member updates (keeping users.in_paid_channel in step).

Everyone gets the same join-request link, so sharing it is harmless: the bot
decides each request. Only the configured paid channel is handled.
"""
from __future__ import annotations

import enum
import html
import logging
from datetime import datetime

from . import texts
from .audit import audit
from .billing import poll_checkouts
from .keyboards import subscribe_keyboard
from .services import Services
from .telegram_gateway import IN_CHANNEL_STATUSES
from .users import ensure_user, get_user

log = logging.getLogger(__name__)


class JoinOutcome(enum.Enum):
    APPROVED = "approved"
    APPROVE_FAILED = "approve_failed"
    DECLINED_UNKNOWN = "declined_unknown"
    DECLINED_BANNED = "declined_banned"
    DECLINED_NOT_ENTITLED = "declined_not_entitled"


def in_channel(status: str, is_member: bool | None = None) -> bool:
    if status == "restricted":
        return bool(is_member)
    return status in IN_CHANNEL_STATUSES


async def set_in_channel(
    svc: Services, telegram_id: int, value: bool, now: datetime, detail: str | None = None
) -> bool:
    """Returns True (and audits channel_join / channel_leave) only if the flag changed."""
    async with svc.db.tx() as conn:
        cur = await conn.execute(
            "UPDATE users SET in_paid_channel=? WHERE telegram_id=? AND in_paid_channel IS NOT ?",
            (int(value), telegram_id, int(value)),
        )
        changed = cur.rowcount == 1
        if changed:
            await audit(conn, now, telegram_id, "channel_join" if value else "channel_leave", detail)
    return changed


async def _decline(svc: Services, user_id: int, now: datetime, why: str) -> None:
    ok = await svc.tg.decline_join(user_id)
    if not ok:
        log.error("Couldn't decline the join request from %s (%s); it stays pending", user_id, why)
    async with svc.db.tx() as conn:
        await audit(conn, now, user_id, "join_declined" if ok else "join_decline_failed", why)


def _approve_failed_text(svc: Services) -> str:
    support = svc.config.support_contact
    if support:
        return texts.JOIN_APPROVE_FAILED + " " + texts.CONTACT_SUPPORT.format(support=html.escape(support))
    return texts.JOIN_APPROVE_FAILED


async def handle_join_request(svc: Services, user_id: int, user_chat_id: int, now: datetime) -> JoinOutcome:
    # user_chat_id only works "until the join request is processed", and for people
    # who never pressed /start it's the only way to DM them, so every DM goes out
    # before the decline. try/finally: the request is declined even if the DM fails.
    user = await get_user(svc.db, user_id)
    if user is None:
        try:
            await svc.tg.send(user_chat_id, texts.TRIAL_START_FIRST)  # failures ignored
        finally:
            await _decline(svc, user_id, now, "unknown user")
        return JoinOutcome.DECLINED_UNKNOWN

    if user.banned:
        await _decline(svc, user_id, now, "banned")
        return JoinOutcome.DECLINED_BANNED

    if not svc.entitled(user, now) and svc.stripe is not None:
        # They may have paid seconds ago, before the 30-second poll saw it.
        try:
            await poll_checkouts(svc, now, only_telegram_id=user_id)
        except Exception:
            log.exception("Checkout poll during join request for %s failed", user_id)
        user = await get_user(svc.db, user_id) or user

    if svc.entitled(user, now):
        if not await svc.tg.approve_join(user_id):
            # Telegram won't send this request again and nothing retries it, so at
            # least leave a trace and tell them (the request is still pending, so
            # user_chat_id still works).
            log.error("Couldn't approve the join request from entitled user %s; it stays pending", user_id)
            async with svc.db.tx() as conn:
                await audit(conn, now, user_id, "join_approve_failed")
            await svc.tg.send(user_chat_id, _approve_failed_text(svc))  # failures ignored
            return JoinOutcome.APPROVE_FAILED
        async with svc.db.tx() as conn:
            await conn.execute("UPDATE users SET in_paid_channel=1 WHERE telegram_id=?", (user_id,))
            await audit(conn, now, user_id, "join_approved")
        return JoinOutcome.APPROVED

    try:
        await svc.tg.send(user_chat_id, texts.JOIN_DECLINED, subscribe_keyboard(user))  # failures ignored
    finally:
        await _decline(svc, user_id, now, "not entitled")
    return JoinOutcome.DECLINED_NOT_ENTITLED


async def handle_member_update(
    svc: Services,
    user_id: int,
    username: str | None,
    first_name: str | None,
    status: str,
    is_member: bool | None,
    now: datetime,
) -> bool:
    """A chat_member update for the paid channel. Returns the new in-channel flag."""
    await ensure_user(svc.db, user_id, username, first_name, now)
    value = in_channel(status, is_member)
    await set_in_channel(svc, user_id, value, now, f"status={status}")
    return value
