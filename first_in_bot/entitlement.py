"""The single source of truth for "should this person have paid-channel access?".

Pure function, clock passed in. Nothing else in the codebase may decide entitlement.
"""
from datetime import datetime, timedelta

from .models import User

ENTITLED_SUB_STATUSES = frozenset({"active", "trialing"})


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
    if user.sub_status == "past_due" and user.sub_period_end:
        return now < user.sub_period_end + past_due_grace
    return False
