"""The test-clock script's scenario logic, run against a fake Stripe that behaves
the way Stripe does over a subscription's life (no network, no real clocks), plus
the real driver's Stripe calls against a recording client."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from first_in_bot.entitlement import is_entitled
from scripts import stripe_test_clock as script
from scripts.stripe_test_clock import BAD_CARD, GOOD_CARD, SCENARIOS, Settings, run_scenario
from tests.fakes import sub_info

D = timedelta(days=1)
H = timedelta(hours=1)
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
MONTH = 30 * D             # the fake's billing period
PRICE = 299
RETRIES_FOR = 3 * D + H    # the fake's failed-payment retries end just after a 72-hour grace
LIVE = ("trialing", "active", "past_due")


class FakeClockDriver:
    """Minimal model of a Stripe subscription over time. Each period end opens the
    next period and charges the current card an hour later; a cancellation at
    period end takes effect at the period end; once the retries run out a past_due
    subscription ends up as after_retries (the dashboard's "Manage failed payments")."""

    def __init__(self):
        self.t0 = T0
        self._now = T0
        self.card = GOOD_CARD
        self.trial_end = T0 + 14 * D
        self.status = "trialing"
        self.period = (T0, self.trial_end)
        self.cancel_at_end = False
        self.invoices: list[list] = []            # [status, amount]
        self.past_due_at: datetime | None = None
        self.after_retries = "past_due"
        self.closed = None

    async def start(self, card, settings):
        self.card = card
        self.trial_end = self.t0 + timedelta(days=settings.trial_days)
        self.period = (self.t0, self.trial_end)
        self.invoices.append(["paid", 0])         # Stripe's £0 trial invoice

    async def now(self):
        return self._now

    async def advance_to(self, when):
        assert when >= self._now, "test clocks only move forward"
        while self.status in LIVE:
            end = self.period[1]
            if self.cancel_at_end and when >= end:
                self.status = "canceled"
            elif when >= end + H:
                self._renew(end)
                assert self.period[1] > end, "a renewal must move the period on"
            else:
                break
        if self.status == "past_due" and when >= self.past_due_at + RETRIES_FOR:
            self.status = self.after_retries
        self._now = when

    def _renew(self, at):
        self.period = (at, at + MONTH)
        if self.card == GOOD_CARD:
            self.invoices.append(["paid", PRICE])
            self.status = "active"
        else:
            self.invoices.append(["open", PRICE])
            if self.status != "past_due":
                self.status, self.past_due_at = "past_due", at

    async def subscription(self):
        start, end = self.period
        return sub_info(id="sub_tc", customer="cus_tc", telegram_id=script.TELEGRAM_ID, created=self.t0,
                        status=self.status, period_start=start, period_end=end,
                        trial_start=self.t0, trial_end=self.trial_end, cancel=self.cancel_at_end)

    async def cancel(self, at_period_end):
        if at_period_end:
            self.cancel_at_end = True
        else:
            self.status = "canceled"

    async def set_card(self, card):
        self.card = card

    async def pay_open_invoices(self):
        unpaid = [inv for inv in self.invoices if inv[0] == "open"]
        if unpaid and self.card != GOOD_CARD:
            raise RuntimeError("Your card was declined.")
        for inv in unpaid:
            inv[0] = "paid"
        if unpaid and self.status == "past_due":
            self.status, self.past_due_at = "active", None
        return len(unpaid)

    async def amount_paid(self):
        return sum(amount for status, amount in self.invoices if status == "paid")

    async def paid_invoice_count(self):
        return sum(1 for status, amount in self.invoices if status == "paid" and amount > 0)

    async def close(self, keep):
        self.closed = keep


def failures(checks):
    return [c for c in checks if not c.ok]


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_every_scenario_passes_against_a_stripe_like_fake(name):
    driver = FakeClockDriver()
    checks = await run_scenario(name, driver, Settings())
    assert len(checks) >= 5 and failures(checks) == [], failures(checks)
    assert driver.closed is False                      # clocks cleaned up unless --keep


@pytest.mark.parametrize("final", ["past_due", "unpaid", "canceled"])
@pytest.mark.parametrize("name", ["trial_failed", "renewal_failed"])
async def test_after_the_grace_only_lost_access_is_checked_not_the_status(name, final):
    """What Stripe ends on after the retries is a dashboard setting; any of them passes."""
    driver = FakeClockDriver()
    driver.after_retries = final
    checks = await run_scenario(name, driver, Settings())
    assert failures(checks) == [], failures(checks)
    assert driver.status == final


async def test_keep_flag_is_passed_through():
    driver = FakeClockDriver()
    await run_scenario("trial_start", driver, Settings(), keep=True)
    assert driver.closed is True


async def test_scenarios_cover_the_whole_life_cycle():
    assert {"trial_start", "trial_paid", "trial_failed", "cancel_in_trial", "renewal", "renewal_failed",
            "cancel_after_paid", "payment_recovered"} <= set(SCENARIOS)


async def test_a_setup_error_is_reported_as_a_failed_check_not_a_crash():
    class Broken(FakeClockDriver):
        async def start(self, card, settings):
            raise RuntimeError("Stripe said no")

    checks = await run_scenario("trial_paid", Broken(), Settings())
    assert [c.ok for c in checks] == [False] and "Stripe said no" in checks[0].detail


async def test_script_catches_a_bot_that_cuts_off_a_cancelled_trial():
    """If cancelling in the trial ended access at once, the script must fail it."""
    class CutsOff(FakeClockDriver):
        async def subscription(self):
            sub = await super().subscription()
            if self.status == "canceled":   # pretend Stripe also dropped the trial end
                return replace(sub, trial_end=None, trial_start=None)
            return sub

    checks = await run_scenario("cancel_in_trial_now", CutsOff(), Settings())
    assert any(not c.ok and "keeps access" in c.detail for c in checks)


async def test_script_catches_a_renewal_that_takes_no_money():
    class RenewsForFree(FakeClockDriver):
        def _renew(self, at):
            if at == self.trial_end:
                return super()._renew(at)
            self.period = (at, at + MONTH)   # the period moves on, nothing is charged

    checks = await run_scenario("renewal", RenewsForFree(), Settings())
    assert [c.detail for c in failures(checks)] == ["renewal charged (paid invoices: 1, want 2)"]


async def test_script_catches_a_renewal_that_does_not_move_the_period():
    class StuckPeriod(FakeClockDriver):
        async def subscription(self):
            sub = await super().subscription()
            if self.period[0] > self.trial_end:   # renewed: still report the old period end
                return replace(sub, period_end=self.period[0])
            return sub

    checks = await run_scenario("renewal", StuckPeriod(), Settings())
    assert any("about a month later" in c.detail for c in failures(checks))


async def test_script_catches_a_past_due_that_never_loses_access(monkeypatch):
    def grace_forever(user, now, grace):
        return user.sub_status == "past_due" or is_entitled(user, now, grace)

    monkeypatch.setattr(script, "is_entitled", grace_forever)
    checks = await run_scenario("renewal_failed", FakeClockDriver(), Settings())
    assert [c.step for c in failures(checks)] == ["grace runs out"]
    assert "no longer entitled after the grace" in failures(checks)[0].detail


async def test_script_catches_a_grace_counted_from_the_wrong_moment():
    """A grace anchored when the bot first saw past_due (two hours late) slips past the
    after-grace check, so the anchor is checked on its own."""
    class LateAnchor(FakeClockDriver):
        async def subscription(self):
            sub = await super().subscription()
            if self.status == "past_due":
                return replace(sub, period_start=self._now)
            return sub

    checks = await run_scenario("renewal_failed", LateAnchor(), Settings())
    assert [c.step for c in failures(checks)] == ["renewal on a failing card"]
    assert failures(checks)[0].detail.startswith("grace counts from")


async def test_script_catches_the_wrong_goodbye_after_a_paid_month(monkeypatch):
    """Without the audit log's "was active", a paid-then-cancelled month looks like a
    failed trial; the script must notice the sweep would send the trial goodbye."""
    async def never_active(svc, user):
        return False

    monkeypatch.setattr(script, "_was_active", never_active)
    checks = await run_scenario("cancel_after_paid", FakeClockDriver(), Settings())
    assert [c.detail for c in failures(checks)] == ["goodbye message would be 'trial_ended', want 'sub_ended'"]


async def test_script_catches_a_charge_after_cancelling():
    class IgnoresCancellation(FakeClockDriver):
        async def advance_to(self, when):
            scheduled, self.cancel_at_end = self.cancel_at_end, False
            await super().advance_to(when)
            self.cancel_at_end = scheduled

    checks = await run_scenario("cancel_after_paid", IgnoresCancellation(), Settings())
    assert any("charged exactly once (paid invoices: 2)" == c.detail for c in failures(checks))


async def test_script_catches_a_payment_that_does_not_restore_the_subscription():
    class PayIgnored(FakeClockDriver):
        async def pay_open_invoices(self):
            return 0

    checks = await run_scenario("payment_recovered", PayIgnored(), Settings())
    details = [c.detail for c in failures(checks)]
    assert "status is 'past_due', want 'active'" in details
    assert any("past-due grace cleared" in d for d in details)


async def test_a_declined_payment_is_a_failed_check():
    class CardStaysBad(FakeClockDriver):
        async def set_card(self, card):
            self.card = BAD_CARD

    checks = await run_scenario("payment_recovered", CardStaysBad(), Settings())
    assert failures(checks)[-1].step == "error" and "declined" in failures(checks)[-1].detail


async def test_reminder_lead_follows_settings():
    checks = await run_scenario("trial_start", FakeClockDriver(),
                                Settings(reminder_days_before=5))
    assert all(c.ok for c in checks)


def test_settings_read_from_env():
    s = script.load_settings({"TRIAL_DAYS": "7", "TRIAL_REMINDER_DAYS_BEFORE": "2", "PAST_DUE_GRACE_HOURS": "48"})
    assert (s.trial_days, s.reminder_days_before, s.past_due_grace) == (7, 2, timedelta(hours=48))
    assert script.load_settings({}) == Settings()


async def test_live_key_is_refused(monkeypatch, capsys):
    import argparse

    monkeypatch.setenv("STRIPE_TEST_CLOCK_KEY", "sk_live_" + "x" * 20)
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_1")
    code = await script.main_async(argparse.Namespace(scenario=None, keep=False))
    assert code == 2 and "TEST-mode key" in capsys.readouterr().out


# --- clock advances in steps Stripe accepts ---------------------------------------------

def test_advance_steps_never_jump_further_than_one_step():
    stops = script.advance_steps(T0, T0 + 70 * D, 28 * D)
    assert stops == [T0 + 28 * D, T0 + 56 * D, T0 + 70 * D]
    assert script.advance_steps(T0, T0 + 2 * H, 28 * D) == [T0 + 2 * H]
    assert script.advance_steps(T0, T0, 28 * D) == []
    with pytest.raises(ValueError):
        script.advance_steps(T0, T0 - H, 28 * D)


@pytest.mark.parametrize("recurring, step", [
    ({"interval": "month", "interval_count": 1}, 28 * D),
    ({"interval": "month", "interval_count": 3}, 84 * D),
    ({"interval": "week", "interval_count": 1}, 7 * D),
    ({"interval": "year", "interval_count": 1}, 365 * D),
    (None, D),
])
def test_max_advance_is_one_interval_of_the_price(recurring, step):
    sub = {"items": {"data": [{"price": {"recurring": recurring}}]}}
    assert script.max_advance(sub) == step


# --- the real driver's Stripe calls, against a recording client -------------------------

class RecordingClient:
    """Just enough of stripe.StripeClient's v1 surface for the driver, recording calls."""

    def __init__(self, invoices=()):
        self.calls: list[tuple] = []
        self.frozen = int(T0.timestamp())
        self.invoices = [SimpleNamespace(**inv) for inv in invoices]
        self._pm = 0
        self.v1 = SimpleNamespace(
            test_helpers=SimpleNamespace(test_clocks=SimpleNamespace(
                create=self._clock_create, retrieve=self._clock_retrieve, advance=self._clock_advance)),
            customers=SimpleNamespace(create=self._record("customers.create", SimpleNamespace(id="cus_1")),
                                      update=self._record("customers.update")),
            payment_methods=SimpleNamespace(attach=self._attach),
            subscriptions=SimpleNamespace(create=self._sub_create),
            invoices=SimpleNamespace(list=self._invoice_list, pay=self._record("invoices.pay")),
        )

    def _record(self, name, result=None):
        def call(*args):
            self.calls.append((name, *args))
            return result
        return call

    def named(self, name):
        return [c[1:] for c in self.calls if c[0] == name]

    def _clock(self):
        return SimpleNamespace(id="clock_1", frozen_time=self.frozen, status="ready")

    def _clock_create(self, params):
        self.frozen = params["frozen_time"]
        return self._clock()

    def _clock_retrieve(self, clock_id):
        return self._clock()

    def _clock_advance(self, clock_id, params):
        assert params["frozen_time"] > self.frozen, "Stripe refuses a clock that doesn't move forward"
        self.calls.append(("test_clocks.advance", params["frozen_time"]))
        self.frozen = params["frozen_time"]
        return self._clock()

    def _attach(self, card, params):
        self._pm += 1
        self.calls.append(("payment_methods.attach", card, params))
        return SimpleNamespace(id=f"pm_attached_{self._pm}")   # a new PaymentMethod, not the token

    def _sub_create(self, params):
        self.calls.append(("subscriptions.create", params))
        price = {"id": params["items"][0]["price"], "recurring": {"interval": "month", "interval_count": 1}}
        data = {"id": "sub_1", "items": {"data": [{"price": price}]}}
        return SimpleNamespace(id="sub_1", to_dict=lambda: data)

    def _invoice_list(self, params):
        self.calls.append(("invoices.list", params))
        wanted = params.get("status")
        return SimpleNamespace(data=[inv for inv in self.invoices if wanted in (None, inv.status)])


def real_driver(client):
    driver = script.StripeClockDriver("sk_test_unused", "price_1", client=client)
    driver.clock_id, driver.customer_id = "clock_1", "cus_1"
    return driver


async def test_real_driver_advances_a_long_jump_in_steps():
    client = RecordingClient()
    driver = real_driver(client)
    driver.max_step = 28 * D
    await driver.advance_to(T0 + 31 * D + timedelta(microseconds=500))   # sub-second part dropped
    assert client.named("test_clocks.advance") == [(int((T0 + 28 * D).timestamp()),),
                                                   (int((T0 + 31 * D).timestamp()),)]


async def test_real_driver_start_uses_the_attached_card_and_the_price_interval():
    client = RecordingClient()
    driver = script.StripeClockDriver("sk_test_unused", "price_1", client=client)
    await driver.start(BAD_CARD, Settings())
    assert client.named("payment_methods.attach") == [(BAD_CARD, {"customer": "cus_1"})]
    assert client.named("customers.update") == [
        ("cus_1", {"invoice_settings": {"default_payment_method": "pm_attached_1"}})]
    (params,), = client.named("subscriptions.create")
    assert params["trial_end"] == int((driver.t0 + 14 * D).timestamp())
    assert driver.max_step == 28 * D


async def test_real_driver_set_card_makes_the_new_card_the_default():
    client = RecordingClient()
    driver = real_driver(client)
    await driver.set_card(GOOD_CARD)
    await driver.set_card(BAD_CARD)
    assert client.named("customers.update")[-1] == (
        "cus_1", {"invoice_settings": {"default_payment_method": "pm_attached_2"}})
    assert driver.card_id == "pm_attached_2"


async def test_real_driver_pays_open_invoices_with_the_current_card_and_counts_real_charges():
    client = RecordingClient(invoices=[
        {"id": "in_trial", "status": "paid", "amount_paid": 0},
        {"id": "in_1", "status": "open", "amount_paid": 0},
        {"id": "in_2", "status": "open", "amount_paid": 0},
        {"id": "in_0", "status": "paid", "amount_paid": PRICE},
    ])
    driver = real_driver(client)
    await driver.set_card(GOOD_CARD)
    assert await driver.pay_open_invoices() == 2
    assert client.named("invoices.pay") == [("in_1", {"payment_method": "pm_attached_1"}),
                                            ("in_2", {"payment_method": "pm_attached_1"})]
    assert await driver.paid_invoice_count() == 1          # the £0 trial invoice doesn't count
    assert await driver.amount_paid() == PRICE
    assert all(p[0]["customer"] == "cus_1" for p in client.named("invoices.list"))
