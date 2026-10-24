"""Owner alerts: one per kind per 30 minutes, the limit kept in kv so it survives
restarts, a network failure retried, never raising, and no secrets in the text."""
import html
from datetime import timedelta

import pytest

from first_in_bot.alerts import ALERT_INTERVAL, ERROR_TEXT_LIMIT, alert_owner, error_text, send_owner, short_error
from first_in_bot.stripe_gateway import StripeGatewayError
from first_in_bot.telegram_gateway import SendResult
from first_in_bot.timeutil import to_iso
from tests.fakes import NOW, OWNER, FakeTelegram, make_svc
from tests.test_config import TOKEN

M = timedelta(minutes=1)


async def test_alert_goes_to_the_owner_once_per_kind_per_half_hour(svc, tg):
    assert await alert_owner(svc, "boom", "⚠️ <b>Boom</b>", NOW)
    assert await svc.db.kv_get("alert_last:boom") == to_iso(NOW)
    assert not await alert_owner(svc, "boom", "again", NOW + ALERT_INTERVAL - timedelta(seconds=1))
    assert await alert_owner(svc, "other", "another kind", NOW + M)
    assert await alert_owner(svc, "boom", "later", NOW + ALERT_INTERVAL)
    assert tg.sent_to(OWNER) == ["⚠️ <b>Boom</b>", "another kind", "later"]


async def test_the_limit_survives_a_restart(db, tg):
    await alert_owner(make_svc(db, tg, None), "startup", "started", NOW)
    restarted = make_svc(db, FakeTelegram(), None)     # a new process on the same DB
    assert not await alert_owner(restarted, "startup", "started", NOW + timedelta(seconds=10))
    assert restarted.tg.events == []


async def test_a_custom_interval(svc, tg):
    await alert_owner(svc, "boom", "x", NOW)
    assert not await alert_owner(svc, "boom", "x", NOW + 2 * M, interval=3 * M)
    assert await alert_owner(svc, "boom", "x", NOW + 3 * M, interval=3 * M)


async def test_a_network_failure_is_not_stamped_so_the_next_one_retries(svc, tg):
    tg.send_results[OWNER] = SendResult.FAILED
    assert not await alert_owner(svc, "boom", "x", NOW)
    assert await svc.db.kv_get("alert_last:boom") is None
    tg.send_results.clear()
    assert await alert_owner(svc, "boom", "x", NOW + M)


@pytest.mark.parametrize("result", [SendResult.BLOCKED, SendResult.REJECTED])
async def test_blocked_or_rejected_is_stamped_as_retrying_wont_help(svc, tg, result):
    tg.send_results[OWNER] = result
    assert not await alert_owner(svc, "boom", "x", NOW)
    assert await svc.db.kv_get("alert_last:boom") == to_iso(NOW)
    assert not await alert_owner(svc, "boom", "x", NOW + M)
    assert len(tg.sent_to(OWNER)) == 1


async def test_a_clock_that_went_back_doesnt_silence_alerts(svc, tg):
    await alert_owner(svc, "boom", "x", NOW)
    assert await alert_owner(svc, "boom", "x", NOW - timedelta(hours=1))


async def test_never_raises(svc, tg, monkeypatch, caplog):
    async def exploding_send(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(tg, "send", exploding_send)
    assert not await alert_owner(svc, "boom", "x", NOW)
    assert await svc.db.kv_get("alert_last:boom") is None     # counts as a network failure: retried
    assert await send_owner(svc, "x") is SendResult.FAILED

    async def broken_kv(key):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(svc.db, "kv_get", broken_kv)
    assert not await alert_owner(svc, "boom", "x", NOW)
    assert "Owner alert boom failed" in caplog.text


async def test_no_owner_configured(db, tg):
    svc = make_svc(db, tg, None, ADMIN_IDS=",")
    assert svc.owner_id is None
    assert not await alert_owner(svc, "boom", "x", NOW)
    assert tg.events == []


# --- exception text --------------------------------------------------------------------

def test_error_text_keeps_the_type_and_hides_keys_and_tokens():
    text = error_text(StripeGatewayError("bad key sk_test_abcdef1234567890 <b>"))
    assert text == "StripeGatewayError: bad key [key] &lt;b&gt;"
    text = error_text(RuntimeError(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed"))
    assert TOKEN not in text and "bot[token]/sendMessage" in text


def test_error_text_is_one_short_line():
    assert short_error(RuntimeError("first\n  second")) == "RuntimeError: first second"
    assert short_error(RuntimeError()) == "RuntimeError"
    long = short_error(ValueError("x" * 1000))
    assert len(long) == ERROR_TEXT_LIMIT and long.endswith("…") and long.startswith("ValueError: xxx")


def test_error_text_is_cut_before_escaping():
    text = error_text(RuntimeError("&" * 1000))
    # Every & is a whole &amp;: nothing was cut in the middle of an entity.
    assert text == html.escape(short_error(RuntimeError("&" * 1000)))
    assert text.replace("&amp;", "").endswith("…") and "&" not in text.replace("&amp;", "")
