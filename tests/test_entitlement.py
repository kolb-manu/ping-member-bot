from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from first_in_bot.entitlement import is_entitled
from first_in_bot.models import User
from first_in_bot.timeutil import format_local

GRACE = timedelta(hours=72)
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def user(**kw) -> User:
    return User(telegram_id=1, **kw)


def test_brand_new_user_not_entitled():
    assert not is_entitled(user(), NOW, GRACE)


def test_trial_running():
    assert is_entitled(user(trial_ends_at=NOW + H), NOW, GRACE)


def test_trial_boundary_exactly_at_end_is_not_entitled():
    assert not is_entitled(user(trial_ends_at=NOW), NOW, GRACE)
    assert is_entitled(user(trial_ends_at=NOW + timedelta(seconds=1)), NOW, GRACE)


def test_trial_over():
    assert not is_entitled(user(trial_ends_at=NOW - H), NOW, GRACE)


def test_manual_grant():
    assert is_entitled(user(manual_grant_until=NOW + H), NOW, GRACE)
    assert not is_entitled(user(manual_grant_until=NOW), NOW, GRACE)
    assert not is_entitled(user(manual_grant_until=NOW - H), NOW, GRACE)


@pytest.mark.parametrize("status", ["active", "trialing"])
def test_active_statuses(status):
    assert is_entitled(user(sub_status=status), NOW, GRACE)


def test_cancelled_but_paid_up_stays_entitled():
    # Stripe keeps status=active until the period ends.
    assert is_entitled(user(sub_status="active", cancel_at_period_end=True,
                            sub_period_end=NOW + H), NOW, GRACE)


@pytest.mark.parametrize("status", ["canceled", "incomplete", "incomplete_expired",
                                    "unpaid", "paused", "disputed", "", None])
def test_other_statuses_not_entitled(status):
    assert not is_entitled(user(sub_status=status, sub_period_end=NOW + H), NOW, GRACE)


def test_past_due_within_grace():
    assert is_entitled(user(sub_status="past_due", sub_period_end=NOW - H), NOW, GRACE)


def test_past_due_grace_boundary():
    end = NOW - GRACE
    assert not is_entitled(user(sub_status="past_due", sub_period_end=end), NOW, GRACE)
    assert is_entitled(user(sub_status="past_due", sub_period_end=end + timedelta(seconds=1)), NOW, GRACE)


def test_past_due_without_period_end_not_entitled():
    assert not is_entitled(user(sub_status="past_due"), NOW, GRACE)


def test_banned_overrides_everything():
    u = user(banned=True, manual_grant_until=NOW + H, trial_ends_at=NOW + H,
             sub_status="active")
    assert not is_entitled(u, NOW, GRACE)


def test_any_one_route_is_enough():
    assert is_entitled(user(trial_ends_at=NOW - H, sub_status="active"), NOW, GRACE)
    assert is_entitled(user(sub_status="canceled", manual_grant_until=NOW + H), NOW, GRACE)


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        is_entitled(user(), datetime(2026, 11, 1, 12, 0), GRACE)


def test_trial_across_october_clock_change():
    """UK clocks go back on 25 Oct 2026. Trial math is in UTC, so a 14-day trial
    is exactly 336 hours, and the London wall-clock end time shifts by an hour."""
    london = ZoneInfo("Europe/London")
    started = datetime(2026, 10, 20, 12, 0, tzinfo=london).astimezone(timezone.utc)
    ends = started + timedelta(days=14)
    assert ends - started == timedelta(hours=336)
    assert format_local(ends, london) == "3 Nov 2026, 11:00"
    u = user(trial_started_at=started, trial_ends_at=ends)
    assert is_entitled(u, ends - timedelta(seconds=1), GRACE)
    assert not is_entitled(u, ends, GRACE)
