"""Handlers with duck-typed fake messages/callbacks: welcome, checkout buttons,
/manage, /status, "I've paid" and the admin commands."""
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject
from aiogram.methods import AnswerCallbackQuery

from first_in_bot import texts
from first_in_bot.billing import start_checkout
from first_in_bot.config import load_config
from first_in_bot.handlers import admin as admin_handlers
from first_in_bot.handlers import billing as billing_handlers
from first_in_bot.handlers.start import how_it_works_text, start_reply, welcome_keyboard, welcome_text
from first_in_bot.models import User
from first_in_bot.status import status_text
from first_in_bot.stripe_gateway import PortalNotConfigured, StripeGatewayError
from first_in_bot.users import get_user
from tests.fakes import NOW, OWNER, PRICE, add_user, make_config, make_svc, trial_sub
from tests.test_config import GOOD

D = timedelta(days=1)


class FakeMessage:
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid, username=f"user{uid}", first_name="U")
        self.chat = SimpleNamespace(id=uid, type="private")
        self.answers = []

    async def answer(self, text, reply_markup=None, **_):
        self.answers.append((text, reply_markup))


class FakeCallback:
    def __init__(self, uid, data, too_old=False):
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.answers = []
        self.too_old = too_old   # Telegram: "query is too old and response timeout expired"

    async def answer(self, text=None, show_alert=False, **_):
        if self.too_old:
            raise TelegramBadRequest(method=AnswerCallbackQuery(callback_query_id="1"),
                                     message="Bad Request: query is too old and response timeout expired")
        self.answers.append((text, show_alert))


def cmd(name, args=None):
    return CommandObject(prefix="/", command=name, args=args)


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


# --- welcome --------------------------------------------------------------------

def test_welcome_offers_trial_with_price_and_card_wording():
    text = welcome_text(User(telegram_id=1), PRICE, load_config(GOOD))
    assert "Your first 14 days are free, then £2.99/month" in text
    assert "A card is required" in text and "/manage" in text and "No card needed" not in text


def test_welcome_for_returning_user_offers_subscription():
    user = User(telegram_id=1, stripe_sub_id="sub_1", sub_status="canceled")
    text, markup = start_reply(user, load_config(GOOD), PRICE)
    assert "free" not in text.split("\n")[-1] and "£2.99/month" in text
    first = buttons(markup)[0]
    assert first.text == "Subscribe" and first.callback_data == "subscribe"


def test_welcome_keyboard_trial_button_is_checkout():
    first = buttons(welcome_keyboard(User(telegram_id=1), load_config(GOOD)))[0]
    assert first.text == "Start my free 2 weeks" and first.callback_data == "trial:start"


@pytest.mark.parametrize("user,trial", [
    (None, True),
    (User(telegram_id=1), True),
    (User(telegram_id=1, stripe_sub_id="sub_1", sub_status="canceled"), False),
    (User(telegram_id=1, trial_started_at=NOW - 40 * D), False),
])
def test_how_it_works_only_promises_a_trial_to_the_eligible(user, trial):
    text = how_it_works_text(user, PRICE, load_config(GOOD), NOW)
    assert ("14 days free" in text) is trial
    assert ("Tap <b>Subscribe</b> and pay £2.99/month" in text) is not trial
    if trial:
        assert "A card is required" in text and "15 Nov 2026" in text and "/manage" in text


def test_copy_never_says_no_card_or_reselling():
    for name in dir(texts):
        value = getattr(texts, name)
        if isinstance(value, str) and not name.startswith("__"):
            assert "no card" not in value.lower(), name
            assert "resell" not in value.lower(), name


def test_brand_name_never_ping_in_code():
    root = Path(__file__).resolve().parent.parent / "first_in_bot"
    for path in root.rglob("*.py"):
        assert not re.search(r"\bPing\b", path.read_text(encoding="utf-8")), path


# --- checkout buttons ----------------------------------------------------------

async def test_trial_button_without_stripe_explains(db, tg):
    svc = make_svc(db, tg, None)
    await add_user(db, 1)
    cb = FakeCallback(1, "trial:start")
    await billing_handlers.on_checkout_button(cb, svc)
    assert cb.answers == [(texts.PAYMENTS_NOT_READY, True)]
    assert len(texts.PAYMENTS_NOT_READY) <= 200   # callback alert limit


async def test_trial_button_sends_checkout_link(svc, tg, stripe_fake):
    await add_user(svc.db, 1)
    cb = FakeCallback(1, "trial:start")
    await billing_handlers.on_checkout_button(cb, svc)
    assert cb.answers == [("Opening checkout…", False)]   # answered before Stripe is called
    [text] = tg.sent_to(1)
    # Everything the buyer must know before the Stripe link.
    assert "14 days free" in text and "£2.99/month" in text
    assert "A card is required" in text
    assert "first payment is taken on <b>15 Nov 2026</b>" in text
    assert "fixed when this link is made" in text
    assert "Cancel any time before then with /manage" in text
    url_button, done_button = buttons(tg.markups_to(1)[0])
    assert url_button.url == "https://checkout.stripe.test/cs_1"
    assert done_button.callback_data == "paid:check" and done_button.text == "I've added my card"
    assert stripe_fake.created[0]["trial_end"] == NOW + 14 * D


async def test_old_trial_button_after_trial_used(svc, tg, stripe_fake):
    await add_user(svc.db, 1, trial_started_at=NOW - 40 * D, stripe_sub_id="sub_old", sub_status="canceled")
    await billing_handlers.on_checkout_button(FakeCallback(1, "trial:start"), svc)
    text = tg.sent_to(1)[0]
    assert text.startswith(texts.ALREADY_HAD_TRIAL) and "subscribe for £2.99/month" in text
    assert stripe_fake.created[0]["trial_end"] is None
    assert buttons(tg.markups_to(1)[0])[1].text == "I've paid"


async def test_stripe_error_sent_as_message_after_answering(svc, tg, stripe_fake):
    await add_user(svc.db, 1)
    stripe_fake.fail_create = True
    cb = FakeCallback(1, "subscribe")
    await billing_handlers.on_checkout_button(cb, svc)
    assert cb.answers == [("Opening checkout…", False)]
    assert tg.sent_to(1) == [texts.STRIPE_ERROR]


async def test_late_answer_never_stops_the_reply(svc, tg, stripe_fake):
    await add_user(svc.db, 1)
    await billing_handlers.on_checkout_button(FakeCallback(1, "trial:start", too_old=True), svc)
    assert buttons(tg.markups_to(1)[0])[0].url == "https://checkout.stripe.test/cs_1"


async def test_failed_alert_answer_falls_back_to_a_message(db, tg):
    svc = make_svc(db, tg, None)
    await add_user(db, 1)
    await billing_handlers.on_checkout_button(FakeCallback(1, "trial:start", too_old=True), svc)
    assert tg.sent_to(1) == [texts.PAYMENTS_NOT_READY]


async def test_unexpected_error_still_gets_a_reply(svc, tg, monkeypatch):
    await add_user(svc.db, 1)

    async def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(billing_handlers, "start_checkout", broken)
    cb = FakeCallback(1, "subscribe")
    await billing_handlers.on_checkout_button(cb, svc)
    assert cb.answers == [("Opening checkout…", False)] and tg.sent_to(1) == [texts.STRIPE_ERROR]


async def test_manage_button_answers_first_then_sends_portal_link(svc, tg, stripe_fake):
    await add_user(svc.db, 2, stripe_customer_id="cus_2", sub_status="active", stripe_sub_id="sub_2")
    cb = FakeCallback(2, "manage")
    await billing_handlers.on_manage_button(cb, svc)
    assert cb.answers == [("Opening your subscription…", False)]
    assert tg.sent_to(2) == [texts.MANAGE_LINK]


async def test_subscribe_command_when_already_subscribed(svc, stripe_fake):
    await add_user(svc.db, 1, sub_status="active", stripe_sub_id="sub_1")
    msg = FakeMessage(1)
    await billing_handlers.cmd_subscribe(msg, svc)
    text, markup = msg.answers[0]
    assert text == texts.ALREADY_SUBSCRIBED
    assert buttons(markup)[0].url == svc.config.paid_join_request_link
    assert stripe_fake.created == []


async def test_ive_paid_runs_poll_then_shows_status(svc, tg, stripe_fake):
    user = await add_user(svc.db, 1)
    await start_checkout(svc, user, NOW)
    cb = FakeCallback(1, "paid:check")
    await billing_handlers.on_paid_check(cb, svc)
    assert cb.answers[0][0] == "Checking…"
    assert tg.sent_to(1)[-1].startswith(texts.PAID_NOT_SEEN)

    stripe_fake.complete("cs_1", trial_sub(1, start=NOW - D))
    await billing_handlers.on_paid_check(FakeCallback(1, "paid:check"), svc)
    welcome, status = tg.sent_to(1)[-2:]
    assert "free trial is on" in welcome
    assert texts.STATUS_ACCESS_YES in status


async def test_unknown_buttons_are_answered():
    cb = FakeCallback(1, "something:old")
    await billing_handlers.on_unknown_button(cb)
    assert cb.answers == [(None, False)]


# --- /manage ---------------------------------------------------------------------

async def test_manage_variants(db, tg, stripe_fake):
    no_stripe = make_svc(db, tg, None)
    svc = make_svc(db, tg, stripe_fake, SUPPORT_CONTACT="@FirstInHelp")
    await add_user(db, 1)
    await add_user(db, 2, stripe_customer_id="cus_2", sub_status="active", stripe_sub_id="sub_2")

    msg = FakeMessage(1)
    await billing_handlers.cmd_manage(msg, no_stripe)
    assert msg.answers[0][0] == texts.PAYMENTS_NOT_READY

    msg = FakeMessage(1)
    await billing_handlers.cmd_manage(msg, svc)
    assert msg.answers[0][0] == texts.MANAGE_NO_CUSTOMER
    assert buttons(msg.answers[0][1])[0].text == "Start my free 2 weeks"

    msg = FakeMessage(2)
    await billing_handlers.cmd_manage(msg, svc)
    text, markup = msg.answers[0]
    assert text == texts.MANAGE_LINK and buttons(markup)[0].url == "https://billing.stripe.test/p/cus_2"

    stripe_fake.portal_error = PortalNotConfigured("off")
    msg = FakeMessage(2)
    await billing_handlers.cmd_manage(msg, svc)
    assert "isn't switched on yet" in msg.answers[0][0] and "@FirstInHelp" in msg.answers[0][0]

    stripe_fake.portal_error = StripeGatewayError("down")
    msg = FakeMessage(2)
    await billing_handlers.cmd_manage(msg, svc)
    assert msg.answers[0][0] == texts.STRIPE_ERROR


async def test_manage_with_customer_deleted_at_stripe(svc, stripe_fake):
    await add_user(svc.db, 2, stripe_customer_id="cus_gone", sub_status="canceled", stripe_sub_id="sub_2")
    stripe_fake.deleted_customers = {"cus_gone"}
    msg = FakeMessage(2)
    await billing_handlers.cmd_manage(msg, svc)
    assert msg.answers[0][0] == texts.MANAGE_NO_CUSTOMER and buttons(msg.answers[0][1])[0].text == "Subscribe"
    assert (await get_user(svc.db, 2)).stripe_customer_id is None   # next checkout makes a new one


# --- /status ---------------------------------------------------------------------

CONFIG = make_config()
GRACE = CONFIG.past_due_grace


@pytest.mark.parametrize("fields,expected,access", [
    ({}, texts.STATUS_NONE_TRIAL, False),
    ({"trial_started_at": NOW - 40 * D, "stripe_sub_id": "s", "sub_status": "canceled"}, texts.STATUS_ENDED, False),
    ({"sub_status": "trialing", "sub_trial_end": NOW + 3 * D},
     "Free trial until <b>4 Nov 2026, 12:00</b>. After that, £2.99/month is charged automatically", True),
    ({"sub_status": "trialing", "sub_trial_end": NOW + 3 * D, "cancel_at_period_end": True},
     "You've cancelled, so you won't be charged", True),
    ({"sub_status": "active", "sub_period_end": NOW + 10 * D},
     "Renews on <b>11 Nov 2026, 12:00</b> (£2.99/month)", True),
    ({"sub_status": "active", "sub_period_end": NOW + 10 * D, "cancel_at_period_end": True},
     "You keep access until <b>11 Nov 2026, 12:00</b>", True),
    # past_due as Stripe stores it: the unpaid period started, its end is a month away.
    ({"sub_status": "past_due", "sub_period_start": NOW - D, "sub_period_end": NOW + 29 * D},
     "before <b>3 Nov 2026, 12:00</b>", True),
    ({"sub_status": "past_due", "sub_period_start": NOW - 5 * D, "sub_period_end": NOW + 25 * D},
     texts.STATUS_PAST_DUE, False),
    # Row from before migration 3 (no period start yet): old rule until the next sync.
    ({"sub_status": "past_due", "sub_period_end": NOW - D}, "before <b>3 Nov 2026, 12:00</b>", True),
    ({"sub_status": "incomplete"}, texts.STATUS_INCOMPLETE, False),
    ({"manual_grant_until": NOW + 5 * D}, "Free access until <b>6 Nov 2026, 12:00</b>", True),
])
def test_status_text(fields, expected, access):
    text = status_text(User(telegram_id=1, **fields), NOW, CONFIG, PRICE)
    assert expected in text
    assert (texts.STATUS_ACCESS_YES if access else texts.STATUS_ACCESS_NO) in text


def test_status_text_banned_and_price_fallback():
    assert status_text(User(telegram_id=1, banned=True, sub_status="active"), NOW, CONFIG, PRICE) == texts.UNAVAILABLE
    text = status_text(User(telegram_id=1, sub_status="trialing", sub_trial_end=NOW + D), NOW, CONFIG, None)
    assert "the subscription price" in text


async def test_status_command(svc):
    await add_user(svc.db, 1)
    msg = FakeMessage(1)
    await billing_handlers.cmd_status(msg, svc)
    text, markup = msg.answers[0]
    assert texts.STATUS_NONE_TRIAL in text and buttons(markup)[0].callback_data == "trial:start"


async def test_terms_mentions_not_affiliated_and_price(svc):
    msg = FakeMessage(1)
    await billing_handlers.cmd_terms(msg, svc)
    assert "not affiliated with FIXR or any venue" in msg.answers[0][0]
    assert "£2.99/month" in msg.answers[0][0]


async def test_help_for_everyone_else_is_unchanged(svc):
    for uid in (1, 999):
        msg = FakeMessage(uid)
        await billing_handlers.cmd_help(msg, svc)
        assert msg.answers == [(texts.HELP, None)]


ADMIN_COMMANDS = ["/stats", "/report", "/user", "/grant", "/revoke", "/ban", "/unban", "/dryrun", "/confirmsweep",
                  "/pause", "/resume"]


@pytest.mark.parametrize("uid", [OWNER, 222])   # every ADMIN_IDS entry, not just the owner
async def test_help_for_admins_adds_the_admin_commands(svc, uid):
    msg = FakeMessage(uid)
    await billing_handlers.cmd_help(msg, svc)
    [(text, _)] = msg.answers
    assert text == texts.HELP + "\n\n" + texts.HELP_ADMIN
    admin_lines = texts.HELP_ADMIN.split("\n")[1:]
    assert [line.split(":")[0].split()[0] for line in admin_lines] == ADMIN_COMMANDS   # one line each
    assert "<id" not in texts.HELP_ADMIN          # angle brackets escaped for HTML parse mode


# --- admin -------------------------------------------------------------------------

async def test_admin_commands_ignored_for_non_admins(svc, tg):
    await add_user(svc.db, 5)
    stranger = 999
    for handler, args in [
        (admin_handlers.cmd_stats, ()),
        (admin_handlers.cmd_report, ()),
        (admin_handlers.cmd_user, (cmd("user", "5"),)),
        (admin_handlers.cmd_grant, (cmd("grant", "5 30"),)),
        (admin_handlers.cmd_dryrun, ()),
        (admin_handlers.cmd_confirmsweep, ()),
        (admin_handlers.cmd_pause, ()),
        (admin_handlers.cmd_resume, ()),
    ]:
        msg = FakeMessage(stranger)
        await handler(msg, *args, svc)
        assert msg.answers == []
    assert (await get_user(svc.db, 5)).manual_grant_until is None
    assert await svc.db.kv_get("maintenance_paused_since") is None
    assert tg.events == []


async def test_stats(svc):
    await add_user(svc.db, 1, sub_status="trialing", trial_started_at=NOW - D)
    await add_user(svc.db, 2, sub_status="active", trial_started_at=NOW - 20 * D, sub_trial_end=NOW - 6 * D)
    await add_user(svc.db, 3, sub_status="past_due")
    await add_user(svc.db, 4, created_at=NOW - 30 * D)
    msg = FakeMessage(OWNER)
    await admin_handlers.cmd_stats(msg, svc)
    text = msg.answers[0][0]
    for line in ["Users: 4", "Active trials: 1", "Paying: 1", "Past due: 1", "Trials ended (7 days): 1",
                 "Conversions (had a trial, now paying): 1", "New users by source (7 days): tt_v1 3"]:
        assert line in text


async def test_user_lookup_and_escaping(svc):
    await add_user(svc.db, 5, username="<b>evil</b>")
    msg = FakeMessage(OWNER)
    await admin_handlers.cmd_user(msg, cmd("user", "@<b>evil</b>"), svc)
    text = msg.answers[0][0]
    assert "User 5" in text and "&lt;b&gt;evil&lt;/b&gt;" in text and "<b>evil</b>" not in text
    assert "user_created" in text

    msg = FakeMessage(OWNER)
    await admin_handlers.cmd_user(msg, cmd("user", "424242"), svc)
    assert "No user found" in msg.answers[0][0]


async def test_grant_and_clear(svc):
    await add_user(svc.db, 5)
    msg = FakeMessage(OWNER)
    await admin_handlers.cmd_grant(msg, cmd("grant", "5 7"), svc)
    user = await get_user(svc.db, 5)
    assert user.manual_grant_until is not None and svc.entitled(user, user.manual_grant_until - D)
    assert "free access until" in msg.answers[0][0]

    msg = FakeMessage(OWNER)
    await admin_handlers.cmd_grant(msg, cmd("grant", "@user5 0"), svc)
    assert (await get_user(svc.db, 5)).manual_grant_until is None
    assert "cleared" in msg.answers[0][0]

    msg = FakeMessage(OWNER)
    await admin_handlers.cmd_grant(msg, cmd("grant", "5 lots"), svc)
    assert msg.answers[0][0].startswith("Usage")
    rows = await svc.db.fetchall("SELECT detail FROM audit_log WHERE action='grant'")
    assert len(rows) == 2 and f"by={OWNER}" in rows[0]["detail"]


async def test_dryrun_command_reports_regardless_of_fingerprint(svc, tg):
    await add_user(svc.db, 5, in_paid_channel=True, manual_grant_until=NOW - D)
    for _ in range(2):
        msg = FakeMessage(OWNER)
        await admin_handlers.cmd_dryrun(msg, svc)
        assert "Would remove (1): 5 @user5" in msg.answers[0][0]
    assert tg.calls("kick") == [] and tg.sent_to(5) == []


async def test_terms_state_the_trial_facts_with_the_first_charge_date(svc, tg):
    msg = FakeMessage(1)
    await billing_handlers.cmd_terms(msg, svc)
    [(text, _)] = msg.answers
    assert "14 days free" in text and "£2.99/month" in text and "A card is required" in text
    assert "first payment is taken on <b>15 Nov 2026</b>" in text
    assert "3 days before your first payment" in text and "/manage" in text
    assert "not affiliated with FIXR" in text


async def test_cancelled_trial_status_says_access_until_trial_end(svc):
    await add_user(svc.db, 1, stripe_sub_id="sub_1", sub_status="canceled", sub_trial_end=NOW + 3 * D)
    reply = await billing_handlers.status_reply(svc, 1, NOW)
    assert "You've cancelled" in reply.text and "won't be charged" in reply.text
    assert texts.STATUS_ACCESS_YES in reply.text


async def test_resent_link_shows_the_original_first_charge_date(svc, tg, stripe_fake):
    """Same session reused 20 hours later: the message must still show the date Stripe will charge."""
    await add_user(svc.db, 1)
    first = await billing_handlers.checkout_reply(svc, 1, NOW)
    second = await billing_handlers.checkout_reply(svc, 1, NOW + timedelta(hours=20))
    assert "<b>15 Nov 2026</b>" in first.text and "<b>15 Nov 2026</b>" in second.text
    assert "16 Nov 2026" not in second.text
    assert first.markup.inline_keyboard[0][0].url == second.markup.inline_keyboard[0][0].url
    assert len(stripe_fake.created) == 1
