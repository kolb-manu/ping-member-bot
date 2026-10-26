import pytest

from first_in_bot import lock
from first_in_bot.db import Database
from tests.fakes import FakeStripe, FakeTelegram, make_svc


@pytest.fixture(autouse=True)
def no_other_bot_processes(monkeypatch):
    """Tests never see the real process list: a copy of the bot running on the
    same machine (the owner's own) mustn't change what they find."""
    monkeypatch.setattr(lock, "_process_table", lambda: [])


@pytest.fixture
async def db():
    d = await Database.open(":memory:")
    yield d
    await d.close()


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def stripe_fake():
    return FakeStripe()


@pytest.fixture
def svc(db, tg, stripe_fake):
    return make_svc(db, tg, stripe_fake)
