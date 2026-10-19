"""Thin wrapper around the Stripe API, so the rest of the bot (and the tests)
never touch the stripe library directly.

The library is synchronous, so every call runs in a worker thread. Results come
back as small frozen dataclasses. Each call is logged by name at INFO; the API
key is never logged. The restricted key only allows: Checkout Sessions write,
Customers write, Subscriptions read, Customer Portal write, Disputes read and
reading prices, so don't add calls to other endpoints (no customers.search).
list_disputes expands each dispute's charge to learn the customer, which
needs Charges read on the key.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

import stripe

log = logging.getLogger(__name__)

STRIPE_API_VERSION = "2026-09-30.endive"
_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)
_KEY_RE = re.compile(r"\b(sk|rk|pk)_(test|live)_\S+")
_CURRENCY_SYMBOLS = {"gbp": "£", "eur": "€", "usd": "$"}

T = TypeVar("T")


class StripeGatewayError(Exception):
    """Any failed Stripe call. The message never contains the API key."""


class PortalNotConfigured(StripeGatewayError):
    """The Customer Portal hasn't been activated in the Stripe dashboard."""


class CustomerMissing(StripeGatewayError):
    """The stored Stripe customer no longer exists (e.g. test data was deleted)."""


class SubscriptionMissing(StripeGatewayError):
    """The subscription no longer exists at Stripe."""


def _resource_missing(exc: BaseException | None, param: str | None = None) -> bool:
    return (
        isinstance(exc, stripe.InvalidRequestError)
        and exc.code == "resource_missing"
        and (param is None or exc.param == param)
    )


@dataclass(frozen=True)
class SubInfo:
    id: str
    status: str
    customer_id: str | None
    telegram_id: int | None          # from subscription metadata
    created: datetime
    period_end: datetime | None      # max of the items' current_period_end
    # min of the items' current_period_start. Stripe starts the next period when it
    # creates the renewal invoice, paid or not, so while past_due this is when the
    # unpaid period began (and period_end is about a month away).
    period_start: datetime | None
    trial_start: datetime | None
    trial_end: datetime | None
    cancel_scheduled: bool           # cancel_at_period_end, or cancel_at set


@dataclass(frozen=True)
class CheckoutInfo:
    id: str
    status: str                      # open / complete / expired
    url: str | None
    expires_at: datetime
    subscription_id: str | None
    subscription: SubInfo | None     # set when the subscription was expanded


@dataclass(frozen=True)
class DisputeInfo:
    id: str
    status: str                      # needs_response / under_review / won / lost / warning_*
    charge_id: str | None
    customer_id: str | None          # None if the charge wasn't expanded or has no customer
    created: datetime | None


# --- Parsing (pure, tested on plain dict payloads) ------------------------------

def _plain(obj: Any) -> dict:
    """StripeObject is not a dict in this library version; turn it into one."""
    to_dict = getattr(obj, "to_dict", None)
    return to_dict() if callable(to_dict) else dict(obj)


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None


def _id_of(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return value.get("id")


def _telegram_id(value: Any) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None


def parse_subscription(obj: Any) -> SubInfo:
    d = _plain(obj)
    # In API version 2026-09-30 current_period_end only exists on the items.
    # Use d["items"], never d.items / sub.items (dict.items collides with it).
    items = (d.get("items") or {}).get("data") or []
    period_ends = [item["current_period_end"] for item in items if item.get("current_period_end")]
    period_starts = [item["current_period_start"] for item in items if item.get("current_period_start")]
    metadata = d.get("metadata") or {}
    return SubInfo(
        id=d["id"],
        status=d.get("status") or "",
        customer_id=_id_of(d.get("customer")),
        telegram_id=_telegram_id(metadata.get("telegram_id")),
        created=_ts(d.get("created")) or _EPOCH,
        period_end=_ts(max(period_ends)) if period_ends else None,
        period_start=_ts(min(period_starts)) if period_starts else None,
        trial_start=_ts(d.get("trial_start")),
        trial_end=_ts(d.get("trial_end")),
        # With flexible billing a period-end cancellation can show up as
        # cancel_at with cancel_at_period_end still false.
        cancel_scheduled=bool(d.get("cancel_at_period_end")) or d.get("cancel_at") is not None,
    )


def parse_checkout(obj: Any) -> CheckoutInfo:
    d = _plain(obj)
    sub = d.get("subscription")
    return CheckoutInfo(
        id=d["id"],
        status=d.get("status") or "",
        url=d.get("url"),
        expires_at=_ts(d.get("expires_at")) or _EPOCH,
        subscription_id=_id_of(sub),
        subscription=parse_subscription(sub) if isinstance(sub, dict) else None,
    )


def parse_dispute(obj: Any) -> DisputeInfo:
    d = _plain(obj)
    charge = d.get("charge")
    customer = _id_of(charge.get("customer")) if isinstance(charge, dict) else None
    return DisputeInfo(
        id=d["id"],
        status=d.get("status") or "",
        charge_id=_id_of(charge),
        customer_id=customer,
        created=_ts(d.get("created")),
    )


def format_price(obj: Any) -> str | None:
    """'£2.99/month'. None if the price has no fixed amount."""
    d = _plain(obj)
    amount = d.get("unit_amount")
    if amount is None:
        return None
    currency = (d.get("currency") or "").lower()
    symbol = _CURRENCY_SYMBOLS.get(currency)
    money = f"{symbol}{amount / 100:.2f}" if symbol else f"{amount / 100:.2f} {currency.upper()}"
    recurring = d.get("recurring") or {}
    interval = recurring.get("interval")
    if not interval:
        return money
    count = recurring.get("interval_count") or 1
    return f"{money}/{interval}" if count == 1 else f"{money}/{count} {interval}s"


def _redact(text: str) -> str:
    return _KEY_RE.sub("[key]", text)


# --- Gateway ----------------------------------------------------------------------

class StripeGateway:
    def __init__(self, api_key: str, *, client: Any | None = None):
        # client is for tests; normally we build the real one here.
        self._client = client or stripe.StripeClient(
            api_key, stripe_version=STRIPE_API_VERSION, max_network_retries=3
        )

    async def _call(self, name: str, fn: Callable[[], T]) -> T:
        log.info("Stripe call: %s", name)
        try:
            return await asyncio.to_thread(fn)
        except stripe.StripeError as exc:
            log.warning("Stripe call %s failed: %s (%s)", name, type(exc).__name__,
                        _redact(str(exc.user_message or exc)))
            error = CustomerMissing if _resource_missing(exc, "customer") else StripeGatewayError
            raise error(f"Stripe {name} failed: {type(exc).__name__}") from exc

    async def price_label(self, price_id: str) -> str | None:
        try:
            price = await self._call("prices.retrieve", lambda: self._client.v1.prices.retrieve(price_id))
        except StripeGatewayError:
            return None
        return format_price(price)

    async def create_customer(
        self, telegram_id: int, username: str | None, idempotency_key: str | None = None
    ) -> str:
        description = f"Telegram user {telegram_id}" + (f" (@{username})" if username else "")
        customer = await self._call(
            "customers.create",
            lambda: self._client.v1.customers.create(
                {"metadata": {"telegram_id": str(telegram_id)}, "description": description},
                {"idempotency_key": idempotency_key or f"customer-{telegram_id}"},
            ),
        )
        return _plain(customer)["id"]

    async def create_checkout(
        self,
        *,
        customer_id: str,
        telegram_id: int,
        price_id: str,
        trial_end: datetime | None,
        success_url: str,
        cancel_url: str,
        idempotency_key: str,
    ) -> CheckoutInfo:
        subscription_data: dict[str, Any] = {"metadata": {"telegram_id": str(telegram_id)}}
        if trial_end is not None:
            # A fixed end, not trial_period_days: the first-charge date shown to the
            # buyer is then the date Stripe charges, even if they finish checkout later.
            subscription_data["trial_end"] = int(trial_end.timestamp())
        params: dict[str, Any] = {
            # No ui_mode: the default is the Stripe-hosted page (the values were renamed in 2026).
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": str(telegram_id),
            "line_items": [{"price": price_id, "quantity": 1}],
            "subscription_data": subscription_data,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "allow_promotion_codes": True,
            # Card required even for the free trial; Stripe charges when it ends.
            "payment_method_collection": "always",
        }
        session = await self._call(
            "checkout.sessions.create",
            lambda: self._client.v1.checkout.sessions.create(params, {"idempotency_key": idempotency_key}),
        )
        return parse_checkout(session)

    async def get_checkout(self, session_id: str) -> CheckoutInfo:
        session = await self._call(
            "checkout.sessions.retrieve",
            lambda: self._client.v1.checkout.sessions.retrieve(session_id, {"expand": ["subscription"]}),
        )
        return parse_checkout(session)

    async def list_subscriptions(self) -> list[SubInfo]:
        def fetch_all() -> list[SubInfo]:
            page = self._client.v1.subscriptions.list({"status": "all", "limit": 100})
            return [parse_subscription(sub) for sub in page.auto_paging_iter()]

        return await self._call("subscriptions.list", fetch_all)

    async def get_subscription(self, sub_id: str) -> SubInfo:
        """Raises SubscriptionMissing if Stripe no longer has it."""
        try:
            sub = await self._call(
                "subscriptions.retrieve", lambda: self._client.v1.subscriptions.retrieve(sub_id)
            )
        except StripeGatewayError as exc:
            if _resource_missing(exc.__cause__):
                raise SubscriptionMissing(f"Stripe has no subscription {sub_id}") from exc.__cause__
            raise
        return parse_subscription(sub)

    async def list_disputes(self) -> list[DisputeInfo]:
        """Every dispute, with its charge expanded so the customer is known."""
        def fetch_all() -> list[DisputeInfo]:
            page = self._client.v1.disputes.list({"limit": 100, "expand": ["data.charge"]})
            return [parse_dispute(d) for d in page.auto_paging_iter()]

        return await self._call("disputes.list", fetch_all)

    async def portal_url(self, customer_id: str, return_url: str) -> str:
        try:
            session = await self._call(
                "billing_portal.sessions.create",
                lambda: self._client.v1.billing_portal.sessions.create(
                    {"customer": customer_id, "return_url": return_url}
                ),
            )
        except StripeGatewayError as exc:
            cause = exc.__cause__
            if isinstance(cause, stripe.InvalidRequestError) and "configuration" in str(cause).lower():
                raise PortalNotConfigured("Customer Portal is not activated in the Stripe dashboard") from cause
            raise
        return _plain(session)["url"]
