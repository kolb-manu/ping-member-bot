"""The /status text. Whether the user has access comes from is_entitled; the
rest just describes their trial or subscription, with dates in local time."""
from __future__ import annotations

import html
from datetime import datetime

from . import texts
from .config import Config
from .entitlement import DISPUTED_STATUS, is_entitled, past_due_until
from .models import User
from .timeutil import format_local
from .users import trial_eligible


def _contact(config: Config) -> str:
    if config.support_contact:
        return texts.CONTACT_SUPPORT.format(support=html.escape(config.support_contact))
    return texts.CONTACT_LATER


def status_text(user: User, now: datetime, config: Config, price_label: str | None) -> str:
    if user.banned:
        return texts.UNAVAILABLE
    tz = config.timezone
    price = texts.price_text(price_label)
    entitled = is_entitled(user, now, config.past_due_grace)
    lines = [texts.STATUS_HEADER, texts.STATUS_ACCESS_YES if entitled else texts.STATUS_ACCESS_NO]

    status = user.sub_status
    detail: str | None = None
    if status == "trialing":
        if user.sub_trial_end is None:
            detail = texts.STATUS_TRIALING_NO_DATE
        else:
            end = format_local(user.sub_trial_end, tz)
            template = texts.STATUS_TRIAL_CANCELLED if user.cancel_at_period_end else texts.STATUS_TRIALING
            detail = template.format(end=end, price=price)
    elif status == "active":
        if user.sub_period_end is None:
            detail = texts.STATUS_ACTIVE_NO_DATE
        elif user.cancel_at_period_end:
            detail = texts.STATUS_CANCELLED.format(end=format_local(user.sub_period_end, tz))
        else:
            detail = texts.STATUS_ACTIVE.format(end=format_local(user.sub_period_end, tz), price=price)
    elif status == DISPUTED_STATUS:
        detail = texts.STATUS_DISPUTED.format(contact=_contact(config))
    elif status == "canceled" and user.sub_trial_end and now < user.sub_trial_end:
        detail = texts.STATUS_TRIAL_CANCELLED.format(end=format_local(user.sub_trial_end, tz))
    elif status == "past_due":
        until = past_due_until(user, config.past_due_grace)
        if until is not None and now < until:
            detail = texts.STATUS_PAST_DUE_UNTIL.format(end=format_local(until, tz))
        else:
            detail = texts.STATUS_PAST_DUE
    elif status == "unpaid":
        detail = texts.STATUS_PAST_DUE
    elif status == "incomplete":
        detail = texts.STATUS_INCOMPLETE
    elif status:
        detail = texts.STATUS_ENDED

    grant = None
    if user.manual_grant_until and now < user.manual_grant_until:
        grant = texts.STATUS_GRANT.format(end=format_local(user.manual_grant_until, tz))

    if detail:
        lines.append(detail)
    if grant:
        lines.append(grant)
    if not detail and not grant:
        lines.append(texts.STATUS_NONE_TRIAL_TEMPLATE.format(trial_length=texts.trial_length(config.trial_days))
                     if trial_eligible(user) else texts.STATUS_NONE)
    return "\n".join(lines)
