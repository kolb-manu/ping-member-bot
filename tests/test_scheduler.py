"""Scheduler loop behaviour, job wiring and the shared-connection tx lock."""
import asyncio

import pytest

from first_in_bot.scheduler import Job, Scheduler, build_jobs
from first_in_bot.users import get_user, register_user
from tests.fakes import NOW, FakeTelegram, add_user, make_svc


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
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    scheduler = Scheduler([Job("flaky", 10, flaky)], sleep=fake_sleep)
    with pytest.raises(asyncio.CancelledError):
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
    assert no_stripe == ["send_notices", "sweep_removals"]
    jobs = {j.name: j for j in build_jobs(make_svc(db, FakeTelegram(), stripe_fake))}
    assert set(jobs) == {"sync_subscriptions", "poll_checkouts", "send_notices", "sweep_removals"}
    assert jobs["poll_checkouts"].interval == 30 and jobs["sweep_removals"].interval == 600
    assert jobs["sync_subscriptions"].initial_delay < jobs["sweep_removals"].initial_delay


async def test_concurrent_transactions_on_one_connection(db):
    """Jobs and handlers share one connection; tx() must serialise them."""
    await asyncio.gather(*(register_user(db, i, f"u{i}", "U", "ig", NOW) for i in range(20)))
    rows = await db.fetchall("SELECT COUNT(*) AS n FROM users")
    assert rows[0]["n"] == 20


async def test_start_again_clears_blocked_bot(db):
    await add_user(db, 1, blocked_bot=True)
    await register_user(db, 1, "user1", "Name", None, NOW)
    assert not (await get_user(db, 1)).blocked_bot
