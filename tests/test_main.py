"""main.run with fakes for the bot, the DB and the dispatcher: the startup alert,
the shutdown order, waiting for updates still being handled, and the price read
at startup."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from first_in_bot import billing, main
from first_in_bot.db import Database, kv_set
from first_in_bot.scheduler import Job
from tests.fakes import OWNER, PRICE, FakeStripe, FakeTelegram, make_config, make_svc


class Harness:
    """main.run with every outside piece faked. events records what happened, in
    order; set jobs (the scheduler's) and handlers (updates being handled when
    polling stops) before run()."""

    def __init__(self, monkeypatch):
        self.events: list = []
        self.jobs: list[Job] = []
        self.handlers: list = []
        self.tg = FakeTelegram()
        self.db: Database | None = None
        events = self.events
        harness = self

        async def rights_problems(bot_id):
            return ["the bot is not an admin of the paid channel"]

        self.tg.bot_rights_problems = rights_problems

        class FakeSession:
            async def close(self):
                events.append("session closed")

        class FakeBot:
            def __init__(self, token, default=None):
                self.session = FakeSession()

            async def get_me(self):
                return SimpleNamespace(id=42, username="FirstInTestBot")

        class FakeDispatcher:
            """Like aiogram 3.31: each update runs as a task in _handle_update_tasks,
            and start_polling returns on SIGTERM without waiting for them."""

            def __init__(self):
                self._handle_update_tasks: set[asyncio.Task] = set()

            async def start_polling(self, bot, **kwargs):
                events.append(("polling", kwargs["close_bot_session"]))
                for handler in harness.handlers:
                    task = asyncio.create_task(handler())
                    self._handle_update_tasks.add(task)
                    task.add_done_callback(self._handle_update_tasks.discard)
                await asyncio.sleep(0.01)   # SIGTERM arrives while they're still running
                events.append("polling stopped")

        class FakeDatabase:
            @staticmethod
            async def open(path):
                db = await Database.open(":memory:")
                close = db.close

                async def closing():
                    events.append("db closed")
                    await close()

                db.close = closing
                harness.db = db
                return db

        monkeypatch.setattr(main, "Bot", FakeBot)
        monkeypatch.setattr(main, "Database", FakeDatabase)
        monkeypatch.setattr(main, "TelegramGateway", lambda bot, channel_id: self.tg)
        monkeypatch.setattr(main, "build_dispatcher", lambda svc: FakeDispatcher())
        monkeypatch.setattr(main, "build_jobs", lambda svc: list(self.jobs))

    async def run(self, **config):
        config = {"STRIPE_API_KEY": "", "STRIPE_PRICE_ID": "", **config}
        await asyncio.wait_for(main.run(make_config(**config)), 5)


def mid_run_job(events, seconds=0.05):
    """A job that is part way through a run when polling stops."""
    async def run():
        await asyncio.sleep(seconds)
        events.append("job finished")

    return Job("mid_run", 3600, run)


async def test_main_shuts_down_in_order_and_alerts_on_startup(monkeypatch):
    """Polling stops (aiogram does that on SIGTERM), then the job that's mid-run
    finishes, then the bot session closes, then the DB."""
    harness = Harness(monkeypatch)
    harness.jobs = [mid_run_job(harness.events)]
    await harness.run()
    # aiogram must not close the session itself: the job may still be sending DMs.
    assert harness.events == [("polling", False), "polling stopped", "job finished", "session closed",
                              "db closed"]
    [alert] = harness.tg.sent_to(OWNER)
    assert "🟢 <b>First In member bot started</b>" in alert
    assert "Stripe: not configured" in alert and "Removals: dry run" in alert
    assert "the bot is not an admin of the paid channel" in alert
    assert "price" not in alert   # no Stripe, so no price to warn about


async def test_updates_still_being_handled_finish_before_the_db_closes(monkeypatch):
    """Telegram already counts an update as delivered once polling has moved past it,
    so a handler cut off at shutdown (a join request, say) is never seen again."""
    async def join_request():
        await asyncio.sleep(0.05)   # e.g. a Stripe call
        async with harness.db.tx() as conn:   # still open: this would fail if it were closed
            await kv_set(conn, "handled", "yes")
        harness.events.append("handler finished")

    harness = Harness(monkeypatch)
    harness.handlers = [join_request]
    await harness.run()
    assert harness.events == [("polling", False), "polling stopped", "handler finished", "session closed",
                              "db closed"]


async def test_handlers_and_jobs_are_waited_for_side_by_side(monkeypatch, caplog):
    """A stuck handler is cancelled after HANDLER_DRAIN_TIMEOUT while the scheduler
    is still waiting for its job: the waits overlap, so the total stays inside
    systemd's stop timeout."""
    async def stuck_handler():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            harness.events.append("handler cancelled")
            raise

    monkeypatch.setattr(main, "HANDLER_DRAIN_TIMEOUT", 0.05)
    harness = Harness(monkeypatch)
    harness.handlers = [stuck_handler]
    harness.jobs = [mid_run_job(harness.events, seconds=0.3)]
    with caplog.at_level(logging.WARNING):
        await harness.run()
    # Waiting one after the other, the job would finish first.
    assert harness.events[2:] == ["handler cancelled", "job finished", "session closed", "db closed"]
    assert "1 update(s) didn't finish within 0.05s; cancelling them" in caplog.text


def test_drain_is_a_no_op_for_a_dispatcher_without_the_task_set():
    # The set is private to aiogram; if a later version drops it, shutdown still works.
    asyncio.run(main.drain_updates(SimpleNamespace(), 1))


# --- the price ------------------------------------------------------------------------------

class NoPriceStripe(FakeStripe):
    def __init__(self):
        super().__init__()
        self.price_calls = 0

    async def price_label(self, price_id):
        self.price_calls += 1
        return None


async def test_startup_retries_the_price_then_runs_without_it(monkeypatch, caplog):
    made = []

    def gateway(api_key):
        made.append(NoPriceStripe())
        return made[-1]

    monkeypatch.setattr(main, "StripeGateway", gateway)
    monkeypatch.setattr(billing, "PRICE_RETRY_DELAYS", (0.0, 0.0))   # no real pauses in tests
    with caplog.at_level(logging.WARNING):
        stripe_gw, label = await main.build_stripe(make_config())
    assert stripe_gw is made[0] and label is None and made[0].price_calls == 1 + 2
    assert "trial and subscribe checkouts are held back" in caplog.text


async def test_startup_alert_warns_when_the_price_is_unknown(db, tg, stripe_fake):
    svc = make_svc(db, tg, stripe_fake)
    assert f"Stripe: TEST mode, {PRICE}" in main.startup_text(svc, [])
    svc.price.label = None
    text = main.startup_text(svc, [])
    assert "Stripe: TEST mode" in text and "⚠️ Couldn't read the price from Stripe" in text


@pytest.mark.parametrize("key, price", [("", "price_123"), ("rk_test_" + "x" * 20, "")])
async def test_stripe_not_configured_still_works(key, price):
    assert await main.build_stripe(make_config(STRIPE_API_KEY=key, STRIPE_PRICE_ID=price)) == (None, None)
