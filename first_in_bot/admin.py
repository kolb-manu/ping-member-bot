"""Owner/admin tools: /stats, /user, /grant, /revoke, /ban, /unban, /pause and
/resume. Handlers check ADMIN_IDS first. Every value that came from a user or
the DB is HTML-escaped before display.

Replies name the person by ID, @username and first name, so the admin can see
who was actually hit. A @username several stored users share is refused (see
users.find_users), never guessed."""
from __future__ import annotations

import html
from dataclasses import fields
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .audit import audit
from .db import Database
from .entitlement import DISPUTED_STATUS
from .maintenance import clear_paused, describe_duration, paused_since, set_paused
from .models import User
from .services import Services
from .sweep import remove_one
from .timeutil import format_local, format_local_date, to_iso
from .users import get_user

MAX_GRANT_DAYS = 3650
# Statuses whose Stripe subscription can still charge the card. "disputed" is ours
# and hides Stripe's own status, which is usually still active.
CHARGEABLE_SUB_STATUSES = frozenset({"trialing", "active", "past_due", DISPUTED_STATUS})


def who(user: User) -> str:
    """Plain text, e.g. "5 (@sam, Sam)". Escape before display."""
    names = [f"@{user.username}" if user.username else None, user.first_name]
    shown = ", ".join(n for n in names if n)
    return f"{user.telegram_id} ({shown})" if shown else str(user.telegram_id)


def ambiguous_text(ref: str, users: list[User], tz: ZoneInfo) -> str:
    """Several stored users have this @username. HTML, escaped."""
    lines = [f"Several users have {html.escape(ref)} stored (one of them has probably changed their "
             "username since the bot last saw them), so I haven't picked one:"]
    for user in users:
        joined = f", joined {format_local_date(user.created_at, tz)}" if user.created_at else ""
        lines.append(f"• {html.escape(who(user))}{joined}")
    lines.append("Please use the numeric ID instead.")
    return "\n".join(lines)


async def _count(db: Database, sql: str, params: tuple = ()) -> int:
    row = await db.fetchone(sql, params)
    return int(row[0]) if row else 0


async def stats_text(db: Database, now: datetime) -> str:
    week_ago = to_iso(now - timedelta(days=7))
    total = await _count(db, "SELECT COUNT(*) FROM users")
    trials = await _count(db, "SELECT COUNT(*) FROM users WHERE sub_status='trialing'")
    paying = await _count(db, "SELECT COUNT(*) FROM users WHERE sub_status='active'")
    past_due = await _count(db, "SELECT COUNT(*) FROM users WHERE sub_status='past_due'")
    disputed = await _count(db, "SELECT COUNT(*) FROM users WHERE sub_status='disputed'")
    trials_ended = await _count(
        db, "SELECT COUNT(*) FROM users WHERE sub_trial_end>=? AND sub_trial_end<?", (week_ago, to_iso(now))
    )
    conversions = await _count(
        db, "SELECT COUNT(*) FROM users WHERE trial_started_at IS NOT NULL AND sub_status='active'"
    )
    in_channel = await _count(db, "SELECT COUNT(*) FROM users WHERE in_paid_channel=1")
    sources = await db.fetchall(
        "SELECT source, COUNT(*) AS n FROM users WHERE created_at>=? GROUP BY source ORDER BY n DESC, source",
        (week_ago,),
    )
    lines = [
        "📊 <b>Stats</b>",
        f"Users: {total}",
        f"Active trials: {trials}",
        f"Paying: {paying}",
        f"Past due: {past_due}",
        f"Disputed: {disputed}",
        f"Trials ended (7 days): {trials_ended}",
        f"Conversions (had a trial, now paying): {conversions}",
        f"In paid channel: {in_channel}",
        "New users by source (7 days): "
        + (", ".join(f"{html.escape(r['source'] or 'unknown')} {r['n']}" for r in sources) or "none"),
    ]
    return "\n".join(lines)


def _show(value: object, tz: ZoneInfo) -> str:
    if isinstance(value, datetime):
        return f"{to_iso(value)} ({format_local(value, tz)})"
    return html.escape(str(value))


async def user_text(db: Database, user: User, tz: ZoneInfo) -> str:
    lines = [f"👤 <b>User {user.telegram_id}</b>"]
    for f in fields(User):
        value = getattr(user, f.name)
        if value is None or value is False or f.name == "telegram_id":
            continue
        lines.append(f"{f.name}: {_show(value, tz)}")
    rows = await db.fetchall(
        "SELECT ts, action, detail FROM audit_log WHERE telegram_id=? ORDER BY id DESC LIMIT 10",
        (user.telegram_id,),
    )
    lines.append("")
    lines.append("<b>Last audit entries</b>" if rows else "No audit entries.")
    for r in rows:
        detail = f" {r['detail']}" if r["detail"] else ""
        lines.append(html.escape(f"{r['ts']} {r['action']}{detail}"))
    return "\n".join(lines)


async def grant(db: Database, user: User, days: int, now: datetime, by: int) -> User:
    """Set manual_grant_until = now + days; 0 clears it."""
    if not 0 <= days <= MAX_GRANT_DAYS:
        raise ValueError(f"days must be between 0 and {MAX_GRANT_DAYS}")
    until = to_iso(now + timedelta(days=days)) if days else None
    async with db.tx() as conn:
        await conn.execute("UPDATE users SET manual_grant_until=? WHERE telegram_id=?", (until, user.telegram_id))
        await audit(conn, now, user.telegram_id, "grant", f"days={days} until={until or 'cleared'} by={by}")
    updated = await get_user(db, user.telegram_id)
    assert updated is not None
    return updated


class AdminTarget(ValueError):
    """The command can't be applied to this user (e.g. an admin); the message is for the admin."""


def _refuse_admin(svc: Services, user: User, verb: str) -> None:
    if user.telegram_id in svc.config.admin_ids:
        raise AdminTarget(f"{who(user)} is an admin, so I won't {verb} them.")


def _still_charged(user: User) -> str:
    """A ban doesn't touch Stripe (the bot's restricted key can't cancel) and a banned
    user can't open /manage, so the owner has to cancel a live subscription themselves."""
    if not user.stripe_sub_id or user.sub_status not in CHARGEABLE_SUB_STATUSES:
        return ""
    if user.cancel_at_period_end and user.sub_status != "past_due":
        return ""   # already set to end without another charge
    return (f" They still have a live Stripe subscription ({user.sub_status}): cancel {user.stripe_sub_id} "
            "in the Stripe dashboard so they aren't charged.")


async def revoke(svc: Services, user: User, now: datetime, by: int) -> str:
    """Clear a manual grant and remove them if nothing else entitles them."""
    _refuse_admin(svc, user, "revoke")
    had_grant = user.manual_grant_until is not None
    async with svc.db.tx() as conn:
        await conn.execute("UPDATE users SET manual_grant_until=NULL WHERE telegram_id=?", (user.telegram_id,))
        await audit(conn, now, user.telegram_id, "revoke", f"had_grant={int(had_grant)} by={by}")
    fresh = await get_user(svc.db, user.telegram_id)
    assert fresh is not None
    lead = f"Grant cleared for {who(user)}." if had_grant else f"{who(user)} had no grant."
    return f"{lead} {await remove_one(svc, fresh, now)}"


async def ban(svc: Services, user: User, now: datetime, by: int) -> str:
    """Flag banned (never entitled, join requests declined) and remove them for good."""
    _refuse_admin(svc, user, "ban")
    if user.banned:
        return f"{who(user)} is already banned.{_still_charged(user)}"
    async with svc.db.tx() as conn:
        await conn.execute("UPDATE users SET banned=1 WHERE telegram_id=?", (user.telegram_id,))
        await audit(conn, now, user.telegram_id, "ban", f"by={by}")
    fresh = await get_user(svc.db, user.telegram_id)
    assert fresh is not None
    return f"Banned {who(user)}. {await remove_one(svc, fresh, now)}{_still_charged(fresh)}"


async def unban(svc: Services, user: User, now: datetime, by: int) -> str:
    """Clear the flag and lift any Telegram-level ban so they can request to join again."""
    if not user.banned:
        return f"{who(user)} isn't banned."
    async with svc.db.tx() as conn:
        await conn.execute("UPDATE users SET banned=0 WHERE telegram_id=?", (user.telegram_id,))
        await audit(conn, now, user.telegram_id, "unban", f"by={by}")
    lifted = await svc.tg.unban(user.telegram_id)
    note = "" if lifted else " I couldn't lift the Telegram ban, so they may still be blocked from rejoining."
    return f"Unbanned {who(user)}. They still need a trial, grant or subscription to get in.{note}"


PAUSE_SCOPE = (
    "Paused: the removal sweep (nobody is removed or sent a goodbye message), removals from /revoke "
    "and /ban (they still set their flags), and the trial reminders.\n"
    "Still running: join requests, checkout checks, the \"You're in\" message and the Stripe sync, "
    "so paying people still get in. /dryrun still works."
)


async def pause(svc: Services, now: datetime, by: int) -> str:
    """/pause: hold removals and reminders until /resume (see maintenance.py)."""
    tz = svc.config.timezone
    if not await set_paused(svc.db, now, by):
        since = await paused_since(svc.db)
        when = f" since {format_local(since, tz)}" if since else ""
        return f"Already paused{when}. /resume switches removals and reminders back on."
    return f"⏸ Paused at {format_local(now, tz)}.\n{PAUSE_SCOPE}\n/resume switches it all back on."


async def resume(svc: Services, now: datetime, by: int) -> str:
    since = await paused_since(svc.db)
    if not await clear_paused(svc.db, now, by):
        return "Not paused, so there was nothing to resume."
    took = ""
    if since is not None:
        took = f" after {describe_duration(now - since)} (paused since {format_local(since, svc.config.timezone)})"
    return (
        f"▶️ Resumed{took}.\n"
        "The removal sweep and reminders run again on their next tick. Reminders that are still due go out "
        "then; ones whose moment has passed are skipped. If a lot of people lost access while paused, the "
        "mass-removal cap may hold the sweep back for /confirmsweep."
    )
