"""TelegramGateway error handling against a scripted fake Bot (no network)."""
import json
from types import SimpleNamespace

import pytest

from aiogram.exceptions import (
    ClientDecodeError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from aiogram.types import User
from pydantic import ValidationError

from first_in_bot.telegram_gateway import SendResult, TelegramGateway

CHANNEL = -1001234567890
M = SendMessage(chat_id=1, text="x")


def forbidden():
    return TelegramForbiddenError(method=M, message="Forbidden: bot was blocked by the user")


def network():
    return TelegramNetworkError(method=M, message="Request timeout error")


def flood(seconds=3):
    return TelegramRetryAfter(method=M, message="Too Many Requests", retry_after=seconds)


def bad_request(text):
    return TelegramBadRequest(method=M, message=f"Bad Request: {text}")


def html_502():
    """What aiogram raises when a proxy answers with an HTML error page."""
    body = "<html><body><h1>502 Bad Gateway</h1></body></html>"
    try:
        json.loads(body)
    except ValueError as exc:
        return ClientDecodeError("Failed to decode object", exc, body)


def unreadable_json():
    """JSON that aiogram couldn't validate: the call itself probably worked."""
    try:
        User.model_validate({})
    except ValidationError as exc:
        return ClientDecodeError("Failed to deserialize object", exc, {"ok": True, "result": {}})


class ScriptedBot:
    """Each method pops its next outcome: an exception to raise, or a value to return."""

    def __init__(self, **scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    def _next(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        script = self.scripts.get(name, [])
        outcome = script.pop(0) if script else True
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def send_message(self, chat_id, text, reply_markup=None):
        return self._next("send_message", chat_id, text)

    async def approve_chat_join_request(self, chat_id, user_id):
        return self._next("approve", chat_id, user_id)

    async def decline_chat_join_request(self, chat_id, user_id):
        return self._next("decline", chat_id, user_id)

    async def get_chat_member(self, chat_id, user_id):
        return self._next("get_chat_member", chat_id, user_id)

    async def ban_chat_member(self, chat_id, user_id):
        return self._next("ban", chat_id, user_id)

    async def unban_chat_member(self, chat_id, user_id, only_if_banned=None):
        return self._next("unban", chat_id, user_id, only_if_banned=only_if_banned)


def gateway(bot):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    gw = TelegramGateway(bot, CHANNEL, sleep=fake_sleep)
    return gw, sleeps


async def test_send_ok():
    bot = ScriptedBot()
    gw, _ = gateway(bot)
    assert await gw.send(5, "hi") is SendResult.OK


async def test_send_403_is_blocked_without_retry():
    bot = ScriptedBot(send_message=[forbidden()])
    gw, _ = gateway(bot)
    assert await gw.send(5, "hi") is SendResult.BLOCKED
    assert len(bot.calls) == 1


async def test_send_waits_out_flood_control():
    bot = ScriptedBot(send_message=[flood(7), True])
    gw, sleeps = gateway(bot)
    assert await gw.send(5, "hi") is SendResult.OK
    assert 7 in sleeps and len(bot.calls) == 2


async def test_send_network_retries_three_times_then_fails():
    bot = ScriptedBot(send_message=[network(), network(), network(), True])
    gw, _ = gateway(bot)
    assert await gw.send(5, "hi") is SendResult.FAILED
    assert len(bot.calls) == 3


async def test_send_network_recovers():
    bot = ScriptedBot(send_message=[network(), True])
    gw, _ = gateway(bot)
    assert await gw.send(5, "hi") is SendResult.OK


async def test_send_bad_request_rejected_without_retry():
    bot = ScriptedBot(send_message=[bad_request("can't parse entities")])
    gw, _ = gateway(bot)
    assert await gw.send(5, "<b>") is SendResult.REJECTED
    assert len(bot.calls) == 1


async def test_kick_is_ban_then_unban_only_if_banned():
    bot = ScriptedBot()
    gw, _ = gateway(bot)
    assert await gw.kick(9) is True
    assert [c[0] for c in bot.calls] == ["ban", "unban"]
    assert bot.calls[1][2] == {"only_if_banned": True}


async def test_kick_not_a_member_counts_as_success():
    bot = ScriptedBot(ban=[bad_request("USER_NOT_PARTICIPANT")])
    gw, _ = gateway(bot)
    assert await gw.kick(9) is True


async def test_kick_other_error_fails():
    bot = ScriptedBot(ban=[bad_request("not enough rights to restrict/unrestrict chat member")])
    gw, _ = gateway(bot)
    assert await gw.kick(9) is False


async def test_member_status():
    member = SimpleNamespace(status="member")
    restricted_out = SimpleNamespace(status="restricted", is_member=False)
    restricted_in = SimpleNamespace(status="restricted", is_member=True)
    bot = ScriptedBot(get_chat_member=[member, restricted_out, restricted_in,
                                       bad_request("user not found"), network(), network(), network()])
    gw, _ = gateway(bot)
    assert await gw.member_status(1) == "member"
    assert await gw.member_status(1) == "left"
    assert await gw.member_status(1) == "restricted"
    assert await gw.member_status(1) == "left"
    assert await gw.member_status(1) is None   # unknown: callers must not act on it


async def test_join_actions():
    bot = ScriptedBot(decline=[bad_request("HIDE_REQUESTER_MISSING")])
    gw, _ = gateway(bot)
    assert await gw.approve_join(3) is True
    assert bot.calls[0] == ("approve", (CHANNEL, 3), {})
    assert await gw.decline_join(3) is False


async def test_bot_rights_problems():
    good = SimpleNamespace(status="administrator", can_invite_users=True, can_restrict_members=True)
    weak = SimpleNamespace(status="administrator", can_invite_users=True, can_restrict_members=False)
    plain = SimpleNamespace(status="member")
    bot = ScriptedBot(get_chat_member=[good, weak, plain, bad_request("chat not found")])
    gw, _ = gateway(bot)
    assert await gw.bot_rights_problems(1) == []
    assert any("Ban users" in p for p in await gw.bot_rights_problems(1))
    assert await gw.bot_rights_problems(1) == ["the bot is not an admin of the paid channel"]
    assert "couldn't check" in (await gw.bot_rights_problems(1))[0]


async def test_throttle_spaces_out_sends():
    bot = ScriptedBot()
    gw, sleeps = gateway(bot)
    for _ in range(5):
        await gw.send(5, "hi")
    # 20/s = one send per 0.05s. The fake sleep doesn't pass time, so the waits
    # queue up: about 0.05, 0.10, 0.15, 0.20 for sends 2-5.
    assert len(sleeps) == 4
    assert sleeps[-1] == pytest.approx(0.2, abs=0.02)


# --- non-JSON replies (ClientDecodeError isn't a TelegramAPIError) ----------------------

async def test_send_html_error_page_is_retried_then_failed():
    bot = ScriptedBot(send_message=[html_502(), html_502(), html_502(), True])
    gw, sleeps = gateway(bot)
    assert await gw.send(5, "hi") is SendResult.FAILED
    assert len(bot.calls) == 3 and 1.0 in sleeps and 2.0 in sleeps


async def test_send_html_error_page_recovers():
    bot = ScriptedBot(send_message=[html_502(), True])
    gw, _ = gateway(bot)
    assert await gw.send(5, "hi") is SendResult.OK


async def test_send_unreadable_json_is_not_retried():
    # Probably delivered: never send it twice. REJECTED keeps the notices_sent row.
    bot = ScriptedBot(send_message=[unreadable_json(), True])
    gw, _ = gateway(bot)
    assert await gw.send(5, "hi") is SendResult.REJECTED
    assert len(bot.calls) == 1


async def test_admin_actions_survive_decode_errors():
    bot = ScriptedBot(
        approve=[html_502()] * 3,
        decline=[unreadable_json()],
        get_chat_member=[html_502()] * 3 + [unreadable_json()],
        ban=[html_502()] * 3,
    )
    gw, _ = gateway(bot)
    assert await gw.approve_join(3) is False
    assert await gw.decline_join(3) is False
    assert await gw.member_status(3) is None
    assert "couldn't check" in (await gw.bot_rights_problems(1))[0]
    assert await gw.kick(3) is False


async def test_unban_decode_error_still_counts_as_removed():
    bot = ScriptedBot(unban=[unreadable_json()])
    gw, _ = gateway(bot)
    assert await gw.kick(3) is True
