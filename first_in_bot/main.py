"""Entry point: load config, open the DB, wire up gateways, handlers and the
background jobs, then long-poll Telegram until stopped."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from .config import ConfigError, Config, load_config
from .db import Database
from .handlers import admin, billing, channel, start
from .handlers.middleware import UnblockMiddleware
from .scheduler import Scheduler, build_jobs
from .services import Services
from .stripe_gateway import StripeGateway
from .telegram_gateway import TelegramGateway

log = logging.getLogger("first_in_bot")

# Telegram doesn't send chat_member updates unless asked.
ALLOWED_UPDATES = ["message", "callback_query", "chat_join_request", "chat_member"]


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
    label = await gateway.price_label(config.stripe_price_id)
    if label is None:
        log.warning("Couldn't read the Stripe price %s; messages will say 'the subscription price'",
                    config.stripe_price_id)
    else:
        log.info("Stripe price: %s", label)
    return gateway, label


def build_dispatcher(svc: Services) -> Dispatcher:
    dp = Dispatcher(db=svc.db, config=svc.config, tg=svc.tg, stripe_gw=svc.stripe,
                    price_label=svc.price_label, svc=svc)
    unblock = UnblockMiddleware()
    dp.message.outer_middleware(unblock)
    dp.callback_query.outer_middleware(unblock)
    # billing goes after start and holds the catch-all that answers stray buttons.
    dp.include_routers(start.router, admin.router, billing.router, channel.router)
    return dp


async def run(config: Config) -> None:
    db = await Database.open(config.db_path)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    tg = TelegramGateway(bot, config.paid_channel_id)
    stripe_gw, price_label = await build_stripe(config)
    svc = Services(db=db, config=config, tg=tg, stripe=stripe_gw, price_label=price_label)
    dp = build_dispatcher(svc)
    scheduler = Scheduler(build_jobs(svc))
    scheduler_task: asyncio.Task | None = None
    try:
        me = await bot.get_me()
        if (me.username or "").lower() != config.bot_username.lower():
            log.warning(
                "BOT_USERNAME is '%s' but this token belongs to @%s", config.bot_username, me.username
            )
        for problem in await tg.bot_rights_problems(me.id):
            log.warning("Paid channel setup: %s", problem)
        log.info("Starting @%s | Stripe: %s | removals enabled: %s",
                 me.username, config.stripe_mode if stripe_gw else "off", config.removals_enabled)
        scheduler_task = asyncio.create_task(scheduler.run(), name="scheduler")
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
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
    asyncio.run(run(config))
