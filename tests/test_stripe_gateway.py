"""Stripe payload parsing and the exact parameters the gateway sends. A fake
client stands in for stripe.StripeClient, so nothing touches the network."""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import stripe

from first_in_bot.stripe_gateway import (
    STRIPE_RETRIES,
    STRIPE_TIMEOUT,
    CheckoutMissing,
    CustomerMissing,
    PortalNotConfigured,
    StripeGateway,
    StripeGatewayError,
    SubscriptionMissing,
    format_price,
    parse_checkout,
    parse_subscription,
)
from tests.fakes import NOW

T0 = int(NOW.timestamp())
DAY = 86400


def sub_payload(**overrides):
    """Shaped like a 2026-09-30.endive subscription: no top-level current_period_end."""
    payload = {
        "id": "sub_1Qtrial",
        "object": "subscription",
        "status": "trialing",
        "customer": "cus_ABC",
        "created": T0,
        "start_date": T0,
        "billing_mode": {"type": "flexible"},
        "cancel_at": None,
        "cancel_at_period_end": False,
        "canceled_at": None,
        "trial_start": T0,
        "trial_end": T0 + 14 * DAY,
        "metadata": {"telegram_id": "42"},
        "items": {
            "object": "list",
            "data": [{
                "id": "si_1",
                "object": "subscription_item",
                "current_period_start": T0,
                "current_period_end": T0 + 14 * DAY,
                "quantity": 1,
                "price": {"id": "price_123", "unit_amount": 299, "currency": "gbp",
                          "recurring": {"interval": "month", "interval_count": 1}},
            }],
            "has_more": False,
            "url": "/v1/subscription_items?subscription=sub_1Qtrial",
        },
    }
    payload.update(overrides)
    return payload


def test_parse_trialing_subscription():
    s = parse_subscription(sub_payload())
    assert s.id == "sub_1Qtrial" and s.status == "trialing"
    assert s.customer_id == "cus_ABC" and s.telegram_id == 42
    assert s.created == NOW
    assert s.trial_start == NOW and s.trial_end == NOW + timedelta(days=14)
    assert s.period_end == NOW + timedelta(days=14)
    assert s.period_start == NOW
    assert s.cancel_scheduled is False


def test_period_end_is_max_over_items():
    items = {"object": "list", "data": [
        {"id": "si_1", "current_period_end": T0 + 5 * DAY},
        {"id": "si_2", "current_period_end": T0 + 30 * DAY},
        {"id": "si_3", "current_period_end": T0 + 10 * DAY},
    ]}
    assert parse_subscription(sub_payload(items=items)).period_end == NOW + timedelta(days=30)


def test_period_start_is_min_over_items():
    items = {"object": "list", "data": [
        {"id": "si_1", "current_period_start": T0 + 2 * DAY, "current_period_end": T0 + 32 * DAY},
        {"id": "si_2", "current_period_start": T0, "current_period_end": T0 + 30 * DAY},
        {"id": "si_3", "current_period_end": T0 + 30 * DAY},
    ]}
    assert parse_subscription(sub_payload(items=items)).period_start == NOW


def test_past_due_payload_period_is_the_unpaid_one():
    # A failed trial conversion: Stripe has already opened the next period.
    trial_end = T0 - DAY
    items = {"object": "list", "data": [
        {"id": "si_1", "current_period_start": trial_end, "current_period_end": trial_end + 30 * DAY}]}
    s = parse_subscription(sub_payload(status="past_due", trial_end=trial_end, items=items))
    assert s.period_start == NOW - timedelta(days=1) and s.period_end == NOW + timedelta(days=29)


def test_no_items_means_no_period_end():
    s = parse_subscription(sub_payload(items={"object": "list", "data": []}))
    assert s.period_end is None and s.period_start is None


@pytest.mark.parametrize("cancel_at,at_period_end,expected", [
    (None, False, False),
    (None, True, True),
    (T0 + 14 * DAY, False, True),   # flexible billing: cancel_at set, cancel_at_period_end false
    (T0 + 14 * DAY, True, True),
])
def test_cancel_scheduled(cancel_at, at_period_end, expected):
    s = parse_subscription(sub_payload(cancel_at=cancel_at, cancel_at_period_end=at_period_end))
    assert s.cancel_scheduled is expected


def test_expanded_customer_and_bad_metadata():
    s = parse_subscription(sub_payload(customer={"id": "cus_X", "object": "customer"}, metadata={}))
    assert s.customer_id == "cus_X" and s.telegram_id is None
    assert parse_subscription(sub_payload(metadata={"telegram_id": "abc"})).telegram_id is None


def test_real_stripe_object_parses_like_a_dict():
    """StripeObject isn't a dict and .items is the API field; parsing must cope."""
    obj = stripe.Subscription.construct_from(sub_payload(), None)
    assert parse_subscription(obj) == parse_subscription(sub_payload())


def test_parse_checkout_expanded_and_not():
    session = {"id": "cs_1", "object": "checkout.session", "status": "complete", "url": None,
               "expires_at": T0 + DAY, "subscription": sub_payload()}
    c = parse_checkout(session)
    assert c.status == "complete" and c.subscription_id == "sub_1Qtrial"
    assert c.subscription is not None and c.subscription.telegram_id == 42
    assert c.expires_at == NOW + timedelta(days=1)
    c = parse_checkout({**session, "subscription": "sub_9", "status": "open", "url": "https://x"})
    assert c.subscription is None and c.subscription_id == "sub_9" and c.url == "https://x"
    obj = stripe.checkout.Session.construct_from(session, None)
    assert parse_checkout(obj) == parse_checkout(session)


@pytest.mark.parametrize("price,label", [
    ({"unit_amount": 299, "currency": "gbp", "recurring": {"interval": "month", "interval_count": 1}}, "£2.99/month"),
    ({"unit_amount": 899, "currency": "gbp", "recurring": {"interval": "month", "interval_count": 3}}, "£8.99/3 months"),
    ({"unit_amount": 299, "currency": "chf", "recurring": {"interval": "month"}}, "2.99 CHF/month"),
    ({"unit_amount": 500, "currency": "gbp", "recurring": None}, "£5.00"),
    ({"unit_amount": None, "currency": "gbp"}, None),
])
def test_format_price(price, label):
    assert format_price(price) == label


# --- Gateway calls against a fake client ---------------------------------------

class Recorder:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error

    def __call__(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


class FakePage:
    def __init__(self, items):
        self.items = items

    def auto_paging_iter(self):
        return iter(self.items)


def fake_client():
    open_session = {"id": "cs_1", "status": "open", "url": "https://checkout.stripe.com/c/cs_1",
                    "expires_at": T0 + DAY, "subscription": None}
    return SimpleNamespace(v1=SimpleNamespace(
        checkout=SimpleNamespace(sessions=SimpleNamespace(
            create=Recorder(open_session), retrieve=Recorder({**open_session, "status": "complete",
                                                              "subscription": sub_payload()}))),
        customers=SimpleNamespace(create=Recorder({"id": "cus_NEW"})),
        subscriptions=SimpleNamespace(list=Recorder(FakePage([sub_payload(), sub_payload(id="sub_2")])),
                                      retrieve=Recorder(sub_payload(id="sub_tc"))),
        billing_portal=SimpleNamespace(sessions=SimpleNamespace(create=Recorder({"url": "https://billing/p"}))),
        prices=SimpleNamespace(retrieve=Recorder({"unit_amount": 299, "currency": "gbp",
                                                  "recurring": {"interval": "month"}})),
    ))


TRIAL_END = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


async def create(gw, trial_end):
    return await gw.create_checkout(customer_id="cus_ABC", telegram_id=42, price_id="price_123",
                                    trial_end=trial_end, success_url="https://t.me/Bot",
                                    cancel_url="https://t.me/Bot", idempotency_key="checkout-42-t-1")


async def test_checkout_params_with_trial():
    client = fake_client()
    gw = StripeGateway("rk_test_x", client=client)
    info = await create(gw, TRIAL_END)
    params, options = client.v1.checkout.sessions.create.calls[0]
    assert params["mode"] == "subscription"
    assert params["customer"] == "cus_ABC" and params["client_reference_id"] == "42"
    assert params["line_items"] == [{"price": "price_123", "quantity": 1}]
    assert params["subscription_data"] == {"metadata": {"telegram_id": "42"}, "trial_end": int(TRIAL_END.timestamp())}
    assert params["payment_method_collection"] == "always"
    assert params["allow_promotion_codes"] is True
    assert params["success_url"] == params["cancel_url"] == "https://t.me/Bot"
    assert "ui_mode" not in params
    assert options == {"idempotency_key": "checkout-42-t-1"}
    assert info.id == "cs_1" and info.url.startswith("https://checkout.stripe.com/")


async def test_checkout_params_without_trial():
    client = fake_client()
    await create(StripeGateway("rk_test_x", client=client), None)
    params, _ = client.v1.checkout.sessions.create.calls[0]
    assert "trial_period_days" not in params["subscription_data"]
    assert "trial_end" not in params["subscription_data"]
    assert params["payment_method_collection"] == "always"


async def test_other_calls():
    client = fake_client()
    gw = StripeGateway("rk_test_x", client=client)
    assert await gw.create_customer(42, "bob") == "cus_NEW"
    params, options = client.v1.customers.create.calls[0]
    assert params["metadata"] == {"telegram_id": "42"} and options == {"idempotency_key": "customer-42"}
    await gw.create_customer(42, "bob", "customer-42-1793534400")
    assert client.v1.customers.create.calls[1][1] == {"idempotency_key": "customer-42-1793534400"}

    info = await gw.get_checkout("cs_1")
    assert client.v1.checkout.sessions.retrieve.calls[0] == ("cs_1", {"expand": ["subscription"]})
    assert info.subscription.status == "trialing"

    subs = await gw.list_subscriptions()
    assert [s.id for s in subs] == ["sub_1Qtrial", "sub_2"]
    assert client.v1.subscriptions.list.calls[0] == ({"status": "all", "limit": 100},)

    assert await gw.portal_url("cus_ABC", "https://t.me/Bot") == "https://billing/p"
    assert await gw.price_label("price_123") == "£2.99/month"

    sub = await gw.get_subscription("sub_tc")
    assert client.v1.subscriptions.retrieve.calls[0] == ("sub_tc",) and sub.id == "sub_tc"


async def test_portal_not_configured_is_its_own_error():
    client = fake_client()
    client.v1.billing_portal.sessions.create.error = stripe.InvalidRequestError(
        "No configuration provided and your test mode default configuration has not been created.", None)
    with pytest.raises(PortalNotConfigured):
        await StripeGateway("rk_test_x", client=client).portal_url("cus_ABC", "https://t.me/Bot")


async def test_errors_wrapped_and_key_never_logged(caplog):
    client = fake_client()
    client.v1.checkout.sessions.create.error = stripe.AuthenticationError(
        "Invalid API Key provided: rk_test_SECRETabcd1234")
    caplog.set_level(logging.INFO)
    gw = StripeGateway("rk_test_SECRETabcd1234", client=client)
    with pytest.raises(StripeGatewayError) as exc:
        await create(gw, None)
    assert "SECRET" not in str(exc.value)
    assert "SECRET" not in caplog.text
    assert "Stripe call: checkout.sessions.create" in caplog.text
    client.v1.prices.retrieve.error = stripe.APIConnectionError("network down")
    assert await gw.price_label("price_123") is None


def no_such(param, what="customer: 'cus_ABC'"):
    return stripe.InvalidRequestError(f"No such {what}", param, code="resource_missing")


async def test_deleted_customer_is_its_own_error():
    client = fake_client()
    client.v1.checkout.sessions.create.error = no_such("customer")
    gw = StripeGateway("rk_test_x", client=client)
    with pytest.raises(CustomerMissing):
        await create(gw, TRIAL_END)
    client.v1.billing_portal.sessions.create.error = no_such("customer")
    with pytest.raises(CustomerMissing):
        await gw.portal_url("cus_ABC", "https://t.me/Bot")
    # Some other missing thing (e.g. the price) is an ordinary error.
    client.v1.checkout.sessions.create.error = no_such("line_items[0][price]", "price: 'price_123'")
    with pytest.raises(StripeGatewayError) as exc:
        await create(gw, TRIAL_END)
    assert not isinstance(exc.value, CustomerMissing)


async def test_get_subscription_missing_vs_other_errors():
    client = fake_client()
    gw = StripeGateway("rk_test_x", client=client)
    client.v1.subscriptions.retrieve.error = no_such("id", "subscription: 'sub_tc'")
    with pytest.raises(SubscriptionMissing):
        await gw.get_subscription("sub_tc")
    client.v1.subscriptions.retrieve.error = stripe.APIConnectionError("network down")
    with pytest.raises(StripeGatewayError) as exc:
        await gw.get_subscription("sub_tc")
    assert not isinstance(exc.value, SubscriptionMissing)


async def test_get_checkout_missing_vs_other_errors():
    client = fake_client()
    gw = StripeGateway("rk_test_x", client=client)
    client.v1.checkout.sessions.retrieve.error = no_such("session", "checkout.session: 'cs_gone'")
    with pytest.raises(CheckoutMissing):
        await gw.get_checkout("cs_gone")
    client.v1.checkout.sessions.retrieve.error = stripe.APIConnectionError("network down")
    with pytest.raises(StripeGatewayError) as exc:
        await gw.get_checkout("cs_1")
    assert not isinstance(exc.value, CheckoutMissing)


def test_real_client_has_a_bounded_timeout_and_keeps_the_api_version():
    """A Stripe call runs in a thread that shutdown can't cancel, so one call with
    all its retries must fit inside systemd's 45 s stop timeout."""
    gw = StripeGateway("rk_test_" + "x" * 20)
    requestor = gw._client._requestor
    http = requestor._client
    assert isinstance(http, stripe.RequestsClient) and http._timeout == STRIPE_TIMEOUT
    assert requestor._options.max_network_retries == STRIPE_RETRIES
    assert requestor._options.stripe_version == "2026-09-30.endive"
    # Every try timing out, plus stripe-python's pauses between tries (0.5 s, doubling).
    pauses = sum(min(stripe.HTTPClient.INITIAL_DELAY * 2 ** n, stripe.HTTPClient.MAX_DELAY)
                 for n in range(STRIPE_RETRIES))
    assert (STRIPE_RETRIES + 1) * STRIPE_TIMEOUT + pauses < 40
