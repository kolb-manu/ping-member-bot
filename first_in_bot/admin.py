"""Owner/admin tools: /stats, /user, /grant. Handlers check ADMIN_IDS first.
Every value that came from a user or the DB is HTML-escaped before display."""
from __future__ import annotations

import html
from dataclasses import fields
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .audit import audit
from .db import Database
from .models import User
from .services import Services
from .sweep import remove_one
from .timeutil import format_local, to_iso
from .users import get_user

MAX_GRANT_DAYS = 3650


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
        raise AdminTarget(f"{user.telegram_id} is an admin, so I won't {verb} them.")


async def revoke(svc: Services, user: User, now: datetime, by: int) -> str:
    """Clear a manual grant and remove them if nothing else entitles them."""
    _refuse_admin(svc, user, "revoke")
    had_grant = user.manual_grant_until is not None
    async with svc.db.tx() as conn:
        await conn.execute("UPDATE users SET manual_grant_until=NULL WHERE telegram_id=?", (user.telegram_id,))
        await audit(conn, now, user.telegram_id, "revoke", f"had_grant={int(had_grant)} by={by}")
    fresh = await get_user(svc.db, user.telegram_id)
    assert fresh is not None
    lead = "Grant cleared." if had_grant else "They had no grant."
    return f"{lead} {await remove_one(svc, fresh, now)}"


async def ban(svc: Services, user: User, now: datetime, by: int) -> str:
    """Flag banned (never entitled, join requests declined) and remove them for good."""
    _refuse_admin(svc, user, "ban")
    if user.banned:
        return f"{user.telegram_id} is already banned."
    async with svc.db.tx() as conn:
        await conn.execute("UPDATE users SET banned=1 WHERE telegram_id=?", (user.telegram_id,))
        await audit(conn, now, user.telegram_id, "ban", f"by={by}")
    fresh = await get_user(svc.db, user.telegram_id)
    assert fresh is not None
    return f"Banned {user.telegram_id}. {await remove_one(svc, fresh, now)}"


async def unban(svc: Services, user: User, now: datetime, by: int) -> str:
    """Clear the flag and lift any Telegram-level ban so they can request to join again."""
    if not user.banned:
        return f"{user.telegram_id} isn't banned."
    async with svc.db.tx() as conn:
        await conn.execute("UPDATE users SET banned=0 WHERE telegram_id=?", (user.telegram_id,))
        await audit(conn, now, user.telegram_id, "unban", f"by={by}")
    lifted = await svc.tg.unban(user.telegram_id)
    note = "" if lifted else " I couldn't lift the Telegram ban, so they may still be blocked from rejoining."
    return f"Unbanned {user.telegram_id}. They still need a trial, grant or subscription to get in.{note}"
