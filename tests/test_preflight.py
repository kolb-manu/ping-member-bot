"""The preflight checks (`python -m first_in_bot check`) against fakes: no network,
and never the real .env (python-dotenv would find one by walking up the folders,
so main() is never called here)."""
import dataclasses
import os
import runpy
import sys
from types import SimpleNamespace

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetChatMember, GetMe
from aiogram.types import User as TgUser

from first_in_bot import preflight, stripe_gateway
from first_in_bot.db import MIGRATIONS, Database, kv_set
from first_in_bot.preflight import Check, Status
from tests.fakes import add_user, make_config
from tests.test_config import GOOD, TOKEN

STRIPE_KEY = "rk_test_" + "x" * 20
BOT_ID = 4242


def statuses(checks: list[Check]) -> list[tuple[str, Status]]:
    return [(c.name, c.status) for c in checks]


class FakeBot:
    def __init__(self, username: str = "FirstInTestBot", error: Exception | None = None):
        self.username = username
        self.error = error

    async def get_me(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=BOT_ID, username=self.username)


class FakeRights:
    def __init__(self, problems: list[str] | None = None):
        self.problems = problems or []
        self.asked: list[int] = []

    async def bot_rights_problems(self, bot_id: int) -> list[str]:
        self.asked.append(bot_id)
        return list(self.problems)


class FakePrice:
    def __init__(self, label: str | None = "£2.99/month"):
        self.label = label
        self.asked: list[str] = []

    async def price_label(self, price_id: str) -> str | None:
        self.asked.append(price_id)
        return self.label


class FakeSession(BaseSession):
    """Answers getMe and getChatMember the way Telegram would, and records close()."""

    def __init__(self, username: str = "FirstInTestBot", get_me_error: Exception | None = None,
                 member_error: Exception | None = None) -> None:
        super().__init__()
        self.username = username
        self.get_me_error = get_me_error
        self.member_error = member_error
        self.requests: list = []
        self.closed = False

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            if self.get_me_error is not None:
                raise self.get_me_error
            return TgUser(id=BOT_ID, is_bot=True, first_name="First In", username=self.username)
        if isinstance(method, GetChatMember):
            if self.member_error is not None:
                raise self.member_error
            return SimpleNamespace(status="administrator", can_invite_users=True, can_restrict_members=True)
        raise AssertionError(f"preflight made an unexpected call: {type(method).__name__}")

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield b""

    async def close(self) -> None:
        self.closed = True


class FakeStripeClient:
    """Just enough of stripe.StripeClient for StripeGateway.price_label."""

    def __init__(self) -> None:
        self.v1 = SimpleNamespace(prices=SimpleNamespace(retrieve=self._retrieve))

    @staticmethod
    def _retrieve(price_id):
        return {"id": price_id, "unit_amount": 299, "currency": "gbp", "recurring": {"interval": "month"}}


@pytest.fixture(autouse=True)
def no_real_clients(monkeypatch):
    """Fail loudly if a test forgets a fake and would reach Telegram or Stripe."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use a real Telegram or Stripe client")

    monkeypatch.setattr(stripe_gateway.stripe, "StripeClient", refuse)
    monkeypatch.setattr(AiohttpSession, "make_request", refuse)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Run in an empty folder, so relative paths (data/, backups/) land in tmp."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- .env and backups folder ---------------------------------------------------

def test_env_file_permissions(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=\n")
    env.chmod(0o600)
    assert preflight.check_env_file(str(env)).status is Status.PASS
    env.chmod(0o644)
    loose = preflight.check_env_file(str(env))
    assert loose.status is Status.WARN and "chmod 600" in loose.detail
    assert preflight.check_env_file(str(tmp_path / "missing")).status is Status.WARN
    assert preflight.check_env_file("").status is Status.WARN


def test_backup_dir_checks(tmp_path):
    backups = tmp_path / "backups"
    missing = preflight.check_backup_dir(str(backups))
    assert missing.status is Status.WARN and "mkdir -m 700" in missing.detail

    backups.mkdir(mode=0o700)
    backups.chmod(0o700)
    assert preflight.check_backup_dir(str(backups)).status is Status.PASS
    assert os.listdir(backups) == []          # the write probe is cleaned up

    backups.chmod(0o755)
    assert preflight.check_backup_dir(str(backups)).status is Status.WARN

    a_file = tmp_path / "not-a-folder"
    a_file.write_text("")
    assert preflight.check_backup_dir(str(a_file)).status is Status.FAIL


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write anywhere")
def test_backup_dir_not_writable_fails(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    backups.chmod(0o500)
    try:
        check = preflight.check_backup_dir(str(backups))
    finally:
        backups.chmod(0o700)
    assert check.status is Status.FAIL and "can't write" in check.detail


# --- Database --------------------------------------------------------------------

async def test_database_created_then_reopened(tmp_path):
    path = tmp_path / "data" / "first-in.db"
    first = await preflight.check_database(str(path))
    assert first.status is Status.PASS
    assert "new, empty database created" in first.detail
    assert f"schema version {len(MIGRATIONS)} of {len(MIGRATIONS)}" in first.detail
    assert path.exists()

    db = await Database.open(str(path))
    await add_user(db, 5)
    await db.close()
    again = await preflight.check_database(str(path))
    assert again.status is Status.PASS and f"1 user in {path}" in again.detail


async def test_database_newer_than_code_fails(tmp_path):
    # What a rollback past a migration looks like: the old code refuses the database.
    path = str(tmp_path / "first-in.db")
    db = await Database.open(path)
    async with db.tx() as conn:
        await kv_set(conn, "schema_version", str(len(MIGRATIONS) + 1))
    await db.close()
    check = await preflight.check_database(path)
    assert check.status is Status.FAIL and "newer than this code" in check.detail


async def test_database_that_cant_be_opened_fails(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("")                    # a file where the data folder should be
    check = await preflight.check_database(str(blocker / "first-in.db"))
    assert check.status is Status.FAIL and "couldn't open" in check.detail


# --- Telegram --------------------------------------------------------------------

async def test_telegram_token_and_channel_rights_pass():
    rights = FakeRights()
    checks = await preflight.check_telegram(FakeBot(), rights, make_config())
    assert statuses(checks) == [("Telegram", Status.PASS), ("Paid channel", Status.PASS)]
    assert "@FirstInTestBot" in checks[0].detail
    assert rights.asked == [BOT_ID]


async def test_telegram_username_match_ignores_case():
    checks = await preflight.check_telegram(FakeBot("firstintestbot"), FakeRights(), make_config())
    assert checks[0].status is Status.PASS


async def test_token_of_another_bot_fails_before_the_channel_check():
    rights = FakeRights()
    [check] = await preflight.check_telegram(FakeBot("FixrAlertsBot"), rights, make_config())
    assert check.status is Status.FAIL
    assert "@FixrAlertsBot" in check.detail and "FirstInTestBot" in check.detail
    assert rights.asked == []


async def test_rejected_token_fails():
    error = TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
    [check] = await preflight.check_telegram(FakeBot(error=error), FakeRights(), make_config())
    assert check.status is Status.FAIL and "BOT_TOKEN" in check.detail


async def test_network_error_fails_without_showing_the_token():
    error = TelegramNetworkError(method=GetMe(), message=f"timeout on https://api.telegram.org/bot{TOKEN}/getMe")
    [check] = await preflight.check_telegram(FakeBot(error=error), FakeRights(), make_config())
    assert check.status is Status.FAIL and "couldn't reach Telegram" in check.detail
    assert TOKEN not in check.detail and TOKEN.split(":")[1] not in check.detail


async def test_each_channel_rights_problem_fails():
    problems = ["the bot lacks the 'Invite users via link' right", "the bot lacks the 'Ban users' right"]
    checks = await preflight.check_telegram(FakeBot(), FakeRights(problems), make_config())
    assert statuses(checks) == [("Telegram", Status.PASS), ("Paid channel", Status.FAIL),
                                ("Paid channel", Status.FAIL)]
    assert [c.detail for c in checks[1:]] == problems


# --- Stripe, removals --------------------------------------------------------------

async def test_stripe_not_configured_is_a_warning():
    check = await preflight.check_stripe(make_config(STRIPE_API_KEY="", STRIPE_PRICE_ID=""), None)
    assert check.status is Status.WARN and "not configured" in check.detail
    check = await preflight.check_stripe(make_config(STRIPE_PRICE_ID=""), FakePrice())
    assert check.status is Status.WARN and "STRIPE_PRICE_ID" in check.detail


async def test_stripe_price_readable_passes():
    price = FakePrice()
    check = await preflight.check_stripe(make_config(), price)
    assert check.status is Status.PASS
    assert check.detail == "TEST mode, price_123 is £2.99/month"
    assert price.asked == ["price_123"]


async def test_stripe_price_unreadable_fails():
    check = await preflight.check_stripe(make_config(), FakePrice(label=None))
    assert check.status is Status.FAIL and "price_123" in check.detail


async def test_stripe_live_key_fails_and_is_never_shown():
    live = "sk_live_" + "y" * 20
    config = dataclasses.replace(make_config(), stripe_api_key=live)   # load_config would refuse it
    price = FakePrice()
    check = await preflight.check_stripe(config, price)
    assert check.status is Status.FAIL and live not in check.detail
    assert price.asked == []


def test_removals_shown():
    assert preflight.check_removals(make_config()).status is Status.PASS
    assert "REMOVALS_ENABLED=false (dry run" in preflight.check_removals(make_config()).detail
    on = preflight.check_removals(make_config(REMOVALS_ENABLED="true"))
    assert on.status is Status.WARN and "REMOVALS_ENABLED=true" in on.detail


def test_scrub_hides_tokens_and_keys():
    text = f"bot{TOKEN}/getMe with {STRIPE_KEY} and sk_live_abc123"
    scrubbed = preflight.scrub(text)
    assert TOKEN not in scrubbed and STRIPE_KEY not in scrubbed and "sk_live_abc123" not in scrubbed


# --- The whole run -------------------------------------------------------------------

async def test_run_checks_in_order(workdir):
    checks = await preflight.run_checks(make_config(), FakeBot(), FakeRights(), FakePrice(), "backups")
    assert [c.name for c in checks] == ["Database", "Telegram", "Paid channel", "Stripe", "Removals", "Backups"]


def test_report_counts_and_verdict(capsys):
    ok = [Check(Status.PASS, "A", "fine"), Check(Status.WARN, "B", "hmm")]
    assert preflight.print_report(ok) is True
    assert preflight.print_report([*ok, Check(Status.FAIL, "C", "broken")]) is False
    out = capsys.readouterr().out
    assert "  [WARN] B: hmm" in out and "  [FAIL] C: broken" in out
    assert "1 passed, 1 warning, 1 failed" in out


async def test_invalid_config_exits_2_and_lists_problems(workdir, capsys):
    env = {**GOOD, "BOT_TOKEN": "not-a-real-token-but-still-private", "ADMIN_IDS": ""}
    assert await preflight.run(env, "") == 2
    out = capsys.readouterr().out
    assert "[FAIL] Config: BOT_TOKEN doesn't look like" in out and "[FAIL] Config: ADMIN_IDS is missing" in out
    assert "not-a-real-token-but-still-private" not in out
    assert not (workdir / "data").exists()    # nothing opened with a bad config


async def test_full_run_passes_with_real_gateways_and_closes_the_session(workdir, capsys):
    env_file = workdir / ".env"
    env_file.write_text("")
    env_file.chmod(0o600)
    (workdir / "backups").mkdir(mode=0o700)
    (workdir / "backups").chmod(0o700)
    env = {**GOOD, "STRIPE_API_KEY": STRIPE_KEY, "STRIPE_PRICE_ID": "price_123"}
    session = FakeSession()
    code = await preflight.run(env, str(env_file), session=session, stripe_client=FakeStripeClient())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[FAIL]" not in out and "[WARN]" not in out
    assert "[PASS] Stripe: TEST mode, price_123 is £2.99/month" in out
    assert "[PASS] Removals: REMOVALS_ENABLED=false" in out
    assert "8 passed, 0 warnings, 0 failed" in out
    assert TOKEN not in out and TOKEN.split(":")[1] not in out and STRIPE_KEY not in out
    assert session.closed
    # Only lookups: never getUpdates, so a running copy isn't disturbed.
    assert {type(m) for m in session.requests} == {GetMe, GetChatMember}
    assert (workdir / "data" / "first-in.db").exists()


async def test_failed_check_exits_1_and_still_closes_the_session(workdir, capsys):
    session = FakeSession(get_me_error=TelegramUnauthorizedError(method=GetMe(), message="Unauthorized"))
    assert await preflight.run(dict(GOOD), "", session=session) == 1
    out = capsys.readouterr().out
    assert "[FAIL] Telegram" in out and "[WARN] Stripe: not configured" in out
    assert session.closed


async def test_session_closed_even_if_a_check_crashes(workdir):
    session = FakeSession(member_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await preflight.preflight(make_config(STRIPE_API_KEY="", STRIPE_PRICE_ID=""), session=session)
    assert session.closed


async def test_backup_dir_comes_from_config_when_it_has_one(workdir, monkeypatch):
    seen = []
    real = preflight.check_backup_dir

    def spy(path):
        seen.append(path)
        return real(path)

    class WithBackupDir:
        def __init__(self, config):
            self._config = config
            self.backup_dir = "elsewhere"

        def __getattr__(self, name):
            return getattr(self._config, name)

    config = make_config(STRIPE_API_KEY="", STRIPE_PRICE_ID="")
    monkeypatch.setattr(preflight, "check_backup_dir", spy)
    await preflight.preflight(config, session=FakeSession())
    await preflight.preflight(WithBackupDir(config), session=FakeSession())
    assert seen == ["backups", "elsewhere"]


# --- Entry point ----------------------------------------------------------------------

@pytest.fixture
def entry(monkeypatch):
    import first_in_bot.main as bot_main
    called: list[str] = []
    monkeypatch.setattr(bot_main, "main", lambda: called.append("bot"))
    monkeypatch.setattr(preflight, "main", lambda: called.append("check"))

    def run_module(*args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["first_in_bot", *args])
        runpy.run_module("first_in_bot", run_name="__main__")

    return run_module, called


def test_plain_run_starts_the_bot(entry):
    run_module, called = entry
    run_module()
    assert called == ["bot"]


def test_check_runs_the_preflight(entry):
    run_module, called = entry
    run_module("check")
    assert called == ["check"]


@pytest.mark.parametrize("args", [("chek",), ("check", "now"), ("run",)])
def test_anything_else_is_refused_rather_than_starting_the_bot(entry, capsys, args):
    run_module, called = entry
    with pytest.raises(SystemExit) as exc:
        run_module(*args)
    assert exc.value.code == 2 and called == []
    assert "Usage" in capsys.readouterr().err
