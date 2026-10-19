# First In Exeter member bot – standing instructions

The spec is `PING_MEMBER_BOT_SPEC.md`. These overrides take precedence over it.

- **Brand:** "First In Exeter" / "First In". Never "Ping" or "Ping Exeter" in messages, README, comments, file or service names.
- **Service name:** `first-in-member-bot` (systemd).
- **Bot username:** always comes from the `BOT_USERNAME` config value. Never hard-code it.
- **Stripe Customer Portal:** must be activated in the Stripe dashboard (Settings > Billing > Customer portal, cancellations allowed). `/manage` must give a clear error message if the portal isn't configured. Keep this in the README checklist.
- **Secrets:** the owner fills in `.env` themselves. Never ask for keys or tokens in chat, never print them in logs or output, never commit them. `.env` stays in `.gitignore`; `.env.example` has every key.
- **Test mode only:** no live Stripe keys or prices.
- **`REMOVALS_ENABLED` stays `false`.**
- **Free trial runs through Stripe (owner decision 2026-10-18, overrides spec 3.2 and the "no card needed" copy):** "Start my free 2 weeks" opens Stripe Checkout with a 14-day subscription trial, card required; Stripe charges automatically when it ends. There is no bot-side trial: `users.trial_ends_at` is never set.
- **One phase at a time.** Build only the requested phase, then stop at the checkpoint and give the owner a "how to test this" list. Don't start the next phase.
- **Ask first** about any decision the spec doesn't cover.
- **Git sync:** this project is worked on from 3 devices. At the **beginning and end of every session**, always `git pull`, commit and `git push`. This is standing permission. Never commit `.env` or `data/`.
- **Commands:** the owner uses a Mac. Write commands in the README and test lists as `.venv/bin/python ...`.
