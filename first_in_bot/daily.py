"""Once-a-day jobs (the 09:00 report, the 03:30 backup) without a cron.

They run as ordinary 60-second scheduler jobs that ask daily_due whether today's
run is still outstanding, and store today's local date in kv only once the work
succeeded. So a bot that was down at the set time catches up as soon as it's back
the same day, and a restart after the work was done never repeats it.

The check compares local wall-clock time, so it holds across clock changes: on the
day the clocks go forward a time that doesn't exist (01:30 in London) is due at
02:00, and on the day they go back a time that happens twice is due only once.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from .db import Database, kv_set


def daily_due(now_utc: datetime, tz: ZoneInfo, at: time, last_done: str | None) -> tuple[bool, str]:
    """(due, today): due once local time has reached `at` (a naive wall-clock time)
    and the job hasn't been done today. today is the local date as YYYY-MM-DD,
    the value to store as last_done once the work succeeded."""
    local = now_utc.astimezone(tz)
    today = local.date().isoformat()
    return local.time() >= at and last_done != today, today


async def mark_done(db: Database, key: str, today: str) -> None:
    async with db.tx() as conn:
        await kv_set(conn, key, today)
