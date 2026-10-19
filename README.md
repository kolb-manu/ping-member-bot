# First In Exeter – member bot

Telegram bot that sells access to the paid First In Exeter alerts channel.
It does not send ticket alerts (that's the separate `fixr-bot`). Full spec: `PING_MEMBER_BOT_SPEC.md` (`CLAUDE.md` overrides it).

**Status: phase 2 (trial, channel access, Stripe test mode).** Built and unit-tested, waiting for the owner's checkpoint test.

- **Free trial via Stripe.** Owner decision 2026-10-18: "Start my free 2 weeks" opens a Stripe Checkout with a 14-day subscription trial. A card is required, and Stripe charges the price automatically when the trial ends unless the person cancels with `/manage`. There is no bot-side, no-card trial any more.
- Payment detection by polling Stripe (no webhooks): open checkouts every 30 s, a full subscription sync every 5 min, plus an **I've paid** button.
- Paid channel via join requests: entitled people are approved, everyone else is declined with a DM. Membership is tracked from `chat_member` updates.
- A reminder 2 days before the trial converts (date, price, how to cancel).
- Removal sweep every 10 min, **dry run only** (`REMOVALS_ENABLED=false`): nobody is removed; the owner gets a "Would remove: ..." DM whenever that list changes.
- User commands: `/start`, `/subscribe`, `/manage`, `/status`, `/help`, `/terms`.
- Admin commands (only for `ADMIN_IDS`, silent for everyone else): `/stats`, `/user <id or @username>`, `/grant <id or @username> <days>` (0 clears), `/dryrun`.

Not built yet: the disputes check, daily report, backups, owner alerts, single-instance lock, systemd setup, `/revoke` `/ban` `/unban` `/pause` `/resume` `/confirmsweep`.

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

Everything else has a default. Only test-mode Stripe keys are accepted. `REMOVALS_ENABLED` stays `false`.
`TRIAL_DAYS=1` (Stripe's minimum) is handy for seeing the reminder and the conversion quickly.

## How to test phase 2 (checkpoint)

Use a test bot, a test paid channel and a second Telegram account.

1. `.venv/bin/python -m pytest` passes. Then start the bot with `.venv/bin/python -m first_in_bot` and check the startup log: Stripe **TEST MODE**, no "Paid channel setup" warnings.
2. Second account: open `t.me/<bot>?start=tt_test`. The welcome says the first 2 weeks are free, then the price, and shows **Start my free 2 weeks**.
3. Before paying, request to join with `PAID_JOIN_REQUEST_LINK`: the request is declined and the bot DMs a Start trial button.
4. Tap **Start my free 2 weeks**, open the Stripe link, and pay with test card `4242 4242 4242 4242` (any future date, any CVC). Within about 30 s (or tap **I've added my card**) the bot says your trial is on until a date, with a **Join the alerts channel** button.
5. Request to join again: approved. `/status` shows the trial end date and the price that follows.
6. `/manage` opens the Stripe portal (if it says it isn't switched on, activate the Customer Portal, see the checklist). Cancel there; within 5 min `/status` says you've cancelled and won't be charged.
7. As the owner: `/stats`, `/user @yourtestaccount`, `/grant @yourtestaccount 1`, `/grant @yourtestaccount 0`, `/dryrun`. From the second account, `/stats` gets no reply.
8. Dry run: while the second account is in the channel, cancel its test subscription **immediately** in the Stripe dashboard. After the next sync (5 min) and sweep (10 min) the owner gets "Would remove: ...", only once, and nobody is actually removed. `/dryrun` shows the same report on demand.
9. Trial reminder: with `TRIAL_DAYS=1`, start a new trial with a fresh account; the 2-day reminder arrives within about 5 minutes, with the date, the price and `/manage`.
10. Press **Start my free 2 weeks** again on an old message after the trial: it says you've already had your free trial and offers a normal subscription.

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
- [ ] Restricted API key created (Checkout Sessions write, Customers write, Subscriptions read, Customer Portal write, Disputes read, and read access to prices), test mode.
- [ ] **Before go-live:** turn on Stripe's **Send a reminder email 7 days before a free trial ends** (Settings > Billing > Subscriptions and emails, customer emails). Card-network rules for trials (e.g. Mastercard's) ask for a reminder 3 to 7 days before the trial ends, which the bot's 2-day DM doesn't meet, and the DM never reaches people who blocked the bot. The bot's DM is sent as well as Stripe's email, not instead of it. Check the current card-network and UK rules for trial reminders.
- [ ] Check the final `/terms` wording against UK consumer rules (subscription trials, cancellation and refunds) before go-live. The current text is a plain-English draft.
