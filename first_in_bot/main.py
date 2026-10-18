from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from .config import ConfigError, Config, load_config
from .db import Database
from .handlers import start

log = logging.getLogger("first_in_bot")

# Telegram doesn't send chat_member updates unless asked.
ALLOWED_UPDATES = ["message", "callback_query", "chat_join_request", "chat_member"]


async def run(config: Config) -> None:
    db = await Database.open(config.db_path)
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, config=config)
    dp.include_router(start.router)
    try:
        me = await bot.get_me()
        if (me.username or "").lower() != config.bot_username.lower():
            log.warning(
                "BOT_USERNAME is '%s' but this token belongs to @%s", config.bot_username, me.username
            )
        log.info("Starting @%s | Stripe: %s | removals enabled: %s",
                 me.username, config.stripe_mode, config.removals_enabled)
        await dp.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
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
    asyncio.run(run(config))
