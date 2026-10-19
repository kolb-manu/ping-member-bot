# First In: project plan

> **Planning document, last updated 2026-10-19.** Things change, so check with the owner before relying on a decision.
>
> Anything marked **TO CHECK** is unverified. Don't treat it as fact.

## Contents

1. [What First In is](#1-what-first-in-is)
2. [The problem and the market](#2-the-problem-and-the-market)
3. [How it works (architecture)](#3-how-it-works-architecture)
4. [The customer journey](#4-the-customer-journey)
5. [Business model](#5-business-model)
6. [Marketing plan](#6-marketing-plan)
7. [Risks and things to check](#7-risks-and-things-to-check)
8. [Build status and roadmap](#8-build-status-and-roadmap)
9. [How to work on this repo](#9-how-to-work-on-this-repo)
10. [Open questions and who does what](#10-open-questions-and-who-does-what)

---

## 1. What First In is

First In is a Telegram alert service for Exeter students. It tells them the second tickets drop on FIXR for the city's popular student nights, so they can buy at face value before the tickets sell out. It starts with Timepiece ("TP") and also covers other venues such as Fever and Cavern.

**Brand**

| Item | Decision |
|---|---|
| Name | "First In". "Exeter" is dropped when we expand to other cities. Today the Exeter service is "First In Exeter". |
| Logo | A ticket with a "1" |
| Look | Premium and elegant |
| Exeter palette | Forest green, brass and ivory |
| Default palette | Neutral black and white, for other uses |
| Other universities | Each future university gets its own palette. Everything else stays the same. |

Brand image files are kept by the owner locally and are not in this repo.

## 2. The problem and the market

**The problem**

- TP Wednesday tickets and similar sell out in seconds to minutes.
- FIXR often crashes at release time on big drops.
- Students who miss out end up paying well above face value to strangers in Facebook's "Overheard" group, and scams are reported there.
- TP now only accepts in-app tickets, so screenshot or PDF tickets obtained from other people get turned away at the door.

**Existing competitor: Exeter Notify** (Telegram alerts, has a website). It claims to cover every night. We found no free trial. Public pricing seen in October 2026:

| Plan | Price |
|---|---|
| TP Wednesday alerts, monthly | £4.99 |
| All TP alerts, monthly | £6.99 |
| TP Wednesday, 3 months | £8.99 |
| All TP, 3 months | £14.99 |

**Our edge**

- A free 14-day trial.
- A free, delayed channel as a funnel.
- More venues beyond TP.
- A more premium brand.

**TO CHECK:** how fast their alerts really arrive compared with ours.

## 3. How it works (architecture)

### Two separate bots

They are deliberately separate, so one can't break the other.

| | Alert bot | Member bot |
|---|---|---|
| Project | Existing `fixr-bot` (separate repo) | This repo |
| Tech | Plain Python, systemd service | Python, aiogram, SQLite, Stripe |
| Job | Watches FIXR venue pages about every 45 seconds, detects new events or tickets going live, posts alerts | The front desk: welcomes people, starts trials, decides who may join the paid channel, takes payment, sends reminders, removes people when access ends |

The alert bot stays the only thing that detects drops.

### Three Telegram chats

| Chat | Type | Alerts arrive |
|---|---|---|
| Owner's existing friends group | Group | Instantly, as now. Never affected by any of this. |
| Paid channel | Private channel | Instantly |
| Free channel | Public channel | About 10 minutes later |

### Alert bot change (not done yet)

A separate task. It adds a list of destinations, each with its own delay. It is off by default. A failure in the new channels must never delay or break the friends-group alert.

### Access control

- Everyone gets the same invite link, and the link uses **join requests**.
- The member bot approves or declines each request depending on whether the person is entitled (trial, paid subscription or manual grant). Sharing the link doesn't let anyone in.
- The channel is private with "restrict saving content" on.
- Entitlement is **one pure function** (`is_entitled`) used everywhere. Stripe status is the source of truth for payment.

### Payments

- Stripe Checkout in subscription mode, plus the Stripe Customer Portal for cancelling and updating a card.
- **No webhooks in v1.** The bot polls Stripe instead. That means no public URL, and it moves to a Raspberry Pi easily.
- **Test mode only** until the owner decides to go live.
- The Customer Portal has to be activated in the Stripe dashboard first (Settings > Billing > Customer portal, with cancellations allowed). `/manage` shows a clear error if it isn't.

### Hosting

- Free Google Cloud e2-micro VM now, Raspberry Pi from December.
- Each bot is its own systemd service. The member bot's service is called `first-in-member-bot`.
- A bot token can only be used by one running copy at a time.

## 4. The customer journey

1. **Arrive.** A TikTok or Instagram bio link opens the member bot with a source tag, for example `?start=tt_v1`. This shows which video brought each person. Only the first real tag is kept.
2. **Welcome.** The bot sends a welcome message with a "Start my free 2 weeks" button.
3. **Trial.** The trial runs through Stripe (owner decision 2026-10-18): the button opens Stripe Checkout, the person adds a card, and the 14 days start when they finish checkout, not at `/start`. Nothing is charged during the trial; after it, the monthly price is charged automatically unless they cancel. One trial per person, enforced in the database.
4. **Reminders.** Before the Stripe link the bot states the price and interval, the trial length, the first-charge date, that a card is required, and that they can cancel before then with `/manage`. Later, one reminder `TRIAL_REMINDER_DAYS_BEFORE` days before the first charge (default 3) and a last-day one about 3 hours before, each with the date, the price and how to cancel. Each is sent once per trial and never to someone who cancelled.
5. **Trial ends.** If they kept the subscription, nothing changes and they're charged. If they cancelled (or the payment fails past the grace period), they're removed from the paid channel and pointed to the free channel.
6. **Paying.** People who've already had their trial get a Subscribe button instead, then Stripe Checkout. The bot polls Stripe every 30 seconds, sees the payment and lets them in.
7. **Managing.** `/manage` opens the Stripe portal. Cancelling keeps access until the end of the paid period. Failed payments get a short grace period.

**Commands:** `/start` `/subscribe` `/manage` `/status` `/help` `/terms`. Admin commands exist for the owner only.

## 5. Business model

**Decided**

- Free 14-day trial, then a monthly subscription.
- A free delayed channel stays as a permanent funnel.

**Open**

| Question | Notes |
|---|---|
| Price | TBD by the owner. The competitor charges £4.99 to £6.99 a month, so something in that range is the starting assumption. |
| Term pass | Whether to offer a cheaper 3-month price. It lowers the Stripe fee impact. |
| Founding-member discount | For early users. Not decided. |

**Stripe fees** are roughly 1.5% + 20p per UK card payment, so about 25-30p on a £4 payment. **TO CHECK** current rates.

**Account setup.** Payments go through a personal (sole trader) Stripe account with a brand name and statement descriptor, so the owner's personal name isn't shown to customers. Stripe still verifies real identity privately.

## 6. Marketing plan

**Main channel: TikTok**

- Lots of short, relatable Exeter-student videos, posted in the evenings and especially Monday and Tuesday before TP Wednesday.
- Aim for 20-30 videos over 2-3 weeks, then judge by **joins per video**, not views.
- Format: a long, painful-scenario line of text over a meme clip, with a small bracketed line at the bottom pointing to First In and the link in bio.
- The first batch is in [VIDEO_IDEAS.md](VIDEO_IDEAS.md).

**Also use:** uni group chats, society pages and Instagram Reels. Word of mouth is likely to beat TikTok.

**Rules**

- Messaging: we help students get tickets at face value before they sell out. Don't describe the service any other way.
- Don't show how the alerts are obtained.
- Use a separate account from any personal one.
- No paid ads until organic proves demand.
- Meme clips and sounds may get muted over time. That's accepted while we're small.

**Expansion**

- One city at a time.
- Only add another university once Exeter converts reliably and the extra city costs little time.
- Separate TikTok account and Telegram channel per city. The brand stays neutral ("First In") with the city as a sub-label.

## 7. Risks and things to check

This is not legal advice.

| Area | Risk | Action |
|---|---|---|
| FIXR terms | Polling venue pages is low-profile for personal use, but a paid service is more visible. We could be blocked or asked to stop. | Keep polling gentle. **TO CHECK** their terms. |
| Ticketing law and venue rules | Rules on ticket transfers and automated access are tightening. Our service only sends alerts and never buys tickets. | **TO CHECK** current UK rules before scaling. |
| Consumer law | Real payments need terms, a refund and cancellation policy, and clear price display. | The `/terms` text in the bot is a plain-English draft and must be reviewed before go-live. |
| Trial reminders | Card-network rules (e.g. Mastercard) may require a reminder 3 to 7 days before a card-required trial converts. The bot's first reminder is `TRIAL_REMINDER_DAYS_BEFORE` days before (default 3), plus a last-day one. | **TO CHECK** current rules. Turn on Stripe's 7-day trial reminder email before go-live (README checklist). Owner to decide whether the bot's reminder moves earlier. |
| Auto-converting free trials | A card-required free trial that turns into a paid subscription automatically is regulated more tightly than a plain subscription (clear upfront terms, reminders, easy cancellation). | **TO CHECK: UK rules on reminders and cancellation for auto-converting free trials before going live.** |
| Tax | Trading income above £1,000 a year needs registering with HMRC. | **TO CHECK** |
| Telegram dependency | If Telegram changes its rules or bans a channel, we lose the audience. | Plan a way to contact members later (for example collecting emails). |
| Data | The member database holds Telegram IDs and usernames. | Keep it private, backed up and out of Git. Never commit `.env` or `data/`. |

## 8. Build status and roadmap

**Done**

- Alert bot v1.0 is running.
- Member bot phase 1: config, database, entitlement function, `/start` with source tracking, welcome message. 62 tests passing.

**Phases left**

| Phase | What |
|---|---|
| 2 | Free trial via Stripe Checkout (test mode), payment polling, full subscription sync, `/manage`, channel access, the trial reminders, dry-run removals, admin commands. Merged. |
| 3 | Disputes check, test-clock script, `/revoke` `/ban` `/unban`, pre-checkout trial terms, configurable reminders. Built on branch `trial-polish`, waiting for the owner's checkpoint test. |
| 4 | Hardening, owner alerts, backups, deploy to the server |
| 5 | Go-live. The owner's decision only. |

Also still to do: the alert bot change that posts to the new channels.

**Safety rules built into the plan**

- Removals start in dry-run mode.
- Paying members are never removed if Stripe data is stale.
- A cap stops mass removals and asks the owner first.
- Nothing goes live without the owner saying so.

Each phase stops at a checkpoint for the owner to test.

## 9. How to work on this repo

**Setup (Mac)**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env     # then fill in .env yourself
.venv/bin/python -m pytest
.venv/bin/python -m first_in_bot
```

**What goes in `.env`** (never commit real values):

| Key | What it is |
|---|---|
| `BOT_TOKEN` | `<BOT_TOKEN>`: token of a test bot from BotFather. Not the alert bot's token. |
| `BOT_USERNAME` | `<BOT_USERNAME>`: that bot's username, no `@` |
| `ADMIN_IDS` | `<YOUR_TELEGRAM_USER_ID>`: comma-separated Telegram user IDs |
| `PAID_CHANNEL_ID` | `<TEST_CHANNEL_ID>`: ID of your test channel, starts `-100` |
| `PAID_JOIN_REQUEST_LINK` | `<TEST_CHANNEL_JOIN_REQUEST_LINK>`: invite link with join requests on |
| `FREE_CHANNEL_LINK` | `<FREE_CHANNEL_LINK>`: a `https://t.me/...` link |
| `REMOVALS_ENABLED` | Keep `false` |
| `STRIPE_API_KEY`, `STRIPE_PRICE_ID` | Needed from phase 2, because the free trial runs through Stripe Checkout: a restricted **test** key (`rk_test_...`) and a test recurring price. Test-mode values only. |

The other keys have defaults. See `.env.example` for the full list.

**Rules**

- Read `CLAUDE.md` for the standing rules.
- Never commit secrets.
- Use a separate test bot and test channel for development, never the real paid channel.
- Follow the branch and pull request workflow in `CONTRIBUTING.md`.
- The original build spec is the `*_SPEC.md` file in the repo root. Its file name carries an old working name, and `CLAUDE.md` overrides where they differ.
- Ask the owner before changing anything in the plan above.

## 10. Open questions and who does what

| Question | Owner |
|---|---|
| Price | TBD |
| Final terms text (needs review) | TBD |
| Other venues to cover | TBD |
| Which city next | TBD |
| Who films and edits videos | TBD |
| Who handles support messages | TBD |
