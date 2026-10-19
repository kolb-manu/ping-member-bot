# First In Exeter – member bot

Telegram bot that sells access to the paid First In Exeter alerts channel.
It does not send ticket alerts (that's the separate `fixr-bot`). Full spec: `PING_MEMBER_BOT_SPEC.md` (`CLAUDE.md` overrides it).

**Status: phase 3 (Stripe in test mode) built on branch `trial-polish`, not merged.** Unit-tested; waiting for the owner's checkpoint test.

- **Free trial via Stripe, card required.** Owner decision 2026-10-18 (confirmed 2026-10-19): "Start my free 2 weeks" opens a Stripe Checkout with a 14-day subscription trial. Stripe charges the price automatically when the trial ends unless the person cancels with `/manage`. Before the Stripe link the bot shows the price and interval, the trial length, the date of the first charge, that a card is required, and that they can cancel before then with `/manage`. The same facts are in "How it works" and `/terms`. There is no bot-side trial.
- Cancelling during the trial keeps access until the trial would have ended, and nothing is ever charged.
- Payment detection by polling Stripe (no webhooks): open checkouts every 30 s, a full subscription sync every 5 min, plus an **I've paid** button.
- Paid channel via join requests: entitled people are approved, everyone else is declined with a DM. Membership is tracked from `chat_member` updates.
- Two reminders before the first charge, each sent once and never to someone who has cancelled: one `TRIAL_REMINDER_DAYS_BEFORE` days ahead (default 3) and a last-day one about 3 hours ahead. Both show the date, the price and `/manage`.
- A daily disputes check: a new dispute sets the user's status to the local `disputed` (no access, no new checkout) and DMs the owner once. It never bans anyone. When every dispute of a user is won, the status is restored from Stripe.
- Removal sweep every 10 min, **dry run only** (`REMOVALS_ENABLED=false`): nobody is removed; the owner gets a "Would remove: ..." DM whenever that list changes.
- User commands: `/start`, `/subscribe`, `/manage`, `/status`, `/help`, `/terms`.
- Admin commands (only for `ADMIN_IDS`, silent for everyone else): `/stats`, `/user <id or @username>`, `/grant <id or @username> <days>` (0 clears), `/dryrun`, `/revoke <id or @username>` (clears a grant, then removes them unless something else entitles them), `/ban <id or @username>` (flags banned and removes them permanently) and `/unban <id or @username>` (clears the flag and lifts the Telegram ban; it doesn't give access). `/revoke` and `/ban` only remove people from the channel when `REMOVALS_ENABLED=true`, and never touch an admin; with it `false` they change the database and say they didn't remove anyone.

Not built yet (phase 4): daily report, backups, owner alerts, single-instance lock, systemd setup, `/pause` `/resume` `/confirmsweep`.

## Run locally (Mac)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env      # then fill in .env yourself
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
| `SUPPORT_CONTACT` | optional, shown in `/terms` and in the `/manage` error |

| `TRIAL_REMINDER_DAYS_BEFORE` | optional, days before the first charge for the first reminder (default 3) |
| `STRIPE_TEST_CLOCK_KEY` | optional, only for `scripts/stripe_test_clock.py` (see below) |

Everything else has a default. Only test-mode Stripe keys are accepted. `REMOVALS_ENABLED` stays `false`.
`TRIAL_DAYS=1` (Stripe's minimum) is handy for seeing the reminder and the conversion quickly. For the whole cycle in minutes, use the test-clock script below.

## How to test (phase 3 checkpoint)

Use a test bot, a test paid channel and a second Telegram account. Everything is Stripe **test mode**.

1. `.venv/bin/python -m pytest` passes. Then start the bot with `.venv/bin/python -m first_in_bot` and check the startup log: Stripe **TEST MODE**, no "Paid channel setup" warnings.
2. Second account: open `t.me/<bot>?start=tt_test`. The welcome says the first 14 days are free, then the price, that a card is required, and that you can cancel before the first payment with `/manage`. **How it works** and `/terms` show the same facts plus the first-charge date. Nothing suggests a card isn't needed.
3. Before paying, request to join with `PAID_JOIN_REQUEST_LINK`: the request is declined and the bot DMs a Start trial button.
4. Tap **Start my free 2 weeks**. The bot first shows: 14 days free, then the price, a card is required, the first payment date (14 days from today), and how to cancel. Then open the Stripe link and pay with test card `4242 4242 4242 4242` (any future date, any CVC). Within about 30 s (or tap **I've added my card**) the bot says your trial is on until a date, with a **Join the alerts channel** button.
5. Request to join again: approved. `/status` shows the trial end date and the price that follows.
6. `/manage` opens the Stripe portal (if it says it isn't switched on, activate the Customer Portal, see the checklist). Cancel there. Within 5 min `/status` says you've cancelled and won't be charged, and you **still have access until the trial end date**.
7. Reminders: with `TRIAL_DAYS=1` and `TRIAL_REMINDER_DAYS_BEFORE=1`, start a trial with a fresh account. The first reminder arrives within about 5 minutes (date, price, `/manage`). The last-day one comes about 3 hours before the end. A user who cancelled gets neither.
8. Press **Start my free 2 weeks** again on an old message after the trial: it says you've already had your free trial and offers a normal subscription.
9. Admin (as the owner): `/stats`, `/user @yourtestaccount`, `/grant @yourtestaccount 1`, `/revoke @yourtestaccount`, `/dryrun`. From the second account, none of them reply.
10. `/ban @yourtestaccount`: with `REMOVALS_ENABLED=false` it says it did not remove them, and their join request is declined from then on. `/unban @yourtestaccount` clears it. (To see real removal, set `REMOVALS_ENABLED=true` on a **test channel** only, then `/ban` kicks them for good and `/revoke` kicks them if nothing else entitles them. Set it back to `false` afterwards.)
11. Disputes: in the Stripe test dashboard create a payment with test card `4000 0000 0000 0259` (it ends in a dispute) for a test customer the bot knows, restart the bot (the check runs 2 minutes after start, then daily), and the owner gets a "Payment dispute" DM once. `/status` for that user says access is on hold, `/subscribe` refuses, and the 5-minute sync doesn't undo it. Winning the dispute in the dashboard restores them on the next daily check.
12. **Test clocks** (the whole life cycle in a minute, no Telegram needed). Put `STRIPE_TEST_CLOCK_KEY` (a test key that may write test clocks, customers, payment methods and subscriptions, and read invoices) in `.env`, then run:

   ```bash
   .venv/bin/python scripts/stripe_test_clock.py                      # all five scenarios
   .venv/bin/python scripts/stripe_test_clock.py --scenario trial_failed
   ```

   It prints PASS/FAIL for: trial start and reminder timing, trial end with a successful charge (becomes active), trial end with a failed payment (past due, access only for the grace period), and cancelling during the trial (at period end and immediately: access until the trial would have ended, never charged). It deletes its test clocks afterwards unless you add `--keep`. Run it after any Stripe change, and set the Customer Portal to cancel **at the end of the billing period**, not immediately, so the bot's behaviour matches what you tested.

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
- [ ] Restricted API key created (Checkout Sessions write, Customers write, Subscriptions read, Customer Portal write, Disputes read, and read access to prices), test mode. Expanding a dispute's charge may also need **Charges read**: if the log says `disputes.list failed` with a permissions error, add it.
- [ ] Customer Portal cancellation set to **at the end of the billing period** (the bot also copes with immediate cancellation during a trial: access stays until the trial would have ended).
- [ ] Optional second key for `scripts/stripe_test_clock.py`, in `STRIPE_TEST_CLOCK_KEY` (test clocks write, customers write, payment methods write, subscriptions write, invoices read).
- [ ] **Before go-live:** turn on Stripe's **Send a reminder email 7 days before a free trial ends** (Settings > Billing > Subscriptions and emails, customer emails). Card-network rules for trials (e.g. Mastercard's) ask for a reminder 3 to 7 days before the trial ends. The bot's first reminder is `TRIAL_REMINDER_DAYS_BEFORE` days before (default 3), but the DM never reaches people who blocked the bot, so Stripe's email is still worth having. TO CHECK: UK rules on reminders and cancellation for auto-converting free trials before going live.
- [ ] Check the final `/terms` wording against UK consumer rules (subscription trials, cancellation and refunds) before go-live. The current text is a plain-English draft.
