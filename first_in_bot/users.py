"""User table access."""
from __future__ import annotations

import re
from datetime import datetime

from .audit import audit
from .db import Database
from .models import User
from .timeutil import to_iso

_SOURCE_RE = re.compile(r"[a-z0-9_]{1,32}")


def sanitise_source(payload: str | None) -> str:
    """Deep-link tag from t.me/<bot>?start=<source>; anything odd becomes 'unknown'."""
    candidate = (payload or "").strip().lower()
    return candidate if _SOURCE_RE.fullmatch(candidate) else "unknown"


def trial_eligible(user: User) -> bool:
    """One free trial per person: never had one and never had any Stripe subscription."""
    return user.trial_started_at is None and user.stripe_sub_id is None


async def get_user(db: Database, telegram_id: int) -> User | None:
    row = await db.fetchone("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
    return User.from_row(row) if row else None


async def find_user(db: Database, ref: str) -> User | None:
    """Admin lookup by numeric Telegram ID or @username."""
    ref = ref.strip()
    if ref.lstrip("-").isdigit():
        return await get_user(db, int(ref))
    name = ref.lstrip("@")
    if not name:
        return None
    row = await db.fetchone(
        "SELECT * FROM users WHERE lower(username)=lower(?) ORDER BY created_at DESC LIMIT 1", (name,)
    )
    return User.from_row(row) if row else None


async def clear_blocked_bot(db: Database, telegram_id: int, now: datetime) -> bool:
    """They just messaged us or pressed a button, so DMs work again. Reads first so
    ordinary updates don't take a write transaction. True if the flag was cleared."""
    row = await db.fetchone("SELECT blocked_bot FROM users WHERE telegram_id=?", (telegram_id,))
    if row is None or not row["blocked_bot"]:
        return False
    async with db.tx() as conn:
        cur = await conn.execute(
            "UPDATE users SET blocked_bot=0 WHERE telegram_id=? AND blocked_bot=1", (telegram_id,)
        )
        if cur.rowcount != 1:
            return False
        await audit(conn, now, telegram_id, "unblocked")
    return True


async def ensure_user(
    db: Database, telegram_id: int, username: str | None, first_name: str | None, now: datetime
) -> bool:
    """Minimal row for someone we only know from the channel (never pressed /start).
    Returns True if the row was created."""
    async with db.tx() as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO users(telegram_id, username, first_name, source, created_at) "
            "VALUES(?,?,?,?,?)",
            (telegram_id, username, first_name, "unknown", to_iso(now)),
        )
        created = cur.rowcount == 1
        if created:
            await audit(conn, now, telegram_id, "user_created", "source=unknown via=channel")
    return created


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
            # They're talking to us again, so DMs work again even if they once blocked us.
            await conn.execute(
                "UPDATE users SET username=?, first_name=?, blocked_bot=0 WHERE telegram_id=?",
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
