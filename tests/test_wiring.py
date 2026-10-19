"""Real aiogram routing (filters, data injection, router order) with a session
that records requests instead of calling Telegram."""
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Update

from first_in_bot import texts
from first_in_bot.main import ALLOWED_UPDATES, build_dispatcher
from first_in_bot.users import get_user
from tests.fakes import OWNER, PRICE, add_user, audit_actions, make_svc
from tests.test_config import TOKEN

_DP = None


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[Any] = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield b""

    async def close(self) -> None:
        pass


@pytest.fixture
def wiring(db, tg, stripe_fake):
    global _DP
    svc = make_svc(db, tg, stripe_fake)
    # Routers are module singletons and can only be attached once, so build the
    # dispatcher once and pass this test's services with each update instead.
    if _DP is None:
        _DP = build_dispatcher(svc)
    session = RecordingSession()
    bot = Bot(TOKEN, session=session)

    async def feed(payload: dict) -> list[Any]:
        update = Update.model_validate({"update_id": 1, **payload}, context={"bot": bot})
        await _DP.feed_update(bot, update, svc=svc, db=db, config=svc.config, tg=tg,
                              stripe_gw=stripe_fake, price_label=svc.price_label)
        return session.requests

    return svc, feed


def message(uid, text, chat_type="private", chat_id=None):
    command = text.split()[0]
    return {"message": {
        "message_id": 1, "date": 0, "text": text,
        "chat": {"id": chat_id or uid, "type": chat_type, **({"title": "g"} if chat_type != "private" else {})},
        "from": {"id": uid, "is_bot": False, "first_name": "U"},
        "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
    }}


def callback(uid, data):
    return {"callback_query": {
        "id": "cb1", "chat_instance": "ci", "data": data,
        "from": {"id": uid, "is_bot": False, "first_name": "U"},
        "message": {"message_id": 2, "date": 0, "chat": {"id": uid, "type": "private"}, "text": "x"},
    }}


def test_allowed_updates():
    assert ALLOWED_UPDATES == ["message", "callback_query", "chat_join_request", "chat_member"]


async def test_start_sends_welcome(wiring):
    svc, feed = wiring
    [req] = await feed(message(5, "/start tt_v1"))
    assert isinstance(req, SendMessage) and "Your first 14 days are free, then £2.99/month" in req.text
    assert req.reply_markup.inline_keyboard[0][0].callback_data == "trial:start"


async def test_status_private_only(wiring):
    svc, feed = wiring
    await add_user(svc.db, 5)
    assert await feed(message(5, "/status", chat_type="group", chat_id=-100555)) == []
    [req] = await feed(message(5, "/status"))
    assert texts.STATUS_HEADER in req.text


async def test_admin_commands_silent_for_others(wiring):
    svc, feed = wiring
    assert await feed(message(999, "/stats")) == []
    [req] = await feed(message(OWNER, "/stats"))
    assert "Users:" in req.text


async def test_buttons_always_answered(wiring):
    svc, feed = wiring
    [req] = await feed(callback(12345, "trial:start"))
    assert isinstance(req, AnswerCallbackQuery) and req.text == texts.TRIAL_START_FIRST and req.show_alert
    [req] = (await feed(callback(5, "stale:button")))[-1:]
    assert isinstance(req, AnswerCallbackQuery) and req.text is None


async def test_join_request_routed_for_paid_channel_only(wiring, tg):
    svc, feed = wiring

    def join(chat_id):
        return {"chat_join_request": {
            "chat": {"id": chat_id, "type": "channel", "title": "c"},
            "from": {"id": 77, "is_bot": False, "first_name": "U"},
            "user_chat_id": 77, "date": 0,
        }}

    await feed(join(-1009999999))
    assert tg.events == []
    await feed(join(svc.config.paid_channel_id))
    assert tg.calls("decline") == [77]


async def test_how_it_works_depends_on_trial_eligibility(wiring):
    svc, feed = wiring
    await add_user(svc.db, 5, stripe_sub_id="sub_1", sub_status="canceled")
    requests = await feed(callback(5, "how"))
    assert isinstance(requests[0], AnswerCallbackQuery)
    assert requests[-1].text == texts.HOW_IT_WORKS_SUBSCRIBE.format(price=PRICE)
    await add_user(svc.db, 6)
    trial_text = (await feed(callback(6, "how")))[-1].text
    assert "14 days free" in trial_text and "A card is required" in trial_text and "/manage" in trial_text


async def test_any_private_message_or_button_clears_blocked_bot(wiring):
    svc, feed = wiring
    await add_user(svc.db, 5, blocked_bot=True)
    await feed(message(5, "/status", chat_type="group", chat_id=-100555))
    assert (await get_user(svc.db, 5)).blocked_bot          # groups don't count
    await feed(message(5, "/status"))
    assert not (await get_user(svc.db, 5)).blocked_bot
    assert (await audit_actions(svc.db, 5)).count("unblocked") == 1
    await feed(message(5, "/status"))
    assert (await audit_actions(svc.db, 5)).count("unblocked") == 1   # no write when already clear

    await add_user(svc.db, 6, blocked_bot=True)
    await feed(callback(6, "stale:button"))
    assert not (await get_user(svc.db, 6)).blocked_bot
