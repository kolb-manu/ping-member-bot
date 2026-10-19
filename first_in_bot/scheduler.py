"""Background jobs as asyncio tasks in the bot's own process.

No per-user timers: every job re-reads the DB on each tick, so a restart loses
nothing. One lock per job means a run never overlaps the previous one, and each
run is wrapped so an exception is logged and the loop carries on next tick.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .billing import poll_checkouts, sync_subscriptions
from .notices import send_notices
from .services import Services
from .sweep import sweep_removals

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    name: str
    interval: float                    # seconds between the end of one run and the next
    run: Callable[[], Awaitable[Any]]
    initial_delay: float = 0.0


def build_jobs(svc: Services) -> list[Job]:
    jobs: list[Job] = []
    if svc.stripe is not None:
        jobs += [
            # Sync first, straight away, so the sweep isn't stale right after a restart.
            Job("sync_subscriptions", 300, lambda: sync_subscriptions(svc, svc.now()), initial_delay=0),
            Job("poll_checkouts", 30, lambda: poll_checkouts(svc, svc.now()), initial_delay=5),
        ]
    jobs += [
        Job("send_notices", 300, lambda: send_notices(svc, svc.now()), initial_delay=30),
        Job("sweep_removals", 600, lambda: sweep_removals(svc, svc.now()), initial_delay=90),
    ]
    return jobs


class Scheduler:
    def __init__(self, jobs: list[Job], *, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep):
        self.jobs = jobs
        self.locks = {job.name: asyncio.Lock() for job in jobs}
        self._sleep = sleep

    async def run_once(self, job: Job) -> bool:
        """One guarded run. False if it failed or the previous run is still going."""
        lock = self.locks[job.name]
        if lock.locked():
            log.warning("Job %s is still running, skipping this tick", job.name)
            return False
        async with lock:
            try:
                await job.run()
            except Exception:
                # TODO(phase 4): rate-limited owner alert when a job crashes.
                log.exception("Job %s failed", job.name)
                return False
        return True

    async def _loop(self, job: Job) -> None:
        await self._sleep(job.initial_delay)
        while True:
            await self.run_once(job)
            await self._sleep(job.interval)

    async def run(self) -> None:
        """Runs until cancelled; cancelling stops every job loop."""
        tasks = [asyncio.create_task(self._loop(job), name=f"job:{job.name}") for job in self.jobs]
        log.info("Scheduler started: %s", ", ".join(job.name for job in self.jobs))
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("Scheduler stopped")
