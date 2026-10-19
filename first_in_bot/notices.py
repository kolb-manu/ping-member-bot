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

TRIAL_REMINDER_LEAD = timedelta(days=2)


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


def trial_reminder_due(user: User, now: datetime) -> bool:
    """2 days before a Stripe trial converts. Only while it's still relevant: still
    trialing, not cancelled, and the trial hasn't ended yet (e.g. after downtime)."""
    end = user.sub_trial_end
    return (
        user.sub_status == "trialing"
        and end is not None
        and not user.cancel_at_period_end
        and not user.blocked_bot
        and not user.banned
        and end - TRIAL_REMINDER_LEAD <= now < end
    )


async def send_notices(svc: Services, now: datetime) -> int:
    """Scheduler job. Returns how many notices went out."""
    rows = await svc.db.fetchall(
        "SELECT * FROM users WHERE sub_status='trialing' AND sub_trial_end IS NOT NULL "
        "AND cancel_at_period_end=0 AND blocked_bot=0 AND banned=0"
    )
    sent = 0
    for row in rows:
        user = User.from_row(row)
        if not trial_reminder_due(user, now):
            continue
        assert user.sub_trial_end is not None
        text = texts.TRIAL_REMINDER_2D.format(
            end_date=format_local(user.sub_trial_end, svc.config.timezone),
            price=texts.price_text(svc.price_label),
        )
        outcome = await deliver_notice(
            svc, user, "trial_2d", to_iso(user.sub_trial_end), text, manage_keyboard(), now
        )
        sent += outcome is NoticeOutcome.SENT
    # TODO: the spec's "last day" reminder (3.3 step 6) isn't built; ask the owner if
    # it's still wanted now that the trial converts automatically via Stripe.
    return sent
