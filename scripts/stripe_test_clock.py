"""Manual end-to-end check of the subscription life cycle against Stripe TEST MODE,
using Stripe test clocks to move time forward without waiting.

For each scenario it builds a subscription like the bot's Checkout does (a
fixed trial_end of creation time + TRIAL_DAYS, card on file), advances the clock, and after every step
feeds what Stripe really reports through the bot's own code (parse_subscription,
write_subscription, is_entitled, the reminder rules and the removal sweep's choice
of goodbye message). Then it checks the result against what the bot is supposed to do:

  trial_start         trial begins, entitled, reminders fall due at the right times
  trial_paid          trial ends with a successful charge -> active, entitled, paid
  trial_failed        trial ends with a failed payment -> past_due, entitled only
                      for the grace period, then the "trial ended" goodbye
  cancel_in_trial     cancel at period end during the trial -> keeps access until
                      the trial would have ended, no reminders, never charged
  cancel_in_trial_now cancel immediately during the trial -> same promise
  renewal             the first paid month renews -> still active and entitled, a
                      second charge, the period end a month later
  renewal_failed      the card fails at the first renewal -> past_due, entitled only
                      for the grace, then the "subscription ended" goodbye
  cancel_after_paid   cancel at period end in the first paid month -> access until
                      the month ends, then the "subscription ended" goodbye, one charge
  payment_recovered   the first charge fails, then a working card pays the open
                      invoice -> active, entitled, the past-due grace cleared

After the failed-payment retries, Stripe leaves the subscription past_due, unpaid or
canceled depending on Settings > Billing > "Manage failed payments", so the checks
after a grace runs out only ask that access has ended, never for one status. Stripe
moves a test clock at most two billing intervals per advance, so long jumps are
made in steps of one interval.

It needs its own key, because the bot's restricted key can't create clocks. In
.env (never commit it):

    STRIPE_TEST_CLOCK_KEY=rk_test_...   (or sk_test_...)
    STRIPE_PRICE_ID=price_...           (the same recurring test price the bot uses)

The key needs: Test clocks write, Customers write, Payment methods write,
Subscriptions write, Invoices write (payment_recovered pays an open invoice).
Live keys are refused. Nothing here touches Telegram or the bot's real database.

Run from the project folder:

    .venv/bin/python scripts/stripe_test_clock.py            # all scenarios
    .venv/bin/python scripts/stripe_test_clock.py --scenario trial_failed
    .venv/bin/python scripts/stripe_test_clock.py --keep     # leave the clocks in Stripe

Exit code 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from first_in_bot.billing import write_subscription  # noqa: E402
from first_in_bot.db import Database  # noqa: E402
from first_in_bot.entitlement import is_entitled  # noqa: E402
from first_in_bot.notices import trial_final_due, trial_reminder_due  # noqa: E402
from first_in_bot.stripe_gateway import SubInfo, parse_subscription  # noqa: E402
from first_in_bot.sweep import _was_active, ended_kind  # noqa: E402
from first_in_bot.users import get_user, register_user  # noqa: E402

H = timedelta(hours=1)
GOOD_CARD = "pm_card_visa"                      # Stripe test payment method: charges succeed
BAD_CARD = "pm_card_chargeCustomerFail"         # attaches fine, every charge fails
TELEGRAM_ID = 900_000_001                       # fake; only used inside the throwaway database
SLACK = timedelta(minutes=5)                    # tolerance when comparing Stripe timestamps
# Stripe finalises a renewal (or trial-conversion) invoice about an hour after
# creating it at the period end, and charges it then.
CHARGE_LAG = 2 * H
# "About a month" for a renewed period: the shortest and longest calendar months.
MONTH_MIN = timedelta(days=28) - SLACK
MONTH_MAX = timedelta(days=31) + SLACK
# Shortest real length of each Stripe billing interval, for sizing clock advances.
INTERVAL_DAYS = {"day": 1, "week": 7, "month": 28, "year": 365}
FALLBACK_STEP = timedelta(days=1)               # unknown interval: slow, but never refused


@dataclass(frozen=True)
class Settings:
    trial_days: int = 14
    reminder_days_before: int = 3
    past_due_grace: timedelta = timedelta(hours=72)


@dataclass(frozen=True)
class Check:
    scenario: str
    step: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Observation:
    """What the bot would conclude from Stripe's current state of the subscription."""
    status: str
    entitled: bool
    early_reminder_due: bool
    final_reminder_due: bool
    cancel_scheduled: bool
    trial_end: datetime | None
    trial_started_recorded: bool
    past_due_since: datetime | None   # where the bot counts the past-due grace from
    goodbye: str                      # the message the removal sweep would send (ended_kind)
    sub: SubInfo


class Driver(Protocol):
    """The Stripe side of a scenario: the real one drives test clocks, tests use a fake."""
    t0: datetime

    async def start(self, card: str, settings: Settings) -> None: ...
    async def now(self) -> datetime: ...
    async def advance_to(self, when: datetime) -> None: ...
    async def subscription(self) -> SubInfo: ...
    async def cancel(self, at_period_end: bool) -> None: ...
    async def set_card(self, card: str) -> None: ...
    async def pay_open_invoices(self) -> int: ...
    async def amount_paid(self) -> int: ...
    async def paid_invoice_count(self) -> int: ...
    async def close(self, keep: bool) -> None: ...


@dataclass(frozen=True)
class _AuditOnly:
    """sweep._was_active only reads svc.db, so the throwaway database is all it needs."""
    db: Database


async def observe(db: Database, sub: SubInfo, now: datetime, settings: Settings) -> Observation:
    """Store the subscription the way the bot's sync does, then read the user back."""
    await register_user(db, TELEGRAM_ID, "testclock", "Test", "script", now)
    await write_subscription(db, TELEGRAM_ID, sub, now)
    user = await get_user(db, TELEGRAM_ID)
    assert user is not None
    # The sweep tells a paid-then-cancelled month from a failed first charge by
    # whether the audit log ever saw this subscription active.
    was_active = await _was_active(_AuditOnly(db), user)
    return Observation(
        status=user.sub_status or "",
        entitled=is_entitled(user, now, settings.past_due_grace),
        early_reminder_due=trial_reminder_due(user, now, settings.reminder_days_before),
        final_reminder_due=trial_final_due(user, now),
        cancel_scheduled=user.cancel_at_period_end,
        trial_end=user.sub_trial_end,
        trial_started_recorded=user.trial_started_at is not None,
        past_due_since=user.past_due_since,
        goodbye=ended_kind(user, was_active),
        sub=sub,
    )


class Scenario:
    def __init__(self, name: str, driver: Driver, settings: Settings):
        self.name = name
        self.driver = driver
        self.settings = settings
        self.checks: list[Check] = []
        self._step = ""

    async def __aenter__(self) -> "Scenario":
        self.db = await Database.open(":memory:")
        return self

    async def __aexit__(self, *exc) -> None:
        await self.db.close()

    def step(self, name: str) -> None:
        self._step = name

    def expect(self, ok: bool, detail: str) -> None:
        self.checks.append(Check(self.name, self._step, bool(ok), detail))

    async def look(self) -> Observation:
        return await observe(self.db, await self.driver.subscription(), await self.driver.now(), self.settings)

    async def go(self, when: datetime) -> Observation:
        await self.driver.advance_to(when)
        return await self.look()


def _close(a: datetime | None, b: datetime) -> bool:
    return a is not None and abs(a - b) <= SLACK


def _period_end(obs: Observation) -> datetime:
    """The current period end as Stripe reports it; later steps are timed from it."""
    if obs.sub.period_end is None:
        raise RuntimeError(f"Stripe reported no current period end for {obs.sub.id}")
    return obs.sub.period_end


async def _begin(s: Scenario, card: str) -> tuple[Observation, datetime]:
    """Common start: a trial begins, the bot sees a trialing, entitled user."""
    s.step("trial starts")
    await s.driver.start(card, s.settings)
    obs = await s.look()
    end = s.driver.t0 + timedelta(days=s.settings.trial_days)
    s.expect(obs.status == "trialing", f"status is {obs.status!r}, want 'trialing'")
    s.expect(_close(obs.trial_end, end), f"trial ends {obs.trial_end}, want about {end}")
    s.expect(obs.entitled, "entitled while trialing")
    s.expect(obs.trial_started_recorded, "trial_started_at recorded (one trial per person)")
    s.expect(not obs.early_reminder_due and not obs.final_reminder_due, "no reminder due on day 0")
    return obs, end


async def _converted(s: Scenario) -> tuple[Observation, datetime]:
    """Common start for the paid scenarios: the trial ends and the first charge goes
    through. Returns what the bot sees just after it, and the trial end."""
    _, end = await _begin(s, GOOD_CARD)
    s.step("trial ends, charge succeeds")
    obs = await s.go(end + CHARGE_LAG)
    s.expect(obs.status == "active", f"status is {obs.status!r}, want 'active'")
    s.expect(obs.entitled, "entitled once paid")
    s.expect(not obs.early_reminder_due and not obs.final_reminder_due, "no trial reminders after conversion")
    paid = await s.driver.amount_paid()
    s.expect(paid > 0, f"first charge taken (amount paid {paid})")
    return obs, end


async def _failed_conversion(s: Scenario) -> datetime:
    """Common start: the trial ends and the first charge fails. Returns the trial end."""
    _, end = await _begin(s, BAD_CARD)
    s.step("trial ends, charge fails")
    obs = await s.go(end + CHARGE_LAG)
    s.expect(obs.status == "past_due", f"status is {obs.status!r}, want 'past_due'")
    s.expect(obs.entitled, "still entitled inside the past-due grace")
    s.expect(_close(obs.past_due_since, end), f"grace counts from {obs.past_due_since}, want the trial end {end}")
    s.expect(await s.driver.amount_paid() == 0, "nothing was paid")
    return end


async def trial_start(s: Scenario) -> None:
    _, end = await _begin(s, GOOD_CARD)
    lead = timedelta(days=s.settings.reminder_days_before)

    s.step("early reminder window")
    obs = await s.go(end - lead - H)
    s.expect(not obs.early_reminder_due, "early reminder not yet due just before its window")
    obs = await s.go(end - lead + H)
    s.expect(obs.early_reminder_due and not obs.final_reminder_due,
             f"early reminder due {s.settings.reminder_days_before} days out (due={obs.early_reminder_due})")
    s.expect(obs.entitled and obs.status == "trialing", "still trialing and entitled")

    s.step("last-day reminder window")
    obs = await s.go(end - 2 * H)
    s.expect(obs.final_reminder_due and not obs.early_reminder_due,
             f"final reminder due about 3 hours out (final={obs.final_reminder_due}, early={obs.early_reminder_due})")
    s.expect(obs.entitled, "entitled until the trial ends")


async def trial_paid(s: Scenario) -> None:
    await _converted(s)


async def trial_failed(s: Scenario) -> None:
    end = await _failed_conversion(s)
    s.step("grace runs out")
    obs = await s.go(end + s.settings.past_due_grace + CHARGE_LAG)
    s.expect(not obs.entitled, f"no longer entitled after the grace (status {obs.status!r})")
    s.expect(obs.goodbye == "trial_ended", f"goodbye message would be {obs.goodbye!r}, want 'trial_ended'")


async def _cancel_in_trial(s: Scenario, at_period_end: bool) -> None:
    _, end = await _begin(s, GOOD_CARD)
    s.step("cancel during the trial")
    obs = await s.go(s.driver.t0 + timedelta(days=min(5, s.settings.trial_days - 1) or 1))
    await s.driver.cancel(at_period_end)
    obs = await s.look()
    if at_period_end:
        s.expect(obs.status == "trialing" and obs.cancel_scheduled,
                 f"still trialing with the cancellation scheduled (status {obs.status!r}, "
                 f"scheduled={obs.cancel_scheduled})")
    else:
        s.expect(obs.status == "canceled", f"status is {obs.status!r}, want 'canceled'")
    s.expect(obs.entitled, "keeps access after cancelling inside the trial")
    s.expect(not obs.early_reminder_due and not obs.final_reminder_due, "no reminder for someone who cancelled")

    s.step("just before the trial would have ended")
    obs = await s.go(end - H)
    s.expect(obs.entitled, "still has access until the trial would have ended")
    s.expect(not obs.final_reminder_due, "still no reminder")

    s.step("after the trial would have ended")
    obs = await s.go(end + 2 * H)
    s.expect(obs.status == "canceled", f"status is {obs.status!r}, want 'canceled'")
    s.expect(not obs.entitled, "access has ended")
    paid = await s.driver.amount_paid()
    s.expect(paid == 0, f"never charged (amount paid {paid})")


async def cancel_in_trial(s: Scenario) -> None:
    await _cancel_in_trial(s, at_period_end=True)


async def cancel_in_trial_now(s: Scenario) -> None:
    await _cancel_in_trial(s, at_period_end=False)


async def renewal(s: Scenario) -> None:
    obs, _ = await _converted(s)
    first_end = _period_end(obs)
    s.step("first paid month renews")
    obs = await s.go(first_end + CHARGE_LAG)
    s.expect(obs.status == "active", f"status is {obs.status!r}, want 'active'")
    s.expect(obs.entitled, "still entitled after the renewal")
    charges = await s.driver.paid_invoice_count()
    s.expect(charges == 2, f"renewal charged (paid invoices: {charges}, want 2)")
    new_end = obs.sub.period_end
    s.expect(new_end is not None and MONTH_MIN <= new_end - first_end <= MONTH_MAX,
             f"period end moved from {first_end} to {new_end}, want about a month later")


async def renewal_failed(s: Scenario) -> None:
    obs, _ = await _converted(s)
    first_end = _period_end(obs)
    s.step("renewal on a failing card")
    await s.driver.set_card(BAD_CARD)
    obs = await s.go(first_end + CHARGE_LAG)
    s.expect(obs.status == "past_due", f"status is {obs.status!r}, want 'past_due'")
    s.expect(obs.entitled, "still entitled inside the past-due grace")
    # The grace must count from the renewal, not from whenever the bot first saw past_due.
    s.expect(_close(obs.past_due_since, first_end),
             f"grace counts from {obs.past_due_since}, want the renewal at {first_end}")
    charges = await s.driver.paid_invoice_count()
    s.expect(charges == 1, f"only the first month was paid (paid invoices: {charges})")

    s.step("grace runs out")
    obs = await s.go(first_end + s.settings.past_due_grace + CHARGE_LAG)
    s.expect(not obs.entitled, f"no longer entitled after the grace (status {obs.status!r})")
    s.expect(obs.goodbye == "sub_ended", f"goodbye message would be {obs.goodbye!r}, want 'sub_ended'")


async def cancel_after_paid(s: Scenario) -> None:
    obs, end = await _converted(s)
    first_end = _period_end(obs)
    s.step("cancel at period end during the first paid month")
    await s.go(end + (first_end - end) / 2)
    await s.driver.cancel(at_period_end=True)
    obs = await s.look()
    s.expect(obs.status == "active" and obs.cancel_scheduled,
             f"still active with the cancellation scheduled (status {obs.status!r}, "
             f"scheduled={obs.cancel_scheduled})")
    s.expect(obs.entitled, "keeps access for the month they paid for")
    s.expect(not obs.early_reminder_due and not obs.final_reminder_due, "no trial reminders")

    s.step("just before the paid month ends")
    obs = await s.go(first_end - H)
    s.expect(obs.entitled, "still has access until the paid month ends")

    s.step("after the paid month ends")
    obs = await s.go(first_end + CHARGE_LAG)
    s.expect(obs.status == "canceled", f"status is {obs.status!r}, want 'canceled'")
    s.expect(not obs.entitled, "access has ended")
    s.expect(obs.goodbye == "sub_ended", f"goodbye message would be {obs.goodbye!r}, want 'sub_ended'")
    charges = await s.driver.paid_invoice_count()
    s.expect(charges == 1, f"charged exactly once (paid invoices: {charges})")


async def payment_recovered(s: Scenario) -> None:
    await _failed_conversion(s)
    s.step("working card added and the open invoice paid")
    await s.driver.set_card(GOOD_CARD)
    paid_now = await s.driver.pay_open_invoices()
    s.expect(paid_now >= 1, f"paid {paid_now} open invoice(s), want at least 1")
    # An hour on, as a later sync would see it, once Stripe has settled the payment.
    obs = await s.go(await s.driver.now() + H)
    s.expect(obs.status == "active", f"status is {obs.status!r}, want 'active'")
    s.expect(obs.entitled, "entitled again")
    s.expect(obs.past_due_since is None, f"past-due grace cleared (past_due_since {obs.past_due_since})")
    charges = await s.driver.paid_invoice_count()
    s.expect(charges == 1, f"charged exactly once (paid invoices: {charges})")


SCENARIOS: dict[str, Callable[[Scenario], Awaitable[None]]] = {
    "trial_start": trial_start,
    "trial_paid": trial_paid,
    "trial_failed": trial_failed,
    "cancel_in_trial": cancel_in_trial,
    "cancel_in_trial_now": cancel_in_trial_now,
    "renewal": renewal,
    "renewal_failed": renewal_failed,
    "cancel_after_paid": cancel_after_paid,
    "payment_recovered": payment_recovered,
}


async def run_scenario(name: str, driver: Driver, settings: Settings, keep: bool = False) -> list[Check]:
    async with Scenario(name, driver, settings) as scenario:
        try:
            await SCENARIOS[name](scenario)
        except Exception as exc:   # a Stripe or setup error is a failed scenario, not a crash
            scenario.step("error")
            scenario.expect(False, f"{type(exc).__name__}: {exc}")
        finally:
            await driver.close(keep)
        return scenario.checks


# --- the real Stripe test-clock driver -------------------------------------------------

def max_advance(sub: dict) -> timedelta:
    """How far one advance may move the clock. Stripe refuses more than two billing
    intervals of the shortest subscription on the clock; one interval stays inside that."""
    steps = []
    for item in (sub.get("items") or {}).get("data") or []:
        recurring = (item.get("price") or {}).get("recurring") or {}
        days = INTERVAL_DAYS.get(recurring.get("interval") or "")
        if days:
            steps.append(timedelta(days=days * int(recurring.get("interval_count") or 1)))
    return min(steps, default=FALLBACK_STEP)


def advance_steps(start: datetime, when: datetime, step: timedelta) -> list[datetime]:
    """The times to stop at on the way from start to when, none more than step apart."""
    if when < start:
        raise ValueError(f"test clocks only move forward ({when} is before {start})")
    stops: list[datetime] = []
    while when - start > step:
        start += step
        stops.append(start)
    if when > start:
        stops.append(when)
    return stops


def _whole_seconds(dt: datetime) -> datetime:
    return datetime.fromtimestamp(int(dt.timestamp()), tz=timezone.utc)


class StripeClockDriver:
    def __init__(self, api_key: str, price_id: str, *, client: Any | None = None):
        # client is for tests; normally we build the real one here.
        if client is None:
            import stripe

            from first_in_bot.stripe_gateway import STRIPE_API_VERSION

            client = stripe.StripeClient(api_key, stripe_version=STRIPE_API_VERSION, max_network_retries=3)
        self.client = client
        self.price_id = price_id
        self.clock_id: str | None = None
        self.customer_id = ""
        self.sub_id = ""
        self.card_id = ""                 # the attached PaymentMethod, not the pm_card_* token
        self.max_step = FALLBACK_STEP
        self.t0 = datetime.now(timezone.utc)

    async def _call(self, fn):
        return await asyncio.to_thread(fn)

    async def start(self, card: str, settings: Settings) -> None:
        v1 = self.client.v1
        self.t0 = datetime.fromtimestamp(int(time.time()), tz=timezone.utc)
        clock = await self._call(lambda: v1.test_helpers.test_clocks.create(
            {"frozen_time": int(self.t0.timestamp()), "name": f"first-in {card}"}))
        self.clock_id = clock.id
        customer = await self._call(lambda: v1.customers.create(
            {"test_clock": clock.id, "description": "First In test-clock customer",
             "metadata": {"telegram_id": str(TELEGRAM_ID)}}))
        self.customer_id = customer.id
        await self.set_card(card)
        sub = await self._call(lambda: v1.subscriptions.create({
            "customer": customer.id,
            "items": [{"price": self.price_id}],
            # Like the bot: a fixed trial_end (Checkout needs it 48 hours or more ahead).
            "trial_end": int((self.t0 + timedelta(days=settings.trial_days)).timestamp()),
            "metadata": {"telegram_id": str(TELEGRAM_ID)},
        }))
        self.sub_id = sub.id
        self.max_step = max_advance(sub.to_dict())

    async def set_card(self, card: str) -> None:
        """Attach a test card and make it the customer's default, as updating the card
        in the portal would. The subscription has no default of its own, so its
        invoices charge the customer's default."""
        v1 = self.client.v1
        # Attaching a pm_card_* token creates a new PaymentMethod; the default must name that.
        method = await self._call(lambda: v1.payment_methods.attach(card, {"customer": self.customer_id}))
        self.card_id = method.id
        await self._call(lambda: v1.customers.update(
            self.customer_id, {"invoice_settings": {"default_payment_method": method.id}}))

    async def now(self) -> datetime:
        assert self.clock_id
        clock = await self._call(lambda: self.client.v1.test_helpers.test_clocks.retrieve(self.clock_id))
        return datetime.fromtimestamp(clock.frozen_time, tz=timezone.utc)

    async def advance_to(self, when: datetime) -> None:
        assert self.clock_id
        for stop in advance_steps(await self.now(), _whole_seconds(when), self.max_step):
            await self._advance_once(stop)

    async def _advance_once(self, when: datetime) -> None:
        assert self.clock_id
        await self._call(lambda: self.client.v1.test_helpers.test_clocks.advance(
            self.clock_id, {"frozen_time": int(when.timestamp())}))
        for _ in range(90):   # Stripe advances in the background
            clock = await self._call(lambda: self.client.v1.test_helpers.test_clocks.retrieve(self.clock_id))
            if clock.status == "ready":
                return
            if clock.status == "internal_failure":
                raise RuntimeError("Stripe test clock reported an internal failure")
            await asyncio.sleep(2)
        raise RuntimeError("Stripe test clock did not finish advancing in 3 minutes")

    async def subscription(self) -> SubInfo:
        sub = await self._call(lambda: self.client.v1.subscriptions.retrieve(self.sub_id))
        return parse_subscription(sub)

    async def cancel(self, at_period_end: bool) -> None:
        if at_period_end:
            await self._call(lambda: self.client.v1.subscriptions.update(
                self.sub_id, {"cancel_at_period_end": True}))
        else:
            await self._call(lambda: self.client.v1.subscriptions.cancel(self.sub_id))

    async def _invoices(self, status: str | None = None) -> list:
        # Test-clock invoices are only listed when filtered, here by customer.
        params: dict[str, Any] = {"customer": self.customer_id, "limit": 100}
        if status:
            params["status"] = status
        page = await self._call(lambda: self.client.v1.invoices.list(params))
        return list(page.data)

    async def pay_open_invoices(self) -> int:
        """Pay every open invoice with the current card, as Stripe's retry would once
        the card is fixed. Returns how many were paid; a decline raises."""
        invoices = await self._invoices("open")
        for invoice in invoices:
            await self._call(lambda invoice_id=invoice.id: self.client.v1.invoices.pay(
                invoice_id, {"payment_method": self.card_id}))
        return len(invoices)

    async def amount_paid(self) -> int:
        return sum(int(inv.amount_paid or 0) for inv in await self._invoices())

    async def paid_invoice_count(self) -> int:
        """Invoices paid with real money: the trial's £0 invoice doesn't count."""
        return sum(1 for inv in await self._invoices("paid") if int(inv.amount_paid or 0) > 0)

    async def close(self, keep: bool) -> None:
        if self.clock_id and not keep:
            try:
                await self._call(lambda: self.client.v1.test_helpers.test_clocks.delete(self.clock_id))
            except Exception as exc:   # cleanup only
                print(f"  (couldn't delete test clock {self.clock_id}: {type(exc).__name__})")


# --- command line ------------------------------------------------------------------------

def load_settings(env: dict[str, str]) -> Settings:
    def number(key: str, default: int) -> int:
        raw = (env.get(key) or "").strip()
        return int(raw) if raw else default

    return Settings(
        trial_days=number("TRIAL_DAYS", 14),
        reminder_days_before=number("TRIAL_REMINDER_DAYS_BEFORE", 3),
        past_due_grace=timedelta(hours=number("PAST_DUE_GRACE_HOURS", 72)),
    )


def print_report(checks: list[Check]) -> bool:
    last = None
    for c in checks:
        if c.scenario != last:
            print(f"\n== {c.scenario}")
            last = c.scenario
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.step}: {c.detail}")
    failed = [c for c in checks if not c.ok]
    print(f"\n{len(checks) - len(failed)} passed, {len(failed)} failed")
    return not failed


async def main_async(args: argparse.Namespace) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    key = (os.environ.get("STRIPE_TEST_CLOCK_KEY") or "").strip()
    price = (os.environ.get("STRIPE_PRICE_ID") or "").strip()
    if not key.startswith(("sk_test_", "rk_test_")):
        print("Set STRIPE_TEST_CLOCK_KEY in .env to a TEST-mode key (sk_test_... or rk_test_...).\n"
              "Live keys are refused. See the top of this script for the permissions it needs.")
        return 2
    if not price.startswith("price_"):
        print("Set STRIPE_PRICE_ID in .env to your recurring test price (price_...).")
        return 2
    names = args.scenario or list(SCENARIOS)
    settings = load_settings(dict(os.environ))
    print(f"Stripe TEST MODE | trial {settings.trial_days} days | reminder {settings.reminder_days_before} "
          f"days before | grace {settings.past_due_grace}")
    checks: list[Check] = []
    for name in names:
        print(f"Running {name} ...", flush=True)
        checks += await run_scenario(name, StripeClockDriver(key, price), settings, keep=args.keep)
    return 0 if print_report(checks) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", action="append", choices=list(SCENARIOS),
                        help="run only this scenario (repeatable); default is all")
    parser.add_argument("--keep", action="store_true", help="leave the test clocks in Stripe afterwards")
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
