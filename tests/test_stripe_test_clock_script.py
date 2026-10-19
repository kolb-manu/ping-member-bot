"""The test-clock script's scenario logic, run against a fake Stripe that behaves
the way Stripe does for a trial (no network, no real clocks)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from scripts import stripe_test_clock as script
from scripts.stripe_test_clock import GOOD_CARD, SCENARIOS, Settings, run_scenario
from tests.fakes import sub_info

D = timedelta(days=1)
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class FakeClockDriver:
    """Minimal model of a Stripe trial subscription over time."""

    def __init__(self):
        self.t0 = T0
        self._now = T0
        self.card = GOOD_CARD
        self.days = 14
        self.cancel_at_end = False
        self.cancelled_now = False
        self.closed = None

    async def start(self, card, settings):
        self.card, self.days = card, settings.trial_days

    async def now(self):
        return self._now

    async def advance_to(self, when):
        assert when >= self._now, "test clocks only move forward"
        self._now = when

    async def subscription(self):
        end = self.t0 + timedelta(days=self.days)
        common = dict(id="sub_tc", customer="cus_tc", telegram_id=script.TELEGRAM_ID, created=self.t0,
                      trial_start=self.t0, trial_end=end)
        if self.cancelled_now:
            return sub_info(status="canceled", period_start=self.t0, period_end=end, **common)
        if self._now < end:
            return sub_info(status="trialing", period_start=self.t0, period_end=end,
                            cancel=self.cancel_at_end, **common)
        if self.cancel_at_end:
            return sub_info(status="canceled", period_start=self.t0, period_end=end, **common)
        status = "active" if self.card == GOOD_CARD else "past_due"
        return sub_info(status=status, period_start=end, period_end=end + 30 * D, **common)

    async def cancel(self, at_period_end):
        if at_period_end:
            self.cancel_at_end = True
        else:
            self.cancelled_now = True

    async def amount_paid(self):
        end = self.t0 + timedelta(days=self.days)
        if self.card == GOOD_CARD and not self.cancel_at_end and not self.cancelled_now \
                and self._now >= end + timedelta(hours=1):
            return 299
        return 0

    async def close(self, keep):
        self.closed = keep


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_every_scenario_passes_against_a_stripe_like_fake(name):
    driver = FakeClockDriver()
    checks = await run_scenario(name, driver, Settings())
    failed = [c for c in checks if not c.ok]
    assert len(checks) >= 5 and failed == [], failed
    assert driver.closed is False                      # clocks cleaned up unless --keep


async def test_keep_flag_is_passed_through():
    driver = FakeClockDriver()
    await run_scenario("trial_start", driver, Settings(), keep=True)
    assert driver.closed is True


async def test_scenarios_cover_the_four_cases_asked_for():
    assert {"trial_start", "trial_paid", "trial_failed", "cancel_in_trial"} <= set(SCENARIOS)


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
            if self.cancelled_now:   # pretend Stripe also dropped the trial end
                return replace(sub, trial_end=None, trial_start=None)
            return sub

    checks = await run_scenario("cancel_in_trial_now", CutsOff(), Settings())
    assert any(not c.ok and "keeps access" in c.detail for c in checks)


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
