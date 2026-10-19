import pytest

from first_in_bot.config import ConfigError, load_config

TOKEN = "123456789:" + "A" * 35

GOOD = {
    "BOT_TOKEN": TOKEN,
    "BOT_USERNAME": "@FirstInTestBot",
    "ADMIN_IDS": "111, 222",
    "PAID_CHANNEL_ID": "-1001234567890",
    "PAID_JOIN_REQUEST_LINK": "https://t.me/+abcdef",
    "FREE_CHANNEL_LINK": "https://t.me/firstinfree",
}


def test_good_config_and_defaults():
    c = load_config(GOOD)
    assert c.bot_username == "FirstInTestBot"
    assert c.admin_ids == (111, 222)
    assert c.paid_channel_id == -1001234567890
    assert c.trial_days == 14
    assert c.past_due_grace.total_seconds() == 72 * 3600
    assert c.removals_enabled is False
    assert c.stripe_mode == "not configured"
    assert c.stripe_success_url == "https://t.me/FirstInTestBot"
    assert str(c.timezone) == "Europe/London"


def test_missing_values_all_reported_at_once():
    with pytest.raises(ConfigError) as e:
        load_config({})
    text = str(e.value)
    for key in ["BOT_TOKEN", "BOT_USERNAME", "ADMIN_IDS", "PAID_CHANNEL_ID",
                "PAID_JOIN_REQUEST_LINK", "FREE_CHANNEL_LINK"]:
        assert key in text


@pytest.mark.parametrize("key,value", [
    ("BOT_TOKEN", "nonsense"),
    ("ADMIN_IDS", "abc"),
    ("PAID_CHANNEL_ID", "12345"),
    ("PAID_CHANNEL_ID", "oops"),
    ("PAID_JOIN_REQUEST_LINK", "http://example.com"),
    ("FREE_CHANNEL_LINK", "t.me/x"),
    ("TRIAL_DAYS", "0"),
    ("TRIAL_DAYS", "2"),      # Stripe Checkout needs trial_end at least 48 hours ahead
    ("REMOVALS_ENABLED", "maybe"),
    ("TIMEZONE", "Mars/Olympus"),
    ("STRIPE_PRICE_ID", "prod_123"),
])
def test_invalid_values_rejected(key, value):
    with pytest.raises(ConfigError):
        load_config({**GOOD, key: value})


def test_live_stripe_key_rejected():
    with pytest.raises(ConfigError) as e:
        load_config({**GOOD, "STRIPE_API_KEY": "sk_live_" + "x" * 20})
    assert "TEST" in str(e.value)


def test_test_stripe_key_accepted():
    c = load_config({**GOOD, "STRIPE_API_KEY": "rk_test_" + "x" * 20})
    assert c.stripe_mode == "TEST"


def test_secrets_never_in_repr_or_errors():
    c = load_config({**GOOD, "STRIPE_API_KEY": "rk_test_secretvalue123"})
    assert TOKEN not in repr(c)
    assert "secretvalue123" not in repr(c)
    with pytest.raises(ConfigError) as e:
        load_config({**GOOD, "BOT_TOKEN": "bad-token-secret", "STRIPE_API_KEY": "sk_live_zzz"})
    assert "bad-token-secret" not in str(e.value)
    assert "sk_live_zzz" not in str(e.value)


def test_trial_reminder_days_default_and_validation():
    assert load_config(GOOD).trial_reminder_days_before == 3
    assert load_config({**GOOD, "TRIAL_REMINDER_DAYS_BEFORE": "5"}).trial_reminder_days_before == 5
    for bad in ("0", "soon"):
        with pytest.raises(ConfigError):
            load_config({**GOOD, "TRIAL_REMINDER_DAYS_BEFORE": bad})
