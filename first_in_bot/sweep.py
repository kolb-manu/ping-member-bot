"""sweep_removals: take people who are no longer entitled out of the paid channel,
behind the safety rails of spec section 8.

Someone is removed only if is_entitled says no, Telegram confirms they're in
the channel, they aren't an admin, and (if they may have a Stripe subscription,
i.e. have a Stripe customer or subscription ID) the last full Stripe sync is
under 15 minutes old. A mass-removal cap blocks the whole sweep. Right before the
DM and again right before the kick the user is re-read, so someone who became
entitled mid-sweep is left alone. With REMOVALS_ENABLED=false (the default, and
how it stays for now) it's a dry run: nobody is removed and only the owner gets a
"Would remove" report, and only when that report changes (a report that couldn't
be delivered is retried next sweep).
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import texts
from .access import set_in_channel
from .audit import audit, audit_if_changed
from .billing import LAST_FULL_SYNC_KEY
from .db import kv_set
from .keyboards import ended_keyboard
from .models import User
from .notices import NoticeOutcome, deliver_notice
from .services import Services
from .telegram_gateway import IN_CHANNEL_STATUSES, SendResult
from .timeutil import from_iso, to_iso
from .users import get_user

log = logging.getLogger(__name__)

SYNC_MAX_AGE = timedelta(minutes=15)
# Last report state, written with its change-only audit rows.
FINGERPRINT_KEY = "sweep_report_fingerprint"
# Last report state the owner was actually told about (or that needed no DM).
NOTIFIED_KEY = "sweep_report_notified_fp"
REPORT_LIST_LIMIT = 40
REPORT_MAX_CHARS = 4000   # Telegram's message limit is 4096 (UTF-16 units)

SKIP_LABELS = {
    "admin": "admin",
    "channel_admin": "channel admin",
    "not_in_channel": "not in channel, flag cleared",
    "status_unknown": "couldn't check membership",
    "stale_sync": "Stripe sync is stale",
    "dm_failed": "DM failed, retrying next sweep",
    "now_entitled": "became entitled during the sweep",
}
ENDED_LABELS = {"trial_ended": "trial ended", "sub_ended": "subscription ended", "access_ended": "free access ended"}
ENDED_TEXTS = {"trial_ended": texts.TRIAL_ENDED, "sub_ended": texts.SUB_ENDED, "access_ended": texts.ACCESS_ENDED}


@dataclass
class SweepReport:
    dry_run: bool
    entitled_in_channel: int = 0
    would_remove: list[User] = field(default_factory=list)
    skipped: list[tuple[User, str]] = field(default_factory=list)
    blocked: str | None = None
    removed: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    owner_notified: bool = False


def ended_kind(user: User, was_active: bool = False) -> str:
    """Which goodbye message fits: a trial that never turned into a paid period,
    a subscription that ended, or a manual grant that ran out. was_active: this
    subscription was seen as "active" (paid) at some point."""
    if user.stripe_sub_id:
        if user.sub_trial_end and not was_active:
            # A failed trial conversion still opens a new period (starting at the
            # trial end), so look at where the current period starts, not ends.
            # Rows from before migration 3 have no period start until the next sync.
            if user.sub_period_start is not None:
                never_paid = user.sub_period_start <= user.sub_trial_end
            else:
                never_paid = user.sub_period_end is None or user.sub_period_end <= user.sub_trial_end
            if never_paid:
                return "trial_ended"
        return "sub_ended"
    return "access_ended"


def _cycle_key(user: User, kind: str) -> str:
    if kind == "trial_ended" and user.sub_trial_end:
        return to_iso(user.sub_trial_end)
    if kind == "sub_ended":
        return f"{user.stripe_sub_id}:{to_iso(user.sub_period_end) if user.sub_period_end else ''}"
    return to_iso(user.manual_grant_until) if user.manual_grant_until else "none"


def cap_problem(count: int, entitled_in_channel: int, max_removals: int) -> str | None:
    if count > max_removals:
        return f"{count} removals is more than MAX_REMOVALS_PER_SWEEP ({max_removals})"
    # Deliberate deviation from spec section 8: the 20% rule only applies above
    # 2 removals. Without the floor a small test channel (say 1 entitled member
    # and 1 to remove = 50%) would block every sweep.
    if count > 2 and count > 0.2 * (entitled_in_channel + count):
        return f"{count} removals is more than 20% of {entitled_in_channel + count} members"
    return None


def fingerprint(report: SweepReport) -> str:
    state = {
        "dry_run": report.dry_run,
        "remove": sorted(u.telegram_id for u in report.would_remove),
        "blocked": report.blocked is not None,
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:16]


def _label(user: User) -> str:
    name = f" @{user.username}" if user.username else (f" {user.first_name}" if user.first_name else "")
    return html.escape(f"{user.telegram_id}{name}")


def _listing(items: list[str]) -> str:
    shown = ", ".join(items[:REPORT_LIST_LIMIT])
    extra = len(items) - REPORT_LIST_LIMIT
    return shown + (f" … and {extra} more" if extra > 0 else "")


def format_report(report: SweepReport) -> str:
    mode = "dry run, nobody removed" if report.dry_run else "removals on"
    lines = [f"🧹 <b>Removal sweep</b> ({mode})", f"Entitled members in channel: {report.entitled_in_channel}"]
    remove = [f"{_label(u)} ({ENDED_LABELS[ended_kind(u)]})" for u in report.would_remove]
    lines.append(f"Would remove ({len(remove)}): {_listing(remove) if remove else 'nobody'}")
    if report.blocked:
        lines.append(f"⛔ Blocked by the mass-removal cap: {html.escape(report.blocked)}. "
                     "Nobody will be removed; removals are held.")
    if report.skipped:
        skipped = [f"{_label(u)} ({SKIP_LABELS[reason]})" for u, reason in report.skipped]
        lines.append(f"Skipped ({len(skipped)}): {_listing(skipped)}")
    if report.removed:
        lines.append(f"Removed ({len(report.removed)}): {_listing([str(i) for i in report.removed])}")
    if report.failed:
        lines.append(f"Removal failed ({len(report.failed)}): {_listing([str(i) for i in report.failed])}")
    return _fit("\n".join(lines))


def _fit(text: str, limit: int = REPORT_MAX_CHARS) -> str:
    """Keep the report under Telegram's length limit, or it's rejected every time."""
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return text
    suffix = "\n… (cut short)"
    budget = limit - len(suffix)
    cut = text.encode("utf-16-le")[: budget * 2].decode("utf-16-le", errors="ignore")
    amp = cut.rfind("&")
    if amp > cut.rfind(";"):   # don't leave half an HTML entity such as "&am"
        cut = cut[:amp]
    return cut + suffix


async def _skip_reason(svc: Services, user: User, sync_fresh: bool, now: datetime) -> str | None:
    if user.telegram_id in svc.config.admin_ids:
        return "admin"
    status = await svc.tg.member_status(user.telegram_id)
    if status is None:
        return "status_unknown"
    if status in ("administrator", "creator"):
        return "channel_admin"
    if status not in IN_CHANNEL_STATUSES:
        await set_in_channel(svc, user.telegram_id, False, now, f"sweep found status={status}")
        return "not_in_channel"
    if (user.stripe_sub_id or user.stripe_customer_id) and not sync_fresh:
        # A Stripe outage must never get a paying member removed. A Stripe customer
        # without a stored subscription may have just paid (poll and sync failing),
        # so they count too. Every checkout creates the customer first.
        return "stale_sync"
    return None


async def _set_notified(svc: Services, fp: str) -> None:
    async with svc.db.tx() as conn:
        await kv_set(conn, NOTIFIED_KEY, fp)


async def _record(svc: Services, report: SweepReport, now: datetime, notify_owner: bool) -> None:
    """Audit the report when it changed (FINGERPRINT_KEY), and tell the owner when it
    differs from what they were last told (NOTIFIED_KEY). The two are separate so a
    DM that failed on the network is retried next sweep without repeating audit rows."""
    fp = fingerprint(report)
    if await svc.db.kv_get(FINGERPRINT_KEY) != fp:
        async with svc.db.tx() as conn:
            await kv_set(conn, FINGERPRINT_KEY, fp)
            if report.dry_run:
                for user in report.would_remove:
                    await audit(conn, now, user.telegram_id, "would_remove", ENDED_LABELS[ended_kind(user)])
            if report.blocked:
                ids = " ".join(str(u.telegram_id) for u in report.would_remove)
                await audit(conn, now, None, "sweep_blocked", f"{report.blocked}: {ids}")

    notified = await svc.db.kv_get(NOTIFIED_KEY)
    if notified == fp:
        return
    nothing_to_say = not report.would_remove and not report.blocked
    if not notify_owner or (notified is None and nothing_to_say) or svc.owner_id is None:
        await _set_notified(svc, fp)   # nothing the owner needs to hear about this state
        return
    result = await svc.tg.send(svc.owner_id, format_report(report))
    if result is SendResult.FAILED:
        log.warning("Couldn't DM the sweep report to the owner; retrying next sweep")
        return
    if result is SendResult.OK:
        report.owner_notified = True
    else:
        # BLOCKED (owner hasn't pressed /start) or REJECTED: retrying won't help. /dryrun still works.
        log.warning("The owner didn't get the sweep report (%s)", result.value)
    await _set_notified(svc, fp)


async def _recheck(svc: Services, user: User, report: SweepReport, now: datetime) -> User | None:
    """Re-read the user just before acting: a sync, a checkout poll, the I've-paid
    button or /grant may have changed them during the sweep's network calls.
    Returns the fresh row, or None (skip, recorded) if they mustn't be removed."""
    fresh = await get_user(svc.db, user.telegram_id)
    reason = None
    if fresh is None or not fresh.in_paid_channel:
        reason = "not_in_channel"
    elif svc.entitled(fresh, now):
        reason = "now_entitled"
    if reason is None:
        return fresh
    log.info("Sweep: not removing %s after all (%s)", user.telegram_id, reason)
    report.skipped.append((user, reason))
    async with svc.db.tx() as conn:
        await audit_if_changed(conn, now, user.telegram_id, "sweep_skip", reason)
    return None


async def _was_active(svc: Services, user: User) -> bool:
    """Someone who paid one month after the trial and then cancelled has a period
    starting at the trial end, just like a failed first charge. The audit log tells
    them apart: write_subscription logs every status change as "<sub>: x -> y"."""
    if not user.stripe_sub_id:
        return False
    row = await svc.db.fetchone(
        "SELECT 1 FROM audit_log WHERE telegram_id=? AND action='sub_status' AND detail LIKE ? LIMIT 1",
        (user.telegram_id, f"{user.stripe_sub_id}: % -> active"),
    )
    return row is not None


async def _remove(svc: Services, user: User, report: SweepReport, now: datetime) -> None:
    fresh = await _recheck(svc, user, report, now)
    if fresh is None:
        return
    user = fresh
    kind = ended_kind(user, await _was_active(svc, user))
    if not user.banned:
        # Explain first, then kick. If they've blocked the bot we still kick.
        outcome = await deliver_notice(
            svc, user, kind, _cycle_key(user, kind), ENDED_TEXTS[kind],
            ended_keyboard(user, svc.config, kind), now,
        )
        if outcome is NoticeOutcome.RETRY:
            report.skipped.append((user, "dm_failed"))
            async with svc.db.tx() as conn:
                await audit_if_changed(conn, now, user.telegram_id, "sweep_skip", "dm_failed")
            return
        # The DM can take seconds (retries, flood waits), so check once more.
        if await _recheck(svc, user, report, now) is None:
            return
    if await svc.tg.kick(user.telegram_id, permanent=user.banned):
        async with svc.db.tx() as conn:
            await conn.execute("UPDATE users SET in_paid_channel=0 WHERE telegram_id=?", (user.telegram_id,))
            await audit(conn, now, user.telegram_id, "removed", ENDED_LABELS[kind])
        report.removed.append(user.telegram_id)
    else:
        async with svc.db.tx() as conn:
            await audit(conn, now, user.telegram_id, "remove_failed", ENDED_LABELS[kind])
        report.failed.append(user.telegram_id)


async def remove_one(svc: Services, user: User, now: datetime) -> str:
    """Admin /revoke and /ban: take one person out of the channel through the same
    checks as the sweep (not an admin, Telegram confirms they're in, not entitled,
    Stripe data fresh), but only when removals are switched on. Returns a sentence
    for the admin saying what happened."""
    if not svc.config.removals_enabled:
        return ("REMOVALS_ENABLED is false, so I did not remove them from the channel. "
                "/dryrun shows who the sweep would remove.")
    if svc.entitled(user, now):
        return "They still have access another way (trial, subscription or grant), so I did not remove them."
    last_sync = from_iso(await svc.db.kv_get(LAST_FULL_SYNC_KEY))
    # A banned user is not entitled whatever Stripe says, so a stale sync doesn't matter.
    sync_fresh = user.banned or (last_sync is not None and now - last_sync < SYNC_MAX_AGE)
    reason = await _skip_reason(svc, user, sync_fresh, now)
    if reason == "not_in_channel":
        return "They aren't in the paid channel."
    if reason is not None:
        return f"Not removed: {SKIP_LABELS[reason]}."
    if not user.in_paid_channel:
        await set_in_channel(svc, user.telegram_id, True, now, "admin removal found them in the channel")
    report = SweepReport(dry_run=False)
    await _remove(svc, user, report, now)
    if report.removed:
        return "Removed from the paid channel."
    if report.failed:
        return "Telegram refused to remove them. Check the bot still has the Ban users right."
    skipped = report.skipped[-1][1] if report.skipped else "unknown"
    return f"Not removed: {SKIP_LABELS.get(skipped, skipped)}."


async def sweep_removals(svc: Services, now: datetime, *, force_dry_run: bool = False) -> SweepReport:
    """Scheduler job, and /dryrun with force_dry_run=True (which only reports:
    no owner DM, no fingerprint, no audit rows)."""
    dry_run = force_dry_run or not svc.config.removals_enabled
    report = SweepReport(dry_run=dry_run)

    candidates: list[User] = []
    for row in await svc.db.fetchall("SELECT * FROM users WHERE in_paid_channel=1 ORDER BY telegram_id"):
        user = User.from_row(row)
        if svc.entitled(user, now):
            report.entitled_in_channel += 1
        else:
            candidates.append(user)

    last_sync = from_iso(await svc.db.kv_get(LAST_FULL_SYNC_KEY))
    sync_fresh = last_sync is not None and now - last_sync < SYNC_MAX_AGE
    for user in candidates:
        reason = await _skip_reason(svc, user, sync_fresh, now)
        if reason is None:
            report.would_remove.append(user)
        else:
            report.skipped.append((user, reason))
            log.info("Sweep: skipping %s (%s)", user.telegram_id, reason)

    report.blocked = cap_problem(len(report.would_remove), report.entitled_in_channel,
                                 svc.config.max_removals_per_sweep)
    if force_dry_run:
        return report

    async with svc.db.tx() as conn:
        for user, reason in report.skipped:
            await audit_if_changed(conn, now, user.telegram_id, "sweep_skip", reason)

    if dry_run or report.blocked:
        if report.blocked:
            log.warning("Sweep blocked by the mass-removal cap: %s", report.blocked)
        # TODO(phase 4): /confirmsweep to approve a sweep the cap blocked.
        await _record(svc, report, now, notify_owner=True)
        return report

    await _record(svc, report, now, notify_owner=False)
    for user in report.would_remove:
        await _remove(svc, user, report, now)
    return report
