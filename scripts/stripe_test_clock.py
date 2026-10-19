"""Manual end-to-end check of the trial life cycle against Stripe TEST MODE, using
Stripe test clocks to move time forward without waiting.

For each scenario it builds a subscription like the bot's Checkout does (a
trial_period_days trial, card on file), advances the clock, and after every step
feeds what Stripe really reports through the bot's own code (parse_subscription,
write_subscription, is_entitled and the reminder rules). Then it checks the
result against what the bot is supposed to do:

  trial_start         trial begins, entitled, reminders fall due at the right times
  trial_paid          trial ends with a successful charge -> active, entitled, paid
  trial_failed        trial ends with a failed payment -> past_due, entitled only
                      for the grace period
  cancel_in_trial     cancel at period end during the trial -> keeps access until
                      the trial would have ended, no reminders, never charged
  cancel_in_trial_now cancel immediately during the trial -> same promise

It needs its own key, because the bot's restricted key can't create clocks. In
.env (never commit it):

    STRIPE_TEST_CLOCK_KEY=rk_test_...   (or sk_test_...)
    STRIPE_PRICE_ID=price_...           (the same recurring test price the bot uses)

The key needs: Test clocks write, Customers write, Payment methods write,
Subscriptions write, Invoices read. Live keys are refused. Nothing here touches
Telegram or the bot's real database.

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
from typing import Awaitable, Callable, Protocol

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from first_in_bot.billing import write_subscription  # noqa: E402
from first_in_bot.db import Database  # noqa: E402
from first_in_bot.entitlement import is_entitled  # noqa: E402
from first_in_bot.notices import trial_final_due, trial_reminder_due  # noqa: E402
from first_in_bot.stripe_gateway import SubInfo, parse_subscription  # noqa: E402
from first_in_bot.users import get_user, register_user  # noqa: E402

H = timedelta(hours=1)
GOOD_CARD = "pm_card_visa"                      # Stripe test payment method: charges succeed
BAD_CARD = "pm_card_chargeCustomerFail"         # attaches fine, every charge fails
TELEGRAM_ID = 900_000_001                       # fake; only used inside the throwaway database
SLACK = timedelta(minutes=5)                    # tolerance when comparing Stripe timestamps


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
    sub: SubInfo


class Driver(Protocol):
    """The Stripe side of a scenario: the real one drives test clocks, tests use a fake."""
    t0: datetime

    async def start(self, card: str, settings: Settings) -> None: ...
    async def now(self) -> datetime: ...
    async def advance_to(self, when: datetime) -> None: ...
    async def subscription(self) -> SubInfo: ...
    async def cancel(self, at_period_end: bool) -> None: ...
    async def amount_paid(self) -> int: ...
    async def close(self, keep: bool) -> None: ...


async def observe(db: Database, sub: SubInfo, now: datetime, settings: Settings) -> Observation:
    """Store the subscription the way the bot's sync does, then read the user back."""
    await register_user(db, TELEGRAM_ID, "testclock", "Test", "script", now)
    await write_subscription(db, TELEGRAM_ID, sub, now)
    user = await get_user(db, TELEGRAM_ID)
    assert user is not None
    return Observation(
        status=user.sub_status or "",
        entitled=is_entitled(user, now, settings.past_due_grace),
        early_reminder_due=trial_reminder_due(user, now, settings.reminder_days_before),
        final_reminder_due=trial_final_due(user, now),
        cancel_scheduled=user.cancel_at_period_end,
        trial_end=user.sub_trial_end,
        trial_started_recorded=user.trial_started_at is not None,
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
    _, end = await _begin(s, GOOD_CARD)
    s.step("trial ends, charge succeeds")
    obs = await s.go(end + 2 * H)
    s.expect(obs.status == "active", f"status is {obs.status!r}, want 'active'")
    s.expect(obs.entitled, "entitled once paid")
    s.expect(not obs.early_reminder_due and not obs.final_reminder_due, "no trial reminders after conversion")
    paid = await s.driver.amount_paid()
    s.expect(paid > 0, f"first charge taken (amount paid {paid})")


async def trial_failed(s: Scenario) -> None:
    _, end = await _begin(s, BAD_CARD)
    grace = s.settings.past_due_grace
    s.step("trial ends, charge fails")
    obs = await s.go(end + 2 * H)
    s.expect(obs.status == "past_due", f"status is {obs.status!r}, want 'past_due'")
    s.expect(obs.entitled, "still entitled inside the past-due grace")
    s.expect(await s.driver.amount_paid() == 0, "nothing was paid")

    s.step("grace runs out")
    obs = await s.go(end + grace + 2 * H)
    s.expect(not obs.entitled, f"no longer entitled after the grace (status {obs.status!r})")


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


SCENARIOS: dict[str, Callable[[Scenario], Awaitable[None]]] = {
    "trial_start": trial_start,
    "trial_paid": trial_paid,
    "trial_failed": trial_failed,
    "cancel_in_trial": cancel_in_trial,
    "cancel_in_trial_now": cancel_in_trial_now,
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

class StripeClockDriver:
    def __init__(self, api_key: str, price_id: str):
        import stripe

        from first_in_bot.stripe_gateway import STRIPE_API_VERSION

        self.client = stripe.StripeClient(api_key, stripe_version=STRIPE_API_VERSION, max_network_retries=3)
        self.price_id = price_id
        self.clock_id: str | None = None
        self.customer_id = ""
        self.sub_id = ""
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
        await self._call(lambda: v1.payment_methods.attach(card, {"customer": customer.id}))
        await self._call(lambda: v1.customers.update(
            customer.id, {"invoice_settings": {"default_payment_method": card}}))
        sub = await self._call(lambda: v1.subscriptions.create({
            "customer": customer.id,
            "items": [{"price": self.price_id}],
            "trial_period_days": settings.trial_days,
            "metadata": {"telegram_id": str(TELEGRAM_ID)},
        }))
        self.sub_id = sub.id

    async def now(self) -> datetime:
        assert self.clock_id
        clock = await self._call(lambda: self.client.v1.test_helpers.test_clocks.retrieve(self.clock_id))
        return datetime.fromtimestamp(clock.frozen_time, tz=timezone.utc)

    async def advance_to(self, when: datetime) -> None:
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

    async def amount_paid(self) -> int:
        invoices = await self._call(lambda: self.client.v1.invoices.list(
            {"customer": self.customer_id, "limit": 20}))
        return sum(int(inv.amount_paid or 0) for inv in invoices.data)

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
