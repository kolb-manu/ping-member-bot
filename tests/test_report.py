"""The daily report: what's in it, sent once a day at 09:00 London, retried a
minute later after a network failure, and the failure counters reset only once
the owner actually got it."""
from datetime import datetime, timedelta, timezone

import pytest

from first_in_bot.billing import LAST_FULL_SYNC_KEY
from first_in_bot.db import kv_set
from first_in_bot.report import REPORT_DATE_KEY, daily_report_job, report_text
from first_in_bot.scheduler import JobHealth, JobHealthStore
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.timeutil import to_iso
from tests.fakes import OWNER, add_user, make_svc

M = timedelta(minutes=1)
H = timedelta(hours=1)
MORNING = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)   # 09:00 GMT


class FakeHealth:
    def __init__(self, health: dict[str, JobHealth] | None = None):
        self.health = dict(health or {})
        self.resets: list[dict] = []

    async def snapshot(self):
        return dict(self.health)

    async def reset_since_report(self, shown):
        self.resets.append(dict(shown))


async def synced_at(db, when):
    async with db.tx() as conn:
        await kv_set(conn, LAST_FULL_SYNC_KEY, to_iso(when))


# --- the text ---------------------------------------------------------------------------

async def test_healthy_report(svc):
    await add_user(svc.db, 1, sub_status="active")
    await synced_at(svc.db, MORNING - 3 * M)
    text = await report_text(svc, MORNING, {"send_notices": JobHealth(last_ok_at=MORNING)})
    assert text.startswith("☀️ <b>First In daily report</b>, 2 Nov 2026")
    assert "📊 <b>Stats</b>" in text and "Users: 1" in text and "Paying: 1" in text
    assert "✅ All jobs healthy" in text
    assert "Last full Stripe sync: 3 min ago" in text
    assert "Stripe: TEST mode" in text and "Removals: dry run (nobody is removed)" in text


async def test_failed_jobs_are_listed(svc):
    health = {
        "check_disputes": JobHealth(consecutive_failures=1, last_error="RuntimeError: old"),
        "poll_checkouts": JobHealth(failures_since_report=1, last_error="RuntimeError: blip",
                                    last_ok_at=MORNING - M),
        "send_notices": JobHealth(last_ok_at=MORNING),
        "sync_subscriptions": JobHealth(consecutive_failures=2, failures_since_report=5,
                                        last_error="StripeGatewayError: <down>", last_ok_at=MORNING - 2 * H),
    }
    text = await report_text(svc, MORNING, health)
    assert "All jobs healthy" not in text and "⚠️ <b>Jobs with failures</b>" in text
    assert ("• sync_subscriptions: failed 5 times since the last report, still failing. "
            "Last error: StripeGatewayError: &lt;down&gt;. Last worked: 2 Nov 2026, 07:00") in text
    assert "• poll_checkouts: failed once since the last report, working again." in text
    assert ("• check_disputes: still failing (it hasn't run since the last report). "
            "Last error: RuntimeError: old. Last worked: never") in text
    assert "send_notices" not in text


async def test_last_worked_is_in_local_time(svc):
    summer = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    health = {"j": JobHealth(failures_since_report=1, last_error="x", last_ok_at=summer - 2 * H)}
    assert "Last worked: 1 Jul 2026, 08:00" in await report_text(svc, summer, health)   # 07:00 UTC is 08:00 BST


@pytest.mark.parametrize("synced, line", [
    (None, "⚠️ Last full Stripe sync: never, so removing anyone with Stripe data is on hold"),
    (MORNING - 20 * M, "⚠️ Last full Stripe sync: 20 min ago, so removing anyone with Stripe data is on hold"),
    (MORNING - 3 * H - 5 * M, "⚠️ Last full Stripe sync: 3 h 5 min ago"),
    (MORNING - timedelta(days=3), "⚠️ Last full Stripe sync: 3 days ago"),
    (MORNING - timedelta(seconds=20), "Last full Stripe sync: under a minute ago"),
])
async def test_sync_age(svc, synced, line):
    if synced is not None:
        await synced_at(svc.db, synced)
    assert line in await report_text(svc, MORNING, {})


async def test_without_stripe(db, tg):
    text = await report_text(make_svc(db, tg, None), MORNING, {})
    assert "Last full Stripe sync" not in text and "Stripe: not configured (payments are off)" in text


async def test_removals_on(db, tg, stripe_fake):
    assert "Removals: on" in await report_text(make_svc(db, tg, stripe_fake, REMOVALS_ENABLED="true"), MORNING, {})


# --- the job ------------------------------------------------------------------------------

async def test_sent_once_a_day_from_nine_london(svc, tg):
    source = FakeHealth()
    assert not await daily_report_job(svc, MORNING - M, source)
    assert tg.sent_to(OWNER) == []
    assert await daily_report_job(svc, MORNING, source)
    assert not await daily_report_job(svc, MORNING + M, source)
    assert len(tg.sent_to(OWNER)) == 1 and await svc.db.kv_get(REPORT_DATE_KEY) == "2026-11-02"
    assert await daily_report_job(svc, MORNING + timedelta(days=1), source)
    assert len(tg.sent_to(OWNER)) == 2


async def test_in_summer_it_goes_at_eight_utc(svc, tg):
    source = FakeHealth()
    assert not await daily_report_job(svc, datetime(2026, 7, 1, 7, 59, tzinfo=timezone.utc), source)
    assert await daily_report_job(svc, datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc), source)


async def test_a_restart_later_the_same_day_catches_up(svc, tg):
    assert await daily_report_job(svc, MORNING + 5 * H, FakeHealth())


async def test_network_failure_retried_next_minute_and_counters_kept(svc, tg):
    store = JobHealthStore(svc.db)
    await store.update("sync_subscriptions", lambda h: h.failed(MORNING - H, "RuntimeError: x"))
    tg.send_results[OWNER] = SendResult.FAILED
    assert not await daily_report_job(svc, MORNING, store)
    assert await svc.db.kv_get(REPORT_DATE_KEY) is None
    assert (await store.get("sync_subscriptions")).failures_since_report == 1

    tg.send_results.clear()
    assert await daily_report_job(svc, MORNING + M, store)
    assert "sync_subscriptions: failed once" in tg.sent_to(OWNER)[-1]
    h = await store.get("sync_subscriptions")
    assert h.failures_since_report == 0 and h.consecutive_failures == 1   # still failing: listed again tomorrow


@pytest.mark.parametrize("result", [SendResult.BLOCKED, SendResult.REJECTED])
async def test_blocked_or_rejected_is_not_retried_every_minute(svc, tg, result):
    store = JobHealthStore(svc.db)
    await store.update("send_notices", lambda h: h.failed(MORNING - H, "x"))
    tg.send_results[OWNER] = result
    assert not await daily_report_job(svc, MORNING, store)
    assert not await daily_report_job(svc, MORNING + M, store)
    assert len(tg.sent_to(OWNER)) == 1 and await svc.db.kv_get(REPORT_DATE_KEY) == "2026-11-02"
    assert (await store.get("send_notices")).failures_since_report == 1   # the owner never saw them


async def test_failures_while_it_was_being_sent_stay_for_the_next_report(svc, tg, monkeypatch):
    store = JobHealthStore(svc.db)
    await store.update("poll_checkouts", lambda h: h.failed(MORNING - H, "first"))
    real_send = tg.send

    async def send_while_another_run_fails(chat_id, text, markup=None):
        await store.update("poll_checkouts", lambda h: h.failed(MORNING, "second"))
        return await real_send(chat_id, text, markup)

    monkeypatch.setattr(tg, "send", send_while_another_run_fails)
    assert await daily_report_job(svc, MORNING, store)
    assert (await store.get("poll_checkouts")).failures_since_report == 1


async def test_a_long_report_fits_in_one_message(svc, tg):
    health = {f"job_{i:02}": JobHealth(consecutive_failures=1, failures_since_report=9,
                                       last_error="E" * 290 + " & <tag>") for i in range(40)}
    source = FakeHealth(health)
    assert await daily_report_job(svc, MORNING, source)
    [text] = tg.sent_to(OWNER)
    assert len(text.encode("utf-16-le")) // 2 <= 4096 and text.endswith("(cut short)")
    assert source.resets == [health]
