import sqlite3
from datetime import datetime, timezone

import pytest

from first_in_bot.config import load_config
from first_in_bot.db import MIGRATIONS, Database
from first_in_bot.handlers.start import welcome_keyboard
from first_in_bot.models import User
from first_in_bot.users import get_user, register_user, sanitise_source
from tests.test_config import GOOD

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    d = await Database.open(":memory:")
    yield d
    await d.close()


@pytest.mark.parametrize("payload,expected", [
    ("tt_v1", "tt_v1"),
    ("IG", "ig"),
    ("flatchat", "flatchat"),
    (None, "unknown"),
    ("", "unknown"),
    ("has space", "unknown"),
    ("semi;colon", "unknown"),
    ("<script>", "unknown"),
    ("a" * 32, "a" * 32),
    ("a" * 33, "unknown"),
])
def test_sanitise_source(payload, expected):
    assert sanitise_source(payload) == expected


async def test_migrations_set_version_and_are_rerunnable(db):
    row = await db.fetchone("SELECT value FROM kv WHERE key='schema_version'")
    assert int(row["value"]) == len(MIGRATIONS)
    from first_in_bot.db import run_migrations
    await run_migrations(db)  # second run changes nothing
    row = await db.fetchone("SELECT value FROM kv WHERE key='schema_version'")
    assert int(row["value"]) == len(MIGRATIONS)


async def test_file_db_uses_wal_and_foreign_keys(tmp_path):
    d = await Database.open(str(tmp_path / "sub" / "t.db"))
    try:
        assert (await d.fetchone("PRAGMA journal_mode"))[0] == "wal"
        assert (await d.fetchone("PRAGMA foreign_keys"))[0] == 1
    finally:
        await d.close()


async def test_first_start_creates_user_and_audit(db):
    user, created = await register_user(db, 42, "bob", "Bob", "tt_v1", NOW)
    assert created and user.source == "tt_v1" and user.created_at == NOW
    audit = await db.fetchall("SELECT action FROM audit_log WHERE telegram_id=42")
    assert [r["action"] for r in audit] == ["user_created"]


async def test_only_first_source_is_kept_and_names_refresh(db):
    await register_user(db, 42, "bob", "Bob", "tt_v1", NOW)
    user, created = await register_user(db, 42, "bobby", "Bobby", "ig", NOW)
    assert not created
    assert user.source == "tt_v1"
    assert user.username == "bobby" and user.first_name == "Bobby"


async def test_unknown_source_when_no_payload(db):
    user, _ = await register_user(db, 7, None, None, None, NOW)
    assert user.source == "unknown"


async def test_trial_start_cannot_be_changed_once_set(db):
    await register_user(db, 42, "bob", "Bob", None, NOW)
    async with db.tx() as conn:
        await conn.execute("UPDATE users SET trial_started_at='2026-11-01T12:00:00+00:00' WHERE telegram_id=42")
    with pytest.raises(sqlite3.IntegrityError):
        async with db.tx() as conn:
            await conn.execute("UPDATE users SET trial_started_at=NULL WHERE telegram_id=42")
    with pytest.raises(sqlite3.IntegrityError):
        async with db.tx() as conn:
            await conn.execute("UPDATE users SET trial_started_at='2027-01-01T00:00:00+00:00' WHERE telegram_id=42")
    user = await get_user(db, 42)
    assert user.trial_started_at == NOW


async def test_failed_transaction_rolls_back(db):
    with pytest.raises(RuntimeError):
        async with db.tx() as conn:
            await conn.execute("INSERT INTO users(telegram_id, created_at) VALUES(1,'x')")
            raise RuntimeError("boom")
    assert await get_user(db, 1) is None


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_welcome_keyboard_for_new_user():
    config = load_config(GOOD)
    kb = buttons(welcome_keyboard(User(telegram_id=1), config))
    assert [b.text for b in kb] == ["Start my free 2 weeks", "Join the free channel", "How it works"]
    assert kb[1].url == config.free_channel_link


def test_welcome_keyboard_hides_trial_button_after_trial():
    config = load_config(GOOD)
    kb = buttons(welcome_keyboard(User(telegram_id=1, trial_started_at=NOW), config))
    assert "Start my free 2 weeks" not in [b.text for b in kb]


async def test_unknown_source_upgraded_to_real_tag(db):
    user, _ = await register_user(db, 5, "al", "Al", None, NOW)
    assert user.source == "unknown"
    user, created = await register_user(db, 5, "al", "Al", "tt_v1", NOW)
    assert not created and user.source == "tt_v1"
    actions = [r["action"] for r in await db.fetchall("SELECT action FROM audit_log WHERE telegram_id=5")]
    assert actions == ["user_created", "source_set"]


async def test_real_tag_never_replaced(db):
    await register_user(db, 5, "al", "Al", "tt_v1", NOW)
    for payload in ["ig", None, "garbage here"]:
        user, _ = await register_user(db, 5, "al", "Al", payload, NOW)
        assert user.source == "tt_v1"


async def test_unknown_stays_unknown_without_real_tag(db):
    await register_user(db, 5, "al", "Al", None, NOW)
    user, _ = await register_user(db, 5, "al", "Al", "bad tag!", NOW)
    assert user.source == "unknown"


async def test_banned_user_start_reply_has_no_buttons(db):
    from first_in_bot import texts
    from first_in_bot.handlers.start import start_reply
    config = load_config(GOOD)
    await register_user(db, 9, "bad", "Bad", "tt_v1", NOW)
    async with db.tx() as conn:
        await conn.execute("UPDATE users SET banned=1 WHERE telegram_id=9")
    user = await get_user(db, 9)
    text, markup = start_reply(user, config)
    assert text == texts.UNAVAILABLE and markup is None


async def test_trial_button_refuses_banned_and_unknown(db):
    # The trial button now opens a Stripe Checkout (phase 2); banned and
    # never-started users still get an alert and nothing is started for them.
    from types import SimpleNamespace
    from first_in_bot import texts
    from first_in_bot.handlers.billing import on_checkout_button
    from tests.fakes import FakeStripe, FakeTelegram, make_svc

    class FakeCallback:
        def __init__(self, uid):
            self.from_user = SimpleNamespace(id=uid)
            self.data = "trial:start"
            self.answers = []

        async def answer(self, text=None, show_alert=False):
            self.answers.append(text)

    stripe = FakeStripe()
    svc = make_svc(db, FakeTelegram(), stripe)
    await register_user(db, 9, "bad", "Bad", None, NOW)
    async with db.tx() as conn:
        await conn.execute("UPDATE users SET banned=1 WHERE telegram_id=9")
    cb = FakeCallback(9)
    await on_checkout_button(cb, svc)
    assert cb.answers == [texts.UNAVAILABLE]

    cb = FakeCallback(12345)  # never pressed /start
    await on_checkout_button(cb, svc)
    assert cb.answers == [texts.TRIAL_START_FIRST]
    assert (await get_user(db, 9)).trial_started_at is None
    assert stripe.created == [] and stripe.customers == []


async def test_upgrade_from_schema_2_keeps_past_due_access(tmp_path):
    """Rows synced before migration 3 have no period start; the old rule applies
    to them until the next sync, so the upgrade can't cut anyone off."""
    from datetime import timedelta

    import aiosqlite

    from first_in_bot import db as db_module
    from first_in_bot.entitlement import is_entitled

    path = str(tmp_path / "old.db")
    conn = await aiosqlite.connect(path, isolation_level=None)
    for migration in db_module.MIGRATIONS[:2]:
        await migration(conn)
    await conn.execute("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)")
    await conn.execute("INSERT INTO kv VALUES('schema_version', '2')")
    await conn.execute(
        "INSERT INTO users(telegram_id, created_at, sub_status, sub_period_end) VALUES(1, ?, 'past_due', ?)",
        ("2026-10-01T00:00:00+00:00", "2026-11-01T11:00:00+00:00"),
    )
    await conn.close()

    d = await Database.open(path)
    try:
        assert (await d.fetchone("SELECT value FROM kv WHERE key='schema_version'"))["value"] == "3"
        user = await get_user(d, 1)
        assert user.sub_period_start is None and user.past_due_since is None
        assert is_entitled(user, NOW, timedelta(hours=72))
    finally:
        await d.close()
