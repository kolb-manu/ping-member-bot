"""Daily disputes check (spec 7.4). A new dispute on a user's charge sets their
sub_status to the local "disputed" (not entitled) and alerts the owner. It never
bans anyone. When every dispute of a disputed user has been won, the status is
restored from Stripe.

write_subscription keeps a "disputed" status through the 5-minute sync, so the
flag only ever changes here.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime

from .audit import audit
from .billing import write_subscription
from .entitlement import DISPUTED_STATUS
from .services import Services
from .stripe_gateway import DisputeInfo, StripeGatewayError, SubscriptionMissing
from .telegram_gateway import SendResult
from .timeutil import to_iso
from .users import get_user

log = logging.getLogger(__name__)

# Dispute statuses that end in the business's favour. A "lost" one stays disputed.
RESOLVED_FOR_US = frozenset({"won", "warning_closed"})


@dataclass
class DisputeResult:
    seen: int = 0
    new: int = 0
    held: int = 0          # users newly put on hold
    alerts_sent: int = 0
    cleared: int = 0
    unmatched: int = 0     # disputes we couldn't tie to a user
    ok: bool = True


async def _user_for_customer(svc: Services, customer_id: str | None) -> int | None:
    if not customer_id:
        return None
    row = await svc.db.fetchone("SELECT telegram_id FROM users WHERE stripe_customer_id=?", (customer_id,))
    return row["telegram_id"] if row else None


async def _record(svc: Services, d: DisputeInfo, telegram_id: int | None, now: datetime) -> bool:
    """Store a new dispute (True) or refresh an existing one (False). A new dispute
    that is already resolved in our favour (old history) is stored as alerted and
    puts nobody on hold."""
    async with svc.db.tx() as conn:
        cur = await conn.execute("SELECT telegram_id, status FROM disputes WHERE id=?", (d.id,))
        old = await cur.fetchone()
        if old is not None:
            if old["status"] != d.status or (old["telegram_id"] is None and telegram_id is not None):
                await conn.execute(
                    "UPDATE disputes SET status=?, telegram_id=COALESCE(telegram_id, ?) WHERE id=?",
                    (d.status, telegram_id, d.id),
                )
            return False
        resolved = d.status in RESOLVED_FOR_US
        await conn.execute(
            "INSERT INTO disputes(id, telegram_id, customer_id, charge_id, status, created_at, seen_at, alerted) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (d.id, telegram_id, d.customer_id, d.charge_id, d.status,
             to_iso(d.created) if d.created else None, to_iso(now), int(resolved)),
        )
        await audit(conn, now, telegram_id, "dispute_seen", f"{d.id} status={d.status}")
    return True


async def _hold(svc: Services, telegram_id: int, d: DisputeInfo, now: datetime) -> bool:
    async with svc.db.tx() as conn:
        cur = await conn.execute("SELECT sub_status FROM users WHERE telegram_id=?", (telegram_id,))
        row = await cur.fetchone()
        if row is None or row["sub_status"] == DISPUTED_STATUS:
            return False
        await conn.execute("UPDATE users SET sub_status=? WHERE telegram_id=?", (DISPUTED_STATUS, telegram_id))
        await audit(conn, now, telegram_id, "disputed", f"{d.id} was={row['sub_status'] or 'none'}")
    return True


async def _alert_owner(svc: Services, d: DisputeInfo, telegram_id: int | None) -> bool:
    """DM the owner once per dispute. False only if the DM failed on the network, so
    the next run tries again."""
    user = await get_user(svc.db, telegram_id) if telegram_id is not None else None
    if user is None:
        who = f"customer {html.escape(d.customer_id or 'unknown')} (no matching user)"
    else:
        name = f" @{user.username}" if user.username else ""
        who = html.escape(f"{user.telegram_id}{name}")
    text = (
        "⚠️ <b>Payment dispute</b>\n"
        f"Who: {who}\n"
        f"Dispute: {html.escape(d.id)} ({html.escape(d.status)})\n"
        + ("Their access is on hold (status 'disputed'). I have not banned them.\n" if user else "")
        + "Look at it in the Stripe dashboard."
    )
    if svc.owner_id is None:
        log.warning("Dispute %s: no owner to alert", d.id)
        return True
    result = await svc.tg.send(svc.owner_id, text)
    if result is SendResult.FAILED:
        return False
    if result is not SendResult.OK:
        log.warning("The owner didn't get the dispute alert for %s (%s)", d.id, result.value)
    return True


async def _forget_status(svc: Services, telegram_id: int) -> None:
    async with svc.db.tx() as conn:
        await conn.execute(
            "UPDATE users SET sub_status=NULL WHERE telegram_id=? AND sub_status=?", (telegram_id, DISPUTED_STATUS)
        )


async def _clear_resolved(svc: Services, now: datetime, result: DisputeResult) -> None:
    """Restore users whose disputes have all been won, from Stripe's own copy."""
    assert svc.stripe is not None
    rows = await svc.db.fetchall(
        "SELECT telegram_id, stripe_sub_id FROM users WHERE sub_status=? AND NOT EXISTS ("
        " SELECT 1 FROM disputes d WHERE d.telegram_id=users.telegram_id"
        " AND d.status NOT IN ('won','warning_closed'))"
        " AND EXISTS (SELECT 1 FROM disputes d WHERE d.telegram_id=users.telegram_id)",
        (DISPUTED_STATUS,),
    )
    for row in rows:
        telegram_id = row["telegram_id"]
        try:
            sub = None
            if row["stripe_sub_id"]:
                try:
                    sub = await svc.stripe.get_subscription(row["stripe_sub_id"])
                except SubscriptionMissing:
                    sub = None
            if sub is not None:
                await write_subscription(svc.db, telegram_id, sub, now, clear_dispute=True)
            else:
                await _forget_status(svc, telegram_id)
        except StripeGatewayError:
            result.ok = False   # still on hold; tried again next run
            continue
        async with svc.db.tx() as conn:
            await audit(conn, now, telegram_id, "dispute_cleared", "all disputes won")
        result.cleared += 1


async def check_disputes(svc: Services, now: datetime) -> DisputeResult:
    """Scheduler job (daily). A Stripe failure raises, so the scheduler logs it."""
    result = DisputeResult()
    if svc.stripe is None:
        return result
    disputes = await svc.stripe.list_disputes()
    result.seen = len(disputes)
    for d in disputes:
        telegram_id = await _user_for_customer(svc, d.customer_id)
        if telegram_id is None:
            result.unmatched += 1
        if await _record(svc, d, telegram_id, now):
            result.new += 1
        if telegram_id is not None and d.status not in RESOLVED_FOR_US and await _hold(svc, telegram_id, d, now):
            result.held += 1
        row = await svc.db.fetchone("SELECT alerted FROM disputes WHERE id=?", (d.id,))
        if row is not None and not row["alerted"]:
            if await _alert_owner(svc, d, telegram_id):
                async with svc.db.tx() as conn:
                    await conn.execute("UPDATE disputes SET alerted=1 WHERE id=?", (d.id,))
                result.alerts_sent += 1
    await _clear_resolved(svc, now, result)
    log.info("Disputes check: %d seen, %d new, %d users put on hold, %d cleared, %d unmatched",
             result.seen, result.new, result.held, result.cleared, result.unmatched)
    return result
