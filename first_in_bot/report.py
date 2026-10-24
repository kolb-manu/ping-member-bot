"""The daily report to the owner (spec section 10), at 09:00 local time: the /stats
summary, which jobs failed since the last report, how fresh the Stripe data is and
which modes the bot is running in.

report_text builds the message from a list of lines so it's easy to add to, and
can be reused by an admin command. daily_report_job only marks the day done after
Telegram took the message, so a network failure is retried a minute later and a
restart never sends it twice.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Mapping, Protocol
from zoneinfo import ZoneInfo

from .admin import stats_text
from .alerts import send_owner
from .billing import LAST_FULL_SYNC_KEY
from .daily import daily_due, mark_done
from .services import Services
from .sweep import SYNC_MAX_AGE, _fit
from .telegram_gateway import SendResult
from .timeutil import format_local, format_local_date, from_iso

if TYPE_CHECKING:   # the scheduler imports this module, so only for type hints
    from .scheduler import JobHealth

log = logging.getLogger(__name__)

REPORT_AT = time(9, 0)                 # local time, config.timezone
REPORT_DATE_KEY = "daily_report_date"  # local date of the last report that went out


class HealthSource(Protocol):
    async def snapshot(self) -> dict[str, JobHealth]: ...

    async def reset_since_report(self, shown: Mapping[str, JobHealth]) -> None: ...


def _ago(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} days"


def _times(n: int) -> str:
    return "once" if n == 1 else f"{n} times"


def health_lines(health: Mapping[str, JobHealth], tz: ZoneInfo) -> list[str]:
    """A job is listed if it failed since the last report or its last run failed
    (a daily job may not have run again since)."""
    troubled = {name: h for name, h in health.items() if h.failures_since_report or h.consecutive_failures}
    if not troubled:
        return ["✅ All jobs healthy"]
    lines = ["⚠️ <b>Jobs with failures</b>"]
    for name, h in troubled.items():
        if not h.failures_since_report:
            what = "still failing (it hasn't run since the last report)"
        else:
            state = "still failing" if h.consecutive_failures else "working again"
            what = f"failed {_times(h.failures_since_report)} since the last report, {state}"
        last_ok = format_local(h.last_ok_at, tz) if h.last_ok_at else "never"
        lines.append(f"• {html.escape(name)}: {what}. Last error: {html.escape(h.last_error or 'unknown')}. "
                     f"Last worked: {last_ok}")
    return lines


async def sync_line(svc: Services, now: datetime) -> str | None:
    if svc.stripe is None:
        return None
    last = from_iso(await svc.db.kv_get(LAST_FULL_SYNC_KEY))
    if last is None:
        return "⚠️ Last full Stripe sync: never, so removing anyone with Stripe data is on hold"
    if now - last >= SYNC_MAX_AGE:
        return f"⚠️ Last full Stripe sync: {_ago(now - last)} ago, so removing anyone with Stripe data is on hold"
    return f"Last full Stripe sync: {_ago(now - last)} ago"


def mode_lines(svc: Services) -> list[str]:
    """Also used by the startup alert."""
    stripe = f"{svc.config.stripe_mode} mode" if svc.stripe is not None else "not configured (payments are off)"
    removals = "on" if svc.config.removals_enabled else "dry run (nobody is removed)"
    return [f"Stripe: {stripe}", f"Removals: {removals}"]


async def report_text(svc: Services, now: datetime, health: Mapping[str, JobHealth]) -> str:
    tz = svc.config.timezone
    lines = [
        f"☀️ <b>First In daily report</b>, {format_local_date(now, tz)}",
        "",
        await stats_text(svc.db, now),
        "",
        *health_lines(health, tz),
        "",
    ]
    sync = await sync_line(svc, now)
    if sync is not None:
        lines.append(sync)
    lines += mode_lines(svc)
    return "\n".join(lines)


async def daily_report_job(svc: Services, now: datetime, health_source: HealthSource) -> bool:
    """Scheduler job, every minute. Sends the report once a day, at REPORT_AT local
    time or as soon after it as the bot is running. True when it went out."""
    due, today = daily_due(now, svc.config.timezone, REPORT_AT, await svc.db.kv_get(REPORT_DATE_KEY))
    if not due:
        return False
    health = await health_source.snapshot()
    result = await send_owner(svc, _fit(await report_text(svc, now, health)))
    if result is SendResult.FAILED:
        log.warning("Couldn't send the daily report; trying again in a minute")
        return False
    await mark_done(svc.db, REPORT_DATE_KEY, today)
    if result is not SendResult.OK:
        # BLOCKED (the owner hasn't pressed /start) or REJECTED: retrying every minute won't help.
        log.warning("The owner didn't get the daily report (%s)", result.value)
        return False
    await health_source.reset_since_report(health)
    log.info("Daily report sent for %s", today)
    return True
