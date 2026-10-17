# Ping Exeter – Member Bot Spec

**For Claude Code.** Build this in the `ping-member-bot` folder. Work in the phases at the bottom, stop at each checkpoint for the owner to test, and ask before making any decision this spec doesn't cover. Never switch Stripe to live mode or turn on real removals unless the owner explicitly says so.

---

## 1. What this bot is

A Telegram bot that sells access to the **paid Ping Exeter alerts channel**. It does not detect or send ticket alerts. That's the existing alert bot's job (the separate `fixr-bot` project), which posts into the channels.

The member bot handles:
- greeting people who arrive from TikTok/Instagram (`t.me/<bot>?start=<source>`)
- starting a one-time 14-day free trial
- letting entitled people into the paid channel and keeping everyone else out
- taking payment via Stripe subscriptions
- reminding trial users before the trial ends
- removing people whose trial or subscription has ended
- giving the owner admin tools and status reports

### The three Telegram chats

| Chat | Type | Who's in it | Who posts |
|---|---|---|---|
| Friends' group | existing group | owner + friends | alert bot (unchanged) |
| **Ping Exeter (paid)** | private **channel** | trial users + paying members | alert bot, instant |
| **Ping Exeter Free** | public **channel** | anyone | alert bot, delayed ~10 min |

This bot only manages membership of the **paid channel**. It links people to the free channel but doesn't manage it.

---

## 2. Fixed decisions (don't change without asking)

- **Language/stack:** Python 3.11+, `aiogram` 3.x (async), `aiosqlite` with SQLite in WAL mode, the official `stripe` Python library, `python-dotenv`. The existing alert bot is plain Python with `requests`, `config.env` and a systemd service. Keep the same style of deployment so the owner can manage both the same way.
- **Separate bot token.** This bot must use its **own BotFather bot**, not the alert bot's token. Only one process can call `getUpdates` per token, and the alert bot already uses it.
- **Telegram updates via long polling** (no Telegram webhook, no public URL).
- **Stripe without webhooks (v1).** The bot **polls Stripe** instead of receiving webhooks, so it needs no public URL, domain or tunnel, and it moves to the Raspberry Pi in December unchanged. (Webhooks can be added later as an upgrade, see §12.)
- **Payments:** Stripe Checkout in `subscription` mode, one recurring Price, set in the Stripe dashboard. The bot only stores the Price ID. The price itself is the owner's decision.
- **Hosting:** the same Google Cloud e2-micro VM as the alert bot, as a **separate systemd service** (`ping-member-bot`), later the Raspberry Pi.
- **All times are stored in UTC.** Show times to users in `Europe/London`.

---

## 3. User flow

### 3.1 Arriving
1. User taps the bio link `t.me/<bot>?start=<source>`. `<source>` is a short tag like `tt_v1`, `ig`, `flatchat`, used to track which video or post brought them. Store the first source only, and sanitise it to `[a-z0-9_]{1,32}`, otherwise `unknown`.
2. The bot creates the user row (if new) and sends the **welcome message** (§9) with buttons:
   - **Start my free 2 weeks** (only if they've never had a trial)
   - **Join the free channel** (URL button, free channel link)
   - **How it works**

### 3.2 Trial
3. On **Start my free 2 weeks**:
   - If `trial_started_at` is already set, reply "You've already had your free trial", and show **Subscribe**.
   - Otherwise set `trial_started_at = now`, `trial_ends_at = now + TRIAL_DAYS`, log it, and send the **paid channel join link** (§4).
4. The trial starts **when the button is pressed**, not at `/start`, so people who only look around don't lose days.

### 3.3 Reminders (sent by the scheduler, §6)
5. **2 days before the trial ends:** a reminder with a **Subscribe** button.
6. **On the last day (about 3 hours before the end):** a final reminder with **Subscribe**.
7. **At the end:** an "Your trial has ended" message with **Subscribe** and **Join the free channel** buttons, and they're removed from the paid channel.

Each reminder is sent **at most once** per user (tracked in the DB, see §5). If the bot was down when a reminder was due, send it when it comes back **only if it's still relevant** (e.g. don't send "2 days left" after the trial has ended).

### 3.4 Paying
8. **Subscribe** (button or `/subscribe`):
   - If the user already has an active subscription, don't create a new checkout. Send `/manage` instead.
   - Otherwise create a Stripe Checkout Session (§7.2) and send its URL as a button.
9. After paying, Stripe shows a success page that tells them to go back to Telegram. The bot detects the payment by polling (§7.3), usually within 30 seconds, and sends "You're in ✅" with the join link if they aren't already in the channel.
10. An **I've paid** button triggers an immediate check for that user's pending checkout, in case they're impatient.

### 3.5 Managing and cancelling
11. `/manage` creates a Stripe **Customer Portal** session, where they cancel, update their card or see invoices. Cancelling keeps access until the end of the paid period.
12. When a subscription ends or payment finally fails (after grace), they're removed with an explanatory message and a **Resubscribe** button.

### 3.6 Commands for users
`/start`, `/subscribe`, `/manage`, `/status` (shows trial/subscription state and end date), `/help`, `/terms`.

---

## 4. Paid channel access (the most important part to get right)

**Use join requests, not one-off invite links.**

- Create **one** invite link for the paid channel with `creates_join_request=True`. Store it in config. Every user gets the same link.
- When someone requests to join (`chat_join_request` update), the bot checks `is_entitled(user, now)` (§5.2):
  - entitled → `approve_chat_join_request`
  - not entitled → `decline_chat_join_request`, and DM them the reason plus **Subscribe** (if the DM fails, ignore it)
  - unknown user (never pressed /start) → decline and DM "Tap /start first" (if possible)
- Because approval depends on entitlement, **sharing the link is harmless.**

**Removal:**
- Remove = `ban_chat_member` followed immediately by `unban_chat_member(only_if_banned=True)`. This kicks them but lets them rejoin later if they pay. Only use a permanent ban for users flagged `banned`.
- If the API says they're not a member, treat it as success.

**Tracking membership:**
- Request `chat_member` updates explicitly in `allowed_updates` (Telegram doesn't send them by default). Update `in_paid_channel` when people join or leave.
- Never rely only on that flag for removals. Before removing, confirm with `get_chat_member`.

**Channel settings the owner must set (put this in the README):**
- The member bot is an admin of the paid channel with the **Invite users via link** and **Ban users** rights, and nothing else.
- The alert bot is an admin with the **Post messages** right.
- **Restrict saving content** is turned on (stops forwarding and copying in most clients).
- The channel is private.

---

## 5. Data

### 5.1 SQLite schema (WAL mode, `foreign_keys=ON`)

```
users(
  telegram_id          INTEGER PRIMARY KEY,
  username             TEXT,
  first_name           TEXT,
  source               TEXT,
  created_at           TEXT NOT NULL,          -- ISO UTC
  trial_started_at     TEXT,
  trial_ends_at        TEXT,
  stripe_customer_id   TEXT UNIQUE,
  stripe_sub_id        TEXT,
  sub_status           TEXT,                   -- Stripe status copied as-is
  sub_period_end       TEXT,
  cancel_at_period_end INTEGER DEFAULT 0,
  manual_grant_until   TEXT,                   -- admin comps
  in_paid_channel      INTEGER DEFAULT 0,
  blocked_bot          INTEGER DEFAULT 0,      -- user blocked us, stop DMing
  banned               INTEGER DEFAULT 0,
  last_synced_at       TEXT                    -- last successful Stripe sync for this user
)

checkout_sessions(
  id           TEXT PRIMARY KEY,               -- Stripe cs_...
  telegram_id  INTEGER NOT NULL REFERENCES users,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  status       TEXT NOT NULL                   -- open / complete / expired
)

notices_sent(
  telegram_id  INTEGER NOT NULL,
  kind         TEXT NOT NULL,                  -- trial_2d / trial_final / trial_ended / sub_ended / welcome_paid ...
  cycle_key    TEXT NOT NULL,                  -- e.g. trial end date or sub period end, so each cycle gets its own notice
  sent_at      TEXT NOT NULL,
  PRIMARY KEY (telegram_id, kind, cycle_key)
)

audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, telegram_id INTEGER, action TEXT NOT NULL, detail TEXT
)

kv(key TEXT PRIMARY KEY, value TEXT)           -- e.g. last_full_sync_ok_at
```

Use a small migrations system (a `schema_version` in `kv`, numbered migration functions) so the schema can change later without losing data.

**Plan for more cities now, don't build it now:** keep the paid channel ID, free channel link and Stripe Price ID in config under a `CITY=exeter` prefix or a single `cities` table, so adding "Ping Bath" later is config plus a channel, not a rewrite. For v1, only one city is active.

### 5.2 Entitlement: one function, used everywhere

```
is_entitled(user, now) =
    not user.banned and (
        (user.manual_grant_until and now < manual_grant_until)
     or (user.trial_ends_at and now < trial_ends_at)
     or user.sub_status in ("active", "trialing")
     or (user.sub_status == "past_due" and now < sub_period_end + PAST_DUE_GRACE)
    )
```

- Stripe keeps `status="active"` until the period ends when someone cancels, so cancelled-but-paid-up users stay entitled automatically.
- This must be a **pure function** with the clock passed in, and it gets thorough unit tests (§11).
- Every decision (approve join, remove, show `/status`) goes through it. **No other entitlement logic anywhere.**

---

## 6. Scheduler

Run with `asyncio` tasks inside the same process. **No in-memory timers for individual users.** Everything is derived from the DB on each tick, so restarts lose nothing.

| Job | Every | What |
|---|---|---|
| `poll_checkouts` | 30 s | check open checkout sessions (§7.3) |
| `sync_subscriptions` | 5 min | full Stripe subscription sync (§7.4) |
| `send_notices` | 5 min | trial reminders and ended messages that are due and not yet sent |
| `sweep_removals` | 10 min | remove non-entitled users still in the channel (§8) |
| `daily_report` | 09:00 London | stats to the owner (§10) |
| `backup` | 03:30 London | SQLite backup (§10) |

- Each job is wrapped so one failure can't kill the others. Log the exception, alert the owner (rate-limited), and carry on next tick.
- Jobs must be **idempotent**: running one twice in a row does nothing harmful.
- Never let two runs of the same job overlap (use a lock per job).

---

## 7. Stripe

### 7.1 Keys and setup
- Use a **restricted API key** with only these permissions: Checkout Sessions (write), Customers (write), Subscriptions (read), Customer Portal (write), Disputes (read). Never use the full secret key.
- Run in **test mode** (`sk_test_`/`rk_test_`) until the owner says go live. Log loudly at startup which mode it's in.
- Business name, statement descriptor and support email are set by the owner in the Stripe dashboard so his personal name isn't shown. Nothing to do in code, but list it in the README checklist.

### 7.2 Creating a checkout
- Look up or create the Stripe Customer for the user, with metadata `telegram_id` and store `stripe_customer_id`. Never create two customers for one Telegram ID.
- Create the Checkout Session with:
  - `mode="subscription"`, `line_items=[{price: PRICE_ID, quantity: 1}]`
  - `customer=<id>`, `client_reference_id=<telegram_id>`
  - `subscription_data.metadata.telegram_id=<telegram_id>`
  - `success_url` / `cancel_url`: a simple page saying "Done, go back to Telegram". A `t.me/<bot>` link works.
  - `allow_promotion_codes=True` (for founding-member coupons later)
  - an **idempotency key** like `checkout-<telegram_id>-<10-minute bucket>`, so double-taps don't create two sessions
- If the user has an **open, unexpired session**, resend its URL instead of creating another.

### 7.3 Detecting payment (`poll_checkouts`)
- For each `checkout_sessions` row with `status=open` and not expired, retrieve it from Stripe.
- `complete` with a subscription → store the subscription details on the user, mark the session complete, send the "You're in ✅" message (once, via `notices_sent`).
- Expired or past `expires_at` → mark expired.
- The **I've paid** button runs the same check for that one user immediately.

### 7.4 Full sync (`sync_subscriptions`)
- List **all** subscriptions (`status="all"`, paginate with `auto_paging_iter`). For each with `metadata.telegram_id`, update `stripe_sub_id`, `sub_status`, `sub_period_end`, `cancel_at_period_end`, `last_synced_at` on that user.
- If a user has several subscriptions (shouldn't happen), use the "best" one (active > trialing > past_due > others) and **alert the owner**.
- On complete success, write `kv.last_full_sync_ok_at = now`. On any failure, **don't** update it.
- Check disputes daily: a new dispute on a user's charge → set `sub_status` to a local `"disputed"` value (not entitled), alert the owner. Don't auto-ban.

### 7.5 Failure rules
- Stripe errors are retried with backoff (the library has `max_network_retries`, set it to 3).
- **A Stripe outage must never cause paying users to be removed.** See the removal safety rules in §8.

---

## 8. Removal safety (this is where a bug costs real customers)

`sweep_removals` removes a user only if **all** of these are true:
1. `is_entitled(user, now)` is false.
2. `get_chat_member` confirms they're currently in the paid channel.
3. If the user has **ever** had a Stripe subscription, `kv.last_full_sync_ok_at` is less than **15 minutes** old. A stale or failed sync means skip this user this round.
4. They're not the owner and not on the `ADMIN_IDS` list.

Global safety rails:
- **`REMOVALS_ENABLED`** config flag, **default `false`**. When false, the sweep runs in **dry-run**: it logs and sends the owner "Would remove: …" but removes nobody. The owner flips it on after checking dry runs.
- **Mass-removal cap:** if one sweep would remove more than `MAX_REMOVALS_PER_SWEEP` users (default 5) **or** more than 20% of current entitled-plus-to-remove users, **remove nobody**, alert the owner, and wait for `/confirmsweep`. This catches bugs, clock problems and Stripe weirdness before they wipe the channel.
- Before kicking, send the explanatory DM (trial ended / subscription ended) and record it in `notices_sent`. Send the DM before the kick, and if the DM fails because they blocked the bot, still kick.
- Every removal and every skipped removal is written to `audit_log`.

---

## 9. Message copy (use as written, small tweaks fine)

Keep messages short. Use HTML parse mode and escape user-supplied text.

- **Welcome:**
  "👋 Welcome to **Ping Exeter**.
  We alert you the second tickets drop on FIXR for TP and other Exeter nights, so you get yours before they sell out.
  🎁 Your first 2 weeks are free. No card needed."
- **Trial started:**
  "✅ Your free trial is on until **{end_date}**.
  Tap below to join the alerts channel. Turn notifications **on** for it so you don't miss a drop."
  *(button: Join the alerts channel → join-request link)*
- **2 days left:**
  "⏳ 2 days left of your free trial (ends {end_date}).
  Keep getting instant alerts for £{price}/{interval}. Cancel anytime."
  *(button: Subscribe)*
- **Final reminder:**
  "Last day of your free trial. Subscribe now to keep your alerts without a gap."
  *(button: Subscribe)*
- **Trial ended:**
  "Your free trial has ended, so you've been removed from the instant alerts.
  You can still follow the free channel (alerts arrive about 10 minutes later), or subscribe to get instant ones back."
  *(buttons: Subscribe · Free channel)*
- **Paid:**
  "You're in ✅ Thanks for subscribing. Manage or cancel anytime with /manage."
  *(button: Join the alerts channel, only if not already in)*
- **Subscription ended:**
  "Your subscription has ended, so you've been removed from instant alerts. Resubscribe anytime."
  *(button: Resubscribe)*
- **Already had trial:** "You've already had your free trial. Subscribe to get instant alerts."
- **Join declined:** "To join the alerts channel, start a free trial or subscribe first 👉 /start"
- **/terms:** a short plain-English summary (what you get, price, renews monthly, cancel anytime via /manage, access until the end of the paid period, alerts are best-effort and don't guarantee you get a ticket, not affiliated with FIXR or any venue, support contact). **Flag to the owner** that the final wording should be checked against UK consumer rules, since Stripe and the law may require proper terms and a refund policy.

Wording rules: never mention reselling. Always say "not affiliated with FIXR or any venue" in `/terms`.

---

## 10. Admin tools, monitoring, backups

**Admin commands** (only work for `ADMIN_IDS`, silently ignored for everyone else):
- `/stats`: users total, active trials, paying, past_due, trials ended in the last 7 days, conversions, and joins by `source` in the last 7 days
- `/user <telegram_id or @username>`: everything about one user plus their last 10 audit-log entries
- `/grant <id> <days>`: sets `manual_grant_until` (for friends, testers, apologies)
- `/revoke <id>`: clears grant, removes from channel (still subject to the admin protection)
- `/ban <id>` / `/unban <id>`
- `/dryrun`: runs the removal sweep in dry-run and reports
- `/confirmsweep`: approve a sweep blocked by the mass-removal cap
- `/pause` / `/resume`: pauses all removals and notices (maintenance switch, stored in `kv` so it survives restarts)
- `/broadcast <text>`: **not in v1**, deliberately left out to avoid accidental mass messages

**Owner alerts** (DM to the owner, rate-limited to max 1 per error type per 30 min):
- job crashed
- Stripe sync failed 3 times in a row
- mass-removal cap hit
- dispute opened
- bot started, with test/live mode shown

**Daily report** at 09:00 London: the `/stats` summary plus "all jobs healthy" or a list of what failed.

**Logging:** standard `logging` to stdout (systemd journal picks it up). Log every Stripe call and Telegram admin action at INFO. Never log full API keys or card info.

**Backups:** nightly `sqlite3` online backup (`.backup` API, not file copy) to `backups/ping-YYYY-MM-DD.db`, keep the last 14. The README tells the owner how to download one occasionally, because the backups live on the same machine.

---

## 11. Robustness checklist (Claude Code: tick these off)

**Telegram**
- [ ] Respect rate limits: global send queue throttled to about 20 messages per second. On a 429, wait `retry_after` and retry.
- [ ] A 403 when DMing (user blocked the bot) → set `blocked_bot=1` and stop DMing them. Don't crash and don't retry.
- [ ] Network errors → retry with backoff, max 3, then log and move on.
- [ ] `allowed_updates` includes `message`, `callback_query`, `chat_join_request`, `chat_member`.
- [ ] Callback buttons answered (`answer_callback_query`) so they don't spin forever.
- [ ] Ignore messages in groups and channels. Only handle private chats, plus the join-request and member updates for the paid channel.
- [ ] Ignore updates about any chat that isn't the configured paid channel.

**Data and logic**
- [ ] Single `is_entitled` function, unit-tested.
- [ ] All writes in transactions. Reminder sends are recorded in `notices_sent` **in the same step** as sending, so a crash can at worst skip a reminder, never double-send. Insert the row first, then send. If the send fails because of the network, delete the row so it's retried.
- [ ] One trial per Telegram ID, enforced by the DB, not just by the code path.
- [ ] One Stripe customer per Telegram ID (unique constraint plus lookup before create).
- [ ] UTC everywhere internally. Tests include a trial ending across the October clock change.
- [ ] Config validated at startup: missing or invalid values → exit with a clear message (systemd will show it).

**Process**
- [ ] systemd service with `Restart=always`, `RestartSec=10`, its own folder, venv and user, same pattern as the alert bot's `setup.sh`.
- [ ] Graceful shutdown on SIGTERM: finish the current job, close the DB.
- [ ] Single-instance guard (lock file), so two copies can never run at once and double-process.
- [ ] `.env` is `chmod 600`, listed in `.gitignore`. `.env.example` has every key with comments.

**Tests (pytest)**
- [ ] `is_entitled` for every state combination, with boundary times (exactly at trial end, grace end).
- [ ] Notices: due, not due, already sent, became irrelevant while the bot was down.
- [ ] Removal sweep: dry-run, cap hit, stale Stripe sync → skip, admin protected, user not in channel.
- [ ] Stripe and Telegram calls behind small wrapper classes so tests use fakes. No real API calls in unit tests.
- [ ] Stripe **test clocks** used in a manual test script to simulate renewal, cancellation and failed payment end to end.

---

## 12. Not in v1 (deliberately)

Stripe webhooks; multiple cities live; referral codes; broadcast command; web dashboard; email collection outside Stripe. Note any of these as TODOs if you see a natural hook, but don't build them.

Upgrade path for webhooks later: the same handlers can be called from a webhook endpoint (behind a Cloudflare Tunnel) without changing the logic, as long as Stripe handling stays in its own module.

---

## 13. Config (`.env.example`)

```
# Telegram – this is a NEW bot from @BotFather, not the alert bot
BOT_TOKEN=
BOT_USERNAME=PingExeterBot
ADMIN_IDS=                       # comma-separated Telegram user IDs (owner first)

# Channels
PAID_CHANNEL_ID=                 # -100...
PAID_JOIN_REQUEST_LINK=          # invite link created with join requests ON
FREE_CHANNEL_LINK=               # https://t.me/...

# Trial / access
TRIAL_DAYS=14
PAST_DUE_GRACE_HOURS=72
REMOVALS_ENABLED=false
MAX_REMOVALS_PER_SWEEP=5

# Stripe
STRIPE_API_KEY=                  # restricted key, rk_test_... until go-live
STRIPE_PRICE_ID=                 # price_...
STRIPE_SUCCESS_URL=https://t.me/PingExeterBot
STRIPE_CANCEL_URL=https://t.me/PingExeterBot

# Display
TIMEZONE=Europe/London
SUPPORT_CONTACT=@PingExeterSupport
DB_PATH=data/ping.db
```

---

## 14. Build phases (stop and check in after each)

1. **Skeleton.** Project structure, config loading and validation, DB plus migrations, `is_entitled` plus its tests, `/start` with source tracking, welcome message. *Checkpoint: owner runs it locally with a test bot.*
2. **Trial and channel access.** Trial button, join-request approve/decline, `chat_member` tracking, `/status`, notices job, removal sweep (dry-run only), admin `/stats` `/user` `/grant` `/dryrun`. *Checkpoint: test with a second Telegram account and a test channel. Shorten `TRIAL_DAYS` via a `TRIAL_MINUTES_OVERRIDE` debug setting to see the full cycle in minutes.*
3. **Stripe (test mode).** Checkout, polling, full sync, `/manage` portal, disputes check, test-clock script. *Checkpoint: owner pays with Stripe test card 4242…, cancels, simulates a failed renewal.*
4. **Hardening and deploy.** Rate limiting, owner alerts, daily report, backups, single-instance lock, `setup.sh` and systemd service, README with the BotFather, channel and Stripe dashboard checklists. *Checkpoint: running on the Google Cloud VM next to the alert bot, dry-run removals for a few days.*
5. **Go-live (owner's call only).** Switch to live Stripe key and live Price, set `REMOVALS_ENABLED=true`, final test with a real card and a refund.

At every checkpoint, write a short "how to test this" list for the owner.
