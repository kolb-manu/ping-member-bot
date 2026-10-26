"""Background jobs as asyncio tasks in the bot's own process.

No per-user timers: every job re-reads the DB on each tick, so a restart loses
nothing. One lock per job means a run never overlaps the previous one, and each
run is wrapped so an exception is logged and the loop carries on next tick.

A run fails if it raises or returns a result whose .ok is False (the Stripe sync,
the checkout poll and the disputes check report partial failures that way). A run
that returns daily.SKIPPED had nothing to do (not due yet, or waiting to retry) and
is recorded as neither. A job with a retry_interval is tried again that much
sooner after a failure than its usual interval. Each job's health
(failures in a row, failures since the last daily report, the last error) is kept
in kv so the daily report survives restarts. After alert_after failures in a row
the owner gets a rate-limited alert, and one "working again" DM once it recovers.

stop() is the graceful shutdown: no new runs start, a run in progress gets a
bounded time to finish, and only then is anything cancelled.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Mapping

import aiosqlite

from .alerts import alert_owner, send_owner, short_error
from .backup import nightly_backup
from .billing import poll_checkouts, refresh_price, sync_subscriptions
from .daily import SKIPPED, Skipped
from .db import Database, kv_set
from .disputes import check_disputes
from .notices import send_notices
from .report import daily_report_job
from .services import Services
from .sweep import sweep_removals
from .telegram_gateway import SendResult
from .timeutil import from_iso, to_iso

log = logging.getLogger(__name__)

HEALTH_KEY_PREFIX = "job_health:"
# systemd is given TimeoutStopSec=45 before it kills the process; this leaves room
# for closing the bot session and the DB after the jobs. (main.py waits for updates
# still being handled at the same time, for less, so that adds nothing.)
STOP_TIMEOUT = 25.0
# Extra lines for failures whose effect on members the owner should know about.
FAILURE_NOTES = {
    "sync_subscriptions": "Removing anyone with Stripe data is on hold until a sync succeeds, "
                          "because the removal sweep skips them while the sync is stale.",
    "sweep_removals": "Telegram refused to remove someone: check the bot still has the Ban users right "
                      "in the paid channel. /dryrun shows who is waiting.",
}


@dataclass(frozen=True)
class Job:
    name: str
    interval: float                    # seconds between the end of one run and the next
    run: Callable[[], Awaitable[Any]]
    initial_delay: float = 0.0
    alert_after: int = 1               # failures in a row before the owner is alerted
    retry_interval: float | None = None  # after a failed run, wait this instead of interval


def build_jobs(svc: Services) -> list[Job]:
    jobs: list[Job] = []
    if svc.stripe is not None:
        jobs += [
            # Sync first, straight away, so the sweep isn't stale right after a restart.
            # Stripe blips are normal, so only a run of failures is worth a DM.
            Job("sync_subscriptions", 300, lambda: sync_subscriptions(svc, svc.now()), initial_delay=0,
                alert_after=3),
            Job("poll_checkouts", 30, lambda: poll_checkouts(svc, svc.now()), initial_delay=5, alert_after=3),
            # Daily, but a failed check is retried after half an hour rather than a day later.
            Job("check_disputes", 86400, lambda: check_disputes(svc, svc.now()), initial_delay=120,
                alert_after=3, retry_interval=1800),
            # Only does anything while the price couldn't be read (at startup, say):
            # until then trial and subscribe checkouts are held back.
            Job("refresh_price", 300, lambda: refresh_price(svc), initial_delay=60, alert_after=3),
        ]
    health = JobHealthStore(svc.db)
    jobs += [
        Job("send_notices", 300, lambda: send_notices(svc, svc.now()), initial_delay=30),
        Job("sweep_removals", 600, lambda: sweep_removals(svc, svc.now()), initial_delay=90),
        # These two check every minute but do their work once a day: the report at
        # 09:00 local time, the backup at 03:30 (or as soon after as the bot is up).
        Job("daily_report", 60, lambda: daily_report_job(svc, svc.now(), health), initial_delay=60),
        Job("backup", 60, lambda: nightly_backup(svc, svc.now()), initial_delay=45),
    ]
    return jobs


# --- Job health ---------------------------------------------------------------------

@dataclass(frozen=True)
class JobHealth:
    consecutive_failures: int = 0
    failures_since_report: int = 0
    last_error: str | None = None      # plain text from short_error; escape before display
    last_ok_at: datetime | None = None
    last_failed_at: datetime | None = None
    alerted: bool = False              # the owner was told about these failures, so is owed a "working again"

    def succeeded(self, now: datetime) -> JobHealth:
        return replace(self, consecutive_failures=0, last_ok_at=now)

    def failed(self, now: datetime, error: str) -> JobHealth:
        return replace(self, consecutive_failures=self.consecutive_failures + 1,
                       failures_since_report=self.failures_since_report + 1,
                       last_error=error, last_failed_at=now)

    def to_json(self) -> str:
        d = asdict(self)
        for name in ("last_ok_at", "last_failed_at"):
            d[name] = to_iso(d[name]) if d[name] else None
        return json.dumps(d, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> JobHealth:
        if not raw:
            return cls()
        try:
            d = json.loads(raw)
            return cls(
                consecutive_failures=int(d.get("consecutive_failures") or 0),
                failures_since_report=int(d.get("failures_since_report") or 0),
                last_error=d.get("last_error"),
                last_ok_at=from_iso(d.get("last_ok_at")),
                last_failed_at=from_iso(d.get("last_failed_at")),
                alerted=bool(d.get("alerted")),
            )
        except (ValueError, TypeError, AttributeError):
            # Health is only bookkeeping: a bad value starts afresh rather than stopping a job.
            log.warning("Ignoring unreadable job health %r", raw[:100])
            return cls()


class JobHealthStore:
    """Job health in kv, one JSON value per job (job_health:<name>). Every change
    is a read-modify-write inside one tx, so a job's update and the daily report's
    reset can't overwrite each other."""

    def __init__(self, db: Database):
        self.db = db

    async def get(self, name: str) -> JobHealth:
        return JobHealth.from_json(await self.db.kv_get(HEALTH_KEY_PREFIX + name))

    async def update(self, name: str, change: Callable[[JobHealth], JobHealth]) -> JobHealth:
        async with self.db.tx() as conn:
            health = change(await _load(conn, name))
            await kv_set(conn, HEALTH_KEY_PREFIX + name, health.to_json())
        return health

    async def snapshot(self) -> dict[str, JobHealth]:
        rows = await self.db.fetchall(
            "SELECT key, value FROM kv WHERE key GLOB ? ORDER BY key", (HEALTH_KEY_PREFIX + "*",)
        )
        return {row["key"][len(HEALTH_KEY_PREFIX):]: JobHealth.from_json(row["value"]) for row in rows}

    async def reset_since_report(self, shown: Mapping[str, JobHealth]) -> None:
        """After a report went out: take off the failures it showed. Any that
        happened while it was being sent stay for the next report."""
        async with self.db.tx() as conn:
            for name, seen in shown.items():
                if not seen.failures_since_report:
                    continue
                health = await _load(conn, name)
                left = max(0, health.failures_since_report - seen.failures_since_report)
                await kv_set(conn, HEALTH_KEY_PREFIX + name, replace(health, failures_since_report=left).to_json())

    async def forget_except(self, names: Iterable[str]) -> None:
        """Drop the health of jobs that no longer exist (e.g. Stripe was switched
        off), so the report doesn't list them as failing for ever."""
        keep = {HEALTH_KEY_PREFIX + name for name in names}
        stale = [row["key"] for row in await self.db.fetchall(
            "SELECT key FROM kv WHERE key GLOB ?", (HEALTH_KEY_PREFIX + "*",)
        ) if row["key"] not in keep]
        if stale:
            async with self.db.tx() as conn:
                await conn.executemany("DELETE FROM kv WHERE key=?", [(key,) for key in stale])


async def _load(conn: aiosqlite.Connection, name: str) -> JobHealth:
    cur = await conn.execute("SELECT value FROM kv WHERE key=?", (HEALTH_KEY_PREFIX + name,))
    row = await cur.fetchone()
    return JobHealth.from_json(row["value"] if row else None)


def failure_text(job: Job, health: JobHealth) -> str:
    n = health.consecutive_failures
    times = "once" if n == 1 else f"{n} times in a row"
    lines = [
        f"⚠️ <b>Job {html.escape(job.name)} failed {times}</b>",
        f"Last error: {html.escape(health.last_error or 'unknown')}",
    ]
    if job.name in FAILURE_NOTES:
        lines.append(FAILURE_NOTES[job.name])
    lines.append("It keeps retrying on its own, and I'll tell you when it works again.")
    return "\n".join(lines)


def recovered_text(job: Job) -> str:
    return f"✅ {html.escape(job.name)} is working again."


# --- Scheduler ----------------------------------------------------------------------

class Scheduler:
    def __init__(
        self,
        jobs: list[Job],
        *,
        svc: Services | None = None,   # None: no health tracking or alerts (unit tests of the loop)
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.jobs = jobs
        self.svc = svc
        self.health = JobHealthStore(svc.db) if svc is not None else None
        self.locks = {job.name: asyncio.Lock() for job in jobs}
        self._sleep = sleep
        self._stopping = False
        self._tasks: dict[str, asyncio.Task] = {}

    async def run_once(self, job: Job) -> bool:
        """One guarded run. False if it failed or the previous run is still going
        (a run with nothing to do counts as not failed)."""
        lock = self.locks[job.name]
        if lock.locked():
            log.warning("Job %s is still running, skipping this tick", job.name)
            return False
        async with lock:
            outcome = await self._attempt(job)
            if outcome is not SKIPPED:
                await self._track(job, outcome)
        return outcome is None or outcome is SKIPPED

    async def _attempt(self, job: Job) -> str | Skipped | None:
        """Run the job. None if it worked, SKIPPED if it had nothing to do, else a
        short description of what went wrong."""
        try:
            result = await job.run()
        except Exception as exc:
            log.exception("Job %s failed", job.name)
            return short_error(exc)
        if result is SKIPPED:
            return SKIPPED
        if getattr(result, "ok", True) is False:
            # The job has logged the details itself.
            log.warning("Job %s finished with errors", job.name)
            return "finished with errors (details in the log)"
        return None

    async def _track(self, job: Job, error: str | None) -> None:
        """Record the outcome, and tell the owner when a job keeps failing or works
        again. Never raises: bookkeeping must not stop the loop."""
        if self.svc is None or self.health is None:
            return
        svc, store = self.svc, self.health
        now = svc.now()
        try:
            if error is None:
                health = await store.update(job.name, lambda h: h.succeeded(now))
                if health.alerted:
                    await self._send_recovered(job)
                return
            health = await store.update(job.name, lambda h: h.failed(now, error))
            # Every failure past the threshold asks; the alert's own rate limit keeps it
            # to one DM per 30 minutes, and a DM lost to the network is retried next time.
            if health.consecutive_failures >= job.alert_after:
                sent = await alert_owner(svc, f"job_failed:{job.name}", failure_text(job, health), now)
                if sent and not health.alerted:
                    await store.update(job.name, lambda h: replace(h, alerted=True))
        except Exception:
            log.exception("Couldn't record how job %s went", job.name)

    async def _send_recovered(self, job: Job) -> None:
        assert self.svc is not None and self.health is not None
        result = await send_owner(self.svc, recovered_text(job))
        if result is SendResult.FAILED:
            return   # still owed: the next good run tries again
        await self.health.update(job.name, lambda h: replace(h, alerted=False))

    async def _loop(self, job: Job) -> None:
        await self._sleep(job.initial_delay)
        while not self._stopping:
            ok = await self.run_once(job)
            if self._stopping:
                break
            await self._sleep(job.interval if ok or job.retry_interval is None else job.retry_interval)

    async def run(self) -> None:
        """Runs until stop() has finished, or until cancelled (which cancels every
        job loop at once)."""
        if self.health is not None:
            try:
                await self.health.forget_except(job.name for job in self.jobs)
            except Exception:
                log.exception("Couldn't tidy up old job health")
        if self._stopping:   # stop() came first: start nothing
            return
        self._tasks = {job.name: asyncio.create_task(self._loop(job), name=f"job:{job.name}")
                       for job in self.jobs}
        log.info("Scheduler started: %s", ", ".join(job.name for job in self.jobs))
        tasks = list(self._tasks.values())
        try:
            # wait, not gather: a loop that stop() cancels mustn't take the others with it.
            if tasks:
                await asyncio.wait(tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("Scheduler stopped")

    async def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Graceful shutdown: start no new runs, give runs in progress up to timeout
        seconds to finish, then cancel whatever is left."""
        self._stopping = True
        busy: dict[str, asyncio.Task] = {}
        for name, task in self._tasks.items():
            if task.done():
                continue
            if self.locks[name].locked():
                busy[name] = task
            else:
                task.cancel()   # only sleeping: nothing to finish
        if busy:
            log.info("Stopping: waiting up to %ss for %s to finish", timeout, ", ".join(busy))
            _, pending = await asyncio.wait(busy.values(), timeout=timeout)
            for name, task in busy.items():
                if task in pending:
                    log.warning("Job %s didn't finish within %ss; cancelling it", name, timeout)
                    task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
