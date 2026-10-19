import pytest

from first_in_bot.db import Database
from tests.fakes import FakeStripe, FakeTelegram, make_svc


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
