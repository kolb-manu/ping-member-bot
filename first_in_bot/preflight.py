"""Preflight checks, run with `python -m first_in_bot check`. setup.sh runs it
before (re)starting the service, and the owner can run it by hand at any time.

It checks what startup needs (config, the database, the token, the paid-channel
rights, the Stripe price and what the Stripe key may read) and prints a
PASS / WARN / FAIL list instead of starting. It changes nothing, so it is safe to
run while the bot is up: it never polls Telegram, it only reads from Stripe, and
it opens the database read-only. In particular it never runs migrations; the bot
does that when it starts. After a git pull, a check that FAILs must leave the
database as the old code left it, so the old code can still be started on it.

It can only see a copy running on this machine (it tries the single-instance
lock, lock.py, and lets go straight away). A copy elsewhere (the Mac) could
only be probed with getUpdates, and Telegram settles a clash by ending the OLDER
long poll in favour of the newer call, so a probe would cut off a running bot
rather than detect it. DEPLOY.md tells the owner to stop the Mac copy first instead.

Each check takes its gateways as arguments, so the tests run them against fakes.
Secrets are never printed, and error text from the APIs is scrubbed of anything
that looks like a token or key. Exit code: 0 if nothing FAILed, 1 if something
did, 2 if the config itself is invalid.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import re
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

import stripe
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import ClientDecodeError, TelegramAPIError, TelegramUnauthorizedError
from dotenv import find_dotenv, load_dotenv

from .config import Config, ConfigError, load_config
from .db import MIGRATIONS
from .lock import AlreadyRunning, acquire_instance_lock, lock_path_for
from .stripe_gateway import StripeGateway, make_client
from .telegram_gateway import TelegramGateway

# Bot tokens (also inside .../bot<token>/getMe URLs) and Stripe keys.
_SECRET_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}|\b(?:sk|rk|pk)_(?:test|live)_\S+")
_MAX_DETAIL = 200


class Status(enum.Enum):
    PASS = "PASS"
    WARN = "WARN"     # works, but the owner should know
    FAIL = "FAIL"     # the bot would not do its job; setup.sh stops here


@dataclass(frozen=True)
class Check:
    status: Status
    name: str
    detail: str


class BotIdentity(Protocol):
    async def get_me(self) -> Any: ...


class ChannelRights(Protocol):
    async def bot_rights_problems(self, bot_id: int) -> list[str]: ...


class PriceReader(Protocol):
    async def price_label(self, price_id: str) -> str | None: ...


class StripeLister(Protocol):
    """Lists at most one object of a kind; raises the stripe library's errors."""
    async def list_one(self, resource: str, expand: tuple[str, ...] = ()) -> None: ...


@dataclass(frozen=True)
class StripeProbe:
    resource: str               # as in client.v1.<resource>, e.g. "checkout.sessions"
    expand: tuple[str, ...]
    needs: str                  # what to give the restricted key if Stripe refuses
    shows: str                  # for the messages


# Read-only calls, one for each kind of object the bot's own calls use. A list only
# proves read access; a write permission (creating a checkout or a customer) can't
# be tested without creating something, so for those the names say "write" because
# that is what the key needs, and write includes read.
STRIPE_PROBES = (
    StripeProbe("subscriptions", (), "Subscriptions read", "subscriptions"),
    # The daily disputes check expands each dispute's charge to find the customer.
    StripeProbe("disputes", ("data.charge",), "Disputes read and Charges read", "disputes with their charges"),
    StripeProbe("checkout.sessions", (), "Checkout Sessions write", "checkout sessions"),
    StripeProbe("customers", (), "Customers write", "customers"),
)


def scrub(text: str) -> str:
    return _SECRET_RE.sub("[hidden]", text)


def _describe(exc: BaseException) -> str:
    # aiogram errors carry .message; ClientDecodeError's str() is the whole response body.
    text = getattr(exc, "message", None) or str(exc)
    return f"{type(exc).__name__}: {scrub(text)[:_MAX_DETAIL]}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _others_can_read(path: str) -> bool:
    return bool(stat.S_IMODE(os.stat(path).st_mode) & 0o077)


# --- The checks -----------------------------------------------------------------

def check_env_file(path: str) -> Check:
    if not path or not os.path.isfile(path):
        return Check(Status.WARN, ".env", "no .env file found, so only the environment's own variables are used")
    if _others_can_read(path):
        return Check(Status.WARN, ".env", f"other users on this machine can read {path}: run chmod 600 .env")
    return Check(Status.PASS, ".env", f"{path} (only you can read it)")


def check_not_running(db_path: str) -> Check:
    """Only looks: the lock is let go straight away, because nothing here writes."""
    try:
        acquire_instance_lock(lock_path_for(db_path)).release()
    except AlreadyRunning as exc:
        return Check(Status.WARN, "Running copy",
                     f"{scrub(str(exc))} The check itself only reads, so it's safe next to it "
                     "(setup.sh stops the service before starting the new one).")
    except OSError as exc:
        return Check(Status.FAIL, "Running copy", f"couldn't create the lock file: {_describe(exc)}")
    return Check(Status.PASS, "Running copy", "no other copy is running on this machine (one on your Mac "
                                              "can't be seen from here)")


def _read_database(path: str) -> tuple[int, int | None]:
    """(schema version, number of users or None if there's no users table yet)."""
    # mode=ro: SQLite itself refuses every write, and a missing file is an error
    # rather than a new empty database.
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = 0
        if "kv" in tables:
            row = conn.execute("SELECT value FROM kv WHERE key='schema_version'").fetchone()
            version = int(row[0]) if row else 0
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] if "users" in tables else None
    finally:
        conn.close()
    return version, users


def _cant_create(path: str) -> str | None:
    """Why the bot couldn't create a database at path, or None if it could."""
    folder = os.path.dirname(path)
    while not os.path.exists(folder):     # Database.open makes the missing folders
        folder = os.path.dirname(folder)
    if not os.path.isdir(folder):
        return f"{folder} is not a folder"
    if not os.access(folder, os.W_OK | os.X_OK):
        return f"can't write to {folder}"
    return None


async def check_database(db_path: str) -> Check:
    """Read-only, and never migrates: the bot runs the migrations when it starts.
    So a FAIL further down leaves the database as the code before a git pull left
    it, and that code can still be started on it."""
    if db_path == ":memory:":
        return Check(Status.PASS, "Database", "in memory, so nothing is kept when the bot stops")
    where = os.path.abspath(db_path)
    if not os.path.exists(where):
        problem = _cant_create(where)
        if problem:
            return Check(Status.FAIL, "Database", f"there's no database at {where} and the bot can't "
                                                  f"create one: {problem}")
        return Check(Status.PASS, "Database", f"none yet: a new database will be created at {where} "
                                              "when the bot starts")
    try:
        version, users = await asyncio.to_thread(_read_database, where)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return Check(Status.FAIL, "Database", f"couldn't read {where}: {_describe(exc)}")
    latest = len(MIGRATIONS)
    if version > latest:
        # The bot would refuse to start (db.run_migrations).
        return Check(Status.FAIL, "Database",
                     f"the schema of {where} (version {version}) is newer than this code ({latest}). "
                     "That happens after rolling back past a migration: restore the before-deploy copy "
                     "from the newer version's deploy (DEPLOY.md, Roll back), or go back to the newer code")
    state = f"{_plural(users, 'user')} in {where}" if users is not None else f"an empty database at {where}"
    pending = latest - version
    if pending:
        return Check(Status.PASS, "Database", f"{state} (schema version {version} of {latest}: "
                                              f"{_plural(pending, 'migration')} will run when the bot starts)")
    return Check(Status.PASS, "Database", f"{state} (schema version {version} of {latest})")


async def check_telegram(bot: BotIdentity, tg: ChannelRights, config: Config) -> list[Check]:
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        return [Check(Status.FAIL, "Telegram", "Telegram rejected BOT_TOKEN (mistyped, or revoked in @BotFather)")]
    except (TelegramAPIError, ClientDecodeError) as exc:
        return [Check(Status.FAIL, "Telegram", f"couldn't reach Telegram: {_describe(exc)}")]
    username = me.username or ""
    if username.lower() != config.bot_username.lower():
        # Most likely the alert bot's token, which would take over that bot's updates.
        return [Check(Status.FAIL, "Telegram",
                      f"this token belongs to @{username}, but BOT_USERNAME is {config.bot_username}. "
                      "Use the member bot's own token, not the alert bot's")]
    checks = [Check(Status.PASS, "Telegram", f"token works for @{username}")]
    problems = await tg.bot_rights_problems(me.id)
    checks += [Check(Status.FAIL, "Paid channel", scrub(p)) for p in problems]
    if not problems:
        checks.append(Check(Status.PASS, "Paid channel",
                            "the bot is an admin with the Invite users and Ban users rights"))
    return checks


async def check_stripe(config: Config, stripe_gw: PriceReader | None) -> Check:
    if not config.stripe_api_key or stripe_gw is None:
        return Check(Status.WARN, "Stripe", "not configured: the trial and subscribe buttons say "
                                            "payments aren't set up yet")
    if not config.stripe_price_id:
        return Check(Status.WARN, "Stripe", "STRIPE_PRICE_ID is empty, so payments are off")
    # load_config already refuses live keys; this is the last line before a deploy.
    if not config.stripe_api_key.startswith(("sk_test_", "rk_test_")):
        return Check(Status.FAIL, "Stripe", "STRIPE_API_KEY is not a test-mode key")
    label = await stripe_gw.price_label(config.stripe_price_id)
    if label is None:
        return Check(Status.FAIL, "Stripe",
                     f"TEST mode, but couldn't read the price {config.stripe_price_id}: check "
                     "STRIPE_PRICE_ID, and that the key is valid and may read prices")
    return Check(Status.PASS, "Stripe", f"TEST mode, {config.stripe_price_id} is {label}")


async def check_stripe_permissions(lister: StripeLister) -> list[Check]:
    """A FAIL for each permission the restricted key lacks. Only lists, so nothing
    is ever created; on a network error it stops after one WARN."""
    checks = []
    for probe in STRIPE_PROBES:
        try:
            await lister.list_one(probe.resource, probe.expand)
        except stripe.PermissionError:
            checks.append(Check(Status.FAIL, "Stripe key",
                                f"the key may not read {probe.shows}: give it {probe.needs} (Stripe "
                                "dashboard > Developers > API keys, edit the restricted key)"))
        except stripe.StripeError as exc:
            # Mostly the network; Stripe's own errors (rate limit, 500) land here too.
            checks.append(Check(Status.WARN, "Stripe key",
                                f"couldn't check what the key may read: {_describe(exc)}"))
            return checks
    if not checks:
        checks.append(Check(Status.PASS, "Stripe key",
                            "it may read subscriptions, disputes with their charges, checkout sessions "
                            "and customers (whether it may also create them can't be checked without "
                            "creating one)"))
    return checks


def check_removals(config: Config) -> Check:
    if config.removals_enabled:
        return Check(Status.WARN, "Removals",
                     f"REMOVALS_ENABLED=true (the sweep really removes people, "
                     f"at most {config.max_removals_per_sweep} per sweep)")
    return Check(Status.PASS, "Removals", "REMOVALS_ENABLED=false (dry run: nobody is removed)")


def check_backup_dir(path: str) -> Check:
    if not os.path.exists(path):
        return Check(Status.WARN, "Backups", f"{path} doesn't exist yet: run mkdir -m 700 {path} "
                                             "(setup.sh does this)")
    if not os.path.isdir(path):
        return Check(Status.FAIL, "Backups", f"{path} is not a folder")
    # Actually write a file: os.access can say yes on a read-only mount.
    try:
        fd, probe = tempfile.mkstemp(prefix=".preflight-", dir=path)
    except OSError as exc:
        return Check(Status.FAIL, "Backups", f"can't write to {path} ({exc.strerror})")
    os.close(fd)
    os.remove(probe)
    if _others_can_read(path):
        return Check(Status.WARN, "Backups", f"the {path} folder is writable, but other users can "
                                             f"read it: run chmod 700 {path}")
    return Check(Status.PASS, "Backups", f"the {path} folder is writable")


# --- Wiring ---------------------------------------------------------------------

class ClientLister:
    """The real StripeLister, on a stripe.StripeClient (client.v1.<resource>.list)."""

    def __init__(self, client: Any):
        self._client = client

    async def list_one(self, resource: str, expand: tuple[str, ...] = ()) -> None:
        service = self._client.v1
        for part in resource.split("."):
            service = getattr(service, part)
        params: dict[str, Any] = {"limit": 1}
        if expand:
            params["expand"] = list(expand)
        # The library is synchronous, as in StripeGateway.
        await asyncio.to_thread(service.list, params)


async def run_checks(config: Config, bot: BotIdentity, tg: ChannelRights, stripe_gw: PriceReader | None,
                     backup_dir: str, stripe_lister: StripeLister | None = None) -> list[Check]:
    checks = [check_not_running(config.db_path), await check_database(config.db_path)]
    checks += await check_telegram(bot, tg, config)
    stripe_check = await check_stripe(config, stripe_gw)
    checks.append(stripe_check)
    # Only once the key and price are known to work, so one problem gives one FAIL.
    if stripe_check.status is Status.PASS and stripe_lister is not None:
        checks += await check_stripe_permissions(stripe_lister)
    checks.append(check_removals(config))
    checks.append(check_backup_dir(backup_dir))
    return checks


async def preflight(config: Config, *, session: BaseSession | None = None,
                    stripe_client: Any | None = None) -> list[Check]:
    """The real gateways, as main.run builds them. session and stripe_client are for tests."""
    bot = Bot(config.bot_token, session=session)
    tg = TelegramGateway(bot, config.paid_channel_id)
    stripe_gw = lister = None
    if config.stripe_api_key:
        # One client for the price and the permission probes, the same as the bot's.
        client = stripe_client or make_client(config.stripe_api_key)
        stripe_gw, lister = StripeGateway(config.stripe_api_key, client=client), ClientLister(client)
    backup_dir = config.backup_dir
    try:
        return await run_checks(config, bot, tg, stripe_gw, backup_dir, lister)
    finally:
        await bot.session.close()


def print_report(checks: list[Check]) -> bool:
    for c in checks:
        print(f"  [{c.status.value}] {c.name}: {c.detail}")
    counts = {s: sum(1 for c in checks if c.status is s) for s in Status}
    print(f"\n{counts[Status.PASS]} passed, {_plural(counts[Status.WARN], 'warning')}, "
          f"{counts[Status.FAIL]} failed")
    return counts[Status.FAIL] == 0


async def run(env: Mapping[str, str], env_path: str, *, session: BaseSession | None = None,
              stripe_client: Any | None = None) -> int:
    print("First In member bot: preflight check")
    env_check = check_env_file(env_path)
    try:
        config = load_config(env)
    except ConfigError as exc:
        print_report([env_check, *(Check(Status.FAIL, "Config", p) for p in exc.problems)])
        print("Fix these in .env (nano .env), then run the check again.")
        return 2
    checks = [env_check, Check(Status.PASS, "Config", "all values are valid")]
    checks += await preflight(config, session=session, stripe_client=stripe_client)
    if print_report(checks):
        print("Ready to run.")
        return 0
    print("Fix the FAIL lines above, then run the check again.")
    return 1


def main() -> None:
    # Only warnings: the gateways' own reasons (e.g. Stripe's "No such price") are
    # printed before the list. Their log messages never contain keys.
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    # Found the same way main.main's load_dotenv() finds it: up from this package.
    env_path = find_dotenv()
    if env_path:
        load_dotenv(env_path)
    sys.exit(asyncio.run(run(os.environ, env_path)))
