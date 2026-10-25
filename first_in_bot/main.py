"""Entry point: load config, open the DB, wire up gateways, handlers and the
background jobs, then long-poll Telegram until stopped. A lock file next to the
database (lock.py) makes sure only one copy runs on this machine.

Shutdown (SIGTERM from systemd, or Ctrl+C): aiogram's start_polling handles both
signals by stopping polling and returning, without waiting for updates that are
still being handled (Telegram already counts those as delivered and won't send
them again). So, in this order: the scheduler lets a job that's mid-run finish
(bounded, see scheduler.STOP_TIMEOUT) while, at the same time, those handlers get
up to HANDLER_DRAIN_TIMEOUT to finish; then the bot session closes (only now, as
both may still be sending messages), and the DB closes.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from .alerts import alert_owner
from .billing import read_price
from .config import ConfigError, Config, load_config
from .db import Database
from .handlers import admin, billing, channel, start
from .handlers.middleware import UnblockMiddleware
from .lock import AlreadyRunning, acquire_instance_lock, lock_path_for, unlocked_copies, unlocked_copy_text
from .report import mode_lines, state_lines
from .scheduler import Scheduler, build_jobs
from .services import Services
from .stripe_gateway import StripeGateway
from .telegram_gateway import TelegramGateway

log = logging.getLogger("first_in_bot")

# Telegram doesn't send chat_member updates unless asked.
ALLOWED_UPDATES = ["message", "callback_query", "chat_join_request", "chat_member"]
# How long updates still being handled at shutdown get to finish. It runs alongside
# the scheduler's STOP_TIMEOUT (25 s), so both fit in systemd's TimeoutStopSec=45.
HANDLER_DRAIN_TIMEOUT = 15.0


async def build_stripe(config: Config) -> tuple[StripeGateway | None, str | None]:
    """The Stripe gateway (None if not configured) and the price label for display."""
    if not config.stripe_api_key:
        log.warning("==== STRIPE: NOT CONFIGURED. Trial and subscribe buttons explain payments aren't set up. ====")
        return None, None
    if not config.stripe_price_id:
        log.warning("==== STRIPE: STRIPE_PRICE_ID is empty, so payments are OFF. ====")
        return None, None
    log.warning("==== STRIPE: %s MODE ====", config.stripe_mode)
    gateway = StripeGateway(config.stripe_api_key)
    label = await read_price(gateway, config.stripe_price_id)
    if label is None:
        log.warning("Couldn't read the Stripe price %s; trial and subscribe checkouts are held back "
                    "until it can be read (tried again every 5 minutes)", config.stripe_price_id)
    else:
        log.info("Stripe price: %s", label)
    return gateway, label


def build_dispatcher(svc: Services) -> Dispatcher:
    # No price_label here: it would be fixed at startup. Handlers read svc.price_label.
    dp = Dispatcher(db=svc.db, config=svc.config, tg=svc.tg, stripe_gw=svc.stripe, svc=svc)
    unblock = UnblockMiddleware()
    dp.message.outer_middleware(unblock)
    dp.callback_query.outer_middleware(unblock)
    # billing goes after start and holds the catch-all that answers stray buttons.
    dp.include_routers(start.router, admin.router, billing.router, channel.router)
    return dp


def startup_text(svc: Services, rights_problems: list[str], state: list[str] | None = None) -> str:
    lines = ["🟢 <b>First In member bot started</b>", *mode_lines(svc), *(state or [])]
    if rights_problems:
        lines.append("⚠️ Paid channel setup:")
        lines += [f"• {html.escape(problem)}" for problem in rights_problems]
    else:
        lines.append("Paid channel rights: OK")
    return "\n".join(lines)


async def drain_updates(dp: Dispatcher, timeout: float) -> None:
    """Wait up to timeout seconds for updates still being handled once polling has
    stopped, then cancel the rest, so none of them is cut off by the session or the
    DB closing under it."""
    # aiogram 3.31 runs each update as a task in this set and never waits for them
    # on shutdown. getattr: it's private, so a later aiogram may not have it.
    tasks = [task for task in getattr(dp, "_handle_update_tasks", ()) if not task.done()]
    if not tasks:
        return
    log.info("Stopping: waiting up to %ss for %d update(s) still being handled", timeout, len(tasks))
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        log.warning("%d update(s) didn't finish within %ss; cancelling them", len(pending), timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def stop_scheduler(scheduler: Scheduler, task: asyncio.Task | None) -> None:
    if task is None:
        return
    await scheduler.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def run(config: Config) -> None:
    db = await Database.open(config.db_path)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    tg = TelegramGateway(bot, config.paid_channel_id)
    stripe_gw, price_label = await build_stripe(config)
    svc = Services(db=db, config=config, tg=tg, stripe=stripe_gw, price_label=price_label)
    dp = build_dispatcher(svc)
    scheduler = Scheduler(build_jobs(svc), svc=svc)
    scheduler_task: asyncio.Task | None = None
    try:
        me = await bot.get_me()
        if (me.username or "").lower() != config.bot_username.lower():
            log.warning(
                "BOT_USERNAME is '%s' but this token belongs to @%s", config.bot_username, me.username
            )
        problems = await tg.bot_rights_problems(me.id)
        for problem in problems:
            log.warning("Paid channel setup: %s", problem)
        log.info("Starting @%s | Stripe: %s | removals enabled: %s",
                 me.username, config.stripe_mode if stripe_gw else "off", config.removals_enabled)
        # Rate-limited like every alert, so a restart loop doesn't flood the owner.
        now = svc.now()
        await alert_owner(svc, "startup", startup_text(svc, problems, await state_lines(svc, now)), now)
        scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler")
        # The session is closed below, after the scheduler, not by aiogram.
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES, close_bot_session=False)
    finally:
        try:
            # Side by side, so the wait is the longer of the two, not their sum.
            await asyncio.gather(stop_scheduler(scheduler, scheduler_task),
                                 drain_updates(dp, HANDLER_DRAIN_TIMEOUT))
        finally:
            await bot.session.close()
            await db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout
    )
    load_dotenv()
    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)
    # Before the DB is opened: a second copy must not touch it or poll Telegram at all.
    try:
        lock = acquire_instance_lock(lock_path_for(config.db_path))
    except AlreadyRunning as exc:
        print(exc, file=sys.stderr)
        sys.exit(3)
    with lock:
        # An older copy that doesn't take the lock: it would poll Telegram too, and
        # the DB would be migrated under it.
        others = unlocked_copies(os.getpid())
        if others:
            print(unlocked_copy_text(others), file=sys.stderr)
            sys.exit(3)
        asyncio.run(run(config))
