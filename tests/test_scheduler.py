"""Scheduler loop behaviour, job wiring, job health and owner alerts, graceful
shutdown, and the shared-connection tx lock."""
import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from first_in_bot.billing import SyncResult
from first_in_bot.daily import SKIPPED
from first_in_bot.scheduler import Job, JobHealth, JobHealthStore, Scheduler, build_jobs
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.timeutil import to_iso
from first_in_bot.users import get_user, register_user
from tests.fakes import NOW, OWNER, FakeTelegram, add_user, make_svc

M = timedelta(minutes=1)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def csvc(svc, clock):
    """Services whose clock the test moves on."""
    return replace(svc, clock=clock)


def outcomes(*steps):
    """A job that, run by run, raises the exception or returns the value given."""
    queue = list(steps)

    async def run():
        step = queue.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    return run


async def test_failing_job_is_logged_and_loop_carries_on(caplog):
    runs = []

    async def flaky():
        runs.append(len(runs))
        if len(runs) == 1:
            raise RuntimeError("boom")

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError   # ends the only loop, so run() returns
        await asyncio.sleep(0)

    scheduler = Scheduler([Job("flaky", 10, flaky)], sleep=fake_sleep)
    await scheduler.run()
    assert len(runs) == 3                      # kept going after the exception
    assert "Job flaky failed" in caplog.text
    assert sleeps[0] == 0 and sleeps[1:] == [10, 10, 10]


async def test_runs_of_one_job_never_overlap():
    release = asyncio.Event()
    started = []

    async def slow():
        started.append(1)
        await release.wait()

    scheduler = Scheduler([Job("slow", 1, slow)])
    first = asyncio.create_task(scheduler.run_once(scheduler.jobs[0]))
    await asyncio.sleep(0)
    assert await scheduler.run_once(scheduler.jobs[0]) is False   # still running: skipped
    release.set()
    assert await first is True and started == [1]


async def test_job_list_depends_on_stripe(db, stripe_fake):
    no_stripe = [j.name for j in build_jobs(make_svc(db, FakeTelegram(), None))]
    assert no_stripe == ["send_notices", "sweep_removals", "daily_report", "backup"]
    jobs = {j.name: j for j in build_jobs(make_svc(db, FakeTelegram(), stripe_fake))}
    assert set(jobs) == {"sync_subscriptions", "poll_checkouts", "check_disputes", "refresh_price",
                         "send_notices", "sweep_removals", "daily_report", "backup"}
    assert jobs["check_disputes"].interval == 86400
    # A failed dispute check is tried again in half an hour, not a day later.
    assert jobs["check_disputes"].retry_interval == 1800
    assert {name for name, j in jobs.items() if j.retry_interval is not None} == {"check_disputes"}
    assert jobs["refresh_price"].interval == 300
    assert jobs["poll_checkouts"].interval == 30 and jobs["sweep_removals"].interval == 600
    assert jobs["sync_subscriptions"].initial_delay < jobs["sweep_removals"].initial_delay
    assert jobs["daily_report"].interval == 60 and jobs["daily_report"].initial_delay == 60
    assert jobs["backup"].interval == 60
    # Stripe blips are normal: only a run of three failures alerts. Everything else alerts at once.
    assert {name: j.alert_after for name, j in jobs.items() if j.alert_after != 1} == {
        "sync_subscriptions": 3, "poll_checkouts": 3, "check_disputes": 3, "refresh_price": 3}


async def test_daily_report_job_is_wired_to_the_report(db, tg, stripe_fake, clock):
    svc = replace(make_svc(db, tg, stripe_fake), clock=clock)
    clock.now = NOW.replace(hour=9)   # 09:00 London in November
    [job] = [j for j in build_jobs(svc) if j.name == "daily_report"]
    assert await job.run() is True
    assert "First In daily report" in tg.sent_to(OWNER)[0]


async def test_concurrent_transactions_on_one_connection(db):
    """Jobs and handlers share one connection; tx() must serialise them."""
    await asyncio.gather(*(register_user(db, i, f"u{i}", "U", "ig", NOW) for i in range(20)))
    rows = await db.fetchall("SELECT COUNT(*) AS n FROM users")
    assert rows[0]["n"] == 20


async def test_start_again_clears_blocked_bot(db):
    await add_user(db, 1, blocked_bot=True)
    await register_user(db, 1, "user1", "Name", None, NOW)
    assert not (await get_user(db, 1)).blocked_bot


# --- job health ---------------------------------------------------------------------------

async def test_health_counts_raises_and_not_ok_results_and_lives_in_kv(csvc, clock):
    job = Job("j", 60, outcomes(SyncResult(ok=False), RuntimeError("boom"), SyncResult()), alert_after=5)
    scheduler = Scheduler([job], svc=csvc)
    store = JobHealthStore(csvc.db)

    assert await scheduler.run_once(job) is False
    h = await store.get("j")
    assert (h.consecutive_failures, h.failures_since_report, h.last_failed_at) == (1, 1, NOW)
    assert "finished with errors" in h.last_error

    clock.advance(M)
    assert await scheduler.run_once(job) is False
    h = await store.get("j")
    assert (h.consecutive_failures, h.failures_since_report, h.last_error) == (2, 2, "RuntimeError: boom")

    clock.advance(M)
    assert await scheduler.run_once(job) is True
    h = await store.get("j")
    assert (h.consecutive_failures, h.failures_since_report, h.last_ok_at) == (0, 2, NOW + 2 * M)
    stored = json.loads(await csvc.db.kv_get("job_health:j"))
    assert stored["consecutive_failures"] == 0 and stored["last_ok_at"] == to_iso(NOW + 2 * M)


async def test_results_without_ok_and_ok_results_count_as_success(csvc):
    job = Job("j", 60, outcomes(3, None, SyncResult(ok=True)))
    scheduler = Scheduler([job], svc=csvc)
    assert [await scheduler.run_once(job) for _ in range(3)] == [True, True, True]
    assert (await JobHealthStore(csvc.db).get("j")).failures_since_report == 0


def test_unreadable_health_starts_afresh():
    assert JobHealth.from_json("{not json") == JobHealth()
    assert JobHealth.from_json(None) == JobHealth()
    h = JobHealth(consecutive_failures=2, last_error="x", last_ok_at=NOW, alerted=True)
    assert JobHealth.from_json(h.to_json()) == h


async def test_health_of_retired_jobs_is_forgotten(csvc):
    store = JobHealthStore(csvc.db)
    await store.update("old_job", lambda h: h.failed(NOW, "x"))
    await store.update("kept", lambda h: h.failed(NOW, "y"))
    scheduler = Scheduler([Job("kept", 60, outcomes())], svc=csvc)
    await scheduler.stop()          # stop first, so run() only tidies up
    await scheduler.run()
    assert set(await store.snapshot()) == {"kept"}


# --- owner alerts for failing jobs -----------------------------------------------------------

async def test_sync_alerts_after_three_failures_in_a_row(csvc, tg, clock):
    job = Job("sync_subscriptions", 300, outcomes(*[RuntimeError("Stripe down")] * 5), alert_after=3)
    scheduler = Scheduler([job], svc=csvc)
    for _ in range(2):
        await scheduler.run_once(job)
        clock.advance(5 * M)
    assert tg.sent_to(OWNER) == []
    await scheduler.run_once(job)
    [alert] = tg.sent_to(OWNER)
    assert "sync_subscriptions failed 3 times in a row" in alert
    assert "RuntimeError: Stripe down" in alert
    assert "Removing anyone with Stripe data is on hold until a sync succeeds" in alert

    clock.advance(5 * M)
    await scheduler.run_once(job)                     # 4th failure, 5 minutes later: rate-limited
    assert len(tg.sent_to(OWNER)) == 1
    clock.advance(30 * M)
    await scheduler.run_once(job)                     # half an hour on: a reminder
    assert len(tg.sent_to(OWNER)) == 2 and "5 times in a row" in tg.sent_to(OWNER)[1]


async def test_working_again_is_sent_once_per_incident(csvc, tg, clock):
    job = Job("send_notices", 300, outcomes(RuntimeError("boom"), 0, 0, RuntimeError("boom"), 0))
    scheduler = Scheduler([job], svc=csvc)
    await scheduler.run_once(job)
    assert "send_notices failed once" in tg.sent_to(OWNER)[0]
    await scheduler.run_once(job)
    await scheduler.run_once(job)
    assert tg.sent_to(OWNER)[1:] == ["✅ send_notices is working again."]

    # A new failure 10 minutes later is rate-limited, so the owner isn't owed a recovery DM.
    clock.advance(10 * M)
    await scheduler.run_once(job)
    await scheduler.run_once(job)
    assert len(tg.sent_to(OWNER)) == 2


async def test_no_working_again_when_the_owner_was_never_told(csvc, tg):
    job = Job("poll_checkouts", 30, outcomes(RuntimeError("blip"), RuntimeError("blip"), None), alert_after=3)
    scheduler = Scheduler([job], svc=csvc)
    for _ in range(3):
        await scheduler.run_once(job)
    assert tg.sent_to(OWNER) == []


async def test_alert_lost_to_the_network_is_retried_on_the_next_failure(csvc, tg, clock):
    job = Job("j", 60, outcomes(RuntimeError("a"), RuntimeError("b"), None))
    scheduler = Scheduler([job], svc=csvc)
    tg.send_results[OWNER] = SendResult.FAILED
    await scheduler.run_once(job)
    assert not (await JobHealthStore(csvc.db).get("j")).alerted
    tg.send_results.clear()
    clock.advance(M)
    await scheduler.run_once(job)                     # not rate-limited: the first never arrived
    assert "failed 2 times in a row" in tg.sent_to(OWNER)[-1]
    await scheduler.run_once(job)
    assert tg.sent_to(OWNER)[-1] == "✅ j is working again."


async def test_working_again_lost_to_the_network_is_retried(csvc, tg):
    job = Job("j", 60, outcomes(RuntimeError("a"), None, None, None))
    scheduler = Scheduler([job], svc=csvc)
    await scheduler.run_once(job)
    tg.send_results[OWNER] = SendResult.FAILED
    await scheduler.run_once(job)
    tg.send_results.clear()
    await scheduler.run_once(job)
    await scheduler.run_once(job)
    assert tg.sent_to(OWNER)[1:] == ["✅ j is working again."] * 2   # the failed try, then the real one


async def test_health_survives_a_restart(csvc, tg, clock):
    job = Job("sync_subscriptions", 300, outcomes(*[RuntimeError("down")] * 3), alert_after=3)
    first = Scheduler([job], svc=csvc)
    await first.run_once(job)
    await first.run_once(job)
    restarted = Scheduler([job], svc=csvc)
    await restarted.run_once(job)
    assert "3 times in a row" in tg.sent_to(OWNER)[0]


async def test_alert_text_is_escaped_and_has_no_keys(csvc, tg):
    job = Job("j", 60, outcomes(RuntimeError("bad <b>reply</b> for sk_test_" + "a" * 24)))
    await Scheduler([job], svc=csvc).run_once(job)
    [alert] = tg.sent_to(OWNER)
    assert "RuntimeError: bad &lt;b&gt;reply&lt;/b&gt; for [key]" in alert and "sk_test_" not in alert


async def test_bookkeeping_errors_never_stop_a_job(csvc, monkeypatch, caplog):
    async def broken(self, name, change):
        raise RuntimeError("disk full")

    monkeypatch.setattr(JobHealthStore, "update", broken)
    job = Job("j", 60, outcomes(None, RuntimeError("boom")))
    scheduler = Scheduler([job], svc=csvc)
    assert await scheduler.run_once(job) is True
    assert await scheduler.run_once(job) is False
    assert "Couldn't record how job j went" in caplog.text


# --- graceful shutdown ---------------------------------------------------------------------

async def test_stop_lets_a_running_job_finish_and_starts_nothing_new(csvc):
    started, release = asyncio.Event(), asyncio.Event()
    runs = []

    async def slow():
        runs.append("start")
        started.set()
        await release.wait()
        runs.append("end")

    async def later():
        runs.append("later")

    scheduler = Scheduler([Job("slow", 0.01, slow), Job("later", 60, later, initial_delay=3600)], svc=csvc)
    running = asyncio.create_task(scheduler.run())
    await started.wait()
    stopping = asyncio.create_task(scheduler.stop(timeout=5))
    await asyncio.sleep(0.05)
    assert not stopping.done()                 # waiting for the run in progress
    release.set()
    await asyncio.wait_for(stopping, 1)
    await asyncio.wait_for(running, 1)
    assert runs == ["start", "end"]            # it finished, and no new run started
    assert (await JobHealthStore(csvc.db).get("slow")).last_ok_at == NOW   # its outcome was recorded


async def test_stop_cancels_a_job_that_overruns(csvc, caplog):
    started = asyncio.Event()
    cancelled = []

    async def stuck():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    scheduler = Scheduler([Job("stuck", 60, stuck)], svc=csvc)
    running = asyncio.create_task(scheduler.run())
    await started.wait()
    await asyncio.wait_for(scheduler.stop(timeout=0.05), 1)
    await asyncio.wait_for(running, 1)
    assert cancelled == [True]
    assert "Job stuck didn't finish within 0.05s; cancelling it" in caplog.text


async def test_cancelling_run_still_stops_every_loop():
    started = asyncio.Event()

    async def stuck():
        started.set()
        await asyncio.Event().wait()

    scheduler = Scheduler([Job("stuck", 60, stuck)])
    running = asyncio.create_task(scheduler.run())
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert all(task.done() for task in scheduler._tasks.values())


# --- ticks with nothing to do, and retrying sooner after a failure ------------------------------

async def test_skipped_ticks_count_as_neither_success_nor_failure(csvc, tg, clock):
    job = Job("j", 60, outcomes(RuntimeError("a"), SKIPPED, SKIPPED, RuntimeError("b"), SKIPPED, None))
    scheduler = Scheduler([job], svc=csvc)
    store = JobHealthStore(csvc.db)
    assert await scheduler.run_once(job) is False
    clock.advance(M)
    assert [await scheduler.run_once(job) for _ in range(2)] == [True, True]   # not failures...
    h = await store.get("j")
    assert (h.consecutive_failures, h.last_ok_at) == (1, None)                 # ...and not successes
    assert tg.sent_to(OWNER) == ["⚠️ <b>Job j failed once</b>\nLast error: RuntimeError: a\n"
                                 "It keeps retrying on its own, and I'll tell you when it works again."]
    clock.advance(M)
    await scheduler.run_once(job)
    await scheduler.run_once(job)
    assert (await store.get("j")).consecutive_failures == 2                    # each real try counts
    await scheduler.run_once(job)
    assert tg.sent_to(OWNER)[-1] == "✅ j is working again."
    assert (await store.get("j")).last_ok_at == NOW + 2 * M


async def test_a_failed_run_is_retried_after_retry_interval():
    job = Job("daily", 86400, outcomes(RuntimeError("blip"), SyncResult(ok=False), None, SKIPPED),
              retry_interval=1800)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 4:
            raise asyncio.CancelledError   # ends the only loop, so run() returns
        await asyncio.sleep(0)

    await Scheduler([job], sleep=fake_sleep).run()
    # Raised, not ok: half an hour. Worked, nothing to do: back to the usual day.
    assert sleeps == [0, 1800, 1800, 86400, 86400]


# --- the real jobs through the scheduler --------------------------------------------------------

NIGHT = NOW.replace(day=2, hour=3, minute=30)   # 03:30 GMT is 03:30 London in November


async def test_failing_backup_alerts_per_real_try_and_never_falsely_recovers(db, tg, clock, tmp_path):
    """The backup ticks every minute but only tries every RETRY_AFTER while it keeps
    failing. The ticks in between mustn't count as it working again."""
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")   # so the backup folder can't be made: disk full, folder gone
    svc = replace(make_svc(db, tg, None, BACKUP_DIR=str(blocker / "backups")), clock=clock)
    [job] = [j for j in build_jobs(svc) if j.name == "backup"]
    scheduler = Scheduler([job], svc=svc)
    store = JobHealthStore(db)

    clock.now = NIGHT
    for _ in range(30):   # 03:30 to 03:59: one real try, then waiting to retry
        await scheduler.run_once(job)
        clock.advance(M)
    [alert] = tg.sent_to(OWNER)
    assert "Job backup failed once" in alert and "BackupError" in alert
    h = await store.get("backup")
    assert (h.consecutive_failures, h.failures_since_report, h.last_ok_at) == (1, 1, None)

    await scheduler.run_once(job)   # 04:00: the next real try fails too, and the count goes up
    assert (await store.get("backup")).consecutive_failures == 2
    assert "Job backup failed 2 times in a row" in tg.sent_to(OWNER)[-1]

    blocker.unlink()                # fixed, but nothing is due until 04:30
    for _ in range(30):
        clock.advance(M)
        await scheduler.run_once(job)
    assert clock.now == NIGHT + 60 * M
    assert [m for m in tg.sent_to(OWNER) if "working again" in m] == ["✅ backup is working again."]
    assert len(tg.sent_to(OWNER)) == 3
    h = await store.get("backup")
    assert (h.consecutive_failures, h.failures_since_report, h.last_ok_at) == (0, 2, NIGHT + 60 * M)
    assert (blocker / "backups" / "first-in-2026-11-02.db").exists()


async def test_failing_daily_report_is_not_working_again_at_midnight(csvc, tg, clock, monkeypatch):
    from first_in_bot import report

    async def broken(*args, **kwargs):
        raise RuntimeError("report broke")

    monkeypatch.setattr(report, "report_text", broken)
    [job] = [j for j in build_jobs(csvc) if j.name == "daily_report"]
    scheduler = Scheduler([job], svc=csvc)
    clock.now = NOW.replace(day=2, hour=9)
    await scheduler.run_once(job)
    assert "Job daily_report failed once" in tg.sent_to(OWNER)[0]
    clock.now = NOW.replace(day=3, hour=0)   # a new day, not due until 09:00
    await scheduler.run_once(job)
    assert len(tg.sent_to(OWNER)) == 1 and (await JobHealthStore(csvc.db).get("daily_report")).last_ok_at is None

    monkeypatch.undo()
    clock.now = NOW.replace(day=3, hour=9)
    await scheduler.run_once(job)
    assert "First In daily report" in tg.sent_to(OWNER)[1]
    assert tg.sent_to(OWNER)[2] == "✅ daily_report is working again."


async def test_checkout_poll_alerts_when_stripe_keeps_failing(csvc, tg, stripe_fake, clock):
    from first_in_bot.billing import start_checkout

    await start_checkout(csvc, await add_user(csvc.db, 1), NOW)
    stripe_fake.fail_get = True
    [job] = [j for j in build_jobs(csvc) if j.name == "poll_checkouts"]
    scheduler = Scheduler([job], svc=csvc)
    for _ in range(2):
        await scheduler.run_once(job)
        clock.advance(timedelta(seconds=30))
    assert tg.sent_to(OWNER) == []                    # a blip or two is normal
    await scheduler.run_once(job)
    [alert] = tg.sent_to(OWNER)
    assert "Job poll_checkouts failed 3 times in a row" in alert
    stripe_fake.fail_get = False
    await scheduler.run_once(job)
    assert tg.sent_to(OWNER)[-1] == "✅ poll_checkouts is working again."
