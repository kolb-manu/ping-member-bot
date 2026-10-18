"""User table access."""
from __future__ import annotations

import re
from datetime import datetime

from .db import Database
from .models import User
from .timeutil import to_iso

_SOURCE_RE = re.compile(r"[a-z0-9_]{1,32}")


def sanitise_source(payload: str | None) -> str:
    """Deep-link tag from t.me/<bot>?start=<source>; anything odd becomes 'unknown'."""
    candidate = (payload or "").strip().lower()
    return candidate if _SOURCE_RE.fullmatch(candidate) else "unknown"


async def get_user(db: Database, telegram_id: int) -> User | None:
    row = await db.fetchone("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
    return User.from_row(row) if row else None


async def register_user(
    db: Database,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    payload: str | None,
    now: datetime,
) -> tuple[User, bool]:
    """Create the user on first contact (storing only the first source), or refresh
    their name fields. Returns (user, created)."""
    source = sanitise_source(payload)
    async with db.tx() as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO users(telegram_id, username, first_name, source, created_at) "
            "VALUES(?,?,?,?,?)",
            (telegram_id, username, first_name, source, to_iso(now)),
        )
        created = cur.rowcount == 1
        if created:
            await conn.execute(
                "INSERT INTO audit_log(ts, telegram_id, action, detail) VALUES(?,?,?,?)",
                (to_iso(now), telegram_id, "user_created", f"source={source}"),
            )
        else:
            await conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE telegram_id=?",
                (username, first_name, telegram_id),
            )
            # A placeholder 'unknown' may be upgraded once to a real tag; a real tag is final.
            if source != "unknown":
                cur = await conn.execute(
                    "UPDATE users SET source=? WHERE telegram_id=? AND (source IS NULL OR source='unknown')",
                    (source, telegram_id),
                )
                if cur.rowcount == 1:
                    await conn.execute(
                        "INSERT INTO audit_log(ts, telegram_id, action, detail) VALUES(?,?,?,?)",
                        (to_iso(now), telegram_id, "source_set", f"source={source}"),
                    )
    user = await get_user(db, telegram_id)
    assert user is not None
    return user, created
