"""Inline keyboards and callback data shared by the handlers and the jobs."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import Config
from .models import User
from .users import trial_eligible

# Callback data. "trial:start" and "subscribe" both open a Stripe Checkout;
# which one we show only changes the label.
CB_TRIAL = "trial:start"
CB_SUBSCRIBE = "subscribe"
CB_PAID_CHECK = "paid:check"
CB_MANAGE = "manage"
CB_HOW = "how"

TRIAL_LABEL = "Start my free 2 weeks"
SUBSCRIBE_LABEL = "Subscribe"


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def checkout_button(user: User | None, label: str | None = None) -> InlineKeyboardButton:
    if user is not None and trial_eligible(user):
        return InlineKeyboardButton(text=label or TRIAL_LABEL, callback_data=CB_TRIAL)
    return InlineKeyboardButton(text=label or SUBSCRIBE_LABEL, callback_data=CB_SUBSCRIBE)


def subscribe_keyboard(user: User | None) -> InlineKeyboardMarkup:
    return _kb([checkout_button(user)])


def join_channel_keyboard(config: Config) -> InlineKeyboardMarkup:
    return _kb([InlineKeyboardButton(text="Join the alerts channel", url=config.paid_join_request_link)])


def checkout_keyboard(url: str, with_trial: bool) -> InlineKeyboardMarkup:
    return _kb(
        [InlineKeyboardButton(text="Start free trial on Stripe" if with_trial else "Pay on Stripe", url=url)],
        [InlineKeyboardButton(text=done_label(with_trial), callback_data=CB_PAID_CHECK)],
    )


def done_label(with_trial: bool) -> str:
    return "I've added my card" if with_trial else "I've paid"


def manage_keyboard() -> InlineKeyboardMarkup:
    return _kb([InlineKeyboardButton(text="Manage or cancel", callback_data=CB_MANAGE)])


def portal_keyboard(url: str) -> InlineKeyboardMarkup:
    return _kb([InlineKeyboardButton(text="Open my subscription", url=url)])


def ended_keyboard(user: User, config: Config, kind: str) -> InlineKeyboardMarkup:
    """Buttons under the 'you've been removed' DM."""
    free = InlineKeyboardButton(text="Free channel", url=config.free_channel_link)
    if kind == "sub_ended":
        return _kb([checkout_button(user, label="Resubscribe")])
    return _kb([checkout_button(user)], [free])
