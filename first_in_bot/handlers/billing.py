"""User commands and buttons for paying: the trial/subscribe buttons, /subscribe,
"I've paid", /manage, /status, /help and /terms. Private chats only."""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .. import texts
from ..billing import forget_missing_customer, poll_checkouts, start_checkout
from ..keyboards import (
    CB_MANAGE,
    CB_PAID_CHECK,
    CB_SUBSCRIBE,
    CB_TRIAL,
    checkout_keyboard,
    done_label,
    join_channel_keyboard,
    manage_keyboard,
    portal_keyboard,
    subscribe_keyboard,
)
from ..models import User
from ..services import Services
from ..status import status_text
from ..stripe_gateway import CustomerMissing, PortalNotConfigured, StripeGatewayError
from ..timeutil import format_local_date
from ..users import get_user

log = logging.getLogger(__name__)

router = Router(name="billing")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


@dataclass(frozen=True)
class Reply:
    text: str
    markup: InlineKeyboardMarkup | None = None
    alert: bool = False   # short; shown as a callback alert when it came from a button


def contact_line(svc: Services) -> str:
    support = svc.config.support_contact
    return texts.CONTACT_SUPPORT.format(support=html.escape(support)) if support else texts.CONTACT_LATER


def trial_terms_text(svc: Services, now: datetime, trial_end: datetime | None = None) -> str:
    """The trial facts. With a checkout's own trial_end the date is exact; without one
    (/terms) it is the date for someone starting today."""
    config = svc.config
    charge = trial_end if trial_end is not None else now + timedelta(days=config.trial_days)
    return texts.trial_terms(svc.price_label, config.trial_days, format_local_date(charge, config.timezone),
                             fixed=trial_end is not None)


def local_reply(svc: Services, user: User | None) -> Reply | None:
    """The answers that need no network call: unknown user, banned, Stripe off."""
    if user is None:
        return Reply(texts.TRIAL_START_FIRST, alert=True)
    if user.banned:
        return Reply(texts.UNAVAILABLE, alert=True)
    if svc.stripe is None:
        return Reply(texts.PAYMENTS_NOT_READY, alert=True)
    return None


async def checkout_reply(svc: Services, telegram_id: int, now: datetime, wanted_trial: bool = False) -> Reply:
    user = await get_user(svc.db, telegram_id)
    early = local_reply(svc, user)
    if early is not None:
        return early
    assert user is not None
    try:
        offer = await start_checkout(svc, user, now)
    except StripeGatewayError:
        return Reply(texts.STRIPE_ERROR, alert=True)

    if offer.kind == "already_in":
        return Reply(texts.ALREADY_SUBSCRIBED, None if user.in_paid_channel else join_channel_keyboard(svc.config))
    if offer.kind == "fix_payment":
        return Reply(texts.PAYMENT_PROBLEM, manage_keyboard())
    if offer.kind == "disputed":
        return Reply(texts.DISPUTED_NO_CHECKOUT.format(contact=contact_line(svc)))
    assert offer.url is not None
    if offer.with_trial:
        assert offer.trial_end is not None
        body = texts.CHECKOUT_TRIAL.format(terms=trial_terms_text(svc, now, offer.trial_end))
    else:
        body = texts.CHECKOUT_SUBSCRIBE.format(price=texts.price_text(svc.price_label))
    if wanted_trial and not offer.with_trial:
        body = texts.ALREADY_HAD_TRIAL + "\n\n" + body
    body += "\n\n" + texts.CHECKOUT_AFTER.format(done_button=html.escape(done_label(offer.with_trial)))
    return Reply(body, checkout_keyboard(offer.url, offer.with_trial))


async def manage_reply(svc: Services, telegram_id: int, now: datetime) -> Reply:
    user = await get_user(svc.db, telegram_id)
    early = local_reply(svc, user)
    if early is not None:
        return early
    assert user is not None and svc.stripe is not None
    if not user.stripe_customer_id:
        return Reply(texts.MANAGE_NO_CUSTOMER, subscribe_keyboard(user))
    try:
        url = await svc.stripe.portal_url(user.stripe_customer_id, svc.config.bot_link)
    except PortalNotConfigured:
        log.error("Customer Portal is not activated: Stripe dashboard > Settings > Billing > Customer portal")
        return Reply(texts.MANAGE_PORTAL_OFF.format(contact=contact_line(svc)))
    except CustomerMissing:
        # Deleted at Stripe (e.g. test data reset), so there's nothing to manage.
        # Forget it; their next checkout creates a new customer.
        await forget_missing_customer(svc.db, telegram_id, user.stripe_customer_id, now)
        user = await get_user(svc.db, telegram_id)
        return Reply(texts.MANAGE_NO_CUSTOMER, subscribe_keyboard(user))
    except StripeGatewayError:
        return Reply(texts.STRIPE_ERROR)
    return Reply(texts.MANAGE_LINK, portal_keyboard(url))


async def status_reply(svc: Services, telegram_id: int, now: datetime) -> Reply:
    user = await get_user(svc.db, telegram_id)
    if user is None:
        return Reply(texts.TRIAL_START_FIRST)
    text = status_text(user, now, svc.config, svc.price_label)
    markup = None
    if not user.banned and not svc.entitled(user, now):
        markup = subscribe_keyboard(user)
    return Reply(text, markup)


async def paid_check_reply(svc: Services, telegram_id: int, now: datetime) -> Reply:
    """'I've paid': check this user's open checkouts now, then show their status."""
    if svc.stripe is not None:
        try:
            await poll_checkouts(svc, now, only_telegram_id=telegram_id)
        except Exception:
            log.exception("Checkout poll for %s failed", telegram_id)
    reply = await status_reply(svc, telegram_id, now)
    user = await get_user(svc.db, telegram_id)
    if user is not None and not user.banned and not svc.entitled(user, now):
        return Reply(texts.PAID_NOT_SEEN + "\n\n" + reply.text, reply.markup)
    return reply


async def _answer(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> bool:
    """Telegram rejects answers to old button presses ("query is too old"); that
    must never stop the real reply from being sent. False if the answer failed."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception as exc:  # too old, network trouble, odd reply: none may block the reply
        log.info("Couldn't answer the button press from %s: %s", callback.from_user.id, exc)
        return False
    return True


async def _send_from_callback(callback: CallbackQuery, svc: Services, reply: Reply) -> None:
    if reply.alert:
        if not await _answer(callback, reply.text, show_alert=True):
            await svc.tg.send(callback.from_user.id, reply.text, reply.markup)
        return
    await _answer(callback)
    await svc.tg.send(callback.from_user.id, reply.text, reply.markup)


async def _stripe_button(
    callback: CallbackQuery, svc: Services, toast: str, make_reply: Callable[[], Awaitable[Reply]]
) -> None:
    """Buttons that call Stripe. Stripe can take a while (it retries), and Telegram
    rejects late answers, so answer straight away and send the result as a message.
    Only the instant answers (unknown, banned, payments off) stay as alerts."""
    early = local_reply(svc, await get_user(svc.db, callback.from_user.id))
    if early is not None:
        await _send_from_callback(callback, svc, early)
        return
    await _answer(callback, toast)
    try:
        reply = await make_reply()
    except Exception:
        log.exception("Button %s for %s failed", callback.data, callback.from_user.id)
        reply = Reply(texts.STRIPE_ERROR)
    await svc.tg.send(callback.from_user.id, reply.text, reply.markup)


@router.callback_query(F.data.in_({CB_TRIAL, CB_SUBSCRIBE}))
async def on_checkout_button(callback: CallbackQuery, svc: Services) -> None:
    await _stripe_button(callback, svc, "Opening checkout…", lambda: checkout_reply(
        svc, callback.from_user.id, svc.now(), wanted_trial=callback.data == CB_TRIAL))


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, svc: Services) -> None:
    if message.from_user is None:
        return
    reply = await checkout_reply(svc, message.from_user.id, svc.now())
    await message.answer(reply.text, reply_markup=reply.markup)


@router.callback_query(F.data == CB_PAID_CHECK)
async def on_paid_check(callback: CallbackQuery, svc: Services) -> None:
    await _answer(callback, "Checking…")
    reply = await paid_check_reply(svc, callback.from_user.id, svc.now())
    await svc.tg.send(callback.from_user.id, reply.text, reply.markup)


@router.message(Command("manage"))
async def cmd_manage(message: Message, svc: Services) -> None:
    if message.from_user is None:
        return
    reply = await manage_reply(svc, message.from_user.id, svc.now())
    await message.answer(reply.text, reply_markup=reply.markup)


@router.callback_query(F.data == CB_MANAGE)
async def on_manage_button(callback: CallbackQuery, svc: Services) -> None:
    await _stripe_button(callback, svc, "Opening your subscription…",
                         lambda: manage_reply(svc, callback.from_user.id, svc.now()))


@router.message(Command("status"))
async def cmd_status(message: Message, svc: Services) -> None:
    if message.from_user is None:
        return
    reply = await status_reply(svc, message.from_user.id, svc.now())
    await message.answer(reply.text, reply_markup=reply.markup)


@router.message(Command("help"))
async def cmd_help(message: Message, svc: Services) -> None:
    await message.answer(texts.HELP)


@router.message(Command("terms"))
async def cmd_terms(message: Message, svc: Services) -> None:
    support = svc.config.support_contact
    support_line = texts.TERMS_SUPPORT.format(support=html.escape(support)) if support else ""
    await message.answer(
        texts.TERMS.format(
            trial_terms=trial_terms_text(svc, svc.now()),
            reminder_days=svc.config.trial_reminder_days_before,
            support_line=support_line,
        ).rstrip()
    )


@router.callback_query()
async def on_unknown_button(callback: CallbackQuery) -> None:
    # Old or unknown buttons: answer anyway so the spinner stops.
    await callback.answer()
