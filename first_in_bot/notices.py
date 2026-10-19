"""One-off DMs (reminders, "you're in", "you've been removed") and the
send_notices job.

Every notice is recorded in notices_sent before it is sent, so a crash can at
worst skip one, never send it twice. A network failure (or an unexpected error
from the send) deletes the row so the next run retries; a 403 keeps the row and
marks the user blocked_bot.
"""
from __future__ import annotations

import enum
import logging
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardMarkup

from . import texts
from .keyboards import manage_keyboard
from .models import User
from .services import Services
from .telegram_gateway import SendResult
from .timeutil import format_local, to_iso

log = logging.getLogger(__name__)

# The last-chance reminder goes out about this long before the first charge. The
# earlier one (TRIAL_REMINDER_DAYS_BEFORE) stops being due once this window starts.
TRIAL_FINAL_LEAD = timedelta(hours=3)
KIND_REMINDER = "trial_reminder"
KIND_FINAL = "trial_final"


class NoticeOutcome(enum.Enum):
    SENT = "sent"
    ALREADY_SENT = "already_sent"
    SKIPPED_BLOCKED = "skipped_blocked"   # blocked_bot=1 already, so nothing was tried
    BLOCKED = "blocked"                   # 403 just now; user marked blocked_bot
    RETRY = "retry"                       # network trouble; row deleted so it's retried
    REJECTED = "rejected"                 # other API error; kept as sent so we don't loop


async def deliver_notice(
    svc: Services,
    user: User,
    kind: str,
    cycle_key: str,
    text: str,
    markup: InlineKeyboardMarkup | None,
    now: datetime,
) -> NoticeOutcome:
    if user.blocked_bot:
        return NoticeOutcome.SKIPPED_BLOCKED
    async with svc.db.tx() as conn:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO notices_sent(telegram_id, kind, cycle_key, sent_at) VALUES(?,?,?,?)",
            (user.telegram_id, kind, cycle_key, to_iso(now)),
        )
        if cur.rowcount == 0:
            return NoticeOutcome.ALREADY_SENT

    try:
        result = await svc.tg.send(user.telegram_id, text, markup)
    except Exception:
        # The gateway maps every Telegram error to a SendResult, so this is
        # unexpected. Like a network failure the outcome is unknown: free the
        # row so the notice is retried rather than silently lost.
        await _forget(svc, user.telegram_id, kind, cycle_key)
        raise
    if result is SendResult.OK:
        log.info("Sent %s notice to %s", kind, user.telegram_id)
        return NoticeOutcome.SENT
    if result is SendResult.FAILED:
        await _forget(svc, user.telegram_id, kind, cycle_key)
        return NoticeOutcome.RETRY
    if result is SendResult.BLOCKED:
        async with svc.db.tx() as conn:
            await conn.execute("UPDATE users SET blocked_bot=1 WHERE telegram_id=?", (user.telegram_id,))
        return NoticeOutcome.BLOCKED
    log.error("%s notice to %s was rejected by Telegram; not retrying", kind, user.telegram_id)
    return NoticeOutcome.REJECTED


async def _forget(svc: Services, telegram_id: int, kind: str, cycle_key: str) -> None:
    async with svc.db.tx() as conn:
        await conn.execute(
            "DELETE FROM notices_sent WHERE telegram_id=? AND kind=? AND cycle_key=?",
            (telegram_id, kind, cycle_key),
        )


def _reminder_candidate(user: User) -> bool:
    """Reminders are only for people who are still on a running, uncancelled Stripe trial."""
    return (
        user.sub_status == "trialing"
        and user.sub_trial_end is not None
        and not user.cancel_at_period_end
        and not user.blocked_bot
        and not user.banned
    )


def trial_reminder_due(user: User, now: datetime, days_before: int) -> bool:
    """The reminder days_before days ahead of the first charge. Only while it's still
    relevant: not cancelled, the trial hasn't ended, and the last-day window hasn't
    started (after downtime the final reminder replaces this one)."""
    if not _reminder_candidate(user):
        return False
    end = user.sub_trial_end
    assert end is not None
    return end - timedelta(days=days_before) <= now < end - TRIAL_FINAL_LEAD


def trial_final_due(user: User, now: datetime) -> bool:
    """The last-day reminder, about 3 hours before the first charge."""
    if not _reminder_candidate(user):
        return False
    end = user.sub_trial_end
    assert end is not None
    return end - TRIAL_FINAL_LEAD <= now < end


async def send_notices(svc: Services, now: datetime) -> int:
    """Scheduler job. Returns how many notices went out. Each kind is sent at most
    once per trial (the cycle key is the trial end), and never to someone who has
    cancelled."""
    rows = await svc.db.fetchall(
        "SELECT * FROM users WHERE sub_status='trialing' AND sub_trial_end IS NOT NULL "
        "AND cancel_at_period_end=0 AND blocked_bot=0 AND banned=0"
    )
    days_before = svc.config.trial_reminder_days_before
    price = texts.price_text(svc.price_label)
    sent = 0
    for row in rows:
        user = User.from_row(row)
        assert user.sub_trial_end is not None
        end_date = format_local(user.sub_trial_end, svc.config.timezone)
        if trial_reminder_due(user, now, days_before):
            kind, template = KIND_REMINDER, texts.TRIAL_REMINDER
        elif trial_final_due(user, now):
            kind, template = KIND_FINAL, texts.TRIAL_FINAL
        else:
            continue
        outcome = await deliver_notice(
            svc, user, kind, to_iso(user.sub_trial_end),
            template.format(end_date=end_date, price=price), manage_keyboard(), now,
        )
        sent += outcome is NoticeOutcome.SENT
    return sent
