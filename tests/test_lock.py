import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest

from first_in_bot import lock as lock_module
from first_in_bot.lock import (
    AlreadyRunning,
    InstanceLock,
    _process_table,   # the real one: conftest swaps it for an empty table in every test
    acquire_instance_lock,
    is_bot_command,
    lock_path_for,
    unlocked_copies,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Run from the repo root, so `python -c` imports this checkout's first_in_bot.
CHILD = """
import sys
from first_in_bot.lock import acquire_instance_lock
lock = acquire_instance_lock(sys.argv[1])
print("locked", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def lock_file(tmp_path) -> str:
    return str(tmp_path / "first-in-member-bot.lock")


def test_second_acquire_in_the_same_process_is_refused(lock_file):
    # flock belongs to the open file, not the process, so a second open() of the
    # same file conflicts even here (on macOS as well as Linux).
    with acquire_instance_lock(lock_file):
        assert Path(lock_file).read_text() == f"{os.getpid()}\n"
        with pytest.raises(AlreadyRunning) as e:
            acquire_instance_lock(lock_file)
        assert f"PID {os.getpid()}" in str(e.value)
        assert lock_file in str(e.value)
        # The refused attempt didn't wipe the holder's PID.
        assert Path(lock_file).read_text() == f"{os.getpid()}\n"


def test_release_then_acquire_again(lock_file):
    lock = acquire_instance_lock(lock_file)
    lock.release()
    lock.release()   # harmless twice
    assert lock.fd is None
    again = acquire_instance_lock(lock_file)
    try:
        assert isinstance(again, InstanceLock)
        assert Path(lock_file).exists()
    finally:
        again.release()


def test_context_manager_releases_on_exit_and_on_error(lock_file):
    with pytest.raises(ZeroDivisionError):
        with acquire_instance_lock(lock_file):
            1 / 0
    with acquire_instance_lock(lock_file) as lock:
        assert lock.fd is not None
    assert lock.fd is None


def test_unreadable_pid_still_refuses_with_a_clear_message(lock_file):
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, b"not a pid")
        with pytest.raises(AlreadyRunning) as e:
            acquire_instance_lock(lock_file)
        assert "PID unknown" in str(e.value)
    finally:
        os.close(fd)


def test_lock_held_by_another_process_blocks_until_it_dies(lock_file):
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, lock_file],
        cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(AlreadyRunning) as e:
            acquire_instance_lock(lock_file)
        assert f"PID {child.pid}" in str(e.value)
        # kill -9 gives it no chance to clean up, and the lock still goes with it.
        child.kill()
        child.wait(timeout=10)
        with acquire_instance_lock(lock_file):
            assert Path(lock_file).read_text() == f"{os.getpid()}\n"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()


def test_acquire_creates_the_folder(tmp_path):
    # main() takes the lock before Database.open has created the data folder.
    path = str(tmp_path / "data" / "first-in-member-bot.lock")
    with acquire_instance_lock(path):
        assert Path(path).exists()


def test_lock_path_sits_next_to_the_database(tmp_path, monkeypatch):
    assert lock_path_for("/srv/first-in/data/first-in.db") == "/srv/first-in/data/first-in-member-bot.lock"
    monkeypatch.chdir(tmp_path)
    assert lock_path_for("data/first-in.db") == str(tmp_path.resolve() / "data" / "first-in-member-bot.lock")
    assert lock_path_for("first-in.db") == str(tmp_path.resolve() / "first-in-member-bot.lock")
    # No folder for an in-memory database, so the working directory.
    assert lock_path_for(":memory:") == str(tmp_path.resolve() / "first-in-member-bot.lock")


# --- main() takes the lock before anything else ------------------------------------

def test_main_exits_when_another_copy_holds_the_lock(tmp_path, monkeypatch, capsys):
    from first_in_bot import main as main_module
    from first_in_bot.lock import acquire_instance_lock, lock_path_for
    from tests.test_config import GOOD

    db_path = str(tmp_path / "data" / "first-in.db")
    # Never read the real .env, and never get as far as the bot.
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_module.os, "environ", {**GOOD, "DB_PATH": db_path})

    def must_not_run(config):
        raise AssertionError("the bot started next to a running copy")

    monkeypatch.setattr(main_module, "run", must_not_run)
    with acquire_instance_lock(lock_path_for(db_path)):
        with pytest.raises(SystemExit) as exc:
            main_module.main()
    assert exc.value.code == 3
    assert "already running" in capsys.readouterr().err
    assert not (tmp_path / "data" / "first-in.db").exists()


# --- Copies from before the lock existed --------------------------------------------

@pytest.mark.parametrize("args, is_bot", [
    (".venv/bin/python -m first_in_bot", True),
    ("/home/tom/first-in-member-bot/.venv/bin/python3 -u -m first_in_bot", True),
    (".venv/bin/python -m first_in_bot check", False),        # the read-only preflight
    (".venv/bin/python -m pytest tests/test_lock.py", False),
    ("python -m first_in_bot_other", False),
    ("vim first_in_bot/main.py", False),
    ("", False),
])
def test_is_bot_command(args, is_bot):
    assert is_bot_command(args) is is_bot


def test_unlocked_copies_leaves_out_this_process_and_checks(monkeypatch):
    monkeypatch.setattr(lock_module, "_process_table", lambda: [
        (100, ".venv/bin/python -m first_in_bot"),
        (200, ".venv/bin/python -m first_in_bot check"),
        (300, ".venv/bin/python -m first_in_bot"),        # this process
        (50, "/usr/bin/python3 -m first_in_bot"),
    ])
    assert unlocked_copies(own_pid=300) == [50, 100]


def test_real_process_table_lists_this_process():
    # The real ps, on macOS and Linux alike.
    table = dict(_process_table())
    assert os.getpid() in table
    assert "python" in table[os.getpid()].lower() or "pytest" in table[os.getpid()].lower()


def test_process_table_is_empty_when_ps_cant_run(monkeypatch, caplog):
    def no_ps(*args, **kwargs):
        raise FileNotFoundError("ps")
    monkeypatch.setattr(lock_module.subprocess, "run", no_ps)
    assert _process_table() == []
    assert "Couldn't list processes" in caplog.text


def test_main_exits_next_to_a_copy_without_the_lock(tmp_path, monkeypatch, capsys):
    from first_in_bot import main as main_module
    from tests.test_config import GOOD

    db_path = str(tmp_path / "data" / "first-in.db")
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_module.os, "environ", {**GOOD, "DB_PATH": db_path})
    monkeypatch.setattr(lock_module, "_process_table", lambda: [(46429, ".venv/bin/python -m first_in_bot")])

    def must_not_run(config):
        raise AssertionError("the bot started next to an older copy")

    monkeypatch.setattr(main_module, "run", must_not_run)
    with pytest.raises(SystemExit) as exc:
        main_module.main()
    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert "without the lock file (PID 46429)" in err and "kill 46429" in err
    assert not (tmp_path / "data" / "first-in.db").exists()
    # It let go of the lock on the way out, so the next start isn't blocked by it.
    acquire_instance_lock(lock_path_for(db_path)).release()
