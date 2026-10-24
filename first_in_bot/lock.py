"""Single-instance guard (spec §11): two copies of the bot must never run at
once, or both would poll Telegram and run the jobs, double-processing everything.

It's an flock() on a file that stays open for the life of the process. The
kernel drops the lock however the process ends, even kill -9 or a crash, so a
stale lock can't happen and nobody ever has to delete the file by hand. The PID
written into the file is only there for humans; the lock is what counts.
"""
from __future__ import annotations

import fcntl
import logging
import os
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
