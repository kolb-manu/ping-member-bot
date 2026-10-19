"""/start, the welcome screen and "How it works"."""
from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import texts
from ..config import Config
from ..db import Database
from ..keyboards import CB_HOW, checkout_button
from ..models import User
from ..timeutil import format_local_date, utcnow
from ..users import get_user, register_user, trial_eligible

router = Router(name="start")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def welcome_keyboard(user: User, config: Config) -> InlineKeyboardMarkup:
    # "Start my free 2 weeks" if they can still have the trial, otherwise "Subscribe".
    # Both open a Stripe Checkout (handlers/billing.py).
    rows: list[list[InlineKeyboardButton]] = [
        [checkout_button(user)],
        [InlineKeyboardButton(text="Join the free channel", url=config.free_channel_link)],
        [InlineKeyboardButton(text="How it works", callback_data=CB_HOW)],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def welcome_text(user: User, price_label: str | None, config: Config) -> str:
    price = texts.price_text(price_label)
    offer = texts.OFFER_TRIAL if trial_eligible(user) else texts.OFFER_SUBSCRIBE
    return texts.WELCOME.format(offer=offer.format(price=price, days=config.trial_days))


def start_reply(
    user: User, config: Config, price_label: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    if user.banned:
        return texts.UNAVAILABLE, None
    return welcome_text(user, price_label, config), welcome_keyboard(user, config)


@router.message(CommandStart())
async def on_start(
    message: Message, command: CommandObject, db: Database, config: Config, price_label: str | None = None
) -> None:
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
    text, markup = start_reply(user, config, price_label)
    await message.answer(text, reply_markup=markup)


def how_it_works_text(user: User | None, price_label: str | None, config: Config, now: datetime) -> str:
    # Only promise a free trial to people who can still have one.
    if user is not None and not trial_eligible(user):
        return texts.HOW_IT_WORKS_SUBSCRIBE.format(price=texts.price_text(price_label))
    charge_date = format_local_date(now + timedelta(days=config.trial_days), config.timezone)
    return texts.HOW_IT_WORKS_TRIAL.format(
        terms=texts.trial_terms(price_label, config.trial_days, charge_date),
        reminder_days=config.trial_reminder_days_before,
    )


@router.callback_query(F.data == CB_HOW)
async def on_how(callback: CallbackQuery, db: Database, config: Config, price_label: str | None = None) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        user = await get_user(db, callback.from_user.id)
        await callback.message.answer(how_it_works_text(user, price_label, config, utcnow()))
