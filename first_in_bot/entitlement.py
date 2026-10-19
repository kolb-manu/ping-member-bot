"""The single source of truth for "should this person have paid-channel access?".

Pure function, clock passed in. Nothing else in the codebase may decide entitlement.
"""
from datetime import datetime, timedelta

from .models import User

ENTITLED_SUB_STATUSES = frozenset({"active", "trialing"})


def past_due_until(user: User, past_due_grace: timedelta) -> datetime | None:
    """When a past_due user's access ends.

    Deliberate reading of spec 5.2's `sub_period_end + PAST_DUE_GRACE`: Stripe
    moves a subscription into its next period as soon as it creates the renewal
    (or trial-conversion) invoice, paid or not, so for past_due the stored period
    end is about a month away. The grace is counted from when the unpaid period
    began instead (= the end of the last paid period, which is what the spec
    meant): past_due_since, else the current period's start. Rows synced before
    migration 3 have neither yet, so they keep the old rule until the next sync.
    """
    anchor = user.past_due_since or user.sub_period_start
    if anchor is not None:
        return anchor + past_due_grace
    if user.sub_period_end is not None:
        return user.sub_period_end + past_due_grace
    return None


def is_entitled(user: User, now: datetime, past_due_grace: timedelta) -> bool:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if user.banned:
        return False
    if user.manual_grant_until and now < user.manual_grant_until:
        return True
    if user.trial_ends_at and now < user.trial_ends_at:
        return True
    if user.sub_status in ENTITLED_SUB_STATUSES:
        return True
    if user.sub_status == "past_due":
        until = past_due_until(user, past_due_grace)
        return until is not None and now < until
    return False
