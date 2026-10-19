"""Stripe billing: starting a checkout (with the free trial when eligible),
detecting payment by polling (no webhooks in v1), and the full subscription sync.

The free trial is a Stripe subscription trial with a card required (owner
decision 2026-10-18), so users.trial_ends_at is never set here. Access still
comes only from is_entitled, which treats sub_status trialing/active as entitled.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import texts
from .audit import audit, audit_if_changed
from .db import Database, kv_set
from .entitlement import DISPUTED_STATUS, ENTITLED_SUB_STATUSES
from .keyboards import join_channel_keyboard
from .models import User
from .notices import NoticeOutcome, deliver_notice
from .services import Services
from .stripe_gateway import CheckoutInfo, CustomerMissing, StripeGatewayError, SubInfo, SubscriptionMissing
from .timeutil import format_local, from_iso, to_iso
from .users import get_user, trial_eligible

log = logging.getLogger(__name__)

LAST_FULL_SYNC_KEY = "last_full_sync_ok_at"
# Don't hand out a stored checkout link that's about to expire.
CHECKOUT_REUSE_MARGIN = timedelta(minutes=5)
# Give up on an open session Stripe can't find once it's this far past expiry.
CHECKOUT_FORGET_AFTER = timedelta(days=1)
IDEMPOTENCY_BUCKET_SECONDS = 600
# "Best" subscription when a user somehow has several; tie -> newest.
STATUS_RANK = {"active": 3, "trialing": 2, "past_due": 1}
TERMINAL_STATUSES = frozenset({"canceled", "incomplete_expired"})
# Local sub_status (not a Stripe one) for a stored subscription Stripe no longer has.
MISSING_STATUS = "missing"
# More than one checkout-poll pass, less than the 5-minute sync interval.
SYNC_RACE_MARGIN = timedelta(seconds=120)


@dataclass(frozen=True)
class CheckoutOffer:
    kind: str                 # "checkout" | "already_in" | "fix_payment" | "disputed"
    url: str | None = None
    with_trial: bool = False
    reused: bool = False


@dataclass
class PollResult:
    checked: int = 0
    completed: int = 0
    expired: int = 0
    errors: int = 0


@dataclass
class SyncResult:
    subscriptions: int = 0
    users_updated: int = 0
    skipped: int = 0          # a checkout poll stored newer data during the listing
    unmatched: int = 0
    multiple: list[int] = field(default_factory=list)
    ok: bool = True


def idempotency_key(telegram_id: int, with_trial: bool, now: datetime) -> str:
    bucket = int(now.timestamp()) // IDEMPOTENCY_BUCKET_SECONDS
    return f"checkout-{telegram_id}-{'t' if with_trial else 'n'}-{bucket}"


# --- Starting a checkout --------------------------------------------------------

async def start_checkout(svc: Services, user: User, now: datetime) -> CheckoutOffer:
    """Callers check first that Stripe is configured and the user isn't banned."""
    if svc.stripe is None:
        raise RuntimeError("Stripe is not configured")
    # Billing decision, not an access one: never sell a second subscription to
    # someone who already has a live one.
    if user.sub_status in ENTITLED_SUB_STATUSES:
        return CheckoutOffer("already_in")
    if user.sub_status == "past_due":
        return CheckoutOffer("fix_payment")
    if user.sub_status == DISPUTED_STATUS:
        return CheckoutOffer("disputed")

    with_trial = trial_eligible(user)
    row = await svc.db.fetchone(
        "SELECT id, url, with_trial FROM checkout_sessions "
        "WHERE telegram_id=? AND status='open' AND expires_at>? AND url IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (user.telegram_id, to_iso(now + CHECKOUT_REUSE_MARGIN)),
    )
    if row is not None and bool(row["with_trial"]) == with_trial:
        log.info("Reusing open checkout %s for %s", row["id"], user.telegram_id)
        return CheckoutOffer("checkout", url=row["url"], with_trial=with_trial, reused=True)

    customer_id = await _get_or_create_customer(svc, user, now)
    key = idempotency_key(user.telegram_id, with_trial, now)
    try:
        info = await _create_checkout(svc, user.telegram_id, customer_id, with_trial, key)
    except CustomerMissing:
        # Deleted at Stripe (e.g. test data reset): make a new customer and try once more,
        # with a new key, because the old one would replay the failed request.
        customer_id = await replace_missing_customer(svc, user.telegram_id, customer_id, now)
        info = await _create_checkout(svc, user.telegram_id, customer_id, with_trial, f"{key}-{customer_id}")
    if not info.url:
        raise StripeGatewayError("Stripe returned a checkout session without a URL")
    async with svc.db.tx() as conn:
        # OR IGNORE: a double tap inside the idempotency window returns the same session.
        cur = await conn.execute(
            "INSERT OR IGNORE INTO checkout_sessions(id, telegram_id, created_at, expires_at, status, url, with_trial) "
            "VALUES(?,?,?,?,?,?,?)",
            (info.id, user.telegram_id, to_iso(now), to_iso(info.expires_at), "open", info.url, int(with_trial)),
        )
        if cur.rowcount == 1:
            await audit(conn, now, user.telegram_id, "checkout_created",
                        f"{info.id} trial={'yes' if with_trial else 'no'}")
    return CheckoutOffer("checkout", url=info.url, with_trial=with_trial)


async def _create_checkout(svc: Services, telegram_id: int, customer_id: str, with_trial: bool,
                           key: str) -> CheckoutInfo:
    assert svc.stripe is not None
    return await svc.stripe.create_checkout(
        customer_id=customer_id,
        telegram_id=telegram_id,
        price_id=svc.config.stripe_price_id,
        trial_days=svc.config.trial_days if with_trial else None,
        success_url=svc.config.stripe_success_url,
        cancel_url=svc.config.stripe_cancel_url,
        idempotency_key=key,
    )


async def forget_missing_customer(db: Database, telegram_id: int, customer_id: str, now: datetime) -> None:
    """Stripe says the stored customer doesn't exist any more; stop using it."""
    async with db.tx() as conn:
        cur = await conn.execute(
            "UPDATE users SET stripe_customer_id=NULL WHERE telegram_id=? AND stripe_customer_id=?",
            (telegram_id, customer_id),
        )
        if cur.rowcount == 1:
            log.warning("Stripe customer %s of %s no longer exists; forgetting it", customer_id, telegram_id)
            await audit(conn, now, telegram_id, "stripe_customer_missing", customer_id)


async def replace_missing_customer(svc: Services, telegram_id: int, customer_id: str, now: datetime) -> str:
    await forget_missing_customer(svc.db, telegram_id, customer_id, now)
    user = await get_user(svc.db, telegram_id)
    assert user is not None
    # The usual key customer-<id> would replay the deleted customer for 24 hours.
    return await _get_or_create_customer(
        svc, user, now, idempotency_key=f"customer-{telegram_id}-{int(now.timestamp())}"
    )


async def _get_or_create_customer(
    svc: Services, user: User, now: datetime, idempotency_key: str | None = None
) -> str:
    """One Stripe customer per Telegram ID: DB first, then create (Stripe-side
    idempotency key customer-<id>), then store only if nothing was stored meanwhile."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    assert svc.stripe is not None
    customer_id = await svc.stripe.create_customer(user.telegram_id, user.username, idempotency_key)
    async with svc.db.tx() as conn:
        cur = await conn.execute(
            "UPDATE users SET stripe_customer_id=? WHERE telegram_id=? AND stripe_customer_id IS NULL",
            (customer_id, user.telegram_id),
        )
        if cur.rowcount == 1:
            await audit(conn, now, user.telegram_id, "stripe_customer_created", customer_id)
    stored = await get_user(svc.db, user.telegram_id)
    assert stored is not None and stored.stripe_customer_id
    return stored.stripe_customer_id


# --- Applying subscription data -------------------------------------------------

async def _user_exists(db: Database, telegram_id: int) -> bool:
    return await db.fetchone("SELECT 1 FROM users WHERE telegram_id=?", (telegram_id,)) is not None


async def resolve_telegram_id(db: Database, sub: SubInfo, hint: int | None = None) -> int | None:
    """metadata.telegram_id first, then the stored Stripe customer, then the hint
    (the checkout row the subscription came from)."""
    if sub.telegram_id is not None and await _user_exists(db, sub.telegram_id):
        return sub.telegram_id
    if sub.customer_id:
        row = await db.fetchone("SELECT telegram_id FROM users WHERE stripe_customer_id=?", (sub.customer_id,))
        if row is not None:
            return row["telegram_id"]
    if hint is not None and await _user_exists(db, hint):
        return hint
    return None


def _written_during_sync(old, sync_started: datetime, snapshot_ids: frozenset[str]) -> bool:
    """True if someone else (a checkout poll) stored this user's subscription while
    the sync's listing was running, so the listing may be older than the DB."""
    stamped = from_iso(old["last_synced_at"])
    if stamped is None:
        return False
    if stamped >= sync_started:   # >=: to_iso has one-second resolution, the poll must win
        return True
    # A poll pass stamps the time the pass began, so a subscription it applied just
    # before the sync started can still be missing from the listing.
    stored = old["stripe_sub_id"]
    return bool(stored) and stored not in snapshot_ids and stamped >= sync_started - SYNC_RACE_MARGIN


async def write_subscription(
    db: Database,
    telegram_id: int,
    sub: SubInfo,
    now: datetime,
    *,
    sync_started: datetime | None = None,
    snapshot_ids: frozenset[str] = frozenset(),
    clear_dispute: bool = False,
) -> bool:
    """Store sub on the user. The full sync passes sync_started (and the ids it
    listed for this user); then the write is skipped, returning False, when a
    checkout poll stored newer data meanwhile. The next sync writes it.

    A user whose sub_status is the local "disputed" keeps it: Stripe knows nothing
    of it, so every other field is refreshed but the status is left alone. Only
    the disputes check passes clear_dispute=True, once the dispute is resolved."""
    async with db.tx() as conn:
        cur = await conn.execute(
            "SELECT stripe_sub_id, sub_status, past_due_since, last_synced_at FROM users WHERE telegram_id=?",
            (telegram_id,),
        )
        old = await cur.fetchone()
        if sync_started is not None and old is not None and _written_during_sync(old, sync_started, snapshot_ids):
            log.info("Sync: %s was updated by a checkout poll meanwhile; leaving it for the next sync", telegram_id)
            return False
        # The past-due grace runs from the first unpaid period, even if Stripe is
        # set to leave the subscription past_due and keeps starting new periods.
        keep_disputed = not clear_dispute and old is not None and old["sub_status"] == DISPUTED_STATUS
        status = DISPUTED_STATUS if keep_disputed else sub.status
        past_due_since = None
        if sub.status == "past_due":
            same_sub = old is not None and old["stripe_sub_id"] == sub.id
            past_due_since = (old["past_due_since"] if same_sub and old["past_due_since"] else
                              to_iso(sub.period_start) if sub.period_start else None)
        await conn.execute(
            "UPDATE users SET stripe_sub_id=?, sub_status=?, sub_period_start=?, sub_period_end=?, "
            "sub_trial_end=?, past_due_since=?, cancel_at_period_end=?, last_synced_at=? WHERE telegram_id=?",
            (
                sub.id,
                status,
                to_iso(sub.period_start) if sub.period_start else None,
                to_iso(sub.period_end) if sub.period_end else None,
                to_iso(sub.trial_end) if sub.trial_end else None,
                past_due_since,
                int(sub.cancel_scheduled),
                to_iso(now),
                telegram_id,
            ),
        )
        if sub.customer_id:
            await conn.execute(
                "UPDATE users SET stripe_customer_id=? WHERE telegram_id=? AND stripe_customer_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM users WHERE stripe_customer_id=?)",
                (sub.customer_id, telegram_id, sub.customer_id),
            )
        if sub.trial_start:
            # Only ever set once; a DB trigger rejects any change after that,
            # which is the one-trial-per-person guarantee.
            cur = await conn.execute(
                "UPDATE users SET trial_started_at=? WHERE telegram_id=? AND trial_started_at IS NULL",
                (to_iso(sub.trial_start), telegram_id),
            )
            if cur.rowcount == 1:
                await audit(conn, now, telegram_id, "trial_started",
                            f"{sub.id} ends {to_iso(sub.trial_end) if sub.trial_end else '?'}")
        if old is None or (old["stripe_sub_id"], old["sub_status"]) != (sub.id, status):
            before = old["sub_status"] if old is not None and old["sub_status"] else "none"
            await audit(conn, now, telegram_id, "sub_status", f"{sub.id}: {before} -> {status}")
    return True


async def apply_subscription(db: Database, sub: SubInfo, now: datetime, hint: int | None = None) -> int | None:
    """Store a subscription on its user. Returns the user's Telegram ID, or None
    if we can't tell whose it is."""
    telegram_id = await resolve_telegram_id(db, sub, hint)
    if telegram_id is None:
        log.warning("Subscription %s doesn't match any user", sub.id)
        return None
    await write_subscription(db, telegram_id, sub, now)
    return telegram_id


# --- poll_checkouts ---------------------------------------------------------------

async def poll_checkouts(svc: Services, now: datetime, only_telegram_id: int | None = None) -> PollResult:
    """Scheduler job (and the "I've paid" button for one user)."""
    result = PollResult()
    if svc.stripe is None:
        return result
    sql = "SELECT id, telegram_id, expires_at FROM checkout_sessions WHERE status='open'"
    params: tuple = ()
    if only_telegram_id is not None:
        sql += " AND telegram_id=?"
        params = (only_telegram_id,)
    for row in await svc.db.fetchall(sql, params):
        result.checked += 1
        try:
            info = await svc.stripe.get_checkout(row["id"])
        except StripeGatewayError:
            result.errors += 1
            expires_at = from_iso(row["expires_at"])
            if expires_at and now > expires_at + CHECKOUT_FORGET_AFTER:
                await _set_checkout_status(svc.db, row["id"], row["telegram_id"], "expired", now,
                                           "not retrievable long after expiry")
                result.expired += 1
            continue
        if info.status == "complete":
            if await _complete_checkout(svc, row["telegram_id"], info, now):
                result.completed += 1
        elif info.status == "expired":
            await _set_checkout_status(svc.db, info.id, row["telegram_id"], "expired", now)
            result.expired += 1
    return result


async def _set_checkout_status(
    db: Database, session_id: str, telegram_id: int, status: str, now: datetime, note: str = ""
) -> None:
    async with db.tx() as conn:
        cur = await conn.execute(
            "UPDATE checkout_sessions SET status=? WHERE id=? AND status='open'", (status, session_id)
        )
        if cur.rowcount == 1:
            await audit(conn, now, telegram_id, f"checkout_{status}", f"{session_id} {note}".strip())


async def _complete_checkout(svc: Services, row_telegram_id: int, info: CheckoutInfo, now: datetime) -> bool:
    sub = info.subscription
    if sub is None:
        # Shouldn't happen in subscription mode; the full sync will pick it up.
        log.warning("Checkout %s is complete but has no subscription", info.id)
        await _set_checkout_status(svc.db, info.id, row_telegram_id, "complete", now, "no subscription")
        return True
    # Stamp with the current time, not when this poll pass started: a full sync that
    # began before this write must see it as newer and leave it alone.
    telegram_id = await apply_subscription(svc.db, sub, svc.now(), hint=row_telegram_id) or row_telegram_id
    user = await get_user(svc.db, telegram_id)
    if user is not None:
        outcome = await _send_welcome(svc, user, sub, now)
        if outcome is NoticeOutcome.RETRY:
            return False  # leave the session open so the next poll retries the message
    await _set_checkout_status(svc.db, info.id, row_telegram_id, "complete", now, sub.id)
    return True


async def _send_welcome(svc: Services, user: User, sub: SubInfo, now: datetime) -> NoticeOutcome:
    if sub.status == "trialing" and sub.trial_end:
        text = texts.TRIAL_STARTED.format(
            end_date=format_local(sub.trial_end, svc.config.timezone),
            price=texts.price_text(svc.price_label),
        )
    else:
        text = texts.PAID
    markup = None
    if not user.in_paid_channel:
        text += "\n" + texts.JOIN_HINT
        markup = join_channel_keyboard(svc.config)
    return await deliver_notice(svc, user, "welcome_paid", sub.id, text, markup, now)


# --- sync_subscriptions -----------------------------------------------------------

def pick_best(subs: list[SubInfo]) -> SubInfo:
    return max(subs, key=lambda s: (STATUS_RANK.get(s.status, 0), s.created))


async def sync_subscriptions(svc: Services, now: datetime) -> SyncResult:
    """Scheduler job: list every subscription and store the best one per user.
    Only a fully successful run refreshes kv.last_full_sync_ok_at, which the
    removal sweep needs before it may remove anyone who ever had a subscription."""
    if svc.stripe is None:
        return SyncResult(ok=False)
    subs = await svc.stripe.list_subscriptions()  # a failure raises: kv not updated
    result = SyncResult(subscriptions=len(subs))

    known = {row["telegram_id"] for row in await svc.db.fetchall("SELECT telegram_id FROM users")}
    by_customer = {
        row["stripe_customer_id"]: row["telegram_id"]
        for row in await svc.db.fetchall(
            "SELECT telegram_id, stripe_customer_id FROM users WHERE stripe_customer_id IS NOT NULL"
        )
    }
    groups: dict[int, list[SubInfo]] = {}
    for sub in subs:
        telegram_id = sub.telegram_id if sub.telegram_id in known else by_customer.get(sub.customer_id or "")
        if telegram_id is None:
            result.unmatched += 1
            continue
        groups.setdefault(telegram_id, []).append(sub)

    # A stored subscription that isn't in the listing proves nothing on its own
    # (test-clock subscriptions aren't listed, a poll may have just stored a new
    # one), so ask Stripe for it. Only "no such subscription" marks it missing.
    listed = {s.id for s in subs}
    for row in await svc.db.fetchall(
        "SELECT telegram_id, stripe_sub_id FROM users WHERE stripe_sub_id IS NOT NULL AND sub_status IS NOT ?",
        (MISSING_STATUS,),
    ):
        if row["stripe_sub_id"] in listed:
            continue
        try:
            sub = await svc.stripe.get_subscription(row["stripe_sub_id"])
        except SubscriptionMissing:
            if row["telegram_id"] not in groups:   # otherwise their listed subs replace it below
                await _mark_sub_missing(svc.db, row["telegram_id"], row["stripe_sub_id"], now)
            continue
        except StripeGatewayError:
            result.ok = False
            continue
        groups.setdefault(row["telegram_id"], []).append(sub)

    for telegram_id, user_subs in groups.items():
        try:
            live = sorted(s.id for s in user_subs if s.status not in TERMINAL_STATUSES)
            if len(live) > 1:
                # TODO(phase 4): also alert the owner (rate-limited owner alerts).
                log.warning("User %s has %d live subscriptions: %s", telegram_id, len(live), ", ".join(live))
                result.multiple.append(telegram_id)
                async with svc.db.tx() as conn:
                    await audit_if_changed(conn, now, telegram_id, "multiple_subs", " ".join(live))
            # `now` was taken before the listing, so it is when this snapshot started.
            if await write_subscription(svc.db, telegram_id, pick_best(user_subs), now, sync_started=now,
                                        snapshot_ids=frozenset(s.id for s in user_subs)):
                result.users_updated += 1
            else:
                result.skipped += 1   # newer data already stored; not a failure
        except Exception:
            log.exception("Sync failed for user %s", telegram_id)
            result.ok = False

    # Disputes are checked by their own daily job (disputes.check_disputes).
    if result.ok:
        async with svc.db.tx() as conn:
            await kv_set(conn, LAST_FULL_SYNC_KEY, to_iso(now))
    log.info("Stripe sync: %d subscriptions, %d users updated, %d left for next sync, %d unmatched, ok=%s",
             result.subscriptions, result.users_updated, result.skipped, result.unmatched, result.ok)
    return result


async def _mark_sub_missing(db: Database, telegram_id: int, sub_id: str, now: datetime) -> None:
    async with db.tx() as conn:
        cur = await conn.execute(
            "UPDATE users SET sub_status=?, past_due_since=NULL WHERE telegram_id=? AND stripe_sub_id=?",
            (MISSING_STATUS, telegram_id, sub_id),
        )
        if cur.rowcount == 1:
            # TODO(phase 4): also alert the owner.
            log.warning("Subscription %s of %s no longer exists at Stripe; marked missing", sub_id, telegram_id)
            await audit(conn, now, telegram_id, "sub_missing", sub_id)
