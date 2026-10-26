"""Single-instance guard (spec §11): two copies of the bot must never run at
once, or both would poll Telegram and run the jobs, double-processing everything.

It's an flock() on a file that stays open for the life of the process. The
kernel drops the lock however the process ends, even kill -9 or a crash, so a
stale lock can't happen and nobody ever has to delete the file by hand. The PID
written into the file is only there for humans; the lock is what counts.

A copy from before the lock existed (phase 2) doesn't take it, so the lock can't
see one. unlocked_copies() looks for those in the process list instead.
"""
from __future__ import annotations

import fcntl
import logging
import os
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

LOCK_FILENAME = "first-in-member-bot.lock"


class AlreadyRunning(RuntimeError):
    pass


@dataclass
class InstanceLock:
    path: str
    # A raw descriptor rather than a file object, so garbage collection can never
    # close it (and drop the lock) while the bot is still running.
    fd: int | None

    def release(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        # The file itself stays. If it were deleted, a copy that had already opened
        # the old file could lock it while another creates and locks a new one.
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass   # only the PID for humans; closing below is what matters
        os.close(fd)   # the only descriptor on this open file, so the flock goes too

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def lock_path_for(db_path: str) -> str:
    """The lock sits next to the database it protects. An in-memory database has
    no folder, so its lock goes in the working directory."""
    if db_path in ("", ":memory:"):
        return os.path.abspath(LOCK_FILENAME)
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), LOCK_FILENAME)


def acquire_instance_lock(path: str) -> InstanceLock:
    """Take the lock, or raise AlreadyRunning straight away. It never waits."""
    # Taken before the database is opened, so its folder may not exist yet.
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # No O_TRUNC: if another copy holds the lock, its PID must survive this open.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pid = _read_pid(fd)
        os.close(fd)
        holder = f"PID {pid}" if pid is not None else "PID unknown"
        raise AlreadyRunning(
            f"Another copy of the First In member bot is already running ({holder}, "
            f"lock file {path}). Only one copy can run at a time, so stop that one first."
        ) from None
    except BaseException:
        os.close(fd)
        raise
    _write_pid(fd)
    log.info("Took the single-instance lock %s (PID %d)", path, os.getpid())
    return InstanceLock(path=path, fd=fd)


def _read_pid(fd: int) -> int | None:
    try:
        pid = int(os.pread(fd, 32, 0).decode("ascii").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _write_pid(fd: int) -> None:
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
    except OSError:
        # We hold the lock either way; a missing PID only makes the message vaguer.
        log.warning("Couldn't write the PID into the lock file", exc_info=True)


# --- Copies that don't take the lock ------------------------------------------------

def _process_table() -> list[tuple[int, str]]:
    """(PID, command line) of every process on this machine, or nothing if ps
    can't be run: this only adds to the lock, so it never stops the bot itself."""
    try:
        # -ww: never cut the command line short. -e and -o work on macOS and Linux.
        out = subprocess.run(["ps", "-ww", "-eo", "pid=,args="], capture_output=True, text=True,
                             timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        log.warning("Couldn't list processes to look for an older copy of the bot", exc_info=True)
        return []
    table = []
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            table.append((int(pid), args.strip()))
    return table


def is_bot_command(args: str) -> bool:
    """`python -m first_in_bot` with nothing after it: the bot itself, not the
    read-only `check`."""
    words = args.split()
    for i, word in enumerate(words[:-1]):
        if word == "-m" and words[i + 1] == "first_in_bot":
            return len(words) == i + 2
    return False


def unlocked_copies(own_pid: int) -> list[int]:
    """PIDs of other copies of the bot running on this machine. Call it once the
    lock is known to be free (or held by us): then any copy found is one that
    doesn't take the lock, i.e. from before it existed."""
    return sorted(pid for pid, args in _process_table() if pid != own_pid and is_bot_command(args))


def unlocked_copy_text(pids: list[int]) -> str:
    listed = ", ".join(str(pid) for pid in pids)
    return (f"Another copy of the First In member bot is running without the lock file (PID {listed}), "
            f"probably a version from before the lock existed. Only one copy can run at a time, "
            f"so stop that one first (Ctrl+C in its terminal, or kill {listed}).")
