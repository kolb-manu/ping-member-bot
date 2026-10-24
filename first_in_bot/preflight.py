"""Preflight checks, run with `python -m first_in_bot check`. setup.sh runs it
before (re)starting the service, and the owner can run it by hand at any time.

It does what startup does (config, database migrations, the token, the paid-channel
rights, the Stripe price) and prints a PASS / WARN / FAIL list instead of starting.
It never polls Telegram, so it is safe to run while the bot is up. It holds the
single-instance lock (lock.py) while it runs, so the bot can't start half way through.
If the bot already holds it, the database check is skipped: opening the database
runs migrations, and after a git pull those mustn't run under the old code. setup.sh
stops the bot first, so it always gets the full check.

It can only see a copy running on this machine. A copy elsewhere (the Mac) could
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
from typing import Any, Mapping, Protocol

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import ClientDecodeError, TelegramAPIError, TelegramUnauthorizedError
from dotenv import find_dotenv, load_dotenv

from .config import Config, ConfigError, load_config
from .db import MIGRATIONS, Database
from .lock import AlreadyRunning, InstanceLock, acquire_instance_lock, lock_path_for
from .stripe_gateway import StripeGateway
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


def check_not_running(db_path: str) -> tuple[Check, InstanceLock | None]:
    """The lock, if no copy of the bot holds it here; the caller releases it."""
    try:
        lock = acquire_instance_lock(lock_path_for(db_path))
    except AlreadyRunning as exc:
        return Check(Status.WARN, "Running copy",
                     f"{scrub(str(exc))} The database check is skipped so new migrations don't run under "
                     "it (setup.sh stops the service first)."), None
    except OSError as exc:
        return Check(Status.FAIL, "Running copy", f"couldn't create the lock file: {_describe(exc)}"), None
    return Check(Status.PASS, "Running copy", "no other copy is running on this machine (one on your Mac "
                                              "can't be seen from here)"), lock


async def check_database(db_path: str) -> Check:
    """Opening runs any new migrations, exactly as startup would."""
    where = db_path if db_path == ":memory:" else os.path.abspath(db_path)
    existed = db_path == ":memory:" or os.path.exists(db_path)
    try:
        db = await Database.open(db_path)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        # RuntimeError: the schema is newer than this code (e.g. after a rollback).
        return Check(Status.FAIL, "Database", f"couldn't open {where}: {_describe(exc)}")
    try:
        version = await db.kv_get("schema_version")
        row = await db.fetchone("SELECT COUNT(*) AS n FROM users")
    finally:
        await db.close()
    state = f"{_plural(row['n'], 'user')} in" if existed else "new, empty database created at"
    return Check(Status.PASS, "Database", f"{state} {where} (schema version {version} of {len(MIGRATIONS)})")


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

async def run_checks(config: Config, bot: BotIdentity, tg: ChannelRights,
                     stripe_gw: PriceReader | None, backup_dir: str) -> list[Check]:
    running, lock = check_not_running(config.db_path)
    checks = [running]
    try:
        if lock is not None:
            checks.append(await check_database(config.db_path))
        checks += await check_telegram(bot, tg, config)
        checks.append(await check_stripe(config, stripe_gw))
        checks.append(check_removals(config))
        checks.append(check_backup_dir(backup_dir))
    finally:
        if lock is not None:
            lock.release()
    return checks


async def preflight(config: Config, *, session: BaseSession | None = None,
                    stripe_client: Any | None = None) -> list[Check]:
    """The real gateways, as main.run builds them. session and stripe_client are for tests."""
    bot = Bot(config.bot_token, session=session)
    tg = TelegramGateway(bot, config.paid_channel_id)
    stripe_gw = StripeGateway(config.stripe_api_key, client=stripe_client) if config.stripe_api_key else None
    backup_dir = config.backup_dir
    try:
        return await run_checks(config, bot, tg, stripe_gw, backup_dir)
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
