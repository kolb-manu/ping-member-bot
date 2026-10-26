"""The removal sweep: dry run, safety rails, real mode and /confirmsweep."""
from datetime import timedelta

import pytest

from first_in_bot import texts
from first_in_bot.audit import audit
from first_in_bot.billing import LAST_FULL_SYNC_KEY, sync_subscriptions
from first_in_bot.db import kv_set
from first_in_bot.admin import grant
from first_in_bot.disputes import check_disputes
from first_in_bot.keyboards import ended_keyboard
from first_in_bot.maintenance import clear_paused, set_paused
from first_in_bot.models import User
from first_in_bot.scheduler import Job, Scheduler
from first_in_bot.services import Services
from first_in_bot.stripe_gateway import DisputeInfo
from first_in_bot.sweep import (
    CONFIRM_KEY,
    CONFIRM_PROMPT,
    FINGERPRINT_KEY,
    NOT_BLOCKED_NOTE,
    NOTIFIED_KEY,
    REPORT_MAX_CHARS,
    SweepReport,
    _utf16_len,
    cap_problem,
    confirm_sweep,
    ended_kind,
    format_report,
    offer_confirm,
    offer_in_reply,
    pending_confirm,
    sweep_removals,
)
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.timeutil import to_iso
from first_in_bot.users import get_user
from tests.fakes import PRICE, NOW, OWNER, FakeTelegram, add_user, audit_actions, make_config, make_svc, sub_info

D = timedelta(days=1)
M = timedelta(minutes=1)

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


async def test_banned_user_is_removed_permanently_without_a_dm(real_svc, tg):
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 2, in_paid_channel=True, banned=True)
    report = await sweep_removals(real_svc, NOW)
    assert report.removed == [2] and tg.permanent_kicks == [2] and tg.sent_to(2) == []


# --- /confirmsweep --------------------------------------------------------------------

def capped_svc(db, tg, stripe_fake, removals=True):
    env = {"MAX_REMOVALS_PER_SWEEP": "2"}
    if removals:
        env["REMOVALS_ENABLED"] = "true"
    return make_svc(db, tg, stripe_fake, **env)


async def blocked_sweep(svc, ids=(10, 11, 12)):
    await fresh_sync(svc.db)
    for i in ids:
        await add_user(svc.db, i, **GRANT_OVER)
    report = await sweep_removals(svc, NOW)
    assert report.blocked and report.removed == []
    return report


def user_events(tg):
    return [(e[0], e[1]) for e in tg.events if e[0] in ("send", "kick") and e[1] != OWNER]


async def confirm_details(db):
    rows = await db.fetchall("SELECT detail FROM audit_log WHERE action='sweep_confirm' ORDER BY id")
    return [r["detail"] for r in rows]


async def test_blocked_report_offers_confirm_and_stores_the_set(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    [dm] = tg.sent_to(OWNER)
    assert "removals are held" in dm and dm.endswith("\n" + CONFIRM_PROMPT)
    pending = await pending_confirm(db)
    assert pending.ids == {10, 11, 12} and pending.at == NOW
    assert await confirm_details(db) == ["offered: 10 11 12"]

    # The same blocked state next tick: no new DM, and the offer (with its time) unchanged.
    await sweep_removals(svc, NOW + 10 * M)
    assert len(tg.sent_to(OWNER)) == 1 and (await pending_confirm(db)).at == NOW
    assert await confirm_details(db) == ["offered: 10 11 12"]


async def test_reports_that_arent_blocked_carry_no_confirm_prompt(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, 2, **TRIAL_OVER)
    await sweep_removals(svc, NOW)
    assert CONFIRM_PROMPT not in tg.sent_to(OWNER)[0]
    assert await pending_confirm(svc.db) is None


async def test_offer_only_stored_once_the_report_is_delivered(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    tg.send_results[OWNER] = SendResult.FAILED
    await blocked_sweep(svc)
    assert await pending_confirm(db) is None
    tg.send_results.clear()
    await fresh_sync(db, at=NOW + 10 * M)
    await sweep_removals(svc, NOW + 10 * M)
    assert (await pending_confirm(db)).at == NOW + 10 * M


@pytest.mark.parametrize("result", [SendResult.BLOCKED, SendResult.REJECTED])
async def test_no_offer_when_the_owner_never_got_the_report(db, tg, stripe_fake, result):
    svc = capped_svc(db, tg, stripe_fake)
    tg.send_results[OWNER] = result
    await blocked_sweep(svc)
    assert await pending_confirm(db) is None


async def test_confirm_removes_exactly_the_approved_set_once(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await add_user(db, 1, **PAYING)
    await blocked_sweep(svc)
    result = await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert result.offer is None
    assert user_events(tg) == [("send", 10), ("kick", 10), ("send", 11), ("kick", 11), ("send", 12), ("kick", 12)]
    assert all(tg.sent_to(i) == [texts.ACCESS_ENDED] for i in (10, 11, 12))
    assert "cap lifted by /confirmsweep" in result.text and "Removed (3): 10, 11, 12" in result.text
    assert "Blocked" not in result.text
    for i in (10, 11, 12):
        assert not (await get_user(db, i)).in_paid_channel and "removed" in await audit_actions(db, i)
    assert (await get_user(db, 1)).in_paid_channel
    assert await pending_confirm(db) is None
    assert await confirm_details(db) == ["offered: 10 11 12", f"approved by={OWNER}: 10 11 12"]

    again = await confirm_sweep(svc, NOW + 6 * M, by=OWNER)
    assert "Nothing is waiting for /confirmsweep" in again.text
    assert len(tg.calls("kick")) == 3


async def test_confirm_goes_ahead_when_fewer_people_are_left(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    await grant(db, User(telegram_id=11), 30, NOW, by=OWNER)       # 11 got access back meanwhile
    result = await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert sorted(tg.calls("kick")) == [10, 12] and tg.sent_to(11) == []
    assert "Removed (2): 10, 12" in result.text


async def test_confirm_refuses_when_someone_new_would_be_removed(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    await add_user(db, 13, **GRANT_OVER)
    result = await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert tg.calls("kick") == [] and user_events(tg) == []
    assert result.text.startswith("Not going ahead: 1 of the people")
    assert "Would remove (4)" in result.text and "13 @user13" in result.text
    assert result.text.endswith(CONFIRM_PROMPT) and result.offer is not None
    assert (await pending_confirm(db)).ids == {10, 11, 12}   # the handler stores the new offer once it's sent
    assert f"refused (not approved: 13) by={OWNER}" in await confirm_details(db)

    # Once the owner has the new report, they can approve that one.
    await offer_confirm(svc, result.offer, NOW + 6 * M)
    await confirm_sweep(svc, NOW + 7 * M, by=OWNER)
    assert sorted(tg.calls("kick")) == [10, 11, 12, 13]


async def test_confirm_refuses_new_people_when_the_cap_no_longer_blocks(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    for i in (10, 11, 12):
        await grant(db, User(telegram_id=i), 30, NOW, by=OWNER)
    await add_user(db, 13, **GRANT_OVER)
    result = await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert tg.calls("kick") == [] and result.offer is None
    assert NOT_BLOCKED_NOTE in result.text and CONFIRM_PROMPT not in result.text
    assert await pending_confirm(db) is None


@pytest.mark.parametrize("after,goes_ahead", [
    (timedelta(minutes=59, seconds=59), True),
    (timedelta(hours=1), False),
    (timedelta(hours=5), False),
    (-M, False),                       # an offer from the future: the clock moved, so don't trust it
])
async def test_confirm_expires_after_an_hour(db, tg, stripe_fake, after, goes_ahead):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    await fresh_sync(db, at=NOW + after)
    result = await confirm_sweep(svc, NOW + after, by=OWNER)
    assert bool(tg.calls("kick")) is goes_ahead
    if not goes_ahead:
        assert result.text.startswith("That approval has expired") and result.text.endswith(CONFIRM_PROMPT)
        assert result.offer is not None and (await pending_confirm(db)).at == NOW


async def test_confirm_dry_run_shows_the_flow_and_removes_nobody(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake, removals=False)
    await blocked_sweep(svc)
    assert tg.sent_to(OWNER)[0].endswith(CONFIRM_PROMPT)
    result = await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert result.text.startswith("🧹 Dry run: with the cap lifted I would remove 3: 10 @user10 (free access ended)")
    assert "Nobody was removed because REMOVALS_ENABLED=false" in result.text
    assert tg.calls("kick") == [] and user_events(tg) == []
    for i in (10, 11, 12):
        assert (await get_user(db, i)).in_paid_channel
    assert await pending_confirm(db) is None
    assert f"approved (dry run) by={OWNER}: 10 11 12" in await confirm_details(db)
    # The cap still holds them back, so the next /confirmsweep shows and offers it again.
    again = await confirm_sweep(svc, NOW + 6 * M, by=OWNER)
    assert again.text.startswith("Nothing is waiting") and again.offer is not None
    # A dry-run approval removes nobody by design, so the next sweep doesn't re-report it.
    owner_dms = len(tg.sent_to(OWNER))
    await sweep_removals(svc, NOW + 10 * M)
    assert len(tg.sent_to(OWNER)) == owner_dms


async def test_confirm_refused_while_paused(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    await set_paused(db, NOW + M, by=OWNER)
    lookups = len(tg.calls("status"))
    result = await confirm_sweep(svc, NOW + 2 * M, by=OWNER)
    assert result.text.startswith("Paused: /resume first")
    assert len(tg.calls("status")) == lookups and tg.calls("kick") == []
    assert await pending_confirm(db) is not None
    await clear_paused(db, NOW + 3 * M, by=OWNER)
    await confirm_sweep(svc, NOW + 4 * M, by=OWNER)
    assert sorted(tg.calls("kick")) == [10, 11, 12]


async def test_confirm_with_nothing_pending(svc, tg):
    result = await confirm_sweep(svc, NOW, by=OWNER)
    assert result.text.startswith("Nothing is waiting for /confirmsweep") and result.offer is None
    assert tg.events == []
    assert await confirm_details(svc.db) == [f"refused (nothing pending) by={OWNER}"]


@pytest.mark.parametrize("stored", ["{not json", "[10, 11, 12]", '{"ids": [10], "at": null}'])
async def test_unreadable_offer_counts_as_nothing_pending(db, tg, stripe_fake, stored):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    async with db.tx() as conn:
        await kv_set(conn, CONFIRM_KEY, stored)
    assert "Nothing is waiting" in (await confirm_sweep(svc, NOW + M, by=OWNER)).text
    assert tg.calls("kick") == []


async def test_confirm_keeps_every_other_check(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await fresh_sync(db)
    await add_user(db, 10, **GRANT_OVER)
    await add_user(db, 11, **GRANT_OVER)
    await add_user(db, 12, **TRIAL_OVER)
    await add_user(db, 13, **GRANT_OVER)
    await sweep_removals(svc, NOW)
    assert (await pending_confirm(db)).ids == {10, 11, 12, 13}
    tg.statuses[11] = "administrator"
    tg.statuses[13] = None
    # 20 minutes after the last sync it's stale, so the subscriber (12) mustn't go.
    result = await confirm_sweep(svc, NOW + 15 * M, by=OWNER)
    assert tg.calls("kick") == [10]
    assert "channel admin" in result.text and "Stripe sync is stale" in result.text
    assert "couldn't check membership" in result.text
    assert (await get_user(db, 12)).in_paid_channel and (await get_user(db, 13)).in_paid_channel


async def test_confirm_rechecks_each_person_before_the_kick(db, stripe_fake):
    tg = GrantingTelegram(db, "dm")      # each goodbye DM is followed by a /grant landing
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    result = await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert tg.calls("kick") == []
    assert "became entitled during the sweep" in result.text
    for i in (10, 11, 12):
        assert (await get_user(db, i)).in_paid_channel


async def test_confirm_never_removes_an_admin(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc, ids=(10, 11, 12, 222))
    assert (await pending_confirm(db)).ids == {10, 11, 12}
    await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert 222 not in tg.calls("kick") and 222 not in tg.calls("status")


async def test_an_approval_is_used_only_once_even_when_replaced_mid_check(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    real_status = tg.member_status

    async def newer_offer_meanwhile(user_id):
        async with db.tx() as conn:   # e.g. a double tap, or a newer report delivered
            await kv_set(conn, CONFIRM_KEY, '{"ids": [10, 11, 12], "at": "2026-11-01T12:04:00+00:00"}')
        return await real_status(user_id)

    tg.member_status = newer_offer_meanwhile
    result = await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert "just used or replaced" in result.text and tg.calls("kick") == []


async def test_a_sweep_that_is_no_longer_blocked_drops_the_offer(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    for i in (11, 12):
        await grant(db, User(telegram_id=i), 30, NOW, by=OWNER)
    report = await sweep_removals(svc, NOW + 10 * M)
    assert report.removed == [10]                               # under the cap: the sweep does it itself
    assert await pending_confirm(db) is None
    assert await confirm_details(db) == ["offered: 10 11 12", "dropped: sweep no longer blocked"]
    await sweep_removals(svc, NOW + 20 * M)
    assert len(await confirm_details(db)) == 2                  # nothing written on quiet ticks


def test_confirm_prompt_survives_a_cut_report():
    def u(i):
        return User(telegram_id=10**12 + i, first_name="Ä&<😀" * 16, manual_grant_until=NOW - D)
    report = SweepReport(dry_run=False, would_remove=[u(i) for i in range(60)],
                         skipped=[(u(i), "stale_sync") for i in range(60)], blocked="60 removals is too many")
    text = format_report(report, offer_confirm=True)
    assert len(text.encode("utf-16-le")) // 2 <= REPORT_MAX_CHARS
    assert text.endswith("(cut short)\n" + CONFIRM_PROMPT)


# --- every /confirmsweep reply fits one Telegram message ------------------------------------

LONG_NAME = "Ä&<😀" * 16   # escapes to far more than it looks, and emoji count double in UTF-16


async def mass_event(db, tg, stripe_fake, removals=True):
    """60 to remove and 60 skipped, all with long names: the cap blocks and every list is cut."""
    env = {"MAX_REMOVALS_PER_SWEEP": "5", **({"REMOVALS_ENABLED": "true"} if removals else {})}
    svc = make_svc(db, tg, stripe_fake, **env)
    await fresh_sync(db)
    for i in range(1000, 1060):
        await add_user(db, i, username=None, first_name=LONG_NAME, **GRANT_OVER)
    for i in range(2000, 2060):
        await add_user(db, i, username=None, first_name=LONG_NAME, **GRANT_OVER)
        tg.statuses[i] = None   # couldn't check membership
    assert (await sweep_removals(svc, NOW)).blocked
    return svc


async def grant_everyone_listed(db):
    async with db.tx() as conn:
        await conn.execute("UPDATE users SET manual_grant_until=? WHERE telegram_id BETWEEN 1000 AND 1059",
                           (to_iso(NOW + 30 * D),))


@pytest.mark.parametrize("case,start,end", [
    ("expired", "That approval has expired", CONFIRM_PROMPT),
    ("not_approved", "Not going ahead: 1 of", CONFIRM_PROMPT),
    ("not_blocked", "Not going ahead: 1 of", NOT_BLOCKED_NOTE),
    ("nobody_left", "Nobody needs removing now", "(cut short)"),
    ("nothing_pending", "Nothing is waiting for /confirmsweep, so nobody was removed.", CONFIRM_PROMPT),
    ("mode_changed", "That approval was for a dry run", CONFIRM_PROMPT),
    ("removed", "🧹 <b>Removal sweep</b> (removals on, cap lifted by /confirmsweep)", "(cut short)"),
    ("dry_run", "🧹 Dry run: with the cap lifted", "(cut short)"),
])
async def test_every_confirmsweep_reply_fits_in_one_message(db, tg, stripe_fake, case, start, end):
    svc = await mass_event(db, tg, stripe_fake, removals=case not in ("mode_changed", "dry_run"))
    when = NOW + 5 * M
    if case == "expired":
        when = NOW + 2 * 60 * M
    elif case == "not_approved":
        await add_user(db, 1999, username=None, first_name=LONG_NAME, **GRANT_OVER)
    elif case == "not_blocked":
        await grant_everyone_listed(db)
        await add_user(db, 1999, username=None, first_name=LONG_NAME, **GRANT_OVER)
    elif case == "nobody_left":
        await grant_everyone_listed(db)
    elif case == "nothing_pending":
        async with db.tx() as conn:
            await conn.execute("DELETE FROM kv WHERE key=?", (CONFIRM_KEY,))
    elif case == "mode_changed":
        svc = make_svc(db, tg, stripe_fake, MAX_REMOVALS_PER_SWEEP="5", REMOVALS_ENABLED="true")
    result = await confirm_sweep(svc, when, by=OWNER)
    assert _utf16_len(result.text) <= REPORT_MAX_CHARS < 4096
    assert result.text.startswith(start) and result.text.endswith(end)
    assert (result.offer is not None) is (end == CONFIRM_PROMPT)
    if case == "removed":
        assert len(tg.calls("kick")) == 60


# --- a disputed member is told about the hold, not an ending ------------------------------------

async def test_disputed_member_gets_the_hold_message_and_no_checkout_button(db, tg, stripe_fake):
    svc = make_svc(db, tg, stripe_fake, REMOVALS_ENABLED="true", SUPPORT_CONTACT="@First<In>Help")
    await fresh_sync(db)
    await add_user(db, 1, in_paid_channel=True, stripe_customer_id="cus_1", stripe_sub_id="sub_1",
                   sub_status="active", sub_period_start=NOW - 3 * D, sub_period_end=NOW + 27 * D)
    # An issuer inquiry holds access too (spec 7.4); Stripe still says active.
    stripe_fake.disputes = [DisputeInfo(id="dp_1", status="warning_needs_response", charge_id="ch_1",
                                        customer_id="cus_1", created=NOW)]
    await check_disputes(svc, NOW)
    report = await sweep_removals(svc, NOW)
    assert report.removed == [1]
    [text] = tg.sent_to(1)
    assert text == texts.disputed_hold("@First&lt;In&gt;Help")
    assert "disputed" in text and "on hold" in text and "If that's a mistake, please message @First&lt;In&gt;Help" in text
    assert "ended" not in text and "Resubscribe" not in text
    assert [b.text for row in tg.markups_to(1)[0].inline_keyboard for b in row] == ["Free channel"]
    kinds = [r["kind"] for r in await db.fetchall("SELECT kind FROM notices_sent WHERE telegram_id=1")]
    assert kinds == ["disputed"]
    assert "payment disputed" in [r["detail"] for r in await db.fetchall(
        "SELECT detail FROM audit_log WHERE telegram_id=1 AND action='removed'")]


async def test_disputed_member_is_labelled_as_such_in_the_owner_report(svc, tg):
    await fresh_sync(svc.db)
    await add_user(svc.db, 2, **dict(SUB_OVER, sub_status="disputed"))
    await sweep_removals(svc, NOW)
    assert "2 @user2 (payment disputed)" in tg.sent_to(OWNER)[0]
    assert ended_kind(User(telegram_id=2, sub_status="disputed")) == "disputed"


def test_disputed_hold_text_without_a_support_contact():
    text = texts.disputed_hold(None)
    assert "on hold" in text and "message" not in text and "free channel" in text


@pytest.mark.parametrize("kind,first", [("access_ended", "Start my free 3 days"), ("disputed", "Free channel"),
                                        ("sub_ended", "Resubscribe")])
def test_goodbye_buttons(kind, first):
    markup = ended_keyboard(User(telegram_id=1), make_config(TRIAL_DAYS="3"), kind)
    assert markup.inline_keyboard[0][0].text == first
    assert (kind == "disputed") is (len(markup.inline_keyboard) == 1 and markup.inline_keyboard[0][0].url is not None)


# --- a kick Telegram refuses -------------------------------------------------------------------

async def test_a_refused_kick_fails_the_job_alerts_the_owner_and_is_audited_once(real_svc, tg):
    await add_user(real_svc.db, 1, **PAYING)
    await add_user(real_svc.db, 10, **GRANT_OVER)
    tg.kick_ok = False      # e.g. the bot lost its Ban users right
    job = Job("sweep_removals", 600, lambda: sweep_removals(real_svc, NOW))
    scheduler = Scheduler([job], svc=real_svc)
    assert [await scheduler.run_once(job) for _ in range(3)] == [False, False, False]
    assert (await audit_actions(real_svc.db, 10)).count("remove_failed") == 1
    assert any("sweep_removals failed" in t for t in tg.sent_to(OWNER))
    tg.kick_ok = True
    assert await scheduler.run_once(job)
    assert SweepReport(dry_run=False).ok and not SweepReport(dry_run=False, failed=[10]).ok


# --- an approved run that didn't get through everyone ----------------------------------------------

@pytest.mark.parametrize("how", ["dm_failed", "paused_mid_run"])
async def test_an_approval_that_left_people_in_is_reported_again(db, tg, stripe_fake, how):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    if how == "dm_failed":
        for i in (10, 11, 12):
            tg.send_results[i] = SendResult.FAILED
        await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
        tg.send_results.clear()
    else:
        real_send = tg.send

        async def pause_during_first_dm(chat_id, text, markup=None):
            if chat_id == 10:
                await set_paused(db, NOW + 5 * M, by=OWNER)
            return await real_send(chat_id, text, markup)

        tg.send = pause_during_first_dm
        await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
        tg.send = real_send
        await clear_paused(db, NOW + 8 * M, by=OWNER)
    assert tg.calls("kick") == [] and await pending_confirm(db) is None
    assert await db.kv_get(NOTIFIED_KEY) is None

    owner_dms = len(tg.sent_to(OWNER))
    await fresh_sync(db, at=NOW + 10 * M)
    report = await sweep_removals(svc, NOW + 10 * M)
    assert report.blocked and report.owner_notified and len(tg.sent_to(OWNER)) == owner_dms + 1
    assert (await pending_confirm(db)).at == NOW + 10 * M
    await confirm_sweep(svc, NOW + 12 * M, by=OWNER)
    assert sorted(tg.calls("kick")) == [10, 11, 12]


async def test_nothing_pending_while_blocked_offers_the_report(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    tg.send_results[OWNER] = SendResult.BLOCKED          # the scheduled report never arrived
    await blocked_sweep(svc)
    assert await pending_confirm(db) is None
    result = await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert result.text.startswith("Nothing is waiting for /confirmsweep") and result.text.endswith(CONFIRM_PROMPT)
    assert "Would remove (3)" in result.text and result.offer is not None and tg.calls("kick") == []
    assert f"refused (nothing pending) by={OWNER}" in await confirm_details(db)


# --- an approval is only good in the mode it was offered in --------------------------------------

async def test_a_dry_run_approval_is_refused_once_removals_are_on(db, tg, stripe_fake):
    await blocked_sweep(capped_svc(db, tg, stripe_fake, removals=False))
    assert (await pending_confirm(db)).dry_run is True
    live = capped_svc(db, tg, stripe_fake)           # REMOVALS_ENABLED=true and a restart
    result = await confirm_sweep(live, NOW + 5 * M, by=OWNER)
    assert tg.calls("kick") == []
    assert result.text.startswith("That approval was for a dry run (REMOVALS_ENABLED=false), but the bot is "
                                  "now set to real removals (REMOVALS_ENABLED=true), so nobody was removed.")
    assert result.text.endswith(CONFIRM_PROMPT) and result.offer is not None and not result.offer.dry_run
    await offer_in_reply(live, result.offer, NOW + 6 * M)
    await confirm_sweep(live, NOW + 7 * M, by=OWNER)
    assert sorted(tg.calls("kick")) == [10, 11, 12]


async def test_an_approval_with_no_stored_mode_is_refused(db, tg, stripe_fake):
    svc = capped_svc(db, tg, stripe_fake)
    await blocked_sweep(svc)
    async with db.tx() as conn:   # as stored before the mode was
        await kv_set(conn, CONFIRM_KEY, '{"ids": [10, 11, 12], "at": "2026-11-01T12:00:00+00:00"}')
    result = await confirm_sweep(svc, NOW + 5 * M, by=OWNER)
    assert tg.calls("kick") == [] and "an unknown mode" in result.text and result.offer is not None


# --- the sync age is checked again right before acting ---------------------------------------------

@pytest.mark.parametrize("slow,kicked", [(False, [10, 11, 12]), (True, [])])
async def test_sync_age_is_checked_again_before_each_dm_and_kick(db, stripe_fake, slow, kicked):
    clock = {"t": NOW}
    tg = FakeTelegram()
    svc = Services(db=db, config=make_config(REMOVALS_ENABLED="true", MAX_REMOVALS_PER_SWEEP="2"), tg=tg,
                   stripe=stripe_fake, price_label=PRICE, clock=lambda: clock["t"])
    await fresh_sync(db, age=13 * M)
    for i in (10, 11, 12):
        await add_user(db, i, in_paid_channel=True, stripe_customer_id=f"cus_{i}", stripe_sub_id=f"sub_{i}",
                       sub_status="canceled", sub_period_end=NOW - D)
    assert (await sweep_removals(svc, NOW)).blocked
    real_send = tg.send

    async def flood_wait(chat_id, text, markup=None):
        if slow:
            clock["t"] += 10 * M   # each goodbye DM takes ten minutes; the sync goes stale meanwhile
        return await real_send(chat_id, text, markup)

    tg.send = flood_wait
    result = await confirm_sweep(svc, NOW + M, by=OWNER)
    assert sorted(tg.calls("kick")) == kicked
    if slow:
        assert "Stripe sync is stale" in result.text and tg.sent_to(11) == [] and tg.sent_to(12) == []


async def test_a_banned_subscriber_is_not_held_by_a_stale_sync_at_the_recheck(real_svc, tg):
    await add_user(real_svc.db, 2, in_paid_channel=True, banned=True, stripe_sub_id="sub_b",
                   stripe_customer_id="cus_b", sub_status="active")
    from first_in_bot.sweep import remove_one
    assert await remove_one(real_svc, await get_user(real_svc.db, 2), NOW) == "Removed from the paid channel."


# --- Stripe's draft hour after a trial isn't a payment -------------------------------------------

async def failed_trial(svc, stripe_fake, seen_in_draft_hour):
    """trialing, maybe 'active' while the first invoice is a draft, then past_due once
    the card fails, and the grace runs out. Fed through the real sync."""
    end = NOW + 14 * D
    await add_user(svc.db, 1, stripe_customer_id="cus_1", in_paid_channel=True)
    base = dict(id="sub_1", customer="cus_1", telegram_id=1, created=NOW, trial_start=NOW, trial_end=end)
    stripe_fake.subscriptions = [sub_info(status="trialing", period_start=NOW, period_end=end, **base)]
    await sync_subscriptions(svc, NOW)
    if seen_in_draft_hour:
        stripe_fake.subscriptions = [sub_info(status="active", period_start=end, period_end=end + 30 * D, **base)]
        await sync_subscriptions(svc, end + 5 * M)
    stripe_fake.subscriptions = [sub_info(status="past_due", period_start=end, period_end=end + 30 * D, **base)]
    await sync_subscriptions(svc, end + 2 * 60 * M)
    later = end + timedelta(hours=75)
    await sync_subscriptions(svc, later)
    return await sweep_removals(svc, later)


@pytest.mark.parametrize("seen_in_draft_hour", [False, True])
async def test_a_failed_first_charge_gets_the_trial_goodbye(real_svc, tg, stripe_fake, seen_in_draft_hour):
    report = await failed_trial(real_svc, stripe_fake, seen_in_draft_hour)
    assert report.removed == [1] and tg.sent_to(1) == [texts.TRIAL_ENDED]


@pytest.mark.parametrize("changes,goodbye", [
    (["none -> trialing", "trialing -> active", "active -> past_due"], texts.TRIAL_ENDED),
    (["none -> trialing", "trialing -> active", "active -> past_due", "past_due -> canceled"], texts.TRIAL_ENDED),
    (["none -> trialing", "trialing -> past_due", "past_due -> unpaid"], texts.TRIAL_ENDED),
    (["none -> trialing", "trialing -> active", "active -> canceled"], texts.SUB_ENDED),    # paid, then cancelled
    (["none -> trialing", "trialing -> active"], texts.SUB_ENDED),
    # The first charge failed, then the overdue invoice was paid: that's a payment.
    (["none -> trialing", "trialing -> active", "active -> past_due", "past_due -> active", "active -> canceled"],
     texts.SUB_ENDED),
])
async def test_which_goodbye_after_a_trial_depends_on_a_payment(real_svc, tg, changes, goodbye):
    await fresh_sync(real_svc.db)
    await add_user(real_svc.db, 2, in_paid_channel=True, stripe_sub_id="sub_x", sub_status="canceled",
                   trial_started_at=NOW - 18 * D, sub_trial_end=NOW - 4 * D, sub_period_start=NOW - 4 * D,
                   sub_period_end=NOW + 26 * D)
    async with real_svc.db.tx() as conn:
        await audit(conn, NOW - 30 * D, 2, "sub_status", "sub_old: trialing -> active")   # another subscription
        for change in changes:
            await audit(conn, NOW - 4 * D, 2, "sub_status", f"sub_x: {change}")
    await sweep_removals(real_svc, NOW)
    assert tg.sent_to(2) == [goodbye]
