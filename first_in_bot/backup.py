"""Nightly database backups (spec §10).

The copy is made with SQLite's online backup API through the bot's own
connection, never by copying the file: a plain copy of a WAL database can miss
recent commits or catch a write half done. Going through the live connection
also means an in-memory database (as in the tests) can be backed up.

Each copy is written to a temp file next to the final one, checked with
PRAGMA quick_check, then renamed into place, so a failed run never leaves a
first-in-YYYY-MM-DD.db that looks like a good backup but isn't. The copies hold
Telegram IDs and usernames, so the folder is created 0700 and each file is 0600.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sqlite3
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

from .db import Database

log = logging.getLogger(__name__)

_BACKUP_RE = re.compile(r"^first-in-(\d{4}-\d{2}-\d{2})\.db$")
# Hidden and not matching _BACKUP_RE, so pruning and anyone listing the folder
# never mistake a half-written copy for a backup.
_TEMP_PREFIX = ".first-in-"
_TEMP_SUFFIX = ".tmp"


class BackupError(RuntimeError):
    pass


def backup_name(day: date) -> str:
    return f"first-in-{day.isoformat()}.db"


def _backup_day(name: str) -> date | None:
    """The date in a backup's file name, or None if the file isn't one of ours."""
    match = _BACKUP_RE.match(name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


async def backup_database(db: Database, backup_dir: str, day: date) -> Path:
    """Back up the live database to <backup_dir>/first-in-YYYY-MM-DD.db, replacing
    that day's file if there is one, and return its path. Raises BackupError."""
    if isinstance(day, datetime):
        # Which day it is (local, not UTC) is the caller's decision, so don't guess.
        raise TypeError("backup_database needs a date, not a datetime")
    directory = Path(backup_dir)
    final = directory / backup_name(day)
    started = time.monotonic()
    try:
        tmp = await asyncio.to_thread(_new_temp_file, directory, day)
    except OSError as exc:
        raise BackupError(f"Can't write a backup in {directory}: {exc}") from exc

    target: sqlite3.Connection | None = None
    try:
        target = await asyncio.to_thread(_connect, tmp)
        await _copy_live_database(db, target)
        await asyncio.to_thread(_make_self_contained, target)
        size = await asyncio.to_thread(_check_and_install, tmp, final)
    except (OSError, sqlite3.Error) as exc:
        await asyncio.to_thread(_discard, target, tmp)
        raise BackupError(f"Backup to {final} failed: {exc}") from exc
    except BaseException:
        await asyncio.to_thread(_discard, target, tmp)
        raise

    log.info("Database backed up to %s (%d KB, %.2fs)", final, size // 1024, time.monotonic() - started)
    return final


def _new_temp_file(directory: Path, day: date) -> Path:
    if not directory.is_dir():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)   # mkdir's mode is filtered by the umask
    _remove_stale_temps(directory)
    # mkstemp creates the file 0600 and the rename keeps that mode.
    fd, name = tempfile.mkstemp(
        prefix=f"{_TEMP_PREFIX}{day.isoformat()}.", suffix=_TEMP_SUFFIX, dir=directory
    )
    os.close(fd)
    return Path(name)


def _remove_stale_temps(directory: Path) -> None:
    # Only one bot runs at a time (lock.py) and only one backup at a time (the
    # scheduler's per-job lock), so any temp file here was left by a run that was
    # killed part way. It's a full copy of the data, so don't leave it lying around.
    for path in directory.glob(f"{_TEMP_PREFIX}*{_TEMP_SUFFIX}"):
        if path.is_file():
            path.unlink(missing_ok=True)
            log.warning("Removed a half-written backup left by an earlier run: %s", path)


def _connect(path: Path) -> sqlite3.Connection:
    # The copy itself runs on aiosqlite's worker thread, not the one that opened this.
    return sqlite3.connect(path, check_same_thread=False)


async def _copy_live_database(db: Database, target: sqlite3.Connection) -> None:
    # Hold the write lock so no tx() block is half done on the shared connection.
    # SQLite won't back up from a connection in the middle of a write, and Python's
    # backup() retries that forever, which would wedge every query queued behind it.
    async with db._tx_lock:
        if db.conn.in_transaction:
            raise BackupError("the database connection is in the middle of a transaction")
        # The whole database in one step: quick at this size, and it never sleeps
        # on aiosqlite's thread between steps while other queries wait.
        await db.conn.backup(target, pages=-1)


def _make_self_contained(target: sqlite3.Connection) -> None:
    # The copy inherits WAL mode from the live database; switch it back so a
    # backup is one file, with no -wal or -shm to go missing when it's downloaded.
    mode = target.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    target.close()
    if mode != "delete":
        raise BackupError(f"couldn't take the backup out of {mode} mode")


def _quick_check(path: Path) -> list[str]:
    # A fresh connection, so the check reads what actually reached the disk.
    # Read-only, so a missing copy is an error rather than a new empty database.
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return [row[0] for row in conn.execute("PRAGMA quick_check")]
    finally:
        conn.close()


def _check_and_install(tmp: Path, final: Path) -> int:
    """Verify the copy, move it into place, and return its size in bytes."""
    problems = _quick_check(tmp)
    if problems != ["ok"]:
        raise BackupError(f"quick_check on the new backup failed: {'; '.join(problems[:5])}")
    os.chmod(tmp, 0o600)
    os.replace(tmp, final)
    return final.stat().st_size


def _discard(target: sqlite3.Connection | None, tmp: Path) -> None:
    # Best effort: the error that got us here is the one worth reporting.
    if target is not None:
        with contextlib.suppress(sqlite3.Error):
            target.close()
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        log.warning("Couldn't remove the unfinished backup %s", tmp, exc_info=True)


def prune_backups(backup_dir: str, keep: int) -> list[Path]:
    """Delete all but the newest `keep` backups, judged by the date in the file
    name, and return what was deleted (oldest first). Only files named exactly
    first-in-YYYY-MM-DD.db are considered; anything else in the folder is left alone."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    directory = Path(backup_dir)
    if not directory.is_dir():
        return []
    dated: list[tuple[date, Path]] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            day = _backup_day(entry.name)
            if day is not None and entry.is_file(follow_symlinks=False):
                dated.append((day, Path(entry.path)))
    dated.sort(reverse=True)
    old = [path for _, path in sorted(dated[keep:])]
    for path in old:
        path.unlink(missing_ok=True)
        log.info("Deleted old backup %s", path)
    return old
