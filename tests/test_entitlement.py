from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from first_in_bot.entitlement import is_entitled, past_due_until
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


# What Stripe really stores for past_due: the renewal (or trial-conversion) invoice
# already opened the new, unpaid period, so its end is about a month ahead and its
# start is when payment became due.
PERIOD = timedelta(days=30)


def past_due(started_ago, **kw):
    start = NOW - started_ago
    return user(sub_status="past_due", sub_period_start=start, sub_period_end=start + PERIOD, **kw)


@pytest.mark.parametrize("ago,entitled", [(H, True), (71 * H, True), (73 * H, False), (10 * 24 * H, False)])
def test_past_due_grace_counts_from_the_unpaid_period_start(ago, entitled):
    u = past_due(ago)
    assert u.sub_period_end > NOW   # the period end is no use for the grace
    assert is_entitled(u, NOW, GRACE) is entitled


def test_past_due_grace_boundary():
    assert not is_entitled(past_due(GRACE), NOW, GRACE)
    assert is_entitled(past_due(GRACE - timedelta(seconds=1)), NOW, GRACE)
    assert past_due_until(past_due(H), GRACE) == NOW - H + GRACE


def test_past_due_since_wins_over_a_newer_period_start():
    # Stripe set to "leave past_due": a new unpaid period started, but the grace doesn't restart.
    assert not is_entitled(past_due(H, past_due_since=NOW - 40 * 24 * H), NOW, GRACE)
    assert is_entitled(past_due(H, past_due_since=NOW - 2 * H), NOW, GRACE)


def test_past_due_row_from_before_migration_3_keeps_the_old_rule():
    # No period start stored until the next sync: the old period-end formula
    # still applies, so the upgrade itself can't cut anyone off.
    assert is_entitled(user(sub_status="past_due", sub_period_end=NOW + 20 * 24 * H), NOW, GRACE)
    assert is_entitled(user(sub_status="past_due", sub_period_end=NOW - H), NOW, GRACE)
    assert not is_entitled(user(sub_status="past_due", sub_period_end=NOW - GRACE), NOW, GRACE)


def test_past_due_without_any_period_not_entitled():
    assert not is_entitled(user(sub_status="past_due"), NOW, GRACE)
    assert past_due_until(user(sub_status="past_due"), GRACE) is None


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


def test_cancelled_during_the_trial_keeps_access_until_the_trial_would_have_ended():
    # Cancelled at once (e.g. a portal that doesn't wait for the period end): Stripe says
    # "canceled", but they never paid and were promised the full trial.
    u = user(sub_status="canceled", sub_trial_end=NOW + 3 * H, sub_period_end=NOW + 3 * H)
    assert is_entitled(u, NOW, GRACE)
    assert not is_entitled(u, NOW + 3 * H, GRACE)          # exactly at the trial end
    assert not is_entitled(user(sub_status="canceled", sub_trial_end=NOW - H), NOW, GRACE)


def test_cancelled_trial_with_cancel_at_period_end_stays_trialing_and_entitled():
    u = user(sub_status="trialing", cancel_at_period_end=True, sub_trial_end=NOW + H)
    assert is_entitled(u, NOW, GRACE)


def test_banned_cancelled_trial_not_entitled():
    assert not is_entitled(user(sub_status="canceled", sub_trial_end=NOW + H, banned=True), NOW, GRACE)
