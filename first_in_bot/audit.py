"""audit_log writes. Always called inside db.tx() with its connection."""
from __future__ import annotations

from datetime import datetime

import aiosqlite

from .timeutil import to_iso


async def audit(
    conn: aiosqlite.Connection,
    now: datetime,
    telegram_id: int | None,
    action: str,
    detail: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO audit_log(ts, telegram_id, action, detail) VALUES(?,?,?,?)",
        (to_iso(now), telegram_id, action, detail),
    )


async def audit_if_changed(
    conn: aiosqlite.Connection,
    now: datetime,
    telegram_id: int | None,
    action: str,
    detail: str,
) -> bool:
    """For things that repeat every job tick (a skipped removal, a duplicate
    subscription): only write a row when the detail differs from the last one."""
    cur = await conn.execute(
        "SELECT detail FROM audit_log WHERE telegram_id IS ? AND action=? ORDER BY id DESC LIMIT 1",
        (telegram_id, action),
    )
    row = await cur.fetchone()
    if row is not None and row["detail"] == detail:
        return False
    await audit(conn, now, telegram_id, action, detail)
    return True
