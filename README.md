# First In Exeter – member bot

Telegram bot that sells access to the paid First In Exeter alerts channel.
It does not send ticket alerts (that's the separate `fixr-bot`). Full spec: `PING_MEMBER_BOT_SPEC.md` (`CLAUDE.md` overrides it).

**Status: phases 3 and 4 built on branch `tom/phase-3-4` (on top of `trial-polish`), not merged and not deployed yet.** 715 unit tests pass. Waiting for the owner's checkpoint tests below. Deploying to the Google Cloud VM is in [DEPLOY.md](DEPLOY.md).

- **Free trial via Stripe, card required.** Owner decision 2026-10-18 (confirmed 2026-10-19): "Start my free 2 weeks" opens a Stripe Checkout with a 14-day subscription trial. Stripe charges the price automatically when the trial ends unless the person cancels with `/manage`. Before the Stripe link the bot shows the price and interval, the trial length, the date of the first charge, that a card is required, and that they can cancel before then with `/manage`. The Checkout is created with a fixed `trial_end` (creation time + 14 days), so that date is exactly the date Stripe charges, even if the student finishes checkout later (they just get a slightly shorter trial). Sending the same open link again shows the same date. The same facts are in "How it works" and `/terms`. There is no bot-side trial.
- Cancelling during the trial keeps access until the trial would have ended, and nothing is ever charged.
- Payment detection by polling Stripe (no webhooks): open checkouts every 30 s, a full subscription sync every 5 min, plus an **I've paid** button.
- Paid channel via join requests: entitled people are approved, everyone else is declined with a DM. Membership is tracked from `chat_member` updates.
- Two reminders before the first charge, each sent once and never to someone who has cancelled: one `TRIAL_REMINDER_DAYS_BEFORE` days ahead (default 3) and a last-day one about 3 hours ahead. Both show the date, the price and `/manage`.
- A daily disputes check: a new dispute sets the user's status to the local `disputed` (no access, no new checkout) and DMs the owner once. It never bans anyone. When every dispute of a user is won, the status is restored from Stripe.
- Removal sweep every 10 min, **dry run only** (`REMOVALS_ENABLED=false`): nobody is removed; the owner gets a "Would remove: ..." DM whenever that list changes. If one sweep would remove more than `MAX_REMOVALS_PER_SWEEP` people (or over 20% of members), it removes nobody and asks the owner to `/confirmsweep` within an hour.
- **Owner alerts** (a DM to the first `ADMIN_IDS`, at most one per kind of problem every 30 minutes, kept across restarts): the bot started (with Stripe mode, removals mode and channel-rights problems), a job failed (the Stripe sync and checkout polling only after 3 failures in a row), and a "working again" DM once it recovers. Also: a new payment dispute (once per dispute), duplicate live subscriptions (at once, then daily), a subscription that vanished from Stripe, and a mass-removal cap hit (in the sweep report).
- **Daily report at 09:00 UK time**: the `/stats` numbers, "✅ All jobs healthy" or which jobs failed since the last report, how fresh the Stripe data is, the last backup, the pause, and a sweep waiting for `/confirmsweep`. `/report` shows it any time.
- **Nightly backup at 03:30 UK time** with SQLite's online backup, to `backups/first-in-YYYY-MM-DD.db` (never overwriting: a second one that day is `-2`), keeping the newest 14 (`BACKUP_KEEP`). A failing backup is retried every 30 minutes and alerts the owner.
- **One copy only**: a lock file next to the database (`data/first-in-member-bot.lock`). A second copy on the same machine exits straight away with "Another copy ... is already running" (exit code 3). A copy from before the lock existed (phase 2) doesn't take it, so the bot and the check also look at the process list and refuse to start next to one, naming its PID. A copy on another machine can't be seen, so stop the Mac copy before starting the VM one.
- **Graceful stop** (Ctrl+C or systemd's SIGTERM): polling stops, a job or update mid-way gets up to 25 s to finish, then the bot session and the database close.
- If the price can't be read from Stripe at startup, the bot retries, then checks every 5 minutes. Until it knows the price it won't send a Stripe link (the CLAUDE.md rule: price first), and the startup alert and daily report say so.
- User commands: `/start`, `/subscribe`, `/manage`, `/status`, `/help`, `/terms`.
- Admin commands (only for `ADMIN_IDS`, silent for everyone else; `/help` lists them for admins): `/stats`, `/report` (the daily report now), `/user <id or @username>`, `/grant <id or @username> <days>` (0 clears), `/dryrun`, `/revoke <id or @username>` (clears a grant, then removes them unless something else entitles them), `/ban <id or @username>` (flags banned and removes them permanently) and `/unban <id or @username>` (clears the flag and lifts the Telegram ban; it doesn't give access). `/revoke` and `/ban` only remove people from the channel when `REMOVALS_ENABLED=true`, and never touch an admin; with it `false` they change the database and say they didn't remove anyone. `/ban` warns if the person still has a live Stripe subscription, so you can cancel it in the dashboard. A `@username` that matches more than one person is refused, with their IDs, so use the numeric ID. `/pause` holds every removal and reminder (joining, paying and the Stripe sync carry on) until `/resume`, and survives restarts. `/confirmsweep` approves exactly the removals a capped sweep report listed, within an hour, in the same mode (dry run or not); everyone still goes through every other check.
- `python -m first_in_bot check`: a preflight check that changes nothing. It covers the config, the database (read-only: it never runs migrations), the token, the paid-channel rights, the Stripe key, the price and what the key may read, removals and the backups folder, as a PASS / WARN / FAIL list. `setup.sh` runs it before starting the service.

Not built (phase 5 and later, owner decisions): live Stripe keys, `REMOVALS_ENABLED=true`, the alert bot's change to post into the new channels.

## Run locally (Mac)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env      # then fill in .env yourself
.venv/bin/python -m first_in_bot check   # optional: PASS/WARN/FAIL list, starts nothing
.venv/bin/python -m first_in_bot
.venv/bin/python -m pytest
```

`requirements.txt` pins `stripe` to major version 16 (tested with Stripe API version `2026-09-30.endive`) and `aiogram` to 3.31+. Re-run the tests and the checkpoint below before raising either major version.

If `.env` has a missing or invalid value the bot exits with a list of everything that's wrong.
Secrets are never printed or logged. `.env` is git-ignored.

At startup the log shows, in capitals, whether Stripe is in TEST mode or not configured, and it warns if the bot isn't an admin of the paid channel with the rights it needs.
Without `STRIPE_API_KEY` and `STRIPE_PRICE_ID` the bot still runs, and the trial/subscribe buttons say payments aren't set up yet.

## `.env` values

| Key | What |
|---|---|
| `BOT_TOKEN` | token of the **new** First In bot from @BotFather (not the alert bot's) |
| `BOT_USERNAME` | that bot's username, no `@` |
| `ADMIN_IDS` | your Telegram user ID first (comma-separate more) |
| `PAID_CHANNEL_ID` | paid channel ID, starts `-100` |
| `PAID_JOIN_REQUEST_LINK` | paid channel invite link with join requests on |
| `FREE_CHANNEL_LINK` | free channel link (`https://t.me/...`) |
| `STRIPE_API_KEY` | restricted **test** key, `rk_test_...` |
| `STRIPE_PRICE_ID` | the recurring `price_...` ID; the amount shown in messages is read from Stripe |
| `SUPPORT_CONTACT` | optional, shown in `/terms`, in the `/manage` error and in the goodbye for a disputed payment |
| `TRIAL_REMINDER_DAYS_BEFORE` | optional, days before the first charge for the first reminder (default 3) |
| `STRIPE_TEST_CLOCK_KEY` | optional, only for `scripts/stripe_test_clock.py` (see below) |
| `BACKUP_DIR` | optional, where the nightly backups go (default `backups`) |
| `BACKUP_KEEP` | optional, how many nightly backups to keep (default 14) |

Everything else has a default. Only test-mode Stripe keys are accepted. `REMOVALS_ENABLED` stays `false`.
`TRIAL_DAYS` must be at least 3, because Stripe Checkout only accepts a fixed trial end 48 hours or more ahead. For the whole cycle in minutes, use the test-clock script below.

## How to test (phase 3 checkpoint)

Use a test bot, a test paid channel and a second Telegram account. Everything is Stripe **test mode**.

Upgrading the phase-2 copy on the Mac: the first start runs database migrations 4 to 6, and the phase-2 code can't use the database after that. So first:

- `chmod 600 .env`
- `.venv/bin/python -m first_in_bot check`: "Running copy" FAILs with the old copy's PID while it still runs. That's expected.
- Stop the old copy (Ctrl+C in its Terminal window, or `kill <PID>`), then keep a copy of the database: `sqlite3 data/first-in.db ".backup data/before-phase-3-4.db"`. To go back to phase 2: stop the bot, delete `data/first-in.db-wal` and `data/first-in.db-shm` if they're there, put that file back as `data/first-in.db`, and check out `main` (the phase-2 code).

1. `.venv/bin/python -m pytest` passes. Then start the bot with `.venv/bin/python -m first_in_bot` and check the startup log: Stripe **TEST MODE**, no "Paid channel setup" warnings.
2. Second account: open `t.me/<bot>?start=tt_test`. The welcome says the first 14 days are free, then the price, that a card is required, and that you can cancel before the first payment with `/manage`. **How it works** and `/terms` show the same facts plus the first-charge date. Nothing suggests a card isn't needed.
3. Before paying, request to join with `PAID_JOIN_REQUEST_LINK`: the request is declined and the bot DMs a Start trial button.
4. Tap **Start my free 2 weeks**. The bot first shows: 14 days free, then the price, a card is required, the first payment date (14 days from now, and the Stripe checkout page's trial end matches it), and how to cancel. The date is the one baked into the Stripe link. Then open the Stripe link and pay with test card `4242 4242 4242 4242` (any future date, any CVC). Within about 30 s (or tap **I've added my card**) the bot says your trial is on until a date, with a **Join the alerts channel** button.
5. Request to join again: approved. `/status` shows the trial end date and the price that follows.
6. `/manage` opens the Stripe portal (if it says it isn't switched on, activate the Customer Portal, see the checklist). Cancel there. Within 5 min `/status` says you've cancelled and won't be charged, and you **still have access until the trial end date**.
7. Reminders: with `TRIAL_DAYS=3` and `TRIAL_REMINDER_DAYS_BEFORE=3`, start a trial with a fresh account. The first reminder arrives within about 5 minutes (date, price, `/manage`). The last-day one comes about 3 hours before the end, 3 days later. A user who cancelled gets neither. (Step 13 shows the whole timeline at once.)
8. Fixed date: with a fresh account tap **Start my free 2 weeks** but don't open the link. Tap it again later the same day (within 24 hours): the same link comes back with the **same** first-payment date, not a later one. Finish checkout the next day and the trial still ends on that date.
9. Press **Start my free 2 weeks** again on an old message after the trial: it says you've already had your free trial and offers a normal subscription.
10. Admin (as the owner): `/stats`, `/user @yourtestaccount`, `/grant @yourtestaccount 1`, `/revoke @yourtestaccount`, `/dryrun`. From the second account, none of them reply.
11. `/ban @yourtestaccount`: with `REMOVALS_ENABLED=false` it says it did not remove them, and their join request is declined from then on. `/unban @yourtestaccount` clears it. (To see real removal, set `REMOVALS_ENABLED=true` on a **test channel** only, then `/ban` kicks them for good and `/revoke` kicks them if nothing else entitles them. Set it back to `false` afterwards.)
12. Disputes: in the Stripe test dashboard create a payment with test card `4000 0000 0000 0259` (it ends in a dispute) for a test customer the bot knows, restart the bot (the check runs 2 minutes after start, then daily), and the owner gets a "Payment dispute" DM once. `/status` for that user says access is on hold, `/subscribe` refuses, and the 5-minute sync doesn't undo it. Winning the dispute in the dashboard restores them on the next daily check.
13. **Test clocks** (the whole life cycle in a few minutes, no Telegram needed). Put `STRIPE_TEST_CLOCK_KEY` (a test key with Test clocks write, Customers write, Payment methods write, Subscriptions write and **Invoices write**) in `.env`, then run:

   ```bash
   .venv/bin/python scripts/stripe_test_clock.py                      # all nine scenarios
   .venv/bin/python scripts/stripe_test_clock.py --scenario trial_failed
   ```

   It prints PASS/FAIL for: trial start and reminder timing, trial end with a successful charge (becomes active), trial end with a failed payment (past due, access only for the grace period), cancelling during the trial (at period end and immediately: access until the trial would have ended, never charged), a renewal a month later (a second charge), a failed renewal (past due, access only for the grace period, the "subscription ended" goodbye), cancelling after paying (access until the paid month ends, one charge only) and a failed first payment that's then paid with a new card (active again). It has only been run against fakes so far, so its first real run is part of this checkpoint. It deletes its test clocks afterwards unless you add `--keep`. Run it after any Stripe change, and set the Customer Portal to cancel **at the end of the billing period**, not immediately, so the bot's behaviour matches what you tested.

## How to test (phase 4 checkpoint)

On the Mac first, with the test bot and test channel, then on the VM. Everything stays Stripe test mode and `REMOVALS_ENABLED=false`.

1. `.venv/bin/python -m pytest` passes. `.venv/bin/python -m first_in_bot check` prints only PASS lines (a WARN for a missing `backups` folder is fine; the bot makes it).
2. Start the bot. You get **🟢 First In member bot started** with Stripe TEST mode, "Removals: dry run" and "Paid channel rights: OK". Stop it with Ctrl+C: the log ends with "Scheduler stopped" and no traceback. Start it again within 30 minutes: no second startup DM (rate limit).
3. While it runs, start a second copy in another Terminal window: it exits at once with "Another copy of the First In member bot is already running".
4. `/report`: the daily report now (stats, "✅ All jobs healthy", the last Stripe sync, "Last backup", modes). The real one arrives at 09:00 the next morning, once.
5. Backups: within about a minute of starting (any time after 03:30), `ls -l backups` shows `first-in-<today>.db`, readable only by you (`-rw-------`). `sqlite3 backups/first-in-<today>.db "select count(*) from users"` matches `/stats`.
6. `/pause`: it says what's held (removals, reminders) and what keeps working. Then `/dryrun` shows "⏸ Paused", a fresh test account can still start a trial and gets approved into the channel, and no reminder arrives. `/report` shows the pause. Restart the bot: still paused. `/resume` says how long it was paused.
7. `/confirmsweep` with nothing held back says nothing is waiting. To see the cap you need two test accounts besides your own (admins are never removed) that have only pressed `/start`. Set `MAX_REMOVALS_PER_SWEEP=1` and restart. `/grant <id> 1` each one, let them join the channel, then `/grant <id> 0` both. Within 10 minutes the sweep report says the cap held 2 removals back and to reply `/confirmsweep` within an hour. `/confirmsweep` answers "Dry run: with the cap lifted I would remove 2: ... Nobody was removed because REMOVALS_ENABLED=false." Set `MAX_REMOVALS_PER_SWEEP` back to 5.
8. Job alerts: in `.env` put a `#` in front of the `STRIPE_API_KEY=` line (keep it, it's your real key) and add the line `STRIPE_API_KEY=rk_test_wrong` under it, then restart. The startup DM warns that the price couldn't be read, `/subscribe` says payments are briefly unavailable, and after about 15 minutes you get "Job sync_subscriptions failed 3 times in a row" (removing Stripe customers is on hold). Delete the wrong line, remove the `#`, restart: "✅ sync_subscriptions is working again", and the next `/report` lists the failures.
9. `/ban <id>` of a test account with a live trial: it warns that their Stripe subscription is still running (cancel it in the dashboard), and with removals off it doesn't kick. `/unban <id>` afterwards. A `@username` two users have had is refused with both IDs (the unit tests cover this, since it needs a username to change hands).
10. On the VM, follow [DEPLOY.md](DEPLOY.md): stop the Mac copy, clone, `bash setup.sh`. The check passes and the service starts. `sudo systemctl restart first-in-member-bot` brings it back, and so does killing it (`sudo pkill -9 -f first_in_bot`, back within 10 s). Download one backup to the Mac as DEPLOY.md shows.
11. Leave it running with dry-run removals for a few days. Each morning, read the report and any "Would remove" DMs, and check every name with `/user`. Nothing gets real until you set `REMOVALS_ENABLED=true` yourself (phase 5).

## Setup checklists

### BotFather
- [ ] Create a new bot for the member bot (separate from the alert bot). Name it First In Exeter.

### Paid channel
- [ ] Channel is private.
- [ ] Member bot is admin with **Invite users via link** and **Ban users** only.
- [ ] Alert bot is admin with **Post messages**.
- [ ] **Restrict saving content** is on.
- [ ] Invite link created with **Request admin approval** on; its URL goes in `PAID_JOIN_REQUEST_LINK`.

People who were already in the channel before the bot was running aren't in its database until they join, leave or press `/start`, so the sweep can't see them.

### Stripe dashboard (test mode)
- [ ] Business name, statement descriptor and support email set, so your personal name isn't shown.
- [ ] Product with one recurring monthly Price created; the `price_...` ID goes in `STRIPE_PRICE_ID`.
- [ ] **Customer Portal activated**: Settings > Billing > Customer portal, with **cancellations allowed**. `/manage` fails until this is done. The bot shows a clear error if the portal isn't configured.
- [ ] Restricted API key created (Checkout Sessions write, Customers write, Subscriptions read, Customer Portal write, Disputes read, **Charges read**, and read access to prices), test mode. Charges read is needed because the daily disputes check expands each dispute's charge to find the customer.
- [ ] Customer Portal cancellation set to **at the end of the billing period** (the bot also copes with immediate cancellation during a trial: access stays until the trial would have ended).
- [ ] Optional second key for `scripts/stripe_test_clock.py`, in `STRIPE_TEST_CLOCK_KEY` (test clocks write, customers write, payment methods write, subscriptions write, invoices write).
- [ ] **Before go-live:** turn on Stripe's **Send a reminder email 7 days before a free trial ends** (Settings > Billing > Subscriptions and emails, customer emails). Card-network rules for trials (e.g. Mastercard's) ask for a reminder 3 to 7 days before the trial ends. The bot's first reminder is `TRIAL_REMINDER_DAYS_BEFORE` days before (default 3), but the DM never reaches people who blocked the bot, so Stripe's email is still worth having. TO CHECK: UK rules on reminders and cancellation for auto-converting free trials before going live.
- [ ] Check the final `/terms` wording against UK consumer rules (subscription trials, cancellation and refunds) before go-live. The current text is a plain-English draft.
