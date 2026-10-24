"""Owner alerts (spec section 10): a DM to the owner when something needs a look,
at most one per kind of problem every 30 minutes.

The last-sent time lives in kv (alert_last:<kind>), not in memory, so a bot stuck
in a restart loop can't send the startup alert every 10 seconds. The stamp is only
written once Telegram took the DM, or when retrying can't help (the owner blocked
the bot, or Telegram rejected the text). A network failure leaves it unstamped, so
the next occurrence tries again rather than going quiet for half an hour.

Alerts run inside jobs and the startup path, so they never raise: an alert that
fails is logged and the job carries on. Exception text only ever reaches an alert
through error_text, which keeps it short and strips anything that looks like a key.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta

from .db import kv_set
from .services import Services
from .stripe_gateway import _redact
from .telegram_gateway import SendResult
from .timeutil import from_iso, to_iso

log = logging.getLogger(__name__)

ALERT_INTERVAL = timedelta(minutes=30)
KEY_PREFIX = "alert_last:"
ERROR_TEXT_LIMIT = 300
# A Telegram bot token, in case one ever turns up in an exception message. No \b:
# in a request URL it follows "bot" directly.
_BOT_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")


def short_error(exc: BaseException) -> str:
    """'TypeName: message', on one line, keys and tokens blanked, cut to about
    ERROR_TEXT_LIMIT characters. Plain text: escape it before putting it in HTML."""
    message = _BOT_TOKEN_RE.sub("[token]", _redact(" ".join(str(exc).split())))
    text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text if len(text) <= ERROR_TEXT_LIMIT else text[: ERROR_TEXT_LIMIT - 1] + "…"


def error_text(exc: BaseException) -> str:
    """short_error, HTML-escaped, ready for an alert."""
    # Escaped after cutting, so the cut can't leave half an entity behind.
    return html.escape(short_error(exc))


async def send_owner(svc: Services, text: str) -> SendResult:
    """DM the owner (HTML) with no rate limit, for messages that are once-only
    already. Never raises: an unexpected error counts as FAILED, so callers retry."""
    if svc.owner_id is None:
        log.warning("No owner to message: ADMIN_IDS is empty")
        return SendResult.REJECTED
    try:
        return await svc.tg.send(svc.owner_id, text)
    except Exception:
        log.exception("DM to the owner failed")
        return SendResult.FAILED


async def alert_owner(
    svc: Services, kind: str, text: str, now: datetime, *, interval: timedelta = ALERT_INTERVAL
) -> bool:
    """DM the owner about a problem of this kind unless one went out less than
    interval ago. True only if Telegram accepted it."""
    try:
        return await _alert(svc, kind, text, now, interval)
    except Exception:
        log.exception("Owner alert %s failed", kind)
        return False


async def _alert(svc: Services, kind: str, text: str, now: datetime, interval: timedelta) -> bool:
    key = KEY_PREFIX + kind
    last = from_iso(await svc.db.kv_get(key))
    # A stamp in the future (the clock went back) doesn't silence alerts.
    if last is not None and timedelta(0) <= now - last < interval:
        log.info("Owner alert %s not sent: one went out at %s", kind, to_iso(last))
        return False
    result = await send_owner(svc, text)
    if result is SendResult.FAILED:
        log.warning("Owner alert %s couldn't be sent; the next one will try again", kind)
        return False
    async with svc.db.tx() as conn:
        await kv_set(conn, key, to_iso(now))
    if result is not SendResult.OK:
        # BLOCKED (the owner hasn't pressed /start) or REJECTED: retrying won't help.
        log.warning("The owner didn't get the %s alert (%s)", kind, result.value)
        return False
    log.info("Owner alert sent: %s", kind)
    return True
