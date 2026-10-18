"""/start and the welcome screen (phase 1)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import texts
from ..config import Config
from ..db import Database
from ..models import User
from ..timeutil import utcnow
from ..users import get_user, register_user

router = Router(name="start")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def welcome_keyboard(user: User, config: Config) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if user.trial_started_at is None:
        rows.append([InlineKeyboardButton(text="Start my free 2 weeks", callback_data="trial:start")])
    rows.append([InlineKeyboardButton(text="Join the free channel", url=config.free_channel_link)])
    rows.append([InlineKeyboardButton(text="How it works", callback_data="how")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_reply(user: User, config: Config) -> tuple[str, InlineKeyboardMarkup | None]:
    if user.banned:
        return texts.UNAVAILABLE, None
    return texts.WELCOME, welcome_keyboard(user, config)


@router.message(CommandStart())
async def on_start(message: Message, command: CommandObject, db: Database, config: Config) -> None:
    if message.from_user is None:
        return
    user, _ = await register_user(
        db,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        payload=command.args,
        now=utcnow(),
    )
    text, markup = start_reply(user, config)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "how")
async def on_how(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.HOW_IT_WORKS)


@router.callback_query(F.data == "trial:start")
async def on_trial_start(callback: CallbackQuery, db: Database) -> None:
    # Phase 2 builds the real trial. In phase 1 this must not grant anything.
    user = await get_user(db, callback.from_user.id)
    if user is None:
        await callback.answer(texts.TRIAL_START_FIRST, show_alert=True)
    elif user.banned:
        await callback.answer(texts.UNAVAILABLE, show_alert=True)
    else:
        await callback.answer(texts.TRIAL_NOT_READY, show_alert=True)
