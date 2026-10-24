"""Checkout, payment polling, applying subscriptions and the full sync."""
from datetime import timedelta

import pytest

from first_in_bot import billing, texts
from first_in_bot.billing import (
    LAST_FULL_SYNC_KEY,
    apply_subscription,
    idempotency_key,
    poll_checkouts,
    start_checkout,
    sync_subscriptions,
)
from first_in_bot.services import Services
from first_in_bot.stripe_gateway import StripeGatewayError, parse_subscription
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.timeutil import from_iso, to_iso
from first_in_bot.users import get_user
from tests.fakes import NOW, OWNER, FakeStripe, add_user, audit_actions, sub_info, trial_sub
from tests.test_stripe_gateway import sub_payload

M = timedelta(minutes=1)


async def checkout_rows(db, telegram_id=1):
    return await db.fetchall("SELECT * FROM checkout_sessions WHERE telegram_id=? ORDER BY created_at", (telegram_id,))


# --- start_checkout --------------------------------------------------------------

async def test_trial_checkout_when_eligible(svc, stripe_fake):
    user = await add_user(svc.db, 1)
    offer = await start_checkout(svc, user, NOW)
    assert offer.kind == "checkout" and offer.with_trial and not offer.reused
    assert offer.url == "https://checkout.stripe.test/cs_1"
    call = stripe_fake.created[0]
    trial_end = NOW + timedelta(days=14)          # checkout creation time + TRIAL_DAYS, a fixed moment
    assert call["trial_end"] == trial_end and offer.trial_end == trial_end
    assert "trial_days" not in call
    assert call["customer_id"] == "cus_1" and call["telegram_id"] == 1
    assert call["price_id"] == "price_123"
    assert call["idempotency_key"] == idempotency_key(1, True, NOW, trial_end)
    assert call["idempotency_key"].startswith("checkout-1-t-")
    rows = await checkout_rows(svc.db)
    assert len(rows) == 1 and rows[0]["status"] == "open" and rows[0]["with_trial"] == 1
    assert from_iso(rows[0]["trial_end"]) == trial_end
    assert rows[0]["url"] == offer.url and from_iso(rows[0]["expires_at"]) == NOW + timedelta(hours=24)
    assert (await get_user(svc.db, 1)).stripe_customer_id == "cus_1"
    assert (await get_user(svc.db, 1)).trial_ends_at is None   # no bot-side trial any more
    assert await audit_actions(svc.db, 1) == ["user_created", "stripe_customer_created", "checkout_created"]


async def test_no_trial_once_trial_used(svc, stripe_fake):
    user = await add_user(svc.db, 1, trial_started_at=NOW - timedelta(days=30))
    offer = await start_checkout(svc, user, NOW)
    assert not offer.with_trial
    assert stripe_fake.created[0]["trial_end"] is None and offer.trial_end is None
    assert "-n-" in stripe_fake.created[0]["idempotency_key"]


async def test_no_trial_after_any_subscription(svc, stripe_fake):
    user = await add_user(svc.db, 1, stripe_sub_id="sub_old", sub_status="canceled", stripe_customer_id="cus_1")
    offer = await start_checkout(svc, user, NOW)
    assert not offer.with_trial and stripe_fake.created[0]["trial_end"] is None
    assert stripe_fake.customers == []  # existing customer reused


async def test_open_session_is_reused(svc, stripe_fake):
    user = await add_user(svc.db, 1)
    first = await start_checkout(svc, user, NOW)
    user = await get_user(svc.db, 1)
    second = await start_checkout(svc, user, NOW + 20 * M)
    assert second.reused and second.url == first.url
    assert len(stripe_fake.created) == 1


async def test_session_with_other_trial_flag_not_reused(svc, stripe_fake):
    user = await add_user(svc.db, 1)
    await start_checkout(svc, user, NOW)
    # Their trial got used meanwhile (e.g. a sync found an older trial subscription).
    user = await add_user(svc.db, 1, trial_started_at=NOW - timedelta(days=60))
    offer = await start_checkout(svc, user, NOW + M)
    assert not offer.reused and not offer.with_trial and len(stripe_fake.created) == 2


async def test_session_close_to_expiry_not_reused(svc, stripe_fake):
    user = await add_user(svc.db, 1)
    await start_checkout(svc, user, NOW)
    later = NOW + timedelta(hours=24) - 2 * M
    offer = await start_checkout(svc, await get_user(svc.db, 1), later)
    assert not offer.reused and len(stripe_fake.created) == 2


async def test_customer_created_only_once(svc, stripe_fake):
    user = await add_user(svc.db, 1)
    await start_checkout(svc, user, NOW)
    async with svc.db.tx() as conn:
        await conn.execute("UPDATE checkout_sessions SET status='expired'")
    await start_checkout(svc, await get_user(svc.db, 1), NOW + timedelta(hours=1))
    assert stripe_fake.customers == [(1, "user1")]
    assert len(stripe_fake.created) == 2
    assert {c["customer_id"] for c in stripe_fake.created} == {"cus_1"}


async def test_customer_stored_meanwhile_wins(svc):
    class RacingStripe(FakeStripe):
        async def create_customer(self, telegram_id, username, idempotency_key=None):
            async with svc.db.tx() as conn:  # another tap stored one first
                await conn.execute("UPDATE users SET stripe_customer_id='cus_first' WHERE telegram_id=?",
                                   (telegram_id,))
            return "cus_second"

    racing = RacingStripe()
    svc2 = Services(db=svc.db, config=svc.config, tg=svc.tg, stripe=racing, price_label=None)
    await start_checkout(svc2, await add_user(svc.db, 1), NOW)
    assert (await get_user(svc.db, 1)).stripe_customer_id == "cus_first"
    assert racing.created[0]["customer_id"] == "cus_first"


@pytest.mark.parametrize("status,kind", [("active", "already_in"), ("trialing", "already_in"),
                                         ("past_due", "fix_payment")])
async def test_existing_subscription_gets_no_new_checkout(svc, stripe_fake, status, kind):
    user = await add_user(svc.db, 1, stripe_sub_id="sub_1", sub_status=status)
    offer = await start_checkout(svc, user, NOW)
    assert offer.kind == kind and stripe_fake.created == []


def test_idempotency_key_buckets():
    # NOW is on a 10-minute boundary, so a double tap 9 minutes later reuses the key.
    assert idempotency_key(5, True, NOW) == idempotency_key(5, True, NOW + 9 * M)
    assert idempotency_key(5, True, NOW) == f"checkout-5-t-{int(NOW.timestamp()) // 600}"
    assert idempotency_key(5, True, NOW) != idempotency_key(5, True, NOW + 11 * M)
    assert idempotency_key(5, True, NOW) != idempotency_key(5, False, NOW)


# --- poll_checkouts --------------------------------------------------------------

async def started(svc, telegram_id=1, **fields):
    user = await add_user(svc.db, telegram_id, **fields)
    return await start_checkout(svc, user, NOW)


async def test_completed_trial_checkout_applies_sub_and_welcomes_once(svc, stripe_fake, tg):
    await started(svc)
    stripe_fake.complete("cs_1", trial_sub(1))
    result = await poll_checkouts(svc, NOW + M)
    assert result.completed == 1
    user = await get_user(svc.db, 1)
    assert user.sub_status == "trialing" and user.stripe_sub_id == "sub_t1"
    assert user.sub_trial_end == NOW + timedelta(days=14)
    assert user.trial_started_at == NOW and user.trial_ends_at is None
    assert svc.entitled(user, NOW + M)
    assert (await checkout_rows(svc.db))[0]["status"] == "complete"

    messages = tg.sent_to(1)
    assert len(messages) == 1
    assert "free trial is on until <b>15 Nov 2026, 12:00</b>" in messages[0]
    assert "first payment of £2.99/month is taken on that date" in messages[0]
    join = tg.markups_to(1)[0].inline_keyboard[0][0]
    assert join.url == svc.config.paid_join_request_link

    # A second poll (even if the row were somehow open again) never re-sends.
    async with svc.db.tx() as conn:
        await conn.execute("UPDATE checkout_sessions SET status='open'")
    await poll_checkouts(svc, NOW + 2 * M)
    assert len(tg.sent_to(1)) == 1
    assert "trial_started" in await audit_actions(svc.db, 1)


async def test_paid_checkout_message_without_join_button_when_in_channel(svc, stripe_fake, tg):
    await started(svc, trial_started_at=NOW - timedelta(days=40), in_paid_channel=True)
    stripe_fake.complete("cs_1", sub_info(telegram_id=1))
    await poll_checkouts(svc, NOW + M)
    assert tg.sent_to(1) == [texts.PAID]
    assert tg.markups_to(1) == [None]


async def test_expired_checkout_marked(svc, stripe_fake, tg):
    await started(svc)
    stripe_fake.expire("cs_1")
    result = await poll_checkouts(svc, NOW + timedelta(hours=25))
    assert result.expired == 1
    assert (await checkout_rows(svc.db))[0]["status"] == "expired"
    assert "checkout_expired" in await audit_actions(svc.db, 1)
    assert tg.events == []


async def test_welcome_network_failure_keeps_session_open_for_retry(svc, stripe_fake, tg):
    await started(svc)
    stripe_fake.complete("cs_1", trial_sub(1))
    tg.send_results[1] = SendResult.FAILED
    assert (await poll_checkouts(svc, NOW + M)).completed == 0
    assert (await checkout_rows(svc.db))[0]["status"] == "open"
    assert await svc.db.fetchall("SELECT * FROM notices_sent") == []
    tg.send_results.clear()
    assert (await poll_checkouts(svc, NOW + 2 * M)).completed == 1
    assert len(tg.sent_to(1)) == 2  # the failed try plus the successful one


async def test_poll_only_one_user(svc, stripe_fake):
    await started(svc, 1)
    await started(svc, 2)
    result = await poll_checkouts(svc, NOW + M, only_telegram_id=2)
    assert result.checked == 1


async def test_poll_stripe_down(svc, stripe_fake):
    await started(svc)
    stripe_fake.fail_get = True
    result = await poll_checkouts(svc, NOW + M)
    assert result.errors == 1 and (await checkout_rows(svc.db))[0]["status"] == "open"
    # Long after expiry and still not retrievable: stop asking.
    result = await poll_checkouts(svc, NOW + timedelta(days=3))
    assert result.expired == 1 and (await checkout_rows(svc.db))[0]["status"] == "expired"


async def test_poll_without_stripe_does_nothing(db, tg):
    from tests.fakes import make_svc
    assert (await poll_checkouts(make_svc(db, tg, None), NOW)).checked == 0


# --- apply_subscription / sync -------------------------------------------------------

async def test_trial_started_at_set_only_once(svc):
    await add_user(svc.db, 1)
    await apply_subscription(svc.db, trial_sub(1, start=NOW), NOW)
    later = trial_sub(1, start=NOW + timedelta(days=60), id="sub_t1b")
    await apply_subscription(svc.db, later, NOW + timedelta(days=60))   # no IntegrityError
    user = await get_user(svc.db, 1)
    assert user.trial_started_at == NOW and user.stripe_sub_id == "sub_t1b"
    assert (await audit_actions(svc.db, 1)).count("trial_started") == 1


async def test_apply_maps_by_customer_and_stores_customer(svc):
    await add_user(svc.db, 5, stripe_customer_id="cus_5")
    await add_user(svc.db, 6)
    assert await apply_subscription(svc.db, sub_info(telegram_id=None, customer="cus_5"), NOW) == 5
    assert await apply_subscription(svc.db, sub_info(telegram_id=None, customer="cus_zzz"), NOW) is None
    # metadata wins; customer stored when missing and not someone else's
    assert await apply_subscription(svc.db, sub_info(id="sub_6", telegram_id=6, customer="cus_6"), NOW) == 6
    assert (await get_user(svc.db, 6)).stripe_customer_id == "cus_6"
    await apply_subscription(svc.db, sub_info(id="sub_6", telegram_id=6, customer="cus_5"), NOW)
    assert (await get_user(svc.db, 6)).stripe_customer_id == "cus_6"


async def test_status_change_audited_only_on_change(svc):
    await add_user(svc.db, 1)
    for _ in range(3):
        await apply_subscription(svc.db, sub_info(), NOW)
    await apply_subscription(svc.db, sub_info(status="past_due"), NOW)
    assert (await audit_actions(svc.db, 1)).count("sub_status") == 2


async def test_period_end_from_items_and_cancel_at(svc):
    await add_user(svc.db, 42)
    T0 = int(NOW.timestamp())
    payload = sub_payload(status="active", trial_start=None, trial_end=None,
                          cancel_at=T0 + 30 * 86400, cancel_at_period_end=False,
                          items={"object": "list", "data": [{"id": "si_1", "current_period_end": T0 + 30 * 86400}]})
    await apply_subscription(svc.db, parse_subscription(payload), NOW)
    user = await get_user(svc.db, 42)
    assert user.sub_period_end == NOW + timedelta(days=30)
    assert user.cancel_at_period_end is True
    assert user.sub_status == "active" and svc.entitled(user, NOW)


async def test_sync_picks_best_subscription(svc, stripe_fake):
    await add_user(svc.db, 1)
    stripe_fake.subscriptions = [
        sub_info(id="sub_c", status="canceled", created=NOW + 3 * M),
        sub_info(id="sub_a_old", status="active", created=NOW),
        sub_info(id="sub_a_new", status="active", created=NOW + M),
        sub_info(id="sub_t", status="trialing", created=NOW + 2 * M),
    ]
    result = await sync_subscriptions(svc, NOW)
    assert result.ok and result.users_updated == 1
    assert (await get_user(svc.db, 1)).stripe_sub_id == "sub_a_new"
    assert result.multiple == [1]
    await sync_subscriptions(svc, NOW + 5 * M)
    assert (await audit_actions(svc.db, 1)).count("multiple_subs") == 1


@pytest.mark.parametrize("statuses,best", [
    # listed oldest first; sub ids are s0, s1, ...
    (["active", "trialing"], "s0"),
    (["trialing", "past_due"], "s0"),
    (["past_due", "canceled"], "s0"),
    (["canceled", "unpaid"], "s1"),               # same rank -> newest
    (["active", "canceled", "active"], "s2"),
])
def test_best_order(statuses, best):
    subs = [sub_info(id=f"s{i}", status=s, created=NOW + i * M) for i, s in enumerate(statuses)]
    assert billing.pick_best(subs).id == best


async def test_sync_writes_kv_only_on_success(svc, stripe_fake, monkeypatch):
    await add_user(svc.db, 1)
    await add_user(svc.db, 2)
    stripe_fake.subscriptions = [sub_info(telegram_id=1), sub_info(id="sub_2", telegram_id=2, customer="cus_2")]
    await sync_subscriptions(svc, NOW)
    assert await svc.db.kv_get(LAST_FULL_SYNC_KEY) == to_iso(NOW)

    stripe_fake.fail_list = True
    with pytest.raises(StripeGatewayError):
        await sync_subscriptions(svc, NOW + 5 * M)
    assert await svc.db.kv_get(LAST_FULL_SYNC_KEY) == to_iso(NOW)

    stripe_fake.fail_list = False
    real_write = billing.write_subscription

    async def flaky_write(db, telegram_id, sub, now, **kw):
        if telegram_id == 2:
            raise RuntimeError("disk full")
        return await real_write(db, telegram_id, sub, now, **kw)

    monkeypatch.setattr(billing, "write_subscription", flaky_write)
    result = await sync_subscriptions(svc, NOW + 10 * M)
    assert not result.ok and result.users_updated == 1
    assert await svc.db.kv_get(LAST_FULL_SYNC_KEY) == to_iso(NOW)


async def test_sync_unmatched_subscriptions_dont_fail_the_sync(svc, stripe_fake):
    await add_user(svc.db, 1)
    stripe_fake.subscriptions = [sub_info(telegram_id=999, customer="cus_unknown")]
    result = await sync_subscriptions(svc, NOW)
    assert result.ok and result.unmatched == 1
    assert await svc.db.kv_get(LAST_FULL_SYNC_KEY) == to_iso(NOW)


# --- past-due anchor -------------------------------------------------------------------

async def test_period_start_and_past_due_since_stored(svc):
    await add_user(svc.db, 1)
    D = timedelta(days=1)
    # Failed renewal: Stripe has already opened the next, unpaid period.
    await apply_subscription(svc.db, sub_info(status="past_due", period_start=NOW - D, period_end=NOW + 29 * D), NOW)
    user = await get_user(svc.db, 1)
    assert user.sub_period_start == NOW - D and user.past_due_since == NOW - D
    assert svc.entitled(user, NOW) and not svc.entitled(user, NOW + 2 * D + M)

    # Stripe left it past_due and started another unpaid period: the grace doesn't restart.
    await apply_subscription(svc.db, sub_info(status="past_due", period_start=NOW + 29 * D,
                                              period_end=NOW + 59 * D), NOW + 29 * D)
    user = await get_user(svc.db, 1)
    assert user.past_due_since == NOW - D and not svc.entitled(user, NOW + 29 * D + M)

    # Paid: cleared. A later failure on the same sub anchors at the new period.
    await apply_subscription(svc.db, sub_info(status="active", period_start=NOW + 29 * D), NOW + 30 * D)
    assert (await get_user(svc.db, 1)).past_due_since is None
    await apply_subscription(svc.db, sub_info(status="past_due", period_start=NOW + 59 * D), NOW + 59 * D)
    assert (await get_user(svc.db, 1)).past_due_since == NOW + 59 * D

    # A different subscription that's past_due gets its own anchor.
    await apply_subscription(svc.db, sub_info(id="sub_2", status="past_due", period_start=NOW + 60 * D),
                             NOW + 60 * D)
    assert (await get_user(svc.db, 1)).past_due_since == NOW + 60 * D


# --- sync vs a checkout poll landing mid-listing ------------------------------------------

async def returning_user_pays(svc, stripe_fake):
    """A former subscriber (old sub canceled) checks out again; Stripe completes it."""
    old = sub_info(id="sub_old", status="canceled", created=NOW - timedelta(days=90),
                   period_end=NOW - timedelta(days=60))
    await add_user(svc.db, 1, trial_started_at=NOW - timedelta(days=100), stripe_customer_id="cus_1")
    await apply_subscription(svc.db, old, NOW - timedelta(days=1))
    await start_checkout(svc, await get_user(svc.db, 1), NOW - timedelta(minutes=2))
    new = sub_info(id="sub_new", status="active", created=NOW)
    stripe_fake.complete("cs_1", new)
    stripe_fake.subscriptions = [old]   # the listing was taken before sub_new existed
    return old, new


@pytest.mark.parametrize("poll_at", [NOW, NOW - timedelta(seconds=30)])
async def test_sync_does_not_overwrite_a_checkout_applied_during_the_listing(svc, stripe_fake, poll_at):
    old, new = await returning_user_pays(svc, stripe_fake)

    async def poll_meanwhile():
        # poll_at = NOW - 30s: a poll pass that began before the sync, so its stamp is older.
        await poll_checkouts(svc, poll_at)

    stripe_fake.on_list = poll_meanwhile
    stripe_fake.retrievable = {}   # isolate the race guard: retrieving sub_new fails here
    stripe_fake.fail_retrieve = False
    result = await sync_subscriptions(svc, NOW)
    user = await get_user(svc.db, 1)
    assert user.stripe_sub_id == "sub_new" and user.sub_status == "active" and svc.entitled(user, NOW)
    assert result.ok and result.users_updated == 0 and result.skipped == 1
    assert await svc.db.kv_get(LAST_FULL_SYNC_KEY) == to_iso(NOW)   # a skip isn't a failure

    # The next sync lists both and keeps the new one.
    stripe_fake.on_list = None
    stripe_fake.subscriptions = [new, old]
    await sync_subscriptions(svc, NOW + 5 * M)
    assert (await get_user(svc.db, 1)).stripe_sub_id == "sub_new"


async def test_sync_still_overwrites_old_data(svc, stripe_fake):
    await add_user(svc.db, 1)
    await apply_subscription(svc.db, sub_info(id="sub_a", status="active"), NOW - 10 * M)
    stripe_fake.subscriptions = [sub_info(id="sub_a", status="canceled")]
    result = await sync_subscriptions(svc, NOW)
    assert result.users_updated == 1 and (await get_user(svc.db, 1)).sub_status == "canceled"


# --- stored subscriptions missing from the listing --------------------------------------

async def test_unlisted_stored_subscription_is_retrieved(svc, stripe_fake):
    # e.g. a test-clock subscription: subscriptions.list leaves those out.
    await add_user(svc.db, 1)
    await apply_subscription(svc.db, sub_info(id="sub_tc", status="trialing"), NOW - timedelta(days=1))
    stripe_fake.retrievable = {"sub_tc": sub_info(id="sub_tc", status="active")}
    result = await sync_subscriptions(svc, NOW)
    assert result.ok and stripe_fake.retrieved == ["sub_tc"]
    assert (await get_user(svc.db, 1)).sub_status == "active"


async def test_deleted_subscription_marked_missing_once(svc, stripe_fake):
    await add_user(svc.db, 1)
    await apply_subscription(svc.db, sub_info(id="sub_gone", status="trialing"), NOW - timedelta(days=1))
    result = await sync_subscriptions(svc, NOW)   # empty listing, retrieve says "no such subscription"
    user = await get_user(svc.db, 1)
    assert result.ok and user.sub_status == billing.MISSING_STATUS and not svc.entitled(user, NOW)
    assert user.stripe_sub_id == "sub_gone"       # still counts as "had a subscription"
    assert "sub_missing" in await audit_actions(svc.db, 1)
    await sync_subscriptions(svc, NOW + 5 * M)
    assert stripe_fake.retrieved == ["sub_gone"]  # not asked about again


async def test_absence_alone_never_downgrades(svc, stripe_fake):
    await add_user(svc.db, 1)
    await apply_subscription(svc.db, sub_info(id="sub_a", status="active"), NOW - timedelta(days=1))
    stripe_fake.fail_retrieve = True
    result = await sync_subscriptions(svc, NOW)
    assert not result.ok and await svc.db.kv_get(LAST_FULL_SYNC_KEY) is None
    assert (await get_user(svc.db, 1)).sub_status == "active"


# --- Stripe customer deleted out of band ---------------------------------------------------

async def test_deleted_customer_replaced_and_checkout_retried(svc, stripe_fake):
    await add_user(svc.db, 1, stripe_customer_id="cus_old")
    stripe_fake.deleted_customers = {"cus_old"}
    offer = await start_checkout(svc, await get_user(svc.db, 1), NOW)
    assert offer.kind == "checkout" and offer.url
    assert stripe_fake.customer_keys == [f"customer-1-{int(NOW.timestamp())}"]   # not customer-1
    user = await get_user(svc.db, 1)
    assert user.stripe_customer_id == "cus_1"
    assert [c["customer_id"] for c in stripe_fake.created] == ["cus_1"]
    assert stripe_fake.created[0]["idempotency_key"] == idempotency_key(1, True, NOW, NOW + timedelta(days=14)) + "-cus_1"
    actions = await audit_actions(svc.db, 1)
    assert "stripe_customer_missing" in actions and "checkout_created" in actions


async def test_sync_mid_listing_poll_with_retrieve_keeps_new_sub(svc, stripe_fake):
    old, new = await returning_user_pays(svc, stripe_fake)

    async def poll_meanwhile():
        await poll_checkouts(svc, NOW - timedelta(seconds=30))

    stripe_fake.on_list = poll_meanwhile
    stripe_fake.retrievable = {"sub_new": new}   # stored but unlisted, so the sync asks Stripe
    result = await sync_subscriptions(svc, NOW)
    assert result.ok and stripe_fake.retrieved == ["sub_new"]
    user = await get_user(svc.db, 1)
    assert user.stripe_sub_id == "sub_new" and svc.entitled(user, NOW)


# --- the fixed trial_end -----------------------------------------------------------------

def test_trial_end_is_creation_time_plus_trial_days_cut_to_the_minute():
    from first_in_bot.billing import trial_end_for

    odd = NOW + timedelta(seconds=37)
    assert trial_end_for(odd, 14) == NOW + timedelta(days=14)
    assert trial_end_for(NOW, 3) == NOW + timedelta(days=3)
    assert trial_end_for(odd, 14).tzinfo is not None


def test_idempotency_key_differs_when_the_trial_end_differs():
    a = NOW + timedelta(days=14)
    assert idempotency_key(1, True, NOW, a) == idempotency_key(1, True, NOW + 30 * timedelta(seconds=1), a)
    assert idempotency_key(1, True, NOW, a) != idempotency_key(1, True, NOW, a + timedelta(minutes=1))


async def test_reused_session_keeps_its_original_trial_end(svc, stripe_fake):
    user = await add_user(svc.db, 1)
    first = await start_checkout(svc, user, NOW)
    later = NOW + timedelta(hours=20)             # still inside the session's 24h life
    second = await start_checkout(svc, await get_user(svc.db, 1), later)
    assert second.reused and second.url == first.url
    assert second.trial_end == first.trial_end == NOW + timedelta(days=14)   # not later + 14 days
    assert len(stripe_fake.created) == 1


async def test_open_trial_session_without_a_stored_trial_end_is_not_reused(svc, stripe_fake):
    user = await add_user(svc.db, 1)
    await start_checkout(svc, user, NOW)
    async with svc.db.tx() as conn:               # as if made before migration 5
        await conn.execute("UPDATE checkout_sessions SET trial_end=NULL")
    offer = await start_checkout(svc, await get_user(svc.db, 1), NOW + 20 * M)
    assert not offer.reused and offer.trial_end == NOW + 20 * M + timedelta(days=14)
    assert len(stripe_fake.created) == 2


# --- owner alerts from the sync -------------------------------------------------------------

async def test_multiple_live_subscriptions_alert_the_owner_rate_limited(svc, stripe_fake, tg):
    await add_user(svc.db, 1)
    await add_user(svc.db, 2)
    stripe_fake.subscriptions = [
        sub_info(id="sub_a", status="active"), sub_info(id="sub_b", status="trialing", created=NOW + M),
        sub_info(id="sub_c", status="active", telegram_id=2, customer="cus_2"),
        sub_info(id="sub_d", status="past_due", telegram_id=2, customer="cus_2"),
    ]
    await sync_subscriptions(svc, NOW)
    [alert] = tg.sent_to(OWNER)
    assert "More than one live subscription" in alert
    assert "user 1: sub_a, sub_b\nuser 2: sub_c, sub_d" in alert       # one DM for everyone
    await sync_subscriptions(svc, NOW + 5 * M)
    assert len(tg.sent_to(OWNER)) == 1
    await sync_subscriptions(svc, NOW + 30 * M)
    assert len(tg.sent_to(OWNER)) == 2                                  # a reminder while it lasts


async def test_subscription_gone_from_stripe_alerts_once(svc, stripe_fake, tg):
    await add_user(svc.db, 1)
    await apply_subscription(svc.db, sub_info(id="sub_gone", status="trialing"), NOW - timedelta(days=1))
    await sync_subscriptions(svc, NOW)
    [alert] = tg.sent_to(OWNER)
    assert "Subscription no longer at Stripe" in alert and "user 1: sub_gone" in alert
    await sync_subscriptions(svc, NOW + 31 * M)
    assert len(tg.sent_to(OWNER)) == 1                                  # marked once, so alerted once


async def test_a_failing_alert_never_fails_the_sync(svc, stripe_fake, tg, monkeypatch):
    async def exploding_send(*args, **kwargs):
        raise RuntimeError("Telegram is down")

    monkeypatch.setattr(tg, "send", exploding_send)
    await add_user(svc.db, 1)
    await add_user(svc.db, 2)
    await apply_subscription(svc.db, sub_info(id="sub_gone", telegram_id=2, customer="cus_2"),
                             NOW - timedelta(days=1))
    stripe_fake.subscriptions = [sub_info(id="sub_a"), sub_info(id="sub_b", created=NOW + M)]
    result = await sync_subscriptions(svc, NOW)
    assert result.ok and result.multiple == [1]
    assert await svc.db.kv_get(LAST_FULL_SYNC_KEY) == to_iso(NOW)
    assert (await get_user(svc.db, 2)).sub_status == billing.MISSING_STATUS
