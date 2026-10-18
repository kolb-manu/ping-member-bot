# First In Exeter – member bot

Telegram bot that sells access to the paid First In Exeter alerts channel.
It does not send ticket alerts (that's the separate `fixr-bot`). Full spec: `PING_MEMBER_BOT_SPEC.md`.

**Status: phase 1 (skeleton).** Config, database, entitlement logic, `/start` with source tracking and the welcome message. The trial button, channel access and Stripe come in later phases.

## Run locally (Mac)

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env      # then fill in .env yourself
.venv/bin/python -m first_in_bot
.venv/bin/python -m pytest
```

If `.env` has a missing or invalid value the bot exits with a list of everything that's wrong.
Secrets are never printed or logged. `.env` is git-ignored.

## `.env` values needed for phase 1

| Key | What |
|---|---|
| `BOT_TOKEN` | token of the **new** First In bot from @BotFather (not the alert bot's) |
| `BOT_USERNAME` | that bot's username, no `@` |
| `ADMIN_IDS` | your Telegram user ID (comma-separate more later) |
| `PAID_CHANNEL_ID` | paid channel ID, starts `-100` |
| `PAID_JOIN_REQUEST_LINK` | paid channel invite link with join requests on |
| `FREE_CHANNEL_LINK` | free channel link (`https://t.me/...`) |

Everything else has a default. Stripe values can stay empty until phase 3. Only test-mode Stripe keys are accepted. `REMOVALS_ENABLED` stays `false`.

## Setup checklists

### BotFather
- [ ] Create a new bot for the member bot (separate from the alert bot). Name it First In Exeter.

### Paid channel
- [ ] Channel is private.
- [ ] Member bot is admin with **Invite users via link** and **Ban users** only.
- [ ] Alert bot is admin with **Post messages**.
- [ ] **Restrict saving content** is on.
- [ ] Invite link created with **Request admin approval** on; its URL goes in `PAID_JOIN_REQUEST_LINK`.

### Stripe dashboard (phase 3)
- [ ] Business name, statement descriptor and support email set, so your personal name isn't shown.
- [ ] Product with one recurring Price created; the `price_...` ID goes in `STRIPE_PRICE_ID`.
- [ ] **Customer Portal activated**: Settings > Billing > Customer portal, with **cancellations allowed**. `/manage` fails until this is done. The bot shows a clear error if the portal isn't configured.
- [ ] Restricted API key created (Checkout Sessions write, Customers write, Subscriptions read, Customer Portal write, Disputes read), test mode.
