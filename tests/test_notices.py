"""The 2-days-before-the-trial-converts reminder (send_notices job)."""
from datetime import datetime, timedelta, timezone

import pytest

from first_in_bot.notices import send_notices
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.timeutil import to_iso
from first_in_bot.users import get_user
from tests.fakes import NOW, add_user

H = timedelta(hours=1)


def trialing(end, **extra):
    return dict(stripe_sub_id="sub_t", sub_status="trialing", trial_started_at=end - timedelta(days=14),
                sub_trial_end=end, sub_period_end=end, **extra)


async def notice_rows(db):
    return [(r["telegram_id"], r["kind"], r["cycle_key"])
            for r in await db.fetchall("SELECT * FROM notices_sent ORDER BY telegram_id")]


async def test_due_reminder_sent_once(svc, tg):
    end = NOW + timedelta(days=1)
    await add_user(svc.db, 1, **trialing(end))
    assert await send_notices(svc, NOW) == 1
    [text] = tg.sent_to(1)
    assert "ends on <b>2 Nov 2026, 12:00</b>" in text
    assert "£2.99/month is charged automatically" in text and "/manage" in text
    assert tg.markups_to(1)[0].inline_keyboard[0][0].callback_data == "manage"
    assert await notice_rows(svc.db) == [(1, "trial_2d", to_iso(end))]

    assert await send_notices(svc, NOW + 5 * timedelta(minutes=1)) == 0
    assert len(tg.sent_to(1)) == 1


@pytest.mark.parametrize("end,due", [
    (NOW + timedelta(days=3), False),
    (NOW + timedelta(days=2, seconds=1), False),
    (NOW + timedelta(days=2), True),          # window opens exactly 2 days before
    (NOW + H, True),
    (NOW, False),                             # trial already over
    (NOW - H, False),                         # bot was down; no "2 days left" after the end
])
async def test_reminder_window(svc, tg, end, due):
    await add_user(svc.db, 1, **trialing(end))
    assert await send_notices(svc, NOW) == int(due)


@pytest.mark.parametrize("extra", [
    {"cancel_at_period_end": True},     # cancelled during the trial: won't be charged
    {"sub_status": "active"},           # already converted
    {"sub_status": "canceled"},
    {"banned": True},
    {"blocked_bot": True},
])
async def test_irrelevant_reminders_skipped(svc, tg, extra):
    fields = trialing(NOW + timedelta(days=1))
    fields.update(extra)
    await add_user(svc.db, 1, **fields)
    assert await send_notices(svc, NOW) == 0
    assert tg.events == [] and await notice_rows(svc.db) == []


async def test_network_failure_deletes_row_so_it_retries(svc, tg):
    await add_user(svc.db, 1, **trialing(NOW + timedelta(days=1)))
    tg.send_results[1] = SendResult.FAILED
    assert await send_notices(svc, NOW) == 0
    assert await notice_rows(svc.db) == []
    tg.send_results.clear()
    assert await send_notices(svc, NOW + timedelta(minutes=5)) == 1


async def test_403_keeps_row_and_marks_blocked(svc, tg):
    await add_user(svc.db, 1, **trialing(NOW + timedelta(days=1)))
    tg.send_results[1] = SendResult.BLOCKED
    assert await send_notices(svc, NOW) == 0
    assert len(await notice_rows(svc.db)) == 1
    assert (await get_user(svc.db, 1)).blocked_bot
    await send_notices(svc, NOW + timedelta(minutes=5))
    assert len(tg.sent_to(1)) == 1     # never DMed again


async def test_reminder_date_in_summer_time(svc, tg):
    # 24 Oct 2026 is still BST (clocks go back on the 25th): 11:00 UTC shows as 12:00.
    end = datetime(2026, 10, 24, 11, 0, tzinfo=timezone.utc)
    await add_user(svc.db, 1, **trialing(end))
    await send_notices(svc, end - timedelta(days=1))
    assert "ends on <b>24 Oct 2026, 12:00</b>" in tg.sent_to(1)[0]


async def test_price_fallback_when_unknown(db, tg):
    from tests.fakes import make_svc
    svc = make_svc(db, tg, None)
    await add_user(db, 1, **trialing(NOW + timedelta(days=1)))
    await send_notices(svc, NOW)
    assert "the subscription price is charged automatically" in tg.sent_to(1)[0]


async def test_unexpected_send_error_frees_the_row_so_it_retries(svc, tg):
    await add_user(svc.db, 1, **trialing(NOW + timedelta(days=1)))
    real_send = tg.send

    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    tg.send = boom
    with pytest.raises(RuntimeError):
        await send_notices(svc, NOW)
    assert await notice_rows(svc.db) == []   # not lost: the pre-charge reminder is retried
    tg.send = real_send
    assert await send_notices(svc, NOW + timedelta(minutes=5)) == 1
