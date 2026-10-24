"""User table access.

Admins name people by numeric ID or @username, so stored usernames are kept
current: /start and every paid-channel member update refresh them, and whoever
claims a username takes it off any other row (Telegram usernames are unique at
any one time, so the other row is out of date). A lookup that still matches
several rows returns them all and the caller asks for the numeric ID; it never
guesses.
"""
from __future__ import annotations

import re
from datetime import datetime

import aiosqlite

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


async def find_users(db: Database, ref: str) -> list[User]:
    """Admin lookup by numeric Telegram ID or @username: everyone it matches, which
    for a username can be more than one row (someone renamed and we haven't seen it)."""
    ref = ref.strip()
    if ref.lstrip("-").isdigit():
        user = await get_user(db, int(ref))
        return [user] if user else []
    name = ref.lstrip("@")
    if not name:
        return []
    rows = await db.fetchall("SELECT * FROM users WHERE lower(username)=lower(?) ORDER BY telegram_id", (name,))
    return [User.from_row(row) for row in rows]


async def _claim_username(conn: aiosqlite.Connection, telegram_id: int, username: str | None, now: datetime) -> None:
    """telegram_id holds this username now, so any other row that has it is out of date."""
    if not username:
        return
    cur = await conn.execute(
        "SELECT telegram_id FROM users WHERE lower(username)=lower(?) AND telegram_id<>?", (username, telegram_id)
    )
    for row in await cur.fetchall():
        await conn.execute("UPDATE users SET username=NULL WHERE telegram_id=?", (row["telegram_id"],))
        await audit(conn, now, row["telegram_id"], "username_cleared", f"@{username} now belongs to {telegram_id}")


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
    """Minimal row for someone we only know from the channel (never pressed /start),
    or, if we know them, their current names from Telegram. Returns True if the row
    was created."""
    async with db.tx() as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO users(telegram_id, username, first_name, source, created_at) "
            "VALUES(?,?,?,?,?)",
            (telegram_id, username, first_name, "unknown", to_iso(now)),
        )
        created = cur.rowcount == 1
        if created:
            await audit(conn, now, telegram_id, "user_created", "source=unknown via=channel")
        else:
            await conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE telegram_id=? "
                "AND (username IS NOT ? OR first_name IS NOT ?)",
                (username, first_name, telegram_id, username, first_name),
            )
        await _claim_username(conn, telegram_id, username, now)
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
        await _claim_username(conn, telegram_id, username, now)
    user = await get_user(db, telegram_id)
    assert user is not None
    return user, created
