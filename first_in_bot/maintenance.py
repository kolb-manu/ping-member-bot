"""The maintenance switch behind /pause and /resume (spec section 10).

While paused the bot takes nothing away and nags nobody: the removal sweep
removes nobody and sends no goodbye messages, /revoke and /ban set their flags
but don't kick, and send_notices sends no reminders. Everything that lets people
in carries on (join-request approvals, checkout polling, the "You're in" message,
the Stripe sync), because pausing must never stop a paying person getting in.

The switch lives in kv, so a restart doesn't quietly undo it. The jobs check it
themselves at the start of each run (and the sweep again right before every DM
and kick), rather than the scheduler skipping them, so every caller is covered.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .audit import audit
from .db import Database
from .timeutil import from_iso, to_iso

# Holds the time the pause was switched on; absent means not paused.
PAUSED_KEY = "maintenance_paused_since"


async def paused_since(db: Database) -> datetime | None:
    """When the pause was switched on, or None if removals and reminders are running."""
    return from_iso(await db.kv_get(PAUSED_KEY))


async def set_paused(db: Database, now: datetime, by: int) -> bool:
    """Switch the pause on. False (and nothing written) if it already was on, so
    a second /pause keeps the original start time."""
    async with db.tx() as conn:
        cur = await conn.execute("INSERT OR IGNORE INTO kv(key, value) VALUES(?, ?)", (PAUSED_KEY, to_iso(now)))
        if cur.rowcount == 0:
            return False
        await audit(conn, now, None, "pause", f"by={by}")
    return True


async def clear_paused(db: Database, now: datetime, by: int) -> bool:
    """Switch the pause off. False if it wasn't on."""
    async with db.tx() as conn:
        cur = await conn.execute("SELECT value FROM kv WHERE key=?", (PAUSED_KEY,))
        row = await cur.fetchone()
        if row is None:
            return False
        await conn.execute("DELETE FROM kv WHERE key=?", (PAUSED_KEY,))
        await audit(conn, now, None, "resume", f"by={by} paused_since={row['value']}")
    return True


def describe_duration(delta: timedelta) -> str:
    """For the admin, e.g. '3 days 4 hours', '2 hours 5 minutes', 'less than a minute'.
    Two units at most: the pause length doesn't need to be exact."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "less than a minute"
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = [f"{n} {unit}{'' if n == 1 else 's'}" for n, unit in ((days, "day"), (hours, "hour"), (mins, "minute"))
             if n]
    return " ".join(parts[:2])
