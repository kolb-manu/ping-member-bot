"""SQLite access (WAL, foreign keys) plus a small numbered-migrations system."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

import aiosqlite

log = logging.getLogger(__name__)


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn
        # Jobs and handlers share this one connection. SQLite can't nest
        # transactions on a connection, so tx() blocks are run one at a time.
        # Never await a network call inside tx(): it would hold up every writer.
        self._tx_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str) -> "Database":
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # isolation_level=None: we manage transactions explicitly in tx().
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        db = cls(conn)
        await run_migrations(db)
        return db

    async def close(self) -> None:
        await self.conn.close()

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """All writes go through this: commit on success, roll back on error."""
        async with self._tx_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    async def fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def kv_get(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None


async def kv_set(conn: aiosqlite.Connection, key: str, value: str) -> None:
    """Call inside db.tx()."""
    await conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# --- Migrations ---------------------------------------------------------------

async def _m001_initial(conn: aiosqlite.Connection) -> None:
    statements = [
        """CREATE TABLE users(
            telegram_id          INTEGER PRIMARY KEY,
            username             TEXT,
            first_name           TEXT,
            source               TEXT,
            created_at           TEXT NOT NULL,
            trial_started_at     TEXT,
            trial_ends_at        TEXT,
            stripe_customer_id   TEXT UNIQUE,
            stripe_sub_id        TEXT,
            sub_status           TEXT,
            sub_period_end       TEXT,
            cancel_at_period_end INTEGER DEFAULT 0,
            manual_grant_until   TEXT,
            in_paid_channel      INTEGER DEFAULT 0,
            blocked_bot          INTEGER DEFAULT 0,
            banned               INTEGER DEFAULT 0,
            last_synced_at       TEXT
        )""",
        """CREATE TABLE checkout_sessions(
            id           TEXT PRIMARY KEY,
            telegram_id  INTEGER NOT NULL REFERENCES users(telegram_id),
            created_at   TEXT NOT NULL,
            expires_at   TEXT NOT NULL,
            status       TEXT NOT NULL
        )""",
        """CREATE TABLE notices_sent(
            telegram_id  INTEGER NOT NULL,
            kind         TEXT NOT NULL,
            cycle_key    TEXT NOT NULL,
            sent_at      TEXT NOT NULL,
            PRIMARY KEY (telegram_id, kind, cycle_key)
        )""",
        """CREATE TABLE audit_log(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            telegram_id INTEGER,
            action      TEXT NOT NULL,
            detail      TEXT
        )""",
        "CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)",
        # One trial per Telegram ID, enforced by the DB: once set, the trial
        # start can never be changed or cleared.
        """CREATE TRIGGER users_trial_once
           BEFORE UPDATE OF trial_started_at ON users
           WHEN OLD.trial_started_at IS NOT NULL
            AND NEW.trial_started_at IS NOT OLD.trial_started_at
           BEGIN
               SELECT RAISE(ABORT, 'trial already used');
           END""",
    ]
    for stmt in statements:
        await conn.execute(stmt)


async def _m002_stripe_trial(conn: aiosqlite.Connection) -> None:
    # The free trial now runs as a Stripe subscription trial (card required),
    # so we keep Stripe's trial end separately from the old bot-side trial_ends_at.
    for stmt in [
        "ALTER TABLE users ADD COLUMN sub_trial_end TEXT",
        "ALTER TABLE checkout_sessions ADD COLUMN url TEXT",
        "ALTER TABLE checkout_sessions ADD COLUMN with_trial INTEGER DEFAULT 0",
        "CREATE INDEX audit_log_user ON audit_log(telegram_id)",
        "CREATE INDEX checkout_sessions_status ON checkout_sessions(status)",
    ]:
        await conn.execute(stmt)


async def _m003_past_due_anchor(conn: aiosqlite.Connection) -> None:
    # Stripe moves a subscription into its next period as soon as it creates the
    # renewal invoice, paid or not, so for past_due the period end is a month away.
    # The past-due grace is counted from when the unpaid period began instead.
    for stmt in [
        "ALTER TABLE users ADD COLUMN sub_period_start TEXT",
        "ALTER TABLE users ADD COLUMN past_due_since TEXT",
    ]:
        await conn.execute(stmt)


async def _m004_disputes(conn: aiosqlite.Connection) -> None:
    # Payment disputes seen at Stripe. One row per dispute, so a dispute is only ever
    # alerted once and its later status changes (won/lost) can be followed.
    await conn.execute(
        """CREATE TABLE disputes(
            id          TEXT PRIMARY KEY,
            telegram_id INTEGER,
            customer_id TEXT,
            charge_id   TEXT,
            status      TEXT NOT NULL,
            created_at  TEXT,
            seen_at     TEXT NOT NULL,
            alerted     INTEGER NOT NULL DEFAULT 0
        )"""
    )


# Append new migrations to the end; never edit or reorder old ones.
MIGRATIONS: list[Callable[[aiosqlite.Connection], Awaitable[None]]] = [
    _m001_initial,
    _m002_stripe_trial,
    _m003_past_due_anchor,
    _m004_disputes,
]


async def run_migrations(db: Database) -> None:
    await db.conn.execute("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)")
    row = await db.fetchone("SELECT value FROM kv WHERE key='schema_version'")
    current = int(row["value"]) if row else 0
    if current > len(MIGRATIONS):
        raise RuntimeError(
            f"Database schema version {current} is newer than this code ({len(MIGRATIONS)}). "
            "Refusing to start."
        )
    for version in range(current + 1, len(MIGRATIONS) + 1):
        async with db.tx() as conn:
            await MIGRATIONS[version - 1](conn)
            await conn.execute(
                "INSERT INTO kv(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(version),),
            )
        log.info("Applied database migration %d", version)
