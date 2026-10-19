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
