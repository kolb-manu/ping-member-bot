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
from .backup import BACKUP_DATE_KEY, BACKUP_FAILED_KEY
from .billing import LAST_FULL_SYNC_KEY
from .daily import SKIPPED, Skipped, daily_due, mark_done
from .maintenance import describe_duration, paused_since
from .services import Services
from .sweep import CONFIRM_TTL, SYNC_MAX_AGE, _fit, pending_confirm
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


async def state_lines(svc: Services, now: datetime) -> list[str]:
    """What the owner switched on, or has to act on. Also used by the startup alert."""
    tz = svc.config.timezone
    lines = []
    since = await paused_since(svc.db)
    if since is not None:
        lines.append(f"⏸ Paused since {format_local(since, tz)} ({describe_duration(now - since)}): "
                     "nobody is removed and no reminders go out until /resume")
    pending = await pending_confirm(svc.db)
    if pending is not None and timedelta(0) <= now - pending.at < CONFIRM_TTL:
        lines.append(f"⛔ The mass-removal cap is holding back {len(pending.ids)} removals: "
                     "/confirmsweep to go ahead, /dryrun to look first")
    return lines


async def backup_line(svc: Services) -> str:
    failed = from_iso(await svc.db.kv_get(BACKUP_FAILED_KEY))
    if failed is not None:
        return f"⚠️ Last backup attempt failed at {format_local(failed, svc.config.timezone)}"
    last = await svc.db.kv_get(BACKUP_DATE_KEY)
    return f"Last backup: {last}" if last else "Last backup: none yet"


def mode_lines(svc: Services) -> list[str]:
    """Also used by the startup alert."""
    removals = f"Removals: {'on' if svc.config.removals_enabled else 'dry run (nobody is removed)'}"
    if svc.stripe is None:
        return ["Stripe: not configured (payments are off)", removals]
    if svc.price_label is None:
        # The price must come before any Stripe link, so no checkouts until it's read.
        return [f"Stripe: {svc.config.stripe_mode} mode",
                "⚠️ Couldn't read the price from Stripe. Until it can (it's retried every 5 minutes), "
                "the trial and subscribe buttons ask people to try again in a few minutes.",
                removals]
    return [f"Stripe: {svc.config.stripe_mode} mode, {html.escape(svc.price_label)}", removals]


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
    lines += await state_lines(svc, now)
    sync = await sync_line(svc, now)
    if sync is not None:
        lines.append(sync)
    lines.append(await backup_line(svc))
    lines += mode_lines(svc)
    return "\n".join(lines)


async def daily_report_job(svc: Services, now: datetime, health_source: HealthSource) -> bool | Skipped:
    """Scheduler job, every minute. Sends the report once a day, at REPORT_AT local
    time or as soon after it as the bot is running. True when it went out.

    SKIPPED when it isn't due, and when Telegram couldn't be reached (it's tried
    again a minute later): neither says whether building the report works, so
    neither may count as the job working again after a failure."""
    due, today = daily_due(now, svc.config.timezone, REPORT_AT, await svc.db.kv_get(REPORT_DATE_KEY))
    if not due:
        return SKIPPED
    health = await health_source.snapshot()
    result = await send_owner(svc, _fit(await report_text(svc, now, health)))
    if result is SendResult.FAILED:
        log.warning("Couldn't send the daily report; trying again in a minute")
        return SKIPPED
    await mark_done(svc.db, REPORT_DATE_KEY, today)
    if result is not SendResult.OK:
        # BLOCKED (the owner hasn't pressed /start) or REJECTED: retrying every minute won't help.
        log.warning("The owner didn't get the daily report (%s)", result.value)
        return False
    await health_source.reset_since_report(health)
    log.info("Daily report sent for %s", today)
    return True
