"""The /pause maintenance switch: what it holds (removals, goodbye DMs, reminders)
and what it must never hold (join approvals, checkout polling, "You're in", sync)."""
from datetime import timedelta

import pytest

from first_in_bot import admin, texts
from first_in_bot.access import JoinOutcome, handle_join_request
from first_in_bot.billing import LAST_FULL_SYNC_KEY, poll_checkouts, start_checkout, sync_subscriptions
from first_in_bot.db import Database
from first_in_bot.handlers import admin as admin_handlers
from first_in_bot.maintenance import PAUSED_KEY, clear_paused, describe_duration, paused_since, set_paused
from first_in_bot.notices import send_notices
from first_in_bot.sweep import FINGERPRINT_KEY, NOTIFIED_KEY, PAUSED_LINE, format_report, sweep_removals
from first_in_bot.timeutil import from_iso
from first_in_bot.users import get_user
from tests.fakes import NOW, OWNER, FakeTelegram, add_user, audit_actions, make_svc, sub_info, trial_sub
from tests.test_handlers import FakeMessage
from tests.test_sweep import GRANT_OVER, PAYING, TRIAL_OVER, fresh_sync

M = timedelta(minutes=1)
D = timedelta(days=1)


@pytest.fixture
def real_svc(db, tg, stripe_fake):
    return make_svc(db, tg, stripe_fake, REMOVALS_ENABLED="true")


async def kv_rows(db):
    return {r["key"]: r["value"] for r in await db.fetchall("SELECT key, value FROM kv")}


# --- the switch itself ------------------------------------------------------------------

async def test_pause_and_resume_are_stored_and_audited(db):
    assert await paused_since(db) is None
    assert await set_paused(db, NOW, by=OWNER)
    assert await paused_since(db) == NOW
    assert not await set_paused(db, NOW + M, by=OWNER)      # already on: start time kept
    assert await paused_since(db) == NOW

    assert await clear_paused(db, NOW + 5 * M, by=222)
    assert await paused_since(db) is None
    assert not await clear_paused(db, NOW + 6 * M, by=OWNER)
    rows = await db.fetchall("SELECT action, detail, telegram_id FROM audit_log WHERE action IN ('pause', 'resume')")
    assert [(r["action"], r["telegram_id"]) for r in rows] == [("pause", None), ("resume", None)]
    assert rows[0]["detail"] == f"by={OWNER}" and "by=222" in rows[1]["detail"]
    assert "paused_since=2026-11-01T12:00:00+00:00" in rows[1]["detail"]


async def test_pause_survives_a_restart(tmp_path):
    path = str(tmp_path / "bot.db")
    db = await Database.open(path)
    await set_paused(db, NOW, by=OWNER)
    await db.close()
    db = await Database.open(path)
    try:
        assert await paused_since(db) == NOW
    finally:
        await db.close()


@pytest.mark.parametrize("delta,text", [
    (timedelta(seconds=59), "less than a minute"),
    (timedelta(seconds=-5), "less than a minute"),
    (timedelta(minutes=1), "1 minute"),
    (timedelta(minutes=7), "7 minutes"),
    (timedelta(hours=2, minutes=5, seconds=30), "2 hours 5 minutes"),
    (timedelta(hours=1), "1 hour"),
    (timedelta(days=3, hours=4, minutes=9), "3 days 4 hours"),
    (timedelta(days=1, minutes=5), "1 day 5 minutes"),
])
def test_describe_duration(delta, text):
    assert describe_duration(delta) == text


# --- the sweep while paused -------------------------------------------------------------

async def test_paused_sweep_removes_nobody_and_touches_nothing(real_svc, tg):
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 1, **PAYING)
    await add_user(real_svc.db, 2, **TRIAL_OVER)
    await add_user(real_svc.db, 3, **GRANT_OVER)
    await set_paused(real_svc.db, NOW, by=OWNER)
    kv_before = await kv_rows(real_svc.db)
    audit_before = await audit_actions(real_svc.db)

    for tick in range(3):
        report = await sweep_removals(real_svc, NOW + tick * 10 * M)
        assert report.paused and report.would_remove == [] and report.removed == []
    assert tg.events == []                      # no lookups, no goodbye DMs, no owner DM, no kicks
    assert await kv_rows(real_svc.db) == kv_before
    assert await audit_actions(real_svc.db) == audit_before
    assert (await get_user(real_svc.db, 2)).in_paid_channel

    await clear_paused(real_svc.db, NOW + 30 * M, by=OWNER)
    await fresh_sync(real_svc.db, at=NOW + 30 * M)
    report = await sweep_removals(real_svc, NOW + 30 * M)
    assert sorted(report.removed) == [2, 3]
    assert tg.sent_to(2) == [texts.TRIAL_ENDED] and tg.sent_to(3) == [texts.ACCESS_ENDED]


async def test_paused_dry_run_sweep_keeps_the_report_state(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, 2, **TRIAL_OVER)
    await sweep_removals(svc, NOW)
    assert len(tg.sent_to(OWNER)) == 1
    fingerprints = (await svc.db.kv_get(FINGERPRINT_KEY), await svc.db.kv_get(NOTIFIED_KEY))

    await set_paused(svc.db, NOW + M, by=OWNER)
    await add_user(svc.db, 3, **GRANT_OVER)
    await fresh_sync(svc.db, at=NOW + 10 * M)
    await sweep_removals(svc, NOW + 10 * M)
    assert len(tg.sent_to(OWNER)) == 1
    assert (await svc.db.kv_get(FINGERPRINT_KEY), await svc.db.kv_get(NOTIFIED_KEY)) == fingerprints
    assert "would_remove" not in await audit_actions(svc.db, 3)

    # After /resume the owner hears about what changed while paused, once.
    await clear_paused(svc.db, NOW + 15 * M, by=OWNER)
    await fresh_sync(svc.db, at=NOW + 20 * M)
    await sweep_removals(svc, NOW + 20 * M)
    assert len(tg.sent_to(OWNER)) == 2 and "3 @user3" in tg.sent_to(OWNER)[-1]


async def test_resume_with_nothing_new_sends_no_repeat_report(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, 2, **TRIAL_OVER)
    await sweep_removals(svc, NOW)
    await set_paused(svc.db, NOW + M, by=OWNER)
    await sweep_removals(svc, NOW + 10 * M)
    await clear_paused(svc.db, NOW + 15 * M, by=OWNER)
    await fresh_sync(svc.db, at=NOW + 20 * M)
    await sweep_removals(svc, NOW + 20 * M)
    assert len(tg.sent_to(OWNER)) == 1
    assert (await audit_actions(svc.db, 2)).count("would_remove") == 1


async def test_dryrun_still_reports_while_paused(real_svc, tg):
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 2, **TRIAL_OVER)
    await set_paused(real_svc.db, NOW, by=OWNER)
    msg = FakeMessage(OWNER)
    await admin_handlers.cmd_dryrun(msg, real_svc)
    [(text, _)] = msg.answers
    assert PAUSED_LINE in text and text.index(PAUSED_LINE) < text.index("Would remove (1): 2 @user2")
    assert tg.calls("kick") == [] and tg.sent_to(2) == []


async def test_paused_line_only_when_paused(svc):
    await add_user(svc.db, 2, **GRANT_OVER)
    report = await sweep_removals(svc, NOW, force_dry_run=True)
    assert not report.paused and PAUSED_LINE not in format_report(report)


class PausingTelegram(FakeTelegram):
    """/pause lands at a chosen moment of a sweep that's already running."""

    def __init__(self, db, when):
        super().__init__()
        self.db, self.when = db, when

    async def member_status(self, user_id):
        status = await super().member_status(user_id)
        if self.when == "status":
            await set_paused(self.db, NOW, by=OWNER)
        return status

    async def send(self, chat_id, text, markup=None):
        result = await super().send(chat_id, text, markup)
        if self.when == "dm" and chat_id != OWNER:
            await set_paused(self.db, NOW, by=OWNER)
        return result


@pytest.mark.parametrize("when,dm_sent", [("status", False), ("dm", True)])
async def test_pause_mid_sweep_stops_the_next_dm_and_kick(db, stripe_fake, when, dm_sent):
    tg = PausingTelegram(db, when)
    svc = make_svc(db, tg, stripe_fake, REMOVALS_ENABLED="true")
    await fresh_sync(db)
    await add_user(db, 2, **TRIAL_OVER)
    await add_user(db, 3, **GRANT_OVER)
    report = await sweep_removals(svc, NOW)
    assert report.removed == [] and tg.calls("kick") == []
    assert "paused" in [r for _, r in report.skipped]
    assert bool(tg.sent_to(2)) is dm_sent and tg.sent_to(3) == []
    assert "sweep_skip" in await audit_actions(db, 2)
    assert (await get_user(db, 2)).in_paid_channel and (await get_user(db, 3)).in_paid_channel


# --- /revoke and /ban while paused -----------------------------------------------------------

async def test_revoke_and_ban_set_flags_but_dont_kick_while_paused(real_svc, tg):
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 5, manual_grant_until=NOW + 5 * D, in_paid_channel=True)
    await add_user(real_svc.db, 6, in_paid_channel=True, manual_grant_until=NOW + 5 * D)
    await set_paused(real_svc.db, NOW, by=OWNER)
    paused_note = "Paused, so I didn't remove them; the sweep will once you /resume."
    assert (await admin.revoke(real_svc, await get_user(real_svc.db, 5), NOW, OWNER)
            == f"Grant cleared for 5 (@user5, Name). {paused_note}")
    assert await admin.ban(real_svc, await get_user(real_svc.db, 6), NOW, OWNER) == f"Banned 6 (@user6, Name). {paused_note}"
    assert (await get_user(real_svc.db, 5)).manual_grant_until is None
    assert (await get_user(real_svc.db, 6)).banned
    assert tg.events == []                                     # not even a membership lookup
    # A banned user's join requests are declined regardless of the pause.
    assert await handle_join_request(real_svc, 6, 6, NOW) is JoinOutcome.DECLINED_BANNED

    await clear_paused(real_svc.db, NOW + M, by=OWNER)
    report = await sweep_removals(real_svc, NOW + 2 * M)
    assert sorted(report.removed) == [5, 6]
    assert tg.permanent_kicks == [6] and tg.sent_to(6) == [] and tg.sent_to(5) == [texts.ACCESS_ENDED]


async def test_revoke_of_someone_still_entitled_says_so_even_when_paused(real_svc, tg):
    await add_user(real_svc.db, 5, manual_grant_until=NOW + 5 * D, **PAYING)
    await set_paused(real_svc.db, NOW, by=OWNER)
    text = await admin.revoke(real_svc, await get_user(real_svc.db, 5), NOW, OWNER)
    assert "still have access another way" in text and tg.events == []


# --- what keeps working while paused -------------------------------------------------------

async def test_join_requests_still_decided_while_paused(svc, tg):
    await set_paused(svc.db, NOW, by=OWNER)
    await add_user(svc.db, 1, **dict(PAYING, in_paid_channel=False))
    await add_user(svc.db, 2, manual_grant_until=NOW + D)
    await add_user(svc.db, 3)
    assert await handle_join_request(svc, 1, 1, NOW) is JoinOutcome.APPROVED
    assert await handle_join_request(svc, 2, 2, NOW) is JoinOutcome.APPROVED
    assert await handle_join_request(svc, 3, 3, NOW) is JoinOutcome.DECLINED_NOT_ENTITLED
    assert tg.calls("approve") == [1, 2] and tg.calls("decline") == [3]
    assert (await get_user(svc.db, 1)).in_paid_channel and (await get_user(svc.db, 2)).in_paid_channel
    assert tg.sent_to(3) == [texts.JOIN_DECLINED]


async def test_someone_who_just_paid_gets_in_while_paused(svc, tg, stripe_fake):
    """Checkout polling, the "You're in" message and the join approval all carry on."""
    await set_paused(svc.db, NOW, by=OWNER)
    await start_checkout(svc, await add_user(svc.db, 1), NOW)
    stripe_fake.complete("cs_1", trial_sub(1))
    # The join request arrives before the 30-second poll: it polls for them itself.
    assert await handle_join_request(svc, 1, 1, NOW + M) is JoinOutcome.APPROVED
    [welcome] = tg.sent_to(1)
    assert "free trial is on until" in welcome

    await start_checkout(svc, await add_user(svc.db, 2, trial_started_at=NOW - 40 * D), NOW)
    stripe_fake.complete("cs_2", sub_info(id="sub_2", customer="cus_2", telegram_id=2))
    assert (await poll_checkouts(svc, NOW + 2 * M)).completed == 1
    assert tg.sent_to(2)[0].startswith(texts.PAID)


async def test_stripe_sync_carries_on_while_paused(svc, stripe_fake):
    await set_paused(svc.db, NOW, by=OWNER)
    await add_user(svc.db, 1)
    stripe_fake.subscriptions = [sub_info(telegram_id=1)]
    assert (await sync_subscriptions(svc, NOW)).ok
    assert from_iso(await svc.db.kv_get(LAST_FULL_SYNC_KEY)) == NOW
    assert (await get_user(svc.db, 1)).sub_status == "active"


async def test_notices_held_while_paused(svc, tg):
    end = NOW + D
    await add_user(svc.db, 1, stripe_sub_id="sub_t", sub_status="trialing", sub_trial_end=end, sub_period_end=end)
    await set_paused(svc.db, NOW, by=OWNER)
    assert await send_notices(svc, NOW) == 0
    assert tg.events == [] and await svc.db.fetchall("SELECT * FROM notices_sent") == []
    await clear_paused(svc.db, NOW + M, by=OWNER)
    assert await send_notices(svc, NOW + 2 * M) == 1


async def test_pause_key_is_the_documented_one(db):
    await set_paused(db, NOW, by=OWNER)
    assert (await kv_rows(db))[PAUSED_KEY] == "2026-11-01T12:00:00+00:00"
