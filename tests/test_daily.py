"""daily_due: once per local day from the set time, catching up after downtime,
and exactly once across the Europe/London clock changes (29 Mar and 25 Oct 2026)."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from first_in_bot.daily import daily_due, mark_done

LONDON = ZoneInfo("Europe/London")
NINE = time(9, 0)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_due_from_the_set_time_until_done():
    assert daily_due(utc(2026, 11, 2, 8, 59), LONDON, NINE, None) == (False, "2026-11-02")
    assert daily_due(utc(2026, 11, 2, 9, 0), LONDON, NINE, None) == (True, "2026-11-02")
    assert daily_due(utc(2026, 11, 2, 9, 1), LONDON, NINE, "2026-11-02") == (False, "2026-11-02")
    assert daily_due(utc(2026, 11, 3, 9, 0), LONDON, NINE, "2026-11-02") == (True, "2026-11-03")


def test_a_bot_that_was_down_catches_up_the_same_day():
    assert daily_due(utc(2026, 11, 2, 23, 59), LONDON, NINE, "2026-11-01") == (True, "2026-11-02")


def test_summer_time_moves_the_utc_moment():
    # 09:00 BST is 08:00 UTC.
    assert daily_due(utc(2026, 7, 1, 7, 59), LONDON, NINE, None) == (False, "2026-07-01")
    assert daily_due(utc(2026, 7, 1, 8, 0), LONDON, NINE, None) == (True, "2026-07-01")


def test_the_date_is_the_local_one():
    # 23:30 UTC on 24 Oct is 00:30 BST on the 25th.
    assert daily_due(utc(2026, 10, 24, 23, 30), LONDON, time(0, 15), "2026-10-24") == (True, "2026-10-25")


def test_report_time_on_both_change_days():
    # 29 Mar: after the change 09:00 is 08:00 UTC. 25 Oct: after the change it's 09:00 UTC.
    assert not daily_due(utc(2026, 3, 29, 7, 59), LONDON, NINE, None)[0]
    assert daily_due(utc(2026, 3, 29, 8, 0), LONDON, NINE, None) == (True, "2026-03-29")
    assert not daily_due(utc(2026, 10, 25, 8, 59), LONDON, NINE, None)[0]
    assert daily_due(utc(2026, 10, 25, 9, 0), LONDON, NINE, None) == (True, "2026-10-25")


def test_a_time_the_spring_change_skips_runs_when_the_clocks_jump():
    # 29 Mar 2026: 01:00 GMT becomes 02:00 BST, so 01:30 never happens; run at 02:00.
    assert daily_due(utc(2026, 3, 29, 0, 59), LONDON, time(1, 30), "2026-03-28") == (False, "2026-03-29")
    assert daily_due(utc(2026, 3, 29, 1, 0), LONDON, time(1, 30), "2026-03-28") == (True, "2026-03-29")


def test_a_time_the_autumn_change_repeats_runs_once():
    # 25 Oct 2026: 02:00 BST becomes 01:00 GMT, so 01:30 happens twice.
    first, second = utc(2026, 10, 25, 0, 30), utc(2026, 10, 25, 1, 30)
    assert first.astimezone(LONDON).time() == second.astimezone(LONDON).time() == time(1, 30)
    assert daily_due(first, LONDON, time(1, 30), "2026-10-24") == (True, "2026-10-25")
    assert daily_due(second, LONDON, time(1, 30), "2026-10-25") == (False, "2026-10-25")


@pytest.mark.parametrize("change_day", [date(2026, 3, 29), date(2026, 10, 25)])
@pytest.mark.parametrize("at", [time(0, 0), time(1, 30), time(3, 30), NINE])
def test_exactly_once_per_local_day_around_a_clock_change(change_day, at):
    """Minute by minute over the day before, the change day and the day after,
    marking the job done whenever it's due, like the scheduler does."""
    t = datetime.combine(change_day - timedelta(days=1), time(0), tzinfo=LONDON).astimezone(timezone.utc)
    end = datetime.combine(change_day + timedelta(days=2), time(0), tzinfo=LONDON).astimezone(timezone.utc)
    last_done, runs = None, []
    while t < end:
        due, today = daily_due(t, LONDON, at, last_done)
        if due:
            runs.append((today, t.astimezone(LONDON).time()))
            last_done = today
        t += timedelta(minutes=1)
    assert [day for day, _ in runs] == [(change_day + timedelta(days=i)).isoformat() for i in (-1, 0, 1)]
    skipped = change_day.month == 3 and time(1) <= at < time(2)   # those times don't exist that day
    assert [local for _, local in runs] == [at, time(2, 0) if skipped else at, at]


async def test_mark_done(db):
    await mark_done(db, "backup_date", "2026-10-25")
    assert await db.kv_get("backup_date") == "2026-10-25"
