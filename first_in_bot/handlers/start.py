"""/start, the welcome screen and "How it works".

The price comes from svc.price_label when each message is made, not from a value
fixed at startup, so a price read later (billing.refresh_price) shows at once."""
from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import texts
from ..config import Config
from ..keyboards import CB_HOW, checkout_button
from ..models import User
from ..services import Services
from ..timeutil import format_local_date
from ..users import get_user, register_user, trial_eligible
from .billing import _answer

router = Router(name="start")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def welcome_keyboard(user: User, config: Config) -> InlineKeyboardMarkup:
    # "Start my free 2 weeks" (or N days, per TRIAL_DAYS) if they can still have the
    # trial, otherwise "Subscribe".
    # Both open a Stripe Checkout (handlers/billing.py).
    rows: list[list[InlineKeyboardButton]] = [
        [checkout_button(user, trial_days=config.trial_days)],
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
async def on_start(message: Message, command: CommandObject, svc: Services) -> None:
    if message.from_user is None:
        return
    user, _ = await register_user(
        svc.db,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        payload=command.args,
        now=svc.now(),
    )
    text, markup = start_reply(user, svc.config, svc.price_label)
    await message.answer(text, reply_markup=markup)


def how_it_works_text(user: User | None, price_label: str | None, config: Config, now: datetime) -> str:
    # Only promise a free trial to people who can still have one.
    if user is not None and not trial_eligible(user):
        return texts.HOW_IT_WORKS_SUBSCRIBE.format(price=texts.price_text(price_label))
    charge_date = format_local_date(now + timedelta(days=config.trial_days), config.timezone)
    return texts.how_it_works_trial(
        texts.trial_terms(price_label, config.trial_days, charge_date),
        config.trial_reminder_days_before,
        config.trial_days,
    )


@router.callback_query(F.data == CB_HOW)
async def on_how(callback: CallbackQuery, svc: Services) -> None:
    # A press from before a restart can be too old to answer; that mustn't stop the reply.
    await _answer(callback)
    user = await get_user(svc.db, callback.from_user.id)
    text = how_it_works_text(user, svc.price_label, svc.config, svc.now())
    if isinstance(callback.message, Message):
        await callback.message.answer(text)
    else:
        # The welcome is too old or was deleted (an InaccessibleMessage), so write
        # to the user directly. In a private chat that's the same chat.
        await svc.tg.send(callback.from_user.id, text)
