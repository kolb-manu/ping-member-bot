"""The removal sweep: dry run, safety rails and (for later) real mode."""
from datetime import timedelta

import pytest

from first_in_bot import texts
from first_in_bot.billing import LAST_FULL_SYNC_KEY
from first_in_bot.db import kv_set
from first_in_bot.admin import grant
from first_in_bot.models import User
from first_in_bot.sweep import (
    FINGERPRINT_KEY,
    REPORT_MAX_CHARS,
    SweepReport,
    cap_problem,
    ended_kind,
    format_report,
    sweep_removals,
)
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.timeutil import to_iso
from first_in_bot.users import get_user
from tests.fakes import NOW, OWNER, FakeTelegram, add_user, audit_actions, make_svc

D = timedelta(days=1)

TRIAL_OVER = dict(in_paid_channel=True, stripe_sub_id="sub_x", sub_status="canceled", trial_started_at=NOW - 15 * D,
                  sub_trial_end=NOW - D, sub_period_start=NOW - 15 * D, sub_period_end=NOW - D)
SUB_OVER = dict(in_paid_channel=True, stripe_sub_id="sub_y", sub_status="canceled", trial_started_at=NOW - 60 * D,
                sub_trial_end=NOW - 46 * D, sub_period_start=NOW - 31 * D, sub_period_end=NOW - D)
GRANT_OVER = dict(in_paid_channel=True, manual_grant_until=NOW - D)
PAYING = dict(in_paid_channel=True, stripe_sub_id="sub_p", sub_status="active", sub_period_end=NOW + 20 * D)


async def fresh_sync(db, age=timedelta(minutes=5), at=NOW):
    async with db.tx() as conn:
        await kv_set(conn, LAST_FULL_SYNC_KEY, to_iso(at - age))


@pytest.fixture
def real_svc(db, tg, stripe_fake):
    return make_svc(db, tg, stripe_fake, REMOVALS_ENABLED="true")


# --- cap rule ----------------------------------------------------------------------

@pytest.mark.parametrize("count,entitled,max_removals,blocked", [
    (0, 0, 5, False),
    (1, 0, 5, False),      # 100% but under the floor of 2
    (2, 0, 5, False),      # still under the floor (deliberate deviation)
    (3, 100, 5, False),    # 3 of 103 < 20%
    (3, 5, 5, True),       # 3 of 8 > 20%
    (6, 1000, 5, True),    # more than MAX_REMOVALS_PER_SWEEP
    (5, 1000, 5, False),
    (3, 12, 5, False),     # exactly 20% of 15 isn't "more than"
])
def test_cap_rules(count, entitled, max_removals, blocked):
    assert (cap_problem(count, entitled, max_removals) is not None) is blocked


# --- dry run -----------------------------------------------------------------------

async def test_dry_run_reports_to_owner_only_when_changed(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, 1, **PAYING)
    await add_user(svc.db, 2, **TRIAL_OVER)
    report = await sweep_removals(svc, NOW)
    assert report.dry_run and [u.telegram_id for u in report.would_remove] == [2]
    assert report.entitled_in_channel == 1
    assert tg.calls("kick") == [] and tg.sent_to(2) == []
    owner_msgs = tg.sent_to(OWNER)
    assert len(owner_msgs) == 1 and "Would remove (1): 2 @user2 (trial ended)" in owner_msgs[0]
    assert {e[1] for e in tg.events if e[0] == "send"} == {OWNER}   # nobody else is DMed

    await fresh_sync(svc.db, at=NOW + timedelta(minutes=10))
    await sweep_removals(svc, NOW + timedelta(minutes=10))
    assert len(tg.sent_to(OWNER)) == 1                       # same set: no repeat
    assert (await audit_actions(svc.db, 2)).count("would_remove") == 1

    await add_user(svc.db, 3, **GRANT_OVER)
    await fresh_sync(svc.db, at=NOW + timedelta(minutes=20))
    await sweep_removals(svc, NOW + timedelta(minutes=20))
    assert len(tg.sent_to(OWNER)) == 2
    assert "3 @user3 (free access ended)" in tg.sent_to(OWNER)[-1]
    assert (await get_user(svc.db, 2)).in_paid_channel     # nobody actually removed


async def test_first_quiet_sweep_sends_nothing(svc, tg):
    await add_user(svc.db, 1, **PAYING)
    await sweep_removals(svc, NOW)
    assert tg.sent_to(OWNER) == []
    assert await svc.db.kv_get(FINGERPRINT_KEY) is not None


async def test_dry_run_blocked_by_cap(db, tg, stripe_fake):
    svc = make_svc(db, tg, stripe_fake, MAX_REMOVALS_PER_SWEEP="2")
    await fresh_sync(db)
    for i in range(10, 13):
        await add_user(db, i, **GRANT_OVER)
    report = await sweep_removals(svc, NOW)
    assert report.blocked and "MAX_REMOVALS_PER_SWEEP" in report.blocked
    assert "removals are held" in tg.sent_to(OWNER)[0]
    assert "sweep_blocked" in await audit_actions(db)


# --- safety rails ------------------------------------------------------------------

async def test_stale_sync_skips_anyone_who_ever_had_a_subscription(svc, tg):
    await add_user(svc.db, 2, **TRIAL_OVER)
    await add_user(svc.db, 3, **GRANT_OVER)        # never had Stripe: not affected by sync age
    report = await sweep_removals(svc, NOW)        # no sync recorded at all
    assert [(u.telegram_id, r) for u, r in report.skipped] == [(2, "stale_sync")]
    assert [u.telegram_id for u in report.would_remove] == [3]

    await fresh_sync(svc.db, age=timedelta(minutes=20))
    report = await sweep_removals(svc, NOW)
    assert [(u.telegram_id, r) for u, r in report.skipped] == [(2, "stale_sync")]

    await fresh_sync(svc.db, age=timedelta(minutes=14))
    report = await sweep_removals(svc, NOW)
    assert sorted(u.telegram_id for u in report.would_remove) == [2, 3]
    # The repeated skip is audited once, not every sweep.
    assert (await audit_actions(svc.db, 2)).count("sweep_skip") == 1


async def test_admins_protected(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, OWNER, **GRANT_OVER)
    await add_user(svc.db, 4, **GRANT_OVER)
    tg.statuses[4] = "administrator"
    report = await sweep_removals(svc, NOW)
    assert sorted((u.telegram_id, r) for u, r in report.skipped) == [(4, "channel_admin"), (OWNER, "admin")]
    assert report.would_remove == []
    assert OWNER not in tg.calls("status")   # ADMIN_IDS aren't even looked up


async def test_not_in_channel_clears_flag(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, 5, **GRANT_OVER)
    await add_user(svc.db, 6, **GRANT_OVER)
    tg.statuses[5] = "left"
    tg.statuses[6] = None    # couldn't check: don't touch
    report = await sweep_removals(svc, NOW)
    assert sorted(r for _, r in report.skipped) == ["not_in_channel", "status_unknown"]
    assert not (await get_user(svc.db, 5)).in_paid_channel
    assert (await get_user(svc.db, 6)).in_paid_channel
    assert "channel_leave" in await audit_actions(svc.db, 5)


async def test_entitled_members_never_candidates(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, 1, **PAYING)
    await add_user(svc.db, 2, in_paid_channel=True, sub_status="trialing", stripe_sub_id="sub_t")
    # Realistic past_due: the unpaid period began an hour ago and ends in a month.
    await add_user(svc.db, 3, in_paid_channel=True, sub_status="past_due", stripe_sub_id="sub_pd",
                   sub_period_start=NOW - timedelta(hours=1), sub_period_end=NOW + 29 * D)
    report = await sweep_removals(svc, NOW)
    assert report.entitled_in_channel == 3 and report.would_remove == [] and tg.calls("status") == []


async def test_past_due_beyond_grace_is_a_candidate_despite_future_period_end(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, 3, in_paid_channel=True, sub_status="past_due", stripe_sub_id="sub_pd",
                   sub_period_start=NOW - 4 * D, sub_period_end=NOW + 26 * D)
    report = await sweep_removals(svc, NOW)
    assert [u.telegram_id for u in report.would_remove] == [3]


# --- forced dry run (/dryrun) ------------------------------------------------------

async def test_forced_dry_run_even_with_removals_enabled(real_svc, tg):
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 2, **TRIAL_OVER)
    report = await sweep_removals(real_svc, NOW, force_dry_run=True)
    assert report.dry_run and [u.telegram_id for u in report.would_remove] == [2]
    assert tg.calls("kick") == [] and tg.sent_to(OWNER) == [] and tg.sent_to(2) == []
    assert await real_svc.db.kv_get(FINGERPRINT_KEY) is None
    assert "Would remove (1)" in format_report(report)


# --- real mode (REMOVALS_ENABLED=true; stays false in .env for now) ------------------

async def test_real_mode_dm_then_kick(real_svc, tg):
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 1, **PAYING)
    await add_user(real_svc.db, 2, **TRIAL_OVER)
    report = await sweep_removals(real_svc, NOW)
    assert report.removed == [2]
    kinds = [(e[0], e[1]) for e in tg.events if e[0] in ("send", "kick")]
    assert kinds == [("send", 2), ("kick", 2)]
    assert tg.sent_to(2) == [texts.TRIAL_ENDED]
    buttons = [b.text for row in tg.markups_to(2)[0].inline_keyboard for b in row]
    assert buttons == ["Subscribe", "Free channel"]
    assert not (await get_user(real_svc.db, 2)).in_paid_channel
    assert "removed" in await audit_actions(real_svc.db, 2)
    notices = await real_svc.db.fetchall("SELECT kind FROM notices_sent WHERE telegram_id=2")
    assert [n["kind"] for n in notices] == ["trial_ended"]


async def test_real_mode_blocked_user_still_kicked(real_svc, tg):
    await fresh_sync(real_svc.db)
    for i in range(1, 6):
        await add_user(real_svc.db, i, **PAYING)
    await add_user(real_svc.db, 20, **SUB_OVER)
    await add_user(real_svc.db, 21, **GRANT_OVER, blocked_bot=True)
    tg.send_results[20] = SendResult.BLOCKED
    report = await sweep_removals(real_svc, NOW)
    assert sorted(report.removed) == [20, 21]
    assert tg.sent_to(20) == [texts.SUB_ENDED] and tg.sent_to(21) == []
    assert (await get_user(real_svc.db, 20)).blocked_bot
    assert tg.markups_to(20)[0].inline_keyboard[0][0].text == "Resubscribe"


async def test_real_mode_network_failure_holds_the_kick(real_svc, tg):
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 3, **GRANT_OVER)
    tg.send_results[3] = SendResult.FAILED
    report = await sweep_removals(real_svc, NOW)
    assert report.removed == [] and tg.calls("kick") == []
    assert await real_svc.db.fetchall("SELECT * FROM notices_sent") == []
    tg.send_results.clear()
    await fresh_sync(real_svc.db, at=NOW + timedelta(minutes=10))
    report = await sweep_removals(real_svc, NOW + timedelta(minutes=10))
    assert report.removed == [3] and tg.sent_to(3)[-1] == texts.ACCESS_ENDED


async def test_real_mode_cap_holds_removals(db, tg, stripe_fake):
    svc = make_svc(db, tg, stripe_fake, REMOVALS_ENABLED="true")
    await fresh_sync(db)
    await add_user(db, 1, **PAYING)
    for i in range(10, 13):
        await add_user(db, i, **GRANT_OVER)
    report = await sweep_removals(svc, NOW)
    assert report.blocked and report.removed == [] and tg.calls("kick") == []
    assert "removals are held" in tg.sent_to(OWNER)[0]
    await fresh_sync(db, at=NOW + timedelta(minutes=10))
    await sweep_removals(svc, NOW + timedelta(minutes=10))
    assert len(tg.sent_to(OWNER)) == 1     # not repeated every sweep


# --- which goodbye fits -------------------------------------------------------------

@pytest.mark.parametrize("fields,kind", [
    # Card failed at trial conversion: Stripe opened a new period starting at the trial end.
    ({"sub_trial_end": NOW - 5 * D, "sub_period_start": NOW - 5 * D, "sub_period_end": NOW + 25 * D}, "trial_ended"),
    # Cancelled during the trial.
    ({"sub_trial_end": NOW - D, "sub_period_start": NOW - 15 * D, "sub_period_end": NOW - D}, "trial_ended"),
    # Paid at least once, then ended.
    ({"sub_trial_end": NOW - 46 * D, "sub_period_start": NOW - 31 * D, "sub_period_end": NOW - D}, "sub_ended"),
    ({"sub_period_start": NOW - 31 * D, "sub_period_end": NOW - D}, "sub_ended"),          # never had a trial
    # Rows from before migration 3 (no period start yet) keep the old rule.
    ({"sub_trial_end": NOW - D, "sub_period_end": NOW - D}, "trial_ended"),
    ({"sub_trial_end": NOW - 46 * D, "sub_period_end": NOW - D}, "sub_ended"),
])
def test_ended_kind(fields, kind):
    assert ended_kind(User(telegram_id=1, stripe_sub_id="sub_1", sub_status="canceled", **fields)) == kind
    assert ended_kind(User(telegram_id=1, manual_grant_until=NOW - D)) == "access_ended"


# --- Stripe customer without a stored subscription ----------------------------------

async def test_stale_sync_also_protects_stripe_customers_without_a_stored_sub(svc, tg):
    # A friend on a grant who just paid: Stripe has their subscription, the DB doesn't yet.
    await add_user(svc.db, 3, **GRANT_OVER, stripe_customer_id="cus_3")
    report = await sweep_removals(svc, NOW)   # no fresh sync
    assert [(u.telegram_id, r) for u, r in report.skipped] == [(3, "stale_sync")]
    await fresh_sync(svc.db)
    report = await sweep_removals(svc, NOW)
    assert [u.telegram_id for u in report.would_remove] == [3]


# --- owner report delivery ------------------------------------------------------------

async def test_failed_owner_report_is_retried_without_new_audit_rows(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, 2, **TRIAL_OVER)
    tg.send_results[OWNER] = SendResult.FAILED
    report = await sweep_removals(svc, NOW)
    assert len(tg.sent_to(OWNER)) == 1 and not report.owner_notified

    tg.send_results.clear()
    await fresh_sync(svc.db, at=NOW + timedelta(minutes=10))
    report = await sweep_removals(svc, NOW + timedelta(minutes=10))
    assert len(tg.sent_to(OWNER)) == 2 and report.owner_notified     # same set, sent again
    assert (await audit_actions(svc.db, 2)).count("would_remove") == 1

    await fresh_sync(svc.db, at=NOW + timedelta(minutes=20))
    await sweep_removals(svc, NOW + timedelta(minutes=20))
    assert len(tg.sent_to(OWNER)) == 2                                # delivered: no repeat


@pytest.mark.parametrize("result", [SendResult.BLOCKED, SendResult.REJECTED])
async def test_owner_report_not_retried_when_retrying_cant_help(svc, tg, result):
    await fresh_sync(svc.db)
    await add_user(svc.db, 2, **TRIAL_OVER)
    tg.send_results[OWNER] = result
    await sweep_removals(svc, NOW)
    await fresh_sync(svc.db, at=NOW + timedelta(minutes=10))
    await sweep_removals(svc, NOW + timedelta(minutes=10))
    assert len(tg.sent_to(OWNER)) == 1


def test_report_fits_in_one_telegram_message():
    def u(i):
        return User(telegram_id=10**12 + i, first_name="Ä&<😀" * 16, stripe_sub_id="s", sub_status="canceled")
    report = SweepReport(dry_run=True, would_remove=[u(i) for i in range(60)],
                         skipped=[(u(i), "stale_sync") for i in range(60)])
    text = format_report(report)
    assert len(text.encode("utf-16-le")) // 2 <= REPORT_MAX_CHARS
    assert text.endswith("(cut short)")
    tail = text[: -len("\n… (cut short)")]
    assert tail.rfind("&") < tail.rfind(";")   # no half HTML entity left behind
    small = format_report(SweepReport(dry_run=True, would_remove=[u(1)]))
    assert not small.endswith("(cut short)")


# --- real mode: re-checked right before acting ------------------------------------------

class GrantingTelegram(FakeTelegram):
    """Runs a DB change at a chosen moment, like a sync or /grant landing mid-sweep."""

    def __init__(self, db, when):
        super().__init__()
        self.db, self.when = db, when

    async def _grant(self, user_id):
        user = User(telegram_id=user_id)
        await grant(self.db, user, 30, NOW, by=OWNER)

    async def member_status(self, user_id):
        status = await super().member_status(user_id)
        if self.when == "status":
            await self._grant(user_id)
        return status

    async def send(self, chat_id, text, markup=None):
        result = await super().send(chat_id, text, markup)
        if self.when == "dm" and chat_id != OWNER:
            await self._grant(chat_id)
        return result


@pytest.mark.parametrize("when,dm_sent", [("status", False), ("dm", True)])
async def test_real_mode_skips_someone_who_became_entitled_mid_sweep(db, stripe_fake, when, dm_sent):
    tg = GrantingTelegram(db, when)
    svc = make_svc(db, tg, stripe_fake, REMOVALS_ENABLED="true")
    await fresh_sync(db)
    await add_user(db, 2, **TRIAL_OVER)
    report = await sweep_removals(svc, NOW)
    assert report.removed == [] and tg.calls("kick") == []
    assert [(u.telegram_id, r) for u, r in report.skipped] == [(2, "now_entitled")]
    assert bool(tg.sent_to(2)) is dm_sent
    assert (await get_user(db, 2)).in_paid_channel
    assert "sweep_skip" in await audit_actions(db, 2)


async def test_real_mode_skips_someone_who_left_mid_sweep(real_svc, tg):
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 2, **TRIAL_OVER)
    real_member_status = tg.member_status

    async def leave_after_check(user_id):
        status = await real_member_status(user_id)
        async with real_svc.db.tx() as conn:   # a chat_member "left" update lands meanwhile
            await conn.execute("UPDATE users SET in_paid_channel=0 WHERE telegram_id=?", (user_id,))
        return status

    tg.member_status = leave_after_check
    report = await sweep_removals(real_svc, NOW)
    assert report.removed == [] and tg.calls("kick") == [] and tg.sent_to(2) == []
    assert [(u.telegram_id, r) for u, r in report.skipped] == [(2, "not_in_channel")]


async def test_real_mode_paid_after_trial_then_cancelled_gets_subscription_goodbye(real_svc, tg):
    # Paid the first month after the trial, then cancelled: the period starts at the
    # trial end, exactly like a failed first charge. The audit log tells them apart.
    from first_in_bot.audit import audit
    paid_then_cancelled = dict(TRIAL_OVER, sub_trial_end=NOW - 31 * D, sub_period_start=NOW - 31 * D)
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 2, **paid_then_cancelled)
    async with real_svc.db.tx() as conn:
        await audit(conn, NOW - 31 * D, 2, "sub_status", "sub_x: trialing -> active")
    await sweep_removals(real_svc, NOW)
    assert tg.sent_to(2) == [texts.SUB_ENDED]
    kinds = [r["kind"] for r in await real_svc.db.fetchall("SELECT kind FROM notices_sent WHERE telegram_id=2")]
    assert kinds == ["sub_ended"]
