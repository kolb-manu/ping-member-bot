"""Join requests and chat_member tracking for the paid channel."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from first_in_bot import texts
from first_in_bot.access import JoinOutcome, handle_join_request, handle_member_update, in_channel
from first_in_bot.billing import start_checkout
from first_in_bot.handlers.channel import on_chat_member, on_join_request
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.users import get_user
from tests.fakes import NOW, add_user, audit_actions, make_svc, trial_sub

CHAT = 9001   # user_chat_id from the join request


def order(tg):
    return [e[:2] for e in tg.events]


async def test_unknown_user_told_to_start_then_declined(svc, tg):
    # They never pressed /start, so user_chat_id only works until the request is processed.
    tg.window_chats[77] = CHAT
    outcome = await handle_join_request(svc, 77, CHAT, NOW)
    assert outcome is JoinOutcome.DECLINED_UNKNOWN
    assert order(tg) == [("send", CHAT), ("decline", 77)]
    assert tg.sent_to(CHAT) == [texts.TRIAL_START_FIRST]
    assert await get_user(svc.db, 77) is None
    assert await audit_actions(svc.db, 77) == ["join_declined"]


async def test_fake_models_the_dm_window(tg):
    tg.window_chats[77] = CHAT
    await tg.decline_join(77)
    assert await tg.send(CHAT, "too late") is SendResult.BLOCKED


async def test_decline_happens_even_if_the_dm_raises(svc, tg):
    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    tg.send = boom
    with pytest.raises(RuntimeError):
        await handle_join_request(svc, 77, CHAT, NOW)
    assert tg.calls("decline") == [77]


async def test_banned_declined_silently(svc, tg):
    await add_user(svc.db, 1, banned=True, sub_status="active")
    assert await handle_join_request(svc, 1, CHAT, NOW) is JoinOutcome.DECLINED_BANNED
    assert tg.calls("decline") == [1] and tg.sent_to(CHAT) == []


@pytest.mark.parametrize("fields", [
    {"sub_status": "active"},
    {"sub_status": "trialing"},
    {"manual_grant_until": NOW + timedelta(days=1)},
])
async def test_entitled_user_approved(svc, tg, fields):
    await add_user(svc.db, 1, **fields)
    assert await handle_join_request(svc, 1, CHAT, NOW) is JoinOutcome.APPROVED
    assert tg.calls("approve") == [1] and tg.sent_to(CHAT) == []
    assert (await get_user(svc.db, 1)).in_paid_channel
    assert "join_approved" in await audit_actions(svc.db, 1)


async def test_not_entitled_declined_with_trial_button(svc, tg):
    await add_user(svc.db, 1)
    tg.window_chats[1] = CHAT
    assert await handle_join_request(svc, 1, CHAT, NOW) is JoinOutcome.DECLINED_NOT_ENTITLED
    assert order(tg) == [("send", CHAT), ("decline", 1)]
    assert tg.sent_to(CHAT) == [texts.JOIN_DECLINED]
    button = tg.markups_to(CHAT)[0].inline_keyboard[0][0]
    assert button.text == "Start my free 2 weeks" and button.callback_data == "trial:start"
    assert not (await get_user(svc.db, 1)).in_paid_channel


async def test_trial_button_follows_trial_days(db, tg, stripe_fake):
    svc = make_svc(db, tg, stripe_fake, TRIAL_DAYS="3")
    await add_user(db, 1)
    await handle_join_request(svc, 1, CHAT, NOW)
    assert tg.markups_to(CHAT)[0].inline_keyboard[0][0].text == "Start my free 3 days"


async def test_not_entitled_after_trial_gets_subscribe_button(svc, tg):
    await add_user(svc.db, 1, stripe_sub_id="sub_1", sub_status="canceled", trial_started_at=NOW - timedelta(days=40))
    await handle_join_request(svc, 1, CHAT, NOW)
    assert tg.markups_to(CHAT)[0].inline_keyboard[0][0].text == "Subscribe"


async def test_poll_then_approve_when_they_just_paid(svc, tg, stripe_fake):
    user = await add_user(svc.db, 1)
    await start_checkout(svc, user, NOW)
    stripe_fake.complete("cs_1", trial_sub(1))   # paid seconds ago, before the poll job ran
    assert await handle_join_request(svc, 1, CHAT, NOW) is JoinOutcome.APPROVED
    assert tg.calls("approve") == [1] and tg.calls("decline") == []
    assert (await get_user(svc.db, 1)).sub_status == "trialing"


async def test_without_stripe_no_poll_just_decline(db, tg):
    svc = make_svc(db, tg, None)
    await add_user(db, 1)
    assert await handle_join_request(svc, 1, CHAT, NOW) is JoinOutcome.DECLINED_NOT_ENTITLED


async def test_dm_failure_ignored_and_approve_failure_reported(svc, tg):
    await add_user(svc.db, 1)
    tg.send_results[CHAT] = SendResult.BLOCKED
    assert await handle_join_request(svc, 1, CHAT, NOW) is JoinOutcome.DECLINED_NOT_ENTITLED
    assert not (await get_user(svc.db, 1)).blocked_bot

    await add_user(svc.db, 2, sub_status="active")
    tg.approve_ok = False
    assert await handle_join_request(svc, 2, CHAT, NOW) is JoinOutcome.APPROVE_FAILED
    assert not (await get_user(svc.db, 2)).in_paid_channel


async def test_approve_failure_is_audited_and_explained(db, tg, stripe_fake):
    svc = make_svc(db, tg, stripe_fake, SUPPORT_CONTACT="@FirstInHelp")
    await add_user(db, 2, sub_status="active")
    tg.approve_ok = False
    tg.window_chats[2] = CHAT
    await handle_join_request(svc, 2, CHAT, NOW)
    assert "join_approve_failed" in await audit_actions(db, 2)
    [text] = tg.sent_to(CHAT)   # still pending, so the DM window is open
    assert text.startswith(texts.JOIN_APPROVE_FAILED) and "@FirstInHelp" in text


async def test_decline_failure_is_audited(svc, tg):
    await add_user(svc.db, 1)
    tg.decline_ok = False
    assert await handle_join_request(svc, 1, CHAT, NOW) is JoinOutcome.DECLINED_NOT_ENTITLED
    assert "join_decline_failed" in await audit_actions(svc.db, 1)
    assert "join_declined" not in await audit_actions(svc.db, 1)


# --- chat_member ---------------------------------------------------------------------

@pytest.mark.parametrize("status,is_member,expected", [
    ("member", None, True),
    ("administrator", None, True),
    ("creator", None, True),
    ("restricted", True, True),
    ("restricted", False, False),
    ("left", None, False),
    ("kicked", None, False),
])
def test_in_channel_mapping(status, is_member, expected):
    assert in_channel(status, is_member) is expected


async def test_member_updates_set_flag_and_audit_on_change(svc):
    await add_user(svc.db, 1)
    assert await handle_member_update(svc, 1, "user1", "Name", "member", None, NOW) is True
    await handle_member_update(svc, 1, "user1", "Name", "member", None, NOW)
    assert (await get_user(svc.db, 1)).in_paid_channel
    assert await handle_member_update(svc, 1, "user1", "Name", "left", None, NOW) is False
    assert not (await get_user(svc.db, 1)).in_paid_channel
    assert await audit_actions(svc.db, 1) == ["user_created", "channel_join", "channel_leave"]


async def test_member_update_creates_minimal_user(svc):
    await handle_member_update(svc, 55, "newbie", "New", "member", None, NOW)
    user = await get_user(svc.db, 55)
    assert user.source == "unknown" and user.in_paid_channel and user.username == "newbie"
    assert user.trial_started_at is None and user.sub_status is None


def member_update(chat_id, user_id, status, is_bot=False, is_member=None):
    member = SimpleNamespace(user=SimpleNamespace(id=user_id, username="u", first_name="U", is_bot=is_bot),
                             status=status)
    if is_member is not None:
        member.is_member = is_member
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), new_chat_member=member)


async def test_handlers_ignore_other_chats_and_bots(svc, tg):
    paid = svc.config.paid_channel_id
    await on_chat_member(member_update(-1009999, 5, "member"), svc)
    await on_chat_member(member_update(paid, 6, "member", is_bot=True), svc)
    assert await get_user(svc.db, 5) is None and await get_user(svc.db, 6) is None
    await on_chat_member(member_update(paid, 7, "restricted", is_member=True), svc)
    assert (await get_user(svc.db, 7)).in_paid_channel

    request = SimpleNamespace(chat=SimpleNamespace(id=-1009999), from_user=SimpleNamespace(id=8), user_chat_id=8)
    await on_join_request(request, svc)
    assert tg.events == []
    request.chat.id = paid
    await on_join_request(request, svc)
    assert tg.calls("decline") == [8]
