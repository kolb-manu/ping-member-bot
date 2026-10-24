import asyncio
import os
import sqlite3
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from first_in_bot import backup as backup_module
from first_in_bot.backup import BackupError, backup_database, backup_name, prune_backups
from first_in_bot.db import MIGRATIONS, Database
from tests.fakes import add_user

DAY = date(2026, 11, 1)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def rows(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


async def test_backup_round_trip(db, tmp_path):
    await add_user(db, 501, username="alice")
    await add_user(db, 502, username="bob")
    path = await backup_database(db, str(tmp_path / "backups"), DAY)
    assert path == tmp_path / "backups" / "first-in-2026-11-01.db"
    assert rows(path, "SELECT telegram_id, username FROM users ORDER BY telegram_id") == [
        (501, "alice"), (502, "bob"),
    ]
    assert rows(path, "SELECT value FROM kv WHERE key='schema_version'") == [(str(len(MIGRATIONS)),)]
    assert rows(path, "PRAGMA quick_check") == [("ok",)]
    # Nothing else left in the folder: no temp file, no -wal or -shm.
    assert sorted(p.name for p in path.parent.iterdir()) == ["first-in-2026-11-01.db"]


async def test_backup_of_a_wal_file_database_is_one_self_contained_file(tmp_path):
    live = await Database.open(str(tmp_path / "data" / "first-in.db"))
    try:
        await add_user(live, 501)
        path = await backup_database(live, str(tmp_path / "backups"), DAY)
        # The live connection still works afterwards.
        await add_user(live, 502)
    finally:
        await live.close()
    assert rows(path, "PRAGMA journal_mode") == [("delete",)]
    assert rows(path, "SELECT telegram_id FROM users") == [(501,)]
    assert sorted(p.name for p in path.parent.iterdir()) == ["first-in-2026-11-01.db"]


async def test_same_day_overwrites_that_days_file(db, tmp_path):
    backups = str(tmp_path / "backups")
    await add_user(db, 501)
    first = await backup_database(db, backups, DAY)
    await add_user(db, 502)
    second = await backup_database(db, backups, DAY)
    assert first == second
    assert rows(second, "SELECT telegram_id FROM users ORDER BY telegram_id") == [(501,), (502,)]
    assert len(list(Path(backups).iterdir())) == 1


async def test_backup_file_and_new_folder_are_private(db, tmp_path):
    backups = tmp_path / "nested" / "my backups"
    old_umask = os.umask(0)   # even with a permissive umask
    try:
        path = await backup_database(db, str(backups), DAY)
    finally:
        os.umask(old_umask)
    assert mode(backups) == 0o700
    assert mode(path) == 0o600


async def test_backup_name_never_uses_a_datetime(db, tmp_path):
    assert backup_name(date(2027, 1, 9)) == "first-in-2027-01-09.db"
    with pytest.raises(TypeError):
        await backup_database(db, str(tmp_path), datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
async def test_unwritable_folder_raises_clearly_and_leaves_nothing(db, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    backups.chmod(0o500)
    try:
        with pytest.raises(BackupError) as e:
            await backup_database(db, str(backups), DAY)
        assert str(backups) in str(e.value)
        assert list(backups.iterdir()) == []
    finally:
        backups.chmod(0o700)


async def test_failed_check_keeps_the_earlier_backup_and_no_temp_file(db, tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    await add_user(db, 501)
    good = await backup_database(db, str(backups), DAY)
    await add_user(db, 502)
    monkeypatch.setattr(backup_module, "_quick_check", lambda path: ["Page 3: btree page is corrupt"])
    with pytest.raises(BackupError) as e:
        await backup_database(db, str(backups), DAY)
    assert "quick_check" in str(e.value) and "Page 3" in str(e.value)
    assert sorted(p.name for p in backups.iterdir()) == [good.name]
    assert rows(good, "SELECT telegram_id FROM users") == [(501,)]


async def test_copy_failure_raises_backup_error_and_leaves_no_temp_file(db, tmp_path, monkeypatch):
    async def broken_backup(target, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db.conn, "backup", broken_backup)
    backups = tmp_path / "backups"
    with pytest.raises(BackupError) as e:
        await backup_database(db, str(backups), DAY)
    assert "disk I/O error" in str(e.value)
    assert list(backups.iterdir()) == []


async def test_backup_waits_for_a_write_in_progress(db, tmp_path):
    # Backing up mid-write would copy half a change, and SQLite refuses to back up
    # from a connection that is writing (Python retries forever), so it must wait.
    inside, finish = asyncio.Event(), asyncio.Event()

    async def writer():
        async with db.tx() as conn:
            await conn.execute("INSERT INTO kv(key, value) VALUES('half', 'done')")
            inside.set()
            await finish.wait()
            await conn.execute("UPDATE kv SET value='whole' WHERE key='half'")

    write = asyncio.create_task(writer())
    await inside.wait()
    copy = asyncio.create_task(backup_database(db, str(tmp_path / "backups"), DAY))
    await asyncio.sleep(0.05)
    assert not copy.done()
    finish.set()
    await write
    path = await asyncio.wait_for(copy, timeout=5)
    assert rows(path, "SELECT value FROM kv WHERE key='half'") == [("whole",)]


async def test_leftover_temp_files_from_a_killed_run_are_removed(db, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    stale = backups / ".first-in-2026-10-31.k3j2h1g0.tmp"
    stale.write_bytes(b"half a copy")
    notes = backups / "notes.txt"
    notes.write_text("mine")
    await backup_database(db, str(backups), DAY)
    assert sorted(p.name for p in backups.iterdir()) == ["first-in-2026-11-01.db", "notes.txt"]


# --- Pruning ------------------------------------------------------------------

def make_backups(folder: Path, days: list[date]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i, day in enumerate(days):
        path = folder / backup_name(day)
        path.write_bytes(b"x")
        # Modification times run backwards, so only the name can be what counts.
        os.utime(path, (1_000_000 + i, 1_000_000 + i))


def test_prune_keeps_exactly_the_newest_n_by_name(tmp_path):
    days = [DAY - timedelta(days=n) for n in range(20)]   # newest first
    make_backups(tmp_path, days)
    deleted = prune_backups(str(tmp_path), keep=14)
    assert deleted == [tmp_path / backup_name(d) for d in sorted(days[14:])]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(backup_name(d) for d in days[:14])
    # Running it again has nothing left to do.
    assert prune_backups(str(tmp_path), keep=14) == []


def test_prune_counts_dates_not_neighbours(tmp_path):
    # Gaps (days the bot was down) don't matter: it keeps the newest N files.
    days = [date(2026, 9, 1), date(2026, 10, 15), date(2026, 10, 31), date(2026, 11, 1)]
    make_backups(tmp_path, days)
    assert prune_backups(str(tmp_path), keep=2) == [
        tmp_path / "first-in-2026-09-01.db", tmp_path / "first-in-2026-10-15.db",
    ]


def test_prune_never_touches_other_files(tmp_path):
    make_backups(tmp_path, [DAY, DAY - timedelta(days=1)])
    others = [
        "notes.txt",
        "first-in-2020-01-01.db.bak",
        "first-in-2020-01-01.db-wal",
        "old-first-in-2020-01-01.db",
        "first-in-2020-1-1.db",
        "first-in-2020-13-45.db",      # not a real date
        "first-in-2020-01-01.DB",
        ".first-in-2020-01-01.abc.tmp",
    ]
    for name in others:
        (tmp_path / name).write_text("keep me")
    (tmp_path / "first-in-2020-01-02.db").mkdir()     # a folder with a backup's name
    outside = tmp_path.parent / f"{tmp_path.name}-outside.db"
    outside.write_text("not ours")
    (tmp_path / "first-in-2020-01-03.db").symlink_to(outside)

    deleted = prune_backups(str(tmp_path), keep=1)

    assert deleted == [tmp_path / backup_name(DAY - timedelta(days=1))]
    for name in others:
        assert (tmp_path / name).read_text() == "keep me"
    assert (tmp_path / "first-in-2020-01-02.db").is_dir()
    assert (tmp_path / "first-in-2020-01-03.db").is_symlink()
    assert outside.read_text() == "not ours"


def test_prune_with_fewer_than_keep_or_no_folder(tmp_path):
    make_backups(tmp_path, [DAY])
    assert prune_backups(str(tmp_path), keep=14) == []
    assert prune_backups(str(tmp_path / "missing"), keep=14) == []


def test_prune_refuses_to_keep_nothing(tmp_path):
    make_backups(tmp_path, [DAY])
    with pytest.raises(ValueError):
        prune_backups(str(tmp_path), keep=0)
    assert (tmp_path / backup_name(DAY)).exists()


# --- the nightly job ---------------------------------------------------------------

from first_in_bot.backup import BACKUP_DATE_KEY, BACKUP_FAILED_KEY, RETRY_AFTER, nightly_backup  # noqa: E402
from tests.fakes import NOW, FakeTelegram, make_svc  # noqa: E402

NIGHT = datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc)   # 03:30 GMT = 03:30 London in November


def _svc(db, folder, **overrides):
    return make_svc(db, FakeTelegram(), None, BACKUP_DIR=str(folder), **overrides)


async def test_nightly_backup_runs_once_a_day_at_half_three(db, tmp_path):
    svc = _svc(db, tmp_path / "b")
    await add_user(db, 1)
    assert await nightly_backup(svc, NIGHT - timedelta(minutes=1)) is None
    path = await nightly_backup(svc, NIGHT)
    assert path == tmp_path / "b" / "first-in-2026-11-02.db"
    assert await db.kv_get(BACKUP_DATE_KEY) == "2026-11-02"
    # Done for today: later ticks (and restarts) don't repeat it.
    assert await nightly_backup(svc, NIGHT + timedelta(hours=5)) is None
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    conn.close()


async def test_nightly_backup_catches_up_after_downtime(db, tmp_path):
    # Down at 03:30: the first tick after that, the same day, does it.
    svc = _svc(db, tmp_path / "b")
    assert await nightly_backup(svc, NOW) == tmp_path / "b" / "first-in-2026-11-01.db"


async def test_nightly_backup_uses_the_local_date_in_summer(db, tmp_path):
    # 23:45 UTC on 24 Oct is 00:45 London (BST), so not yet due on the 25th...
    svc = _svc(db, tmp_path / "b")
    assert await nightly_backup(svc, datetime(2026, 10, 24, 23, 45, tzinfo=timezone.utc)) is None
    # ...and 02:30 UTC on the 25th (after the clocks went back) is 02:30 GMT: still not 03:30.
    assert await nightly_backup(svc, datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc)) is None
    path = await nightly_backup(svc, datetime(2026, 10, 25, 3, 30, tzinfo=timezone.utc))
    assert path.name == "first-in-2026-10-25.db"


async def test_nightly_backup_prunes_to_backup_keep(db, tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    for day in ("2026-10-28", "2026-10-29", "2026-10-30"):
        (folder / f"first-in-{day}.db").write_bytes(b"old")
    (folder / "before-deploy.db").write_bytes(b"keep me")
    svc = _svc(db, folder, BACKUP_KEEP="2")
    await nightly_backup(svc, NIGHT)
    assert sorted(p.name for p in folder.iterdir()) == [
        "before-deploy.db", "first-in-2026-10-30.db", "first-in-2026-11-02.db"]


async def test_failed_backup_raises_and_is_retried_every_half_hour(db, tmp_path):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    svc = _svc(db, blocker)
    with pytest.raises(BackupError):
        await nightly_backup(svc, NIGHT)
    assert await db.kv_get(BACKUP_DATE_KEY) is None
    assert await db.kv_get(BACKUP_FAILED_KEY) is not None
    # The job ticks every minute, but a failing backup isn't hammered.
    assert await nightly_backup(svc, NIGHT + timedelta(minutes=1)) is None
    with pytest.raises(BackupError):
        await nightly_backup(svc, NIGHT + RETRY_AFTER)
    # Once it works again the failure mark goes.
    blocker.unlink()
    good = _svc(db, blocker)
    assert await nightly_backup(good, NIGHT + 2 * RETRY_AFTER) is not None
    assert await db.kv_get(BACKUP_FAILED_KEY) is None
