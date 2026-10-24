"""/revoke, /ban, /unban, /pause, /resume and /confirmsweep (spec section 10)."""
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.filters import CommandObject

from first_in_bot import admin
from first_in_bot.billing import LAST_FULL_SYNC_KEY
from first_in_bot.db import kv_set
from first_in_bot.handlers import admin as admin_handlers
from first_in_bot.maintenance import paused_since, set_paused
from first_in_bot.models import User
from first_in_bot.sweep import CONFIRM_PROMPT, pending_confirm, sweep_removals
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.timeutil import to_iso
from first_in_bot.users import get_user, register_user
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


# --- /pause, /resume and /confirmsweep -----------------------------------------------------

async def say(handler, svc, uid=OWNER):
    msg = FakeMessage(uid)
    await handler(msg, svc)
    return [text for text, _ in msg.answers]


@pytest.mark.parametrize("handler", [admin_handlers.cmd_pause, admin_handlers.cmd_resume,
                                     admin_handlers.cmd_confirmsweep])
async def test_maintenance_commands_ignored_for_non_admins(svc, tg, handler):
    if handler is admin_handlers.cmd_resume:
        await set_paused(svc.db, NOW, by=OWNER)   # so a stranger's /resume would have something to undo
    before = await svc.db.fetchall("SELECT key, value FROM kv ORDER BY key")
    assert await say(handler, svc, uid=999) == []
    assert await svc.db.fetchall("SELECT key, value FROM kv ORDER BY key") == before
    assert tg.events == []


async def test_pause_says_what_is_and_isnt_paused(svc):
    [reply] = await say(admin_handlers.cmd_pause, svc)
    assert reply.startswith("⏸ Paused at 1 Nov 2026, 12:00.")
    assert "removal sweep" in reply and "/revoke" in reply and "trial reminders" in reply
    assert "Still running: join requests, checkout checks" in reply and "Stripe sync" in reply
    assert "/dryrun still works" in reply and "/resume" in reply
    assert await paused_since(svc.db) == NOW
    [again] = await say(admin_handlers.cmd_pause, svc)
    assert again.startswith("Already paused since 1 Nov 2026, 12:00")
    assert (await audit_actions(svc.db)).count("pause") == 1


async def test_resume_says_how_long_it_was_paused(svc):
    assert "Not paused" in (await say(admin_handlers.cmd_resume, svc))[0]
    await set_paused(svc.db, NOW - timedelta(hours=2, minutes=5), by=OWNER)
    [reply] = await say(admin_handlers.cmd_resume, svc)
    assert reply.startswith("▶️ Resumed after 2 hours 5 minutes (paused since 1 Nov 2026, 09:55).")
    assert "still due go out" in reply and "/confirmsweep" in reply
    assert await paused_since(svc.db) is None
    assert "resume" in await audit_actions(svc.db)


async def test_ban_while_paused_through_the_handler(real_svc, tg):
    await add_user(real_svc.db, 5, in_paid_channel=True)
    await say(admin_handlers.cmd_pause, real_svc)
    [reply] = await run(admin_handlers.cmd_ban, real_svc, "ban", "5")
    assert reply == "Banned 5 (@user5, Name). Paused, so I didn't remove them; the sweep will once you /resume."
    assert (await get_user(real_svc.db, 5)).banned and tg.calls("kick") == []


async def blocked(db, tg, stripe_fake):
    svc = make_svc(db, tg, stripe_fake, REMOVALS_ENABLED="true", MAX_REMOVALS_PER_SWEEP="2")
    async with db.tx() as conn:
        await kv_set(conn, LAST_FULL_SYNC_KEY, to_iso(NOW - timedelta(minutes=5)))
    for i in (10, 11, 12):
        await add_user(db, i, in_paid_channel=True, manual_grant_until=NOW - D)
    assert (await sweep_removals(svc, NOW)).blocked
    return svc


async def test_confirmsweep_command_removes_the_approved_set(db, tg, stripe_fake):
    svc = await blocked(db, tg, stripe_fake)
    [reply] = await say(admin_handlers.cmd_confirmsweep, svc)
    assert "Removed (3): 10, 11, 12" in reply and sorted(tg.calls("kick")) == [10, 11, 12]
    assert "Nothing is waiting" in (await say(admin_handlers.cmd_confirmsweep, svc))[0]


async def test_confirmsweep_command_offers_the_new_report_once_sent(db, tg, stripe_fake):
    svc = await blocked(db, tg, stripe_fake)
    await add_user(db, 13, in_paid_channel=True, manual_grant_until=NOW - D)
    [reply] = await say(admin_handlers.cmd_confirmsweep, svc)
    assert reply.startswith("Not going ahead") and reply.endswith(CONFIRM_PROMPT)
    assert (await pending_confirm(db)).ids == {10, 11, 12, 13}
    await say(admin_handlers.cmd_confirmsweep, svc)
    assert sorted(tg.calls("kick")) == [10, 11, 12, 13]


async def test_confirmsweep_reply_that_never_arrived_offers_nothing(db, tg, stripe_fake):
    svc = await blocked(db, tg, stripe_fake)
    await add_user(db, 13, in_paid_channel=True, manual_grant_until=NOW - D)

    class LostMessage(FakeMessage):
        async def answer(self, text, reply_markup=None, **_):
            raise RuntimeError("network down")

    with pytest.raises(RuntimeError):
        await admin_handlers.cmd_confirmsweep(LostMessage(OWNER), svc)
    assert (await pending_confirm(db)).ids == {10, 11, 12}      # 13 was never shown to the owner
    assert tg.calls("kick") == []


async def test_confirmsweep_command_while_paused(db, tg, stripe_fake):
    svc = await blocked(db, tg, stripe_fake)
    await say(admin_handlers.cmd_pause, svc)
    assert (await say(admin_handlers.cmd_confirmsweep, svc))[0].startswith("Paused: /resume first")
    assert tg.calls("kick") == []


async def test_confirmsweep_refusal_counts_as_the_report_so_the_next_sweep_stays_quiet(db, tg, stripe_fake):
    svc = await blocked(db, tg, stripe_fake)
    await add_user(db, 13, in_paid_channel=True, manual_grant_until=NOW - D)
    [reply] = await say(admin_handlers.cmd_confirmsweep, svc)
    assert reply.startswith("Not going ahead")
    owner_dms = len(tg.sent_to(OWNER))
    async with db.tx() as conn:
        await kv_set(conn, LAST_FULL_SYNC_KEY, to_iso(NOW + timedelta(minutes=5)))
    await sweep_removals(svc, NOW + timedelta(minutes=10))
    assert len(tg.sent_to(OWNER)) == owner_dms                  # the same report isn't DMed again
    pending = await pending_confirm(db)
    assert pending.ids == {10, 11, 12, 13} and pending.at == NOW  # and its hour didn't restart
    rows = await db.fetchall("SELECT detail FROM audit_log WHERE action='sweep_blocked'")
    assert len(rows) == 2                                       # the new set audited once, by the reply


async def test_confirmsweep_with_nothing_pending_offers_a_blocked_sweep(db, tg, stripe_fake):
    tg.send_results[OWNER] = SendResult.BLOCKED     # the owner never got the scheduled report
    svc = await blocked(db, tg, stripe_fake)
    assert await pending_confirm(db) is None
    [reply] = await say(admin_handlers.cmd_confirmsweep, svc)
    assert reply.startswith("Nothing is waiting for /confirmsweep, so nobody was removed.")
    assert "Would remove (3)" in reply and reply.endswith(CONFIRM_PROMPT)
    assert (await pending_confirm(db)).ids == {10, 11, 12} and tg.calls("kick") == []
    await say(admin_handlers.cmd_confirmsweep, svc)
    assert sorted(tg.calls("kick")) == [10, 11, 12]


# --- who a command lands on --------------------------------------------------------------

async def share_username(db, *ids, name="sam"):
    """Rows from before usernames were claimed can still share one."""
    for i in ids:
        await add_user(db, i)
    marks = ",".join("?" * len(ids))
    async with db.tx() as conn:
        await conn.execute(f"UPDATE users SET username=? WHERE telegram_id IN ({marks})", (name, *ids))


@pytest.mark.parametrize("handler,name,args", [
    (admin_handlers.cmd_ban, "ban", "@sam"),
    (admin_handlers.cmd_revoke, "revoke", "@sam"),
    (admin_handlers.cmd_unban, "unban", "@Sam"),
    (admin_handlers.cmd_grant, "grant", "@sam 30"),
    (admin_handlers.cmd_user, "user", "@sam"),
])
async def test_a_shared_username_is_refused_with_the_list(real_svc, tg, handler, name, args):
    await share_username(real_svc.db, 1, 2)
    async with real_svc.db.tx() as conn:
        await conn.execute("UPDATE users SET banned=1, in_paid_channel=1, manual_grant_until=? "
                           "WHERE telegram_id IN (1, 2)", (to_iso(NOW + 5 * D),))
    before = [tuple(r) for r in await real_svc.db.fetchall("SELECT * FROM users ORDER BY telegram_id")]
    [reply] = await run(handler, real_svc, name, args)
    assert reply.startswith("Several users have @") and "use the numeric ID" in reply
    assert "• 1 (@sam, Name), joined 1 Nov 2026" in reply and "• 2 (@sam, Name)" in reply
    assert [tuple(r) for r in await real_svc.db.fetchall("SELECT * FROM users ORDER BY telegram_id")] == before
    assert tg.events == []


async def test_ban_by_username_after_a_rename_hits_whoever_has_it_now(real_svc, tg):
    # Paying A joined in October as @sam and renamed; B (September) took @sam and pressed /start.
    await add_user(real_svc.db, 2, username="bob", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    await add_user(real_svc.db, 1, username="sam", created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                   in_paid_channel=True, stripe_sub_id="sub_1", sub_status="active", sub_period_end=NOW + 20 * D)
    await register_user(real_svc.db, 2, "sam", "B", None, NOW)
    [reply] = await run(admin_handlers.cmd_ban, real_svc, "ban", "@sam")
    assert reply.startswith("Banned 2 (@sam, B).")
    assert not (await get_user(real_svc.db, 1)).banned and (await get_user(real_svc.db, 2)).banned
    assert 1 not in tg.calls("kick") and 1 not in tg.calls("status")


async def test_replies_name_the_person_escaped(real_svc):
    await add_user(real_svc.db, 5, username="<sam>", manual_grant_until=NOW + 5 * D)
    async with real_svc.db.tx() as conn:
        await conn.execute("UPDATE users SET first_name='Sam & co' WHERE telegram_id=5")
    msg = FakeMessage(OWNER)
    await admin_handlers.cmd_grant(msg, cmd("grant", "5 7"), real_svc)
    assert msg.answers[0][0].startswith("✅ 5 (@&lt;sam&gt;, Sam &amp; co) has free access until")
    [reply] = await run(admin_handlers.cmd_revoke, real_svc, "revoke", "5")
    assert reply.startswith("Grant cleared for 5 (@&lt;sam&gt;, Sam &amp; co).")
    assert admin.who(User(telegram_id=7)) == "7" and admin.who(User(telegram_id=7, first_name="Al")) == "7 (Al)"


@pytest.mark.parametrize("fields,warned", [
    ({"sub_status": "active"}, True),
    ({"sub_status": "trialing"}, True),
    ({"sub_status": "past_due"}, True),
    ({"sub_status": "disputed"}, True),                                   # hides Stripe's own, usually active
    ({"sub_status": "past_due", "cancel_at_period_end": True}, True),    # still retrying the open invoice
    ({"sub_status": "active", "cancel_at_period_end": True}, False),     # ends without another charge
    ({"sub_status": "canceled"}, False),
])
async def test_ban_warns_when_stripe_will_keep_charging(svc, fields, warned):
    await add_user(svc.db, 5, in_paid_channel=True, stripe_customer_id="cus_5", stripe_sub_id="sub_5",
                   sub_period_end=NOW + 20 * D, **fields)
    [reply] = await run(admin_handlers.cmd_ban, svc, "ban", "5")
    assert reply.startswith("Banned 5 (@user5, Name).")
    assert ("cancel sub_5 in the Stripe dashboard so they aren't charged" in reply) is warned


async def test_ban_without_a_subscription_has_no_stripe_warning(svc):
    await add_user(svc.db, 5, manual_grant_until=NOW + 5 * D)
    [reply] = await run(admin_handlers.cmd_ban, svc, "ban", "5")
    assert "Stripe" not in reply
