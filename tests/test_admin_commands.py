"""/revoke, /ban and /unban (spec section 10)."""
from datetime import timedelta

import pytest
from aiogram.filters import CommandObject

from first_in_bot import admin
from first_in_bot.billing import LAST_FULL_SYNC_KEY
from first_in_bot.db import kv_set
from first_in_bot.handlers import admin as admin_handlers
from first_in_bot.timeutil import to_iso
from first_in_bot.users import get_user
from tests.fakes import NOW, OWNER, add_user, audit_actions, make_svc
from tests.test_handlers import FakeMessage, cmd

D = timedelta(days=1)


@pytest.fixture
def real_svc(db, tg, stripe_fake):
    return make_svc(db, tg, stripe_fake, REMOVALS_ENABLED="true")


async def run(handler, svc, name, args, uid=OWNER):
    msg = FakeMessage(uid)
    await handler(msg, cmd(name, args), svc)
    return [text for text, _ in msg.answers]


# --- who may use them -----------------------------------------------------------------

@pytest.mark.parametrize("handler,name,args", [
    (admin_handlers.cmd_revoke, "revoke", "5"),
    (admin_handlers.cmd_ban, "ban", "5"),
    (admin_handlers.cmd_unban, "unban", "5"),
])
async def test_ignored_for_non_admins(svc, tg, handler, name, args):
    await add_user(svc.db, 5, manual_grant_until=NOW + 5 * D)
    assert await run(handler, svc, name, args, uid=999) == []
    user = await get_user(svc.db, 5)
    assert user.manual_grant_until is not None and not user.banned
    assert tg.events == []


@pytest.mark.parametrize("handler,name", [
    (admin_handlers.cmd_revoke, "revoke"),
    (admin_handlers.cmd_ban, "ban"),
    (admin_handlers.cmd_unban, "unban"),
])
async def test_usage_and_unknown_user(svc, handler, name):
    assert "Usage: /" + name in (await run(handler, svc, name, None))[0]
    assert "Usage: /" + name in (await run(handler, svc, name, "5 6"))[0]
    assert "No user found for 424242" in (await run(handler, svc, name, "424242"))[0]


# --- /revoke ----------------------------------------------------------------------------

async def test_revoke_clears_grant_and_removes_when_nothing_else_entitles(real_svc, tg):
    await add_user(real_svc.db, 5, manual_grant_until=NOW + 5 * D, in_paid_channel=True)
    [reply] = await run(admin_handlers.cmd_revoke, real_svc, "revoke", "5")
    assert "Grant cleared" in reply and "Removed from the paid channel" in reply
    user = await get_user(real_svc.db, 5)
    assert user.manual_grant_until is None and not user.in_paid_channel
    assert tg.calls("kick") == [5] and tg.permanent_kicks == []   # can rejoin if they pay later
    assert "revoke" in await audit_actions(real_svc.db, 5) and "removed" in await audit_actions(real_svc.db, 5)


async def test_revoke_keeps_a_paying_member_in(real_svc, tg):
    await add_user(real_svc.db, 5, manual_grant_until=NOW + 5 * D, in_paid_channel=True,
                   stripe_sub_id="sub_p", sub_status="active", sub_period_end=NOW + 20 * D)
    [reply] = await run(admin_handlers.cmd_revoke, real_svc, "revoke", "5")
    assert "Grant cleared" in reply and "still have access another way" in reply
    assert (await get_user(real_svc.db, 5)).manual_grant_until is None
    assert tg.calls("kick") == []


async def test_revoke_with_removals_off_only_clears_the_grant(svc, tg):
    await add_user(svc.db, 5, manual_grant_until=NOW + 5 * D, in_paid_channel=True)
    [reply] = await run(admin_handlers.cmd_revoke, svc, "revoke", "5")
    assert "Grant cleared" in reply and "REMOVALS_ENABLED is false" in reply
    assert (await get_user(svc.db, 5)).in_paid_channel
    assert tg.calls("kick") == [] and tg.calls("status") == []


async def test_revoke_never_touches_an_admin(real_svc, tg):
    await add_user(real_svc.db, 222, manual_grant_until=NOW + 5 * D, in_paid_channel=True)   # 222 is in ADMIN_IDS
    [reply] = await run(admin_handlers.cmd_revoke, real_svc, "revoke", "222")
    assert "is an admin" in reply
    assert (await get_user(real_svc.db, 222)).manual_grant_until is not None
    assert tg.calls("kick") == []


async def test_revoke_user_not_in_channel(real_svc, tg):
    await add_user(real_svc.db, 5, manual_grant_until=NOW + 5 * D)
    tg.statuses[5] = "left"
    [reply] = await run(admin_handlers.cmd_revoke, real_svc, "revoke", "5")
    assert "Grant cleared" in reply and "aren't in the paid channel" in reply
    assert tg.calls("kick") == []


async def test_revoke_stale_stripe_sync_blocks_removal_of_a_subscriber(real_svc, tg):
    await add_user(real_svc.db, 5, manual_grant_until=NOW + 5 * D, in_paid_channel=True,
                   stripe_customer_id="cus_5", stripe_sub_id="sub_5", sub_status="canceled")
    [reply] = await run(admin_handlers.cmd_revoke, real_svc, "revoke", "5")
    assert "Stripe sync is stale" in reply and tg.calls("kick") == []
    async with real_svc.db.tx() as conn:
        await kv_set(conn, LAST_FULL_SYNC_KEY, to_iso(NOW - timedelta(minutes=2)))
    [reply] = await run(admin_handlers.cmd_revoke, real_svc, "revoke", "5")
    assert "Removed from the paid channel" in reply


# --- /ban and /unban ------------------------------------------------------------------

async def test_ban_flags_declines_and_permanently_removes(real_svc, tg):
    await add_user(real_svc.db, 5, in_paid_channel=True, manual_grant_until=NOW + 5 * D)
    [reply] = await run(admin_handlers.cmd_ban, real_svc, "ban", "@user5")
    assert "Banned 5" in reply and "Removed from the paid channel" in reply
    user = await get_user(real_svc.db, 5)
    assert user.banned and not real_svc.entitled(user, NOW) and not user.in_paid_channel
    assert tg.permanent_kicks == [5]
    assert tg.sent_to(5) == []                       # no goodbye DM for a ban
    assert "ban" in await audit_actions(real_svc.db, 5)


async def test_ban_with_removals_off_still_flags(svc, tg):
    await add_user(svc.db, 5, in_paid_channel=True)
    [reply] = await run(admin_handlers.cmd_ban, svc, "ban", "5")
    assert "Banned 5" in reply and "REMOVALS_ENABLED is false" in reply
    assert (await get_user(svc.db, 5)).banned and tg.calls("kick") == []


async def test_ban_is_not_blocked_by_a_stale_stripe_sync(real_svc, tg):
    await add_user(real_svc.db, 5, in_paid_channel=True, stripe_customer_id="cus_5",
                   stripe_sub_id="sub_5", sub_status="active", sub_period_end=NOW + 20 * D)
    [reply] = await run(admin_handlers.cmd_ban, real_svc, "ban", "5")
    assert "Removed from the paid channel" in reply and tg.permanent_kicks == [5]


async def test_ban_twice_and_ban_admin(real_svc, tg):
    await add_user(real_svc.db, 5)
    await run(admin_handlers.cmd_ban, real_svc, "ban", "5")
    assert "already banned" in (await run(admin_handlers.cmd_ban, real_svc, "ban", "5"))[0]
    await add_user(real_svc.db, 222, in_paid_channel=True)
    assert "is an admin" in (await run(admin_handlers.cmd_ban, real_svc, "ban", "222"))[0]
    assert not (await get_user(real_svc.db, 222)).banned


async def test_unban_clears_flag_and_lifts_telegram_ban(svc, tg):
    await add_user(svc.db, 5, banned=True)
    [reply] = await run(admin_handlers.cmd_unban, svc, "unban", "5")
    assert "Unbanned 5" in reply and "still need" in reply
    user = await get_user(svc.db, 5)
    assert not user.banned and not svc.entitled(user, NOW)    # no access just because they're unbanned
    assert tg.calls("unban") == [5]
    assert "unban" in await audit_actions(svc.db, 5)


async def test_unban_when_not_banned_and_when_telegram_fails(svc, tg):
    await add_user(svc.db, 5)
    assert "isn't banned" in (await run(admin_handlers.cmd_unban, svc, "unban", "5"))[0]
    assert tg.calls("unban") == []
    await add_user(svc.db, 6, banned=True)
    tg.unban_ok = False
    [reply] = await run(admin_handlers.cmd_unban, svc, "unban", "6")
    assert "couldn't lift the Telegram ban" in reply and not (await get_user(svc.db, 6)).banned


async def test_banned_user_join_request_is_declined(real_svc, tg):
    from first_in_bot.access import JoinOutcome, handle_join_request

    await add_user(real_svc.db, 5, manual_grant_until=NOW + 5 * D)
    await run(admin_handlers.cmd_ban, real_svc, "ban", "5")
    assert await handle_join_request(real_svc, 5, 5, NOW) is JoinOutcome.DECLINED_BANNED


def test_admin_target_error_is_a_value_error():
    assert issubclass(admin.AdminTarget, ValueError) and CommandObject is not None
