"""sweep_removals: take people who are no longer entitled out of the paid channel,
behind the safety rails of spec section 8.

Someone is removed only if is_entitled says no, Telegram confirms they're in
the channel, they aren't an admin, and (if they may have a Stripe subscription,
i.e. have a Stripe customer or subscription ID) the last full Stripe sync is
under 15 minutes old. A mass-removal cap blocks the whole sweep. Right before the
DM and again right before the kick the user is re-read, and the sync age checked
again against the clock, so someone who became entitled mid-sweep is left alone
and a sweep slowed down by Telegram never acts on stale Stripe data. With
REMOVALS_ENABLED=false (the default, and how it stays for now) it's a dry run:
nobody is removed and only the owner gets a "Would remove" report, and only when
that report changes (a report that couldn't be delivered is retried next sweep).
A kick Telegram refuses makes the run count as failed, so the scheduler alerts
the owner.

While paused (/pause, see maintenance.py) the scheduled sweep doesn't run at all:
no Telegram lookups, no audit rows, no report fingerprints, no owner DM. Leaving
the fingerprints as they were means that after /resume the owner only hears
about what changed, and the pause and resume are themselves in the audit log. A
pause that lands mid-sweep is caught by the re-check before each DM and kick.

When the mass-removal cap blocks a sweep, the owner's report invites
/confirmsweep, and once that report has been delivered the exact set of people
it listed is stored (CONFIRM_KEY), with whether it was a dry run. confirm_sweep
re-runs the whole evaluation with only the cap lifted, and goes ahead only within
an hour, in the same mode, and only if everyone it would now remove was in that
set; anything else is refused with the new report, which becomes the next
approval once the reply has reached the owner. An approval is used once, and a
sweep that is no longer blocked drops it. If nothing is pending while the cap is
holding removals back, /confirmsweep shows and offers that report, and an approved
run that didn't get through everyone has the next sweep report again, so the
owner can never be left with removals held and no way to approve them.
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
from .db import Database, kv_set
from .entitlement import DISPUTED_STATUS
from .keyboards import ended_keyboard
from .maintenance import paused_since
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
# The cap-blocked set the owner was shown, and when, for /confirmsweep.
CONFIRM_KEY = "sweep_confirm_pending"
CONFIRM_TTL = timedelta(hours=1)
CONFIRM_PROMPT = "Reply /confirmsweep within an hour to go ahead with exactly these removals."
PAUSED_LINE = "⏸ Paused: nobody is removed and no reminders go out until /resume."
NOT_BLOCKED_NOTE = "The cap isn't holding this back any more, so the regular sweep handles it on its next run."
NOTHING_PENDING = ("Nothing is waiting for /confirmsweep. It's only needed after a sweep report "
                   "says the mass-removal cap held removals back.")
NOTHING_PENDING_BUT_BLOCKED = ("Nothing is waiting for /confirmsweep, so nobody was removed. The mass-removal "
                               "cap is holding removals back right now, though, so here's the sweep as it stands.")

SKIP_LABELS = {
    "admin": "admin",
    "channel_admin": "channel admin",
    "not_in_channel": "not in channel, flag cleared",
    "status_unknown": "couldn't check membership",
    "stale_sync": "Stripe sync is stale",
    "dm_failed": "DM failed, retrying next sweep",
    "now_entitled": "became entitled during the sweep",
    "paused": "paused, held until /resume",
}
ENDED_LABELS = {"trial_ended": "trial ended", "sub_ended": "subscription ended", "access_ended": "free access ended",
                "disputed": "payment disputed"}
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
    paused: bool = False       # /pause is on (the scheduled sweep then does nothing)
    cap_lifted: bool = False   # a run approved with /confirmsweep

    @property
    def ok(self) -> bool:
        """False when Telegram refused a kick (usually the bot lost its Ban users right):
        the scheduler then counts the run as failed and alerts the owner."""
        return not self.failed


@dataclass(frozen=True)
class PendingConfirm:
    """A cap-blocked set the owner was shown and may approve with /confirmsweep."""
    ids: frozenset[int]
    at: datetime   # when the owner was shown it; the approval lasts CONFIRM_TTL
    raw: str       # the stored value, so using it is a compare-and-delete
    dry_run: bool | None = None   # the mode it was offered in; None if it wasn't stored


@dataclass(frozen=True)
class ConfirmResult:
    """The /confirmsweep reply. offer: a new cap-blocked report shown in the reply, to
    be stored as the next approval only once the reply has reached the owner."""
    text: str
    offer: SweepReport | None = None


def ended_kind(user: User, was_active: bool = False) -> str:
    """Which goodbye message fits: access on hold because of a payment dispute, a trial
    that never turned into a paid period, a subscription that ended, or a manual grant
    that ran out. was_active: this subscription took a payment at some point."""
    if user.sub_status == DISPUTED_STATUS:
        # Nothing has ended: the subscription may still be running at Stripe.
        return "disputed"
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


def _goodbye_text(svc: Services, kind: str) -> str:
    if kind == "disputed":
        support = svc.config.support_contact
        return texts.disputed_hold(html.escape(support) if support else None)
    return ENDED_TEXTS[kind]


def _cycle_key(user: User, kind: str) -> str:
    if kind == "trial_ended" and user.sub_trial_end:
        return to_iso(user.sub_trial_end)
    if kind in ("sub_ended", "disputed"):
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


def _removals(report: SweepReport) -> list[str]:
    return [f"{_label(u)} ({ENDED_LABELS[ended_kind(u)]})" for u in report.would_remove]


def _skipped_line(report: SweepReport) -> str:
    skipped = [f"{_label(u)} ({SKIP_LABELS[reason]})" for u, reason in report.skipped]
    return f"Skipped ({len(skipped)}): {_listing(skipped)}"


def format_report(report: SweepReport, *, offer_confirm: bool = False, lead: str | None = None,
                  note: str | None = None) -> str:
    """The owner's report, always short enough for one Telegram message. lead: a
    sentence put first (the /confirmsweep replies). offer_confirm: a cap-blocked report
    the owner may approve, so it ends with the /confirmsweep line; note: any other
    closing line. Both survive even if the list is cut."""
    mode = "dry run, nobody removed" if report.dry_run else "removals on"
    if report.cap_lifted:
        mode += ", cap lifted by /confirmsweep"
    lines = [lead, ""] if lead else []
    lines.append(f"🧹 <b>Removal sweep</b> ({mode})")
    if report.paused:
        lines.append(PAUSED_LINE)
    lines.append(f"Entitled members in channel: {report.entitled_in_channel}")
    remove = _removals(report)
    lines.append(f"Would remove ({len(remove)}): {_listing(remove) if remove else 'nobody'}")
    if report.blocked:
        lines.append(f"⛔ Blocked by the mass-removal cap: {html.escape(report.blocked)}. "
                     "Nobody will be removed; removals are held.")
    if report.skipped:
        lines.append(_skipped_line(report))
    if report.removed:
        lines.append(f"Removed ({len(report.removed)}): {_listing([str(i) for i in report.removed])}")
    if report.failed:
        lines.append(f"Removal failed ({len(report.failed)}): {_listing([str(i) for i in report.failed])}")
    return _fit_with_tail("\n".join(lines), CONFIRM_PROMPT if offer_confirm else note)


def format_confirm_dry_run(report: SweepReport) -> str:
    """/confirmsweep with REMOVALS_ENABLED=false: the whole flow, minus the removals."""
    remove = _removals(report)
    lines = [f"🧹 Dry run: with the cap lifted I would remove {len(remove)}: {_listing(remove)}.",
             "Nobody was removed because REMOVALS_ENABLED=false."]
    if report.skipped:
        lines.append(_skipped_line(report))
    return _fit("\n".join(lines))


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _fit(text: str, limit: int = REPORT_MAX_CHARS) -> str:
    """Keep the report under Telegram's length limit, or it's rejected every time."""
    if _utf16_len(text) <= limit:
        return text
    suffix = "\n… (cut short)"
    budget = limit - len(suffix)
    cut = text.encode("utf-16-le")[: budget * 2].decode("utf-16-le", errors="ignore")
    amp = cut.rfind("&")
    if amp > cut.rfind(";"):   # don't leave half an HTML entity such as "&am"
        cut = cut[:amp]
    return cut + suffix


def _fit_with_tail(text: str, tail: str | None) -> str:
    """_fit, keeping tail whole on its own last line."""
    if not tail:
        return _fit(text)
    return _fit(text, REPORT_MAX_CHARS - _utf16_len(tail) - 1) + "\n" + tail


async def _sync_fresh(svc: Services, at: datetime) -> bool:
    last_sync = from_iso(await svc.db.kv_get(LAST_FULL_SYNC_KEY))
    return last_sync is not None and at - last_sync < SYNC_MAX_AGE


def _may_have_stripe_sub(user: User) -> bool:
    # A Stripe customer without a stored subscription may have just paid (poll and
    # sync failing), so they count too. Every checkout creates the customer first.
    return bool(user.stripe_sub_id or user.stripe_customer_id)


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
    if _may_have_stripe_sub(user) and not sync_fresh:
        # A Stripe outage must never get a paying member removed.
        return "stale_sync"
    return None


async def _set_notified(svc: Services, fp: str) -> None:
    async with svc.db.tx() as conn:
        await kv_set(conn, NOTIFIED_KEY, fp)


async def _forget_notified(svc: Services) -> None:
    """The next scheduled sweep tells the owner again, even if its report is unchanged."""
    async with svc.db.tx() as conn:
        await conn.execute("DELETE FROM kv WHERE key=?", (NOTIFIED_KEY,))


async def _audit_report(svc: Services, report: SweepReport, now: datetime) -> str:
    """Audit the report's state once, when it changes (FINGERPRINT_KEY). Returns its fingerprint."""
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
    return fp


async def _record(svc: Services, report: SweepReport, now: datetime, notify_owner: bool) -> None:
    """Audit the report when it changed (FINGERPRINT_KEY), and tell the owner when it
    differs from what they were last told (NOTIFIED_KEY). The two are separate so a
    DM that failed on the network is retried next sweep without repeating audit rows."""
    fp = await _audit_report(svc, report, now)
    notified = await svc.db.kv_get(NOTIFIED_KEY)
    if notified == fp:
        return
    nothing_to_say = not report.would_remove and not report.blocked
    if not notify_owner or (notified is None and nothing_to_say) or svc.owner_id is None:
        await _set_notified(svc, fp)   # nothing the owner needs to hear about this state
        return
    result = await svc.tg.send(svc.owner_id, format_report(report, offer_confirm=report.blocked is not None))
    if result is SendResult.FAILED:
        log.warning("Couldn't DM the sweep report to the owner; retrying next sweep")
        return
    if result is SendResult.OK:
        report.owner_notified = True
        if report.blocked:
            # Only now: an approval must always match a report the owner actually got.
            await offer_confirm(svc, report, now)
    else:
        # BLOCKED (owner hasn't pressed /start) or REJECTED: retrying won't help. /dryrun
        # still works, and /confirmsweep offers a blocked sweep when nothing is pending.
        log.warning("The owner didn't get the sweep report (%s)", result.value)
    await _set_notified(svc, fp)


async def _recheck(svc: Services, user: User, report: SweepReport, now: datetime) -> User | None:
    """Re-read the user just before acting: a sync, a checkout poll, the I've-paid
    button or /grant may have changed them during the sweep's network calls, and
    /pause may have been switched on. The run can take minutes (DM retries, flood
    waits) while the Stripe sync starts failing, so the sync age is checked again
    against the clock, never laxer than at the start of the run. Returns the fresh
    row, or None (skip, recorded) if they mustn't be removed."""
    fresh = await get_user(svc.db, user.telegram_id)
    reason = None
    if await paused_since(svc.db) is not None:
        reason = "paused"
    elif fresh is None or not fresh.in_paid_channel:
        reason = "not_in_channel"
    elif svc.entitled(fresh, now):
        reason = "now_entitled"
    elif _may_have_stripe_sub(fresh) and not fresh.banned and not await _sync_fresh(svc, max(now, svc.now())):
        # A banned user isn't entitled whatever Stripe says (see remove_one).
        reason = "stale_sync"
    if reason is None:
        return fresh
    log.info("Sweep: not removing %s after all (%s)", user.telegram_id, reason)
    report.skipped.append((user, reason))
    async with svc.db.tx() as conn:
        await audit_if_changed(conn, now, user.telegram_id, "sweep_skip", reason)
    return None


async def _was_active(svc: Services, user: User) -> bool:
    """Did this subscription ever take a payment? Someone who paid one month after the
    trial and then cancelled has a period starting at the trial end, just like a failed
    first charge. The audit log tells them apart: write_subscription logs every status
    change as "<sub>: x -> y".

    Stripe says "active" as soon as the trial ends, while the first invoice is still a
    draft, and only tries the card about an hour later. So an "-> active" followed
    straight away by "active -> past_due" is that hour, not a payment. A later
    "past_due -> active" does count: the overdue invoice got paid. A failed renewal
    also goes "active -> past_due", but it opens a period after the trial end, so
    ended_kind doesn't ask."""
    if not user.stripe_sub_id:
        return False
    prefix = f"{user.stripe_sub_id}: "
    rows = await svc.db.fetchall(
        "SELECT detail FROM audit_log WHERE telegram_id=? AND action='sub_status' ORDER BY id",
        (user.telegram_id,),
    )
    changes = [r["detail"][len(prefix):] for r in rows if (r["detail"] or "").startswith(prefix)]
    for i, change in enumerate(changes):
        if change.endswith(" -> active"):
            following = changes[i + 1] if i + 1 < len(changes) else None
            if following != "active -> past_due":
                return True
    return False


async def _remove(svc: Services, user: User, report: SweepReport, now: datetime) -> None:
    fresh = await _recheck(svc, user, report, now)
    if fresh is None:
        return
    user = fresh
    kind = ended_kind(user, await _was_active(svc, user))
    if not user.banned:
        # Explain first, then kick. If they've blocked the bot we still kick.
        outcome = await deliver_notice(
            svc, user, kind, _cycle_key(user, kind), _goodbye_text(svc, kind),
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
        log.error("Telegram refused to remove %s from the paid channel; does the bot still have "
                  "the Ban users right?", user.telegram_id)
        async with svc.db.tx() as conn:
            # Retried every sweep, so only audited when it changes.
            await audit_if_changed(conn, now, user.telegram_id, "remove_failed", ENDED_LABELS[kind])
        report.failed.append(user.telegram_id)


async def remove_one(svc: Services, user: User, now: datetime) -> str:
    """Admin /revoke and /ban: take one person out of the channel through the same
    checks as the sweep (not an admin, Telegram confirms they're in, not entitled,
    Stripe data fresh), but only when removals are switched on and not paused.
    Returns a sentence for the admin saying what happened."""
    if not svc.config.removals_enabled:
        return ("REMOVALS_ENABLED is false, so I did not remove them from the channel. "
                "/dryrun shows who the sweep would remove.")
    if svc.entitled(user, now):
        return "They still have access another way (trial, subscription or grant), so I did not remove them."
    if await paused_since(svc.db) is not None:
        # Their flag is already set, so the sweep takes them out after /resume.
        return "Paused, so I didn't remove them; the sweep will once you /resume."
    # A banned user is not entitled whatever Stripe says, so a stale sync doesn't matter.
    sync_fresh = user.banned or await _sync_fresh(svc, now)
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


async def _evaluate(svc: Services, now: datetime, dry_run: bool) -> SweepReport:
    """Who would be removed right now, why everyone else is skipped, and the cap
    verdict. Asks Telegram about each candidate but changes nothing, apart from
    clearing the in-channel flag of someone who turns out to have left."""
    report = SweepReport(dry_run=dry_run)
    candidates: list[User] = []
    for row in await svc.db.fetchall("SELECT * FROM users WHERE in_paid_channel=1 ORDER BY telegram_id"):
        user = User.from_row(row)
        if svc.entitled(user, now):
            report.entitled_in_channel += 1
        else:
            candidates.append(user)

    sync_fresh = await _sync_fresh(svc, now)
    for user in candidates:
        reason = await _skip_reason(svc, user, sync_fresh, now)
        if reason is None:
            report.would_remove.append(user)
        else:
            report.skipped.append((user, reason))
            log.info("Sweep: skipping %s (%s)", user.telegram_id, reason)

    report.blocked = cap_problem(len(report.would_remove), report.entitled_in_channel,
                                 svc.config.max_removals_per_sweep)
    return report


async def _audit_skips(svc: Services, report: SweepReport, now: datetime) -> None:
    async with svc.db.tx() as conn:
        for user, reason in report.skipped:
            await audit_if_changed(conn, now, user.telegram_id, "sweep_skip", reason)


async def sweep_removals(svc: Services, now: datetime, *, force_dry_run: bool = False) -> SweepReport:
    """Scheduler job, and /dryrun with force_dry_run=True (which only reports:
    no owner DM, no fingerprint, no audit rows). While paused the job does nothing
    at all (see the module docstring); /dryrun still reports, with a paused line."""
    dry_run = force_dry_run or not svc.config.removals_enabled
    since = await paused_since(svc.db)
    if since is not None and not force_dry_run:
        log.info("Sweep: paused since %s, so not running", to_iso(since))
        return SweepReport(dry_run=dry_run, paused=True)

    report = await _evaluate(svc, now, dry_run)
    report.paused = since is not None
    if force_dry_run:
        return report

    await _audit_skips(svc, report, now)
    if not report.blocked:
        await _drop_confirm(svc, now, "sweep no longer blocked")

    if dry_run or report.blocked:
        if report.blocked:
            log.warning("Sweep blocked by the mass-removal cap: %s", report.blocked)
        await _record(svc, report, now, notify_owner=True)
        return report

    await _record(svc, report, now, notify_owner=False)
    for user in report.would_remove:
        await _remove(svc, user, report, now)
    return report


# --- /confirmsweep ------------------------------------------------------------------

def _ids_text(ids) -> str:
    return " ".join(str(i) for i in sorted(ids))


async def pending_confirm(db: Database) -> PendingConfirm | None:
    raw = await db.kv_get(CONFIRM_KEY)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        at = from_iso(data["at"])
        if at is None:
            raise ValueError("no time")
        dry_run = data.get("dry_run")
        return PendingConfirm(frozenset(int(i) for i in data["ids"]), at, raw,
                              dry_run=dry_run if isinstance(dry_run, bool) else None)
    except (ValueError, KeyError, TypeError, AttributeError):
        # Nothing readable to approve, so /confirmsweep refuses as if nothing were pending.
        log.error("The stored /confirmsweep approval can't be read; ignoring it")
        return None


async def offer_confirm(svc: Services, report: SweepReport, now: datetime) -> None:
    """The owner has just been shown this cap-blocked report: from now they have
    CONFIRM_TTL to approve exactly these people, in this mode. Replaces any earlier offer."""
    ids = sorted(u.telegram_id for u in report.would_remove)
    async with svc.db.tx() as conn:
        await kv_set(conn, CONFIRM_KEY, json.dumps({"ids": ids, "at": to_iso(now), "dry_run": report.dry_run}))
        await audit(conn, now, None, "sweep_confirm", f"offered: {_ids_text(ids)}")


async def offer_in_reply(svc: Services, report: SweepReport, now: datetime) -> None:
    """A /confirmsweep reply showing this cap-blocked report has reached the owner. It
    becomes the approval, and the report counts as delivered, so the next scheduled
    sweep neither DMs it again nor restarts the hour."""
    fp = await _audit_report(svc, report, now)
    await offer_confirm(svc, report, now)
    await _set_notified(svc, fp)


async def _drop_confirm(svc: Services, now: datetime, why: str) -> None:
    if await svc.db.kv_get(CONFIRM_KEY) is None:
        return   # the usual case, and no write, so nothing churns every tick
    async with svc.db.tx() as conn:
        cur = await conn.execute("DELETE FROM kv WHERE key=?", (CONFIRM_KEY,))
        if cur.rowcount:
            await audit(conn, now, None, "sweep_confirm", f"dropped: {why}")


async def _use_confirm(svc: Services, pending: PendingConfirm, now: datetime, detail: str) -> bool:
    """Use the approval exactly once. False if a double tap or a newer report got
    there first, in which case this /confirmsweep must do nothing."""
    async with svc.db.tx() as conn:
        cur = await conn.execute("DELETE FROM kv WHERE key=? AND value=?", (CONFIRM_KEY, pending.raw))
        if cur.rowcount == 0:
            return False
        await audit(conn, now, None, "sweep_confirm", detail)
    return True


async def _audit_confirm(svc: Services, now: datetime, detail: str) -> None:
    async with svc.db.tx() as conn:
        await audit(conn, now, None, "sweep_confirm", detail)


def _mode_name(dry_run: bool | None) -> str:
    if dry_run is None:
        return "an unknown mode"
    return "a dry run (REMOVALS_ENABLED=false)" if dry_run else "real removals (REMOVALS_ENABLED=true)"


async def _refuse_with_report(svc: Services, report: SweepReport, now: datetime, why: str) -> ConfirmResult:
    """Show the sweep as it stands so the owner can look again. If the cap still
    blocks it, that report becomes the next approval once the reply reaches them."""
    if report.blocked:
        return ConfirmResult(format_report(report, offer_confirm=True, lead=why), offer=report)
    await _drop_confirm(svc, now, "sweep no longer blocked")
    return ConfirmResult(format_report(report, lead=why, note=NOT_BLOCKED_NOTE))


async def _nothing_pending(svc: Services, now: datetime, dry_run: bool) -> ConfirmResult:
    """No approval is open: the owner never got the blocked report (their DMs came
    back blocked), or an approved run didn't get through everyone. If the cap is
    holding removals back right now, show that report and offer it."""
    report = await _evaluate(svc, now, dry_run)
    await _audit_skips(svc, report, now)
    if not report.blocked:
        return ConfirmResult(NOTHING_PENDING)
    return ConfirmResult(format_report(report, offer_confirm=True, lead=NOTHING_PENDING_BUT_BLOCKED), offer=report)


async def confirm_sweep(svc: Services, now: datetime, by: int) -> ConfirmResult:
    """Admin /confirmsweep: run a sweep the mass-removal cap blocked, with the cap
    lifted and every other check intact (not entitled, Telegram says they're in,
    fresh Stripe sync, not an admin, re-checked before the DM and the kick).
    Refuses while paused, when nothing is pending, when the approval is an hour
    old or was offered in the other mode (dry run or not), and when anyone outside
    the approved set would now be removed."""
    if await paused_since(svc.db) is not None:
        await _audit_confirm(svc, now, f"refused (paused) by={by}")
        return ConfirmResult("Paused: /resume first. Nobody was removed.")
    dry_run = not svc.config.removals_enabled
    pending = await pending_confirm(svc.db)
    if pending is None:
        await _audit_confirm(svc, now, f"refused (nothing pending) by={by}")
        return await _nothing_pending(svc, now, dry_run)

    report = await _evaluate(svc, now, dry_run)
    await _audit_skips(svc, report, now)
    ids = frozenset(u.telegram_id for u in report.would_remove)

    # A time in the future means the clock jumped; that's no approval to rely on either.
    if not timedelta(0) <= now - pending.at < CONFIRM_TTL:
        await _audit_confirm(svc, now, f"refused (expired, offered {to_iso(pending.at)}) by={by}")
        return await _refuse_with_report(
            svc, report, now, "That approval has expired (they last an hour), so nobody was removed. "
                              "Here's the sweep as it stands now.")
    if pending.dry_run is not dry_run:
        # Approving a dry-run report must never turn into real kicks after a restart.
        await _audit_confirm(svc, now, f"refused (offered with dry_run={pending.dry_run}, now {dry_run}) by={by}")
        return await _refuse_with_report(
            svc, report, now, f"That approval was for {_mode_name(pending.dry_run)}, but the bot is now set to "
                              f"{_mode_name(dry_run)}, so nobody was removed. Here's the sweep as it stands now.")
    new = ids - pending.ids
    if new:
        await _audit_confirm(svc, now, f"refused (not approved: {_ids_text(new)}) by={by}")
        return await _refuse_with_report(
            svc, report, now, f"Not going ahead: {len(new)} of the people it would now remove weren't in "
                              "the report you were sent, so nobody was removed. Please look again.")
    if not ids:
        await _drop_confirm(svc, now, f"nobody left to remove at /confirmsweep by={by}")
        return ConfirmResult(format_report(report, lead="Nobody needs removing now (they renewed, left or are "
                                                         "skipped below), so nobody was removed."))

    mode = " (dry run)" if dry_run else ""
    if not await _use_confirm(svc, pending, now, f"approved{mode} by={by}: {_ids_text(ids)}"):
        return ConfirmResult("That approval was just used or replaced, so I did nothing. "
                             "Check the latest sweep report.")
    report.blocked = None
    report.cap_lifted = True
    if dry_run:
        return ConfirmResult(format_confirm_dry_run(report))
    log.warning("Sweep cap lifted by /confirmsweep from %s for %d people", by, len(ids))
    for user in report.would_remove:
        await _remove(svc, user, report, now)
    if len(report.removed) < len(ids):
        # Some are still in (a DM failed, /pause landed, a kick failed) and the approval
        # is used up. The blocked set may look unchanged, so the next sweep must report it
        # again, with a new approval, rather than stay quiet.
        await _forget_notified(svc)
    return ConfirmResult(format_report(report))
