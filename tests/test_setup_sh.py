"""setup.sh, run for real by bash in a copy of the project folder, with stand-ins
for sudo, apt-get, systemctl, git, python3 and the venv's python, so nothing on
this machine is installed, started or stopped. Only the unit file's path is
changed (it points into tmp). sqlite3 is the real one, so the before-deploy copy
is a real online backup. Also runs the DEPLOY.md lines that the code depends on."""
from __future__ import annotations

import os
import pty
import re
import select
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from first_in_bot import maintenance
from first_in_bot.db import Database
from first_in_bot.timeutil import from_iso, to_iso, utcnow
from tests.fakes import add_user

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "setup.sh"
DEPLOY = ROOT / "DEPLOY.md"
BASH = shutil.which("bash")
SQLITE3 = shutil.which("sqlite3")
REAL_UNIT = 'UNIT="/etc/systemd/system/${SERVICE}.service"'
SHA_A, SHA_B = "a" * 40, "b" * 40
COPY_RE = re.compile(r"^before-deploy-\d{8}-\d{6}\.db$")
STOPPED = "The bot is STOPPED"

pytestmark = [
    pytest.mark.skipif(BASH is None or SQLITE3 is None, reason="needs bash and sqlite3"),
    pytest.mark.skipif(os.geteuid() == 0, reason="setup.sh refuses to run as root"),
]

# Each stand-in writes what it was asked to do to $FAKE_LOG, one line per call.
STUBS = {
    "sudo": 'echo "sudo $*" >> "$FAKE_LOG"\nexec "$@"\n',
    "apt-get": 'echo "apt-get $*" >> "$FAKE_LOG"\n',
    "python3": "exit 0\n",                      # the version check and `-m venv`
    "sleep": "exit 0\n",
    # The service's state lives in a file. enable --now sets $FAKE_STARTS_AS.
    "systemctl": """echo "systemctl $*" >> "$FAKE_LOG"
state="$FAKE_ROOT/service-state"
case " $* " in
    *" is-active "*) [ "$(cat "$state" 2>/dev/null)" = active ]; exit $? ;;
    *" stop "*) echo inactive > "$state" ;;
    *" enable --now "*) echo "${FAKE_STARTS_AS:-active}" > "$state" ;;
    *" start "*) echo active > "$state" ;;
esac
exit 0
""",
    "git": """[ -f "$FAKE_ROOT/head" ] || exit 128     # not a git clone
case "$1" in
    rev-parse) cat "$FAKE_ROOT/head" ;;
    branch) cat "$FAKE_ROOT/branch" ;;
    *) exit 1 ;;
esac
""",
}

# The venv's python: pip does nothing, and the check passes, fails or is
# interrupted as if Ctrl+C were pressed (SIGINT to the whole process group).
VENV_PYTHON = """echo "python $*" >> "$FAKE_LOG"
if [ "$*" = "-m first_in_bot check" ]; then
    case "$FAKE_CHECK" in
        fail) echo "  [FAIL] Telegram: couldn't reach Telegram"; exit 1 ;;
        interrupt) kill -INT 0 ;;
    esac
fi
exit 0
"""


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@dataclass
class Rig:
    root: Path

    @property
    def repo(self) -> Path:
        return self.root / "repo"

    @property
    def unit(self) -> Path:
        return self.root / "etc" / "first-in-member-bot.service"

    @property
    def log(self) -> list[str]:
        path = self.root / "log"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def state(self) -> str:
        return (self.root / "service-state").read_text().strip()

    @property
    def history(self) -> list[str]:
        path = self.repo / "data" / "deploy-history.log"
        return path.read_text().splitlines() if path.exists() else []

    def copies(self) -> list[str]:
        return sorted(p.name for p in (self.repo / "backups").glob("before-deploy-*"))

    def installed(self, running: bool = True) -> None:
        """As after an earlier setup.sh: the unit file is there."""
        self.unit.write_text("[Unit]\n")
        (self.root / "service-state").write_text("active\n" if running else "inactive\n")

    def at_commit(self, sha: str | None, branch: str = "main") -> None:
        if sha is None:
            (self.root / "head").unlink(missing_ok=True)
            return
        (self.root / "head").write_text(sha + "\n")
        (self.root / "branch").write_text(branch + "\n")

    async def with_database(self, users: int) -> None:
        (self.repo / "data").mkdir(mode=0o700, exist_ok=True)
        db = await Database.open(str(self.repo / "data" / "first-in.db"))
        try:
            for telegram_id in range(1, users + 1):
                await add_user(db, telegram_id)
        finally:
            await db.close()

    def env(self, check: str = "pass", **extra: str) -> dict[str, str]:
        return {**os.environ, "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
                "FAKE_LOG": str(self.root / "log"), "FAKE_ROOT": str(self.root), "FAKE_CHECK": check, **extra}

    def run(self, check: str = "pass", **extra: str) -> subprocess.CompletedProcess:
        # A session of its own, so the "Ctrl+C" stand-in only reaches setup.sh.
        return subprocess.run([BASH, str(self.repo / "setup.sh")], env=self.env(check, **extra),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                              start_new_session=True)

    def run_in_terminal(self, answer_prompt: bool) -> tuple[int | None, str]:
        """setup.sh with a terminal as its input, as over SSH. Presses Enter at the
        Mac prompt if asked to; if it waits for input otherwise, it's killed."""
        master, slave = pty.openpty()
        proc = subprocess.Popen([BASH, str(self.repo / "setup.sh")], env=self.env(), stdin=slave,
                                stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        out, answered, deadline = b"", False, time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                chunk = b""
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:             # Linux: EIO once the script has exited
                        pass
                    out += chunk
                    if answer_prompt and not answered and b"Press Enter" in out:
                        os.write(master, b"\n")
                        answered = True
                if not chunk and proc.poll() is not None:
                    break
            else:
                proc.kill()
            proc.wait(timeout=10)
        finally:
            os.close(master)
        return proc.returncode, out.decode(errors="replace").replace("\r", "")

    def index(self, prefix: str) -> int:
        return next(i for i, line in enumerate(self.log) if line.startswith(prefix))


@pytest.fixture
def rig(tmp_path) -> Rig:
    rig = Rig(tmp_path)
    (tmp_path / "bin").mkdir()
    for name, body in STUBS.items():
        _script(tmp_path / "bin" / name, body)
    _script(tmp_path / "bin" / "sqlite3", f'echo "sqlite3 $*" >> "$FAKE_LOG"\nexec "{SQLITE3}" "$@"\n')
    (tmp_path / "etc").mkdir()

    text = SETUP.read_text()
    assert text.count(REAL_UNIT) == 1, "setup.sh's UNIT line changed: update this test"
    rig.repo.mkdir()
    (rig.repo / "setup.sh").write_text(text.replace(REAL_UNIT, f'UNIT="{rig.unit}"'))
    (rig.repo / "requirements.txt").write_text("")
    (rig.repo / ".env").write_text("")
    (rig.repo / ".venv" / "bin").mkdir(parents=True)
    _script(rig.repo / ".venv" / "bin" / "python", VENV_PYTHON)
    rig.at_commit(SHA_A)
    return rig


def test_setup_sh_parses():
    assert subprocess.run([BASH, "-n", str(SETUP)], capture_output=True).returncode == 0


# --- The before-deploy copy ----------------------------------------------------------

async def test_redeploy_saves_a_copy_after_the_stop_and_before_the_check(rig):
    rig.installed()
    await rig.with_database(users=3)
    result = rig.run()
    assert result.returncode == 0, result.stdout + result.stderr

    [name] = rig.copies()
    assert COPY_RE.match(name) and not name.startswith("first-in-")   # never mistaken for a nightly
    assert f"Saved backups/{name}" in result.stdout
    copy = rig.repo / "backups" / name
    assert copy.stat().st_mode & 0o777 == 0o600
    conn = sqlite3.connect(copy)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    finally:
        conn.close()
    # Stopped first, so the copy is exactly what the old code left; made before the check.
    assert (rig.index("sudo systemctl stop") < rig.index("sqlite3 data/first-in.db .backup")
            < rig.index("python -m first_in_bot check") < rig.index("sudo systemctl enable --now"))
    assert rig.state == "active" and STOPPED not in result.stdout


async def test_only_the_newest_ten_copies_are_kept(rig):
    rig.installed()
    await rig.with_database(users=1)
    backups = rig.repo / "backups"
    backups.mkdir(mode=0o700)
    old = [f"before-deploy-202601{day:02d}-120000.db" for day in range(1, 13)]
    for name in [*old, "before-deploy.db", "first-in-2026-10-01.db", "before-restore-20260101-000000.db"]:
        (backups / name).write_text("")
    assert rig.run().returncode == 0

    kept = [n for n in rig.copies() if COPY_RE.match(n)]
    assert len(kept) == 10
    assert kept[:9] == old[-9:]                 # the newest old ones, plus the one just made
    for name in ("before-deploy.db", "first-in-2026-10-01.db", "before-restore-20260101-000000.db"):
        assert (backups / name).exists()        # not setup.sh's to delete


def test_no_database_means_no_copy(rig):
    result = rig.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No database at data/first-in.db yet" in result.stdout
    assert rig.copies() == []
    assert rig.history[0].split()[3] == "-"


# --- A run that ends early ----------------------------------------------------------

async def test_failed_check_after_stopping_the_bot_says_it_is_stopped(rig):
    rig.installed(running=True)
    await rig.with_database(users=1)
    result = rig.run(check="fail")
    assert result.returncode == 1
    assert "has NOT been started" in result.stdout
    assert STOPPED in result.stdout and "sudo systemctl start first-in-member-bot" in result.stdout
    assert rig.state == "inactive"
    assert not any("enable" in line for line in rig.log)
    assert len(rig.copies()) == 1               # the copy was made before the check
    assert rig.history == []


async def test_ctrl_c_during_the_check_says_the_bot_is_stopped(rig):
    rig.installed(running=True)
    await rig.with_database(users=1)
    result = rig.run(check="interrupt")
    assert result.returncode == 130
    assert STOPPED in result.stdout
    assert rig.state == "inactive" and rig.history == []


def test_failed_check_on_first_install_stopped_nothing(rig):
    result = rig.run(check="fail")
    assert result.returncode == 1
    assert "has NOT been started" in result.stdout and STOPPED not in result.stdout
    assert not any("stop" in line for line in rig.log)


def test_service_that_was_already_stopped_isnt_reported_as_stopped_by_setup(rig):
    rig.installed(running=False)
    result = rig.run(check="fail")
    assert result.returncode == 1 and STOPPED not in result.stdout


def test_service_that_doesnt_stay_up_points_at_the_log_not_the_banner(rig):
    rig.installed(running=True)
    result = rig.run(FAKE_STARTS_AS="failed")
    assert result.returncode == 1
    assert "didn't stay running" in result.stdout and "journalctl" in result.stdout
    assert STOPPED not in result.stdout
    assert rig.history == []                    # only versions that really started


# --- The Mac prompt ---------------------------------------------------------------------

def test_mac_prompt_only_on_first_install(rig):
    code, out = rig.run_in_terminal(answer_prompt=True)
    assert code == 0, out
    assert "Press Enter once the Mac copy is stopped" in out
    assert rig.unit.exists()

    # Every later run (a routine deploy) goes straight through, with nobody pressing anything.
    code, out = rig.run_in_terminal(answer_prompt=False)
    assert code == 0, out
    assert "Mac copy" not in out and STOPPED not in out
    assert rig.state == "active"


# --- data/deploy-history.log and the rollback line in DEPLOY.md ------------------------

def deploy_md() -> str:
    return DEPLOY.read_text()


def rollback_commit(repo: Path) -> str:
    """What DEPLOY.md's `git checkout "$(...)"` line would check out."""
    match = re.search(r'git checkout "\$\((tail -n 2 data/deploy-history\.log[^"]*)\)"', deploy_md())
    assert match, "DEPLOY.md's rollback line changed: update this test"
    result = subprocess.run([BASH, "-c", match.group(1)], cwd=repo, capture_output=True, text=True)
    return result.stdout.strip()


async def test_history_has_one_line_per_version_and_rollback_picks_the_one_before(rig):
    await rig.with_database(users=1)
    assert rig.run().returncode == 0
    assert rig.run().returncode == 0            # the same version again: no new line
    [first] = rig.history
    when, sha, branch, copy = first.split()
    assert sha == SHA_A and branch == "main"
    assert abs(from_iso(when.replace("Z", "+00:00")) - utcnow()) < timedelta(minutes=5)

    rig.at_commit(SHA_B)
    assert rig.run().returncode == 0
    assert [line.split()[1] for line in rig.history] == [SHA_A, SHA_B]
    # The copy on B's line was saved just before B first started: the one to restore
    # when rolling back past one of B's migrations.
    assert COPY_RE.match(Path(rig.history[-1].split()[3]).name)
    assert rollback_commit(rig.repo) == SHA_A

    rig.at_commit(SHA_A, branch="")             # after `git checkout <commit>`
    assert rig.run().returncode == 0
    assert rig.history[-1].split()[1:3] == [SHA_A, "(detached)"]
    assert rollback_commit(rig.repo) == SHA_B   # DEPLOY.md warns about this


def test_folder_that_isnt_a_git_clone_still_starts(rig):
    rig.at_commit(None)
    result = rig.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert rig.history == []


# --- DEPLOY.md's restore steps ------------------------------------------------------------

def restore_line(marker: str) -> str:
    [line] = [line for line in deploy_md().splitlines() if line.startswith("sqlite3 ") and marker in line]
    return line.split("  #")[0]


async def test_restore_pause_line_matches_the_bots_pause(tmp_path):
    (tmp_path / "data").mkdir()
    path = tmp_path / "data" / "first-in.db"
    db = await Database.open(str(path))
    await db.close()

    line = restore_line(maintenance.PAUSED_KEY)
    assert subprocess.run([BASH, "-c", line], cwd=tmp_path).returncode == 0
    db = await Database.open(str(path))
    try:
        since = await maintenance.paused_since(db)
        stored = await db.kv_get(maintenance.PAUSED_KEY)
    finally:
        await db.close()
    assert since is not None and abs(since - utcnow()) < timedelta(minutes=1)
    assert stored == to_iso(since)              # the same format /pause writes


async def test_restore_safety_copy_has_a_timestamp(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "backups").mkdir()
    db = await Database.open(str(tmp_path / "data" / "first-in.db"))
    await db.close()
    line = restore_line("before-restore")
    assert subprocess.run([BASH, "-c", line], cwd=tmp_path).returncode == 0
    [made] = [p.name for p in (tmp_path / "backups").iterdir()]
    assert re.fullmatch(r"before-restore-\d{8}-\d{6}\.db", made)
