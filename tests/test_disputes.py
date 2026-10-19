"""The daily disputes check: a new dispute puts the user on hold ('disputed', not
entitled) and alerts the owner once, the 5-minute sync can't undo it, and winning
every dispute restores the user from Stripe."""
from datetime import datetime, timedelta, timezone

import pytest

from first_in_bot.billing import start_checkout, sync_subscriptions, write_subscription
from first_in_bot.disputes import check_disputes
from first_in_bot.stripe_gateway import DisputeInfo, parse_dispute
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.users import get_user
from tests.fakes import NOW, OWNER, add_user, audit_actions, sub_info

D = timedelta(days=1)
ACTIVE = dict(stripe_customer_id="cus_1", stripe_sub_id="sub_1", sub_status="active",
              sub_period_end=NOW + 20 * D, in_paid_channel=True)


def dispute(id="dp_1", status="needs_response", customer="cus_1"):
    return DisputeInfo(id=id, status=status, charge_id="ch_1", customer_id=customer, created=NOW)


async def disputes_table(db):
    return [(r["id"], r["telegram_id"], r["status"], r["alerted"])
            for r in await db.fetchall("SELECT * FROM disputes ORDER BY id")]


# --- parsing -------------------------------------------------------------------------

def test_parse_dispute_with_expanded_charge():
    d = parse_dispute({"id": "dp_1", "status": "under_review", "created": 1_790_000_000,
                       "charge": {"id": "ch_9", "customer": "cus_9"}})
    assert (d.id, d.status, d.charge_id, d.customer_id) == ("dp_1", "under_review", "ch_9", "cus_9")
    assert d.created == datetime.fromtimestamp(1_790_000_000, tz=timezone.utc)


def test_parse_dispute_without_expanded_charge_has_no_customer():
    d = parse_dispute({"id": "dp_1", "status": "lost", "charge": "ch_9"})
    assert d.charge_id == "ch_9" and d.customer_id is None


# --- a new dispute -----------------------------------------------------------------------

async def test_new_dispute_holds_the_user_and_alerts_the_owner(svc, tg, stripe_fake):
    await add_user(svc.db, 1, **ACTIVE)
    stripe_fake.disputes = [dispute()]
    result = await check_disputes(svc, NOW)
    assert (result.new, result.held, result.alerts_sent) == (1, 1, 1)
    user = await get_user(svc.db, 1)
    assert user.sub_status == "disputed" and not svc.entitled(user, NOW)
    assert not user.banned                                  # never an automatic ban
    [alert] = tg.sent_to(OWNER)
    assert "Payment dispute" in alert and "dp_1" in alert and "@user1" in alert and "not banned" in alert
    assert await disputes_table(svc.db) == [("dp_1", 1, "needs_response", 1)]
    actions = await audit_actions(svc.db, 1)
    assert "dispute_seen" in actions and "disputed" in actions


async def test_second_run_does_nothing_new(svc, tg, stripe_fake):
    await add_user(svc.db, 1, **ACTIVE)
    stripe_fake.disputes = [dispute()]
    await check_disputes(svc, NOW)
    result = await check_disputes(svc, NOW + D)
    assert (result.new, result.held, result.alerts_sent) == (0, 0, 0)
    assert len(tg.sent_to(OWNER)) == 1


async def test_a_trialing_user_is_held_too(svc, tg, stripe_fake):
    await add_user(svc.db, 1, stripe_customer_id="cus_1", stripe_sub_id="sub_1", sub_status="trialing",
                   sub_trial_end=NOW + 5 * D)
    stripe_fake.disputes = [dispute()]
    await check_disputes(svc, NOW)
    assert not svc.entitled(await get_user(svc.db, 1), NOW)


async def test_dispute_for_an_unknown_customer_still_alerts(svc, tg, stripe_fake):
    stripe_fake.disputes = [dispute(customer="cus_nobody")]
    result = await check_disputes(svc, NOW)
    assert (result.unmatched, result.held, result.alerts_sent) == (1, 0, 1)
    assert "no matching user" in tg.sent_to(OWNER)[0]


async def test_old_dispute_already_won_is_recorded_quietly(svc, tg, stripe_fake):
    await add_user(svc.db, 1, **ACTIVE)
    stripe_fake.disputes = [dispute(status="won")]
    result = await check_disputes(svc, NOW)
    assert (result.held, result.alerts_sent) == (0, 0)
    assert (await get_user(svc.db, 1)).sub_status == "active" and tg.events == []


async def test_failed_alert_is_retried_next_run(svc, tg, stripe_fake):
    await add_user(svc.db, 1, **ACTIVE)
    stripe_fake.disputes = [dispute()]
    tg.send_results[OWNER] = SendResult.FAILED
    assert (await check_disputes(svc, NOW)).alerts_sent == 0
    assert (await disputes_table(svc.db))[0][3] == 0
    assert (await get_user(svc.db, 1)).sub_status == "disputed"      # the hold doesn't wait for the DM
    tg.send_results.clear()
    assert (await check_disputes(svc, NOW + D)).alerts_sent == 1


async def test_stripe_failure_raises_and_changes_nothing(svc, tg, stripe_fake):
    from first_in_bot.stripe_gateway import StripeGatewayError

    await add_user(svc.db, 1, **ACTIVE)
    stripe_fake.fail_disputes = True
    with pytest.raises(StripeGatewayError):
        await check_disputes(svc, NOW)
    assert (await get_user(svc.db, 1)).sub_status == "active"


async def test_no_stripe_no_check(db, tg):
    from tests.fakes import make_svc
    result = await check_disputes(make_svc(db, tg, None), NOW)
    assert result.seen == 0


# --- 'disputed' survives the sync, blocks a new checkout --------------------------------------

async def test_sync_does_not_undo_the_hold(svc, tg, stripe_fake):
    await add_user(svc.db, 1, **ACTIVE, telegram_id=None) if False else await add_user(svc.db, 1, **ACTIVE)
    stripe_fake.disputes = [dispute()]
    await check_disputes(svc, NOW)
    stripe_fake.subscriptions = [sub_info(id="sub_1", status="active", customer="cus_1", telegram_id=1,
                                          period_end=NOW + 25 * D)]
    result = await sync_subscriptions(svc, NOW + timedelta(minutes=5))
    assert result.ok
    user = await get_user(svc.db, 1)
    assert user.sub_status == "disputed"
    assert user.sub_period_end == NOW + 25 * D      # everything else is still refreshed


async def test_disputed_user_cannot_start_a_new_checkout(svc, stripe_fake):
    user = await add_user(svc.db, 1, **{**ACTIVE, "sub_status": "disputed"})
    offer = await start_checkout(svc, user, NOW)
    assert offer.kind == "disputed" and stripe_fake.created == []


async def test_status_text_and_checkout_reply_explain(svc):
    from first_in_bot.handlers.billing import checkout_reply, status_reply

    await add_user(svc.db, 1, **{**ACTIVE, "sub_status": "disputed"})
    assert "payment dispute" in (await status_reply(svc, 1, NOW)).text
    assert "payment dispute" in (await checkout_reply(svc, 1, NOW)).text


# --- clearing -----------------------------------------------------------------------------------

async def test_winning_every_dispute_restores_the_stripe_status(svc, tg, stripe_fake):
    await add_user(svc.db, 1, **ACTIVE)
    stripe_fake.disputes = [dispute("dp_1"), dispute("dp_2")]
    await check_disputes(svc, NOW)
    stripe_fake.retrievable["sub_1"] = sub_info(id="sub_1", status="active", customer="cus_1", telegram_id=1)

    stripe_fake.disputes = [dispute("dp_1", "won"), dispute("dp_2", "under_review")]
    assert (await check_disputes(svc, NOW + D)).cleared == 0
    assert (await get_user(svc.db, 1)).sub_status == "disputed"          # one is still open

    stripe_fake.disputes = [dispute("dp_1", "won"), dispute("dp_2", "warning_closed")]
    assert (await check_disputes(svc, NOW + 2 * D)).cleared == 1
    user = await get_user(svc.db, 1)
    assert user.sub_status == "active" and svc.entitled(user, NOW + 2 * D)
    assert "dispute_cleared" in await audit_actions(svc.db, 1)


async def test_a_lost_dispute_stays_disputed(svc, tg, stripe_fake):
    await add_user(svc.db, 1, **ACTIVE)
    stripe_fake.disputes = [dispute()]
    await check_disputes(svc, NOW)
    stripe_fake.disputes = [dispute(status="lost")]
    assert (await check_disputes(svc, NOW + D)).cleared == 0
    assert (await get_user(svc.db, 1)).sub_status == "disputed"


async def test_clearing_waits_if_stripe_cant_be_reached(svc, tg, stripe_fake):
    await add_user(svc.db, 1, **ACTIVE)
    stripe_fake.disputes = [dispute()]
    await check_disputes(svc, NOW)
    stripe_fake.disputes = [dispute(status="won")]
    stripe_fake.fail_retrieve = True
    result = await check_disputes(svc, NOW + D)
    assert result.cleared == 0 and not result.ok
    assert (await get_user(svc.db, 1)).sub_status == "disputed"
    stripe_fake.fail_retrieve = False
    stripe_fake.retrievable["sub_1"] = sub_info(id="sub_1", status="active", customer="cus_1", telegram_id=1)
    assert (await check_disputes(svc, NOW + 2 * D)).cleared == 1


async def test_write_subscription_clear_flag_is_the_only_way_out(svc):
    await add_user(svc.db, 1, **{**ACTIVE, "sub_status": "disputed"})
    sub = sub_info(id="sub_1", status="active", customer="cus_1", telegram_id=1)
    await write_subscription(svc.db, 1, sub, NOW)
    assert (await get_user(svc.db, 1)).sub_status == "disputed"
    await write_subscription(svc.db, 1, sub, NOW, clear_dispute=True)
    assert (await get_user(svc.db, 1)).sub_status == "active"
