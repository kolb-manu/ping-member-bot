"""In-memory fakes for the Telegram and Stripe gateways, plus small builders.
Unit tests never make real API calls."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from first_in_bot.config import Config, load_config
from first_in_bot.db import Database
from first_in_bot.models import User
from first_in_bot.services import Services
from first_in_bot.stripe_gateway import CheckoutInfo, CustomerMissing, StripeGatewayError, SubInfo, SubscriptionMissing
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.timeutil import to_iso
from first_in_bot.users import get_user, register_user
from tests.test_config import GOOD

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
OWNER = 111      # first of GOOD's ADMIN_IDS
PRICE = "£2.99/month"


class FakeTelegram:
    def __init__(self) -> None:
        self.events: list[tuple] = []          # everything, in order
        self.send_results: dict[int, SendResult] = {}
        self.statuses: dict[int, str | None] = {}
        self.default_status: str | None = "member"
        self.approve_ok = True
        self.decline_ok = True
        self.kick_ok = True
        # user_id -> user_chat_id for join requesters who never started the bot:
        # Telegram only lets us DM that chat until their request is processed.
        self.window_chats: dict[int, int] = {}
        self._closed_chats: set[int] = set()

    def sent_to(self, chat_id: int) -> list[str]:
        return [e[2] for e in self.events if e[0] == "send" and e[1] == chat_id]

    def markups_to(self, chat_id: int) -> list:
        return [e[3] for e in self.events if e[0] == "send" and e[1] == chat_id]

    def calls(self, kind: str) -> list[int]:
        return [e[1] for e in self.events if e[0] == kind]

    async def send(self, chat_id, text, markup=None) -> SendResult:
        self.events.append(("send", chat_id, text, markup))
        if chat_id in self._closed_chats:
            return SendResult.BLOCKED   # "bot can't initiate conversation with a user"
        return self.send_results.get(chat_id, SendResult.OK)

    def _processed(self, user_id: int, ok: bool) -> None:
        if ok and user_id in self.window_chats:
            self._closed_chats.add(self.window_chats[user_id])

    async def approve_join(self, user_id: int) -> bool:
        self.events.append(("approve", user_id))
        self._processed(user_id, self.approve_ok)
        return self.approve_ok

    async def decline_join(self, user_id: int) -> bool:
        self.events.append(("decline", user_id))
        self._processed(user_id, self.decline_ok)
        return self.decline_ok

    async def member_status(self, user_id: int) -> str | None:
        self.events.append(("status", user_id))
        return self.statuses.get(user_id, self.default_status)

    async def kick(self, user_id: int) -> bool:
        self.events.append(("kick", user_id))
        return self.kick_ok


class FakeStripe:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.customers: list[tuple[int, str | None]] = []
        self.checkouts: dict[str, CheckoutInfo] = {}
        self.created: list[dict] = []
        self.subscriptions: list[SubInfo] = []
        self.fail_list = False
        self.fail_get = False
        self.fail_create = False
        self.portal_error: Exception | None = None
        self.portal_calls: list[str] = []
        self.deleted_customers: set[str] = set()     # "No such customer" from Stripe
        self.customer_keys: list[str | None] = []
        self.retrievable: dict[str, SubInfo] = {}   # subscriptions.retrieve; anything else is missing
        self.fail_retrieve = False
        self.retrieved: list[str] = []
        self.on_list = None                          # async hook run while the listing is "in flight"
        self._n = 0

    async def price_label(self, price_id: str) -> str | None:
        return PRICE

    async def create_customer(self, telegram_id: int, username: str | None,
                              idempotency_key: str | None = None) -> str:
        self.customers.append((telegram_id, username))
        self.customer_keys.append(idempotency_key)
        n = sum(1 for t, _ in self.customers if t == telegram_id)
        return f"cus_{telegram_id}" if n == 1 else f"cus_{telegram_id}_{n}"

    async def create_checkout(self, **kwargs) -> CheckoutInfo:
        if self.fail_create:
            raise StripeGatewayError("Stripe checkout.sessions.create failed: APIConnectionError")
        if kwargs["customer_id"] in self.deleted_customers:
            raise CustomerMissing("Stripe checkout.sessions.create failed: InvalidRequestError")
        self._n += 1
        self.created.append(kwargs)
        info = CheckoutInfo(
            id=f"cs_{self._n}", status="open", url=f"https://checkout.stripe.test/cs_{self._n}",
            expires_at=self.now + timedelta(hours=24), subscription_id=None, subscription=None,
        )
        self.checkouts[info.id] = info
        return info

    async def get_checkout(self, session_id: str) -> CheckoutInfo:
        if self.fail_get:
            raise StripeGatewayError("Stripe checkout.sessions.retrieve failed: APIConnectionError")
        return self.checkouts[session_id]

    def complete(self, session_id: str, sub: SubInfo) -> None:
        self.checkouts[session_id] = replace(
            self.checkouts[session_id], status="complete", url=None, subscription_id=sub.id, subscription=sub
        )

    def expire(self, session_id: str) -> None:
        self.checkouts[session_id] = replace(self.checkouts[session_id], status="expired", url=None)

    async def list_subscriptions(self) -> list[SubInfo]:
        if self.fail_list:
            raise StripeGatewayError("Stripe subscriptions.list failed: APIConnectionError")
        snapshot = list(self.subscriptions)
        if self.on_list is not None:
            await self.on_list()   # e.g. a checkout poll landing mid-listing
        return snapshot

    async def get_subscription(self, sub_id: str) -> SubInfo:
        self.retrieved.append(sub_id)
        if self.fail_retrieve:
            raise StripeGatewayError("Stripe subscriptions.retrieve failed: APIConnectionError")
        if sub_id not in self.retrievable:
            raise SubscriptionMissing(f"Stripe has no subscription {sub_id}")
        return self.retrievable[sub_id]

    async def portal_url(self, customer_id: str, return_url: str) -> str:
        self.portal_calls.append(customer_id)
        if customer_id in self.deleted_customers:
            raise CustomerMissing("Stripe billing_portal.sessions.create failed: InvalidRequestError")
        if self.portal_error is not None:
            raise self.portal_error
        return f"https://billing.stripe.test/p/{customer_id}"


def make_config(**overrides: str) -> Config:
    env = {**GOOD, "STRIPE_API_KEY": "rk_test_" + "x" * 20, "STRIPE_PRICE_ID": "price_123", **overrides}
    return load_config(env)


def make_svc(db: Database, tg: FakeTelegram, stripe: FakeStripe | None, **config_overrides: str) -> Services:
    return Services(db=db, config=make_config(**config_overrides), tg=tg, stripe=stripe,
                    price_label=PRICE if stripe else None, clock=lambda: NOW)


def sub_info(
    id: str = "sub_1",
    status: str = "active",
    customer: str | None = "cus_1",
    telegram_id: int | None = 1,
    created: datetime = NOW,
    period_end: datetime | None = NOW + timedelta(days=30),
    period_start: datetime | None = NOW,
    trial_start: datetime | None = None,
    trial_end: datetime | None = None,
    cancel: bool = False,
) -> SubInfo:
    return SubInfo(id=id, status=status, customer_id=customer, telegram_id=telegram_id, created=created,
                   period_end=period_end, period_start=period_start, trial_start=trial_start,
                   trial_end=trial_end, cancel_scheduled=cancel)


def trial_sub(telegram_id: int = 1, start: datetime = NOW, days: int = 14, **kw) -> SubInfo:
    end = start + timedelta(days=days)
    kw.setdefault("id", f"sub_t{telegram_id}")
    kw.setdefault("customer", f"cus_{telegram_id}")
    return sub_info(status="trialing", telegram_id=telegram_id, created=start, period_end=end,
                    period_start=start, trial_start=start, trial_end=end, **kw)


async def add_user(db: Database, telegram_id: int, **fields) -> User:
    """Register via the normal /start path, then set any extra columns directly."""
    username = fields.pop("username", f"user{telegram_id}")
    await register_user(db, telegram_id, username, "Name", "tt_v1", fields.pop("created_at", NOW))
    if fields:
        def sql_value(v):
            if isinstance(v, datetime):
                return to_iso(v)
            if isinstance(v, bool):
                return int(v)
            return v
        sets = ", ".join(f"{k}=?" for k in fields)
        async with db.tx() as conn:
            await conn.execute(f"UPDATE users SET {sets} WHERE telegram_id=?",
                               (*[sql_value(v) for v in fields.values()], telegram_id))
    user = await get_user(db, telegram_id)
    assert user is not None
    return user


async def audit_actions(db: Database, telegram_id: int | None = None) -> list[str]:
    if telegram_id is None:
        rows = await db.fetchall("SELECT action FROM audit_log ORDER BY id")
    else:
        rows = await db.fetchall("SELECT action FROM audit_log WHERE telegram_id=? ORDER BY id", (telegram_id,))
    return [r["action"] for r in rows]

