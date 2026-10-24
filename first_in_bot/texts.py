"""User-facing copy. HTML parse mode; escape any user-supplied text with html.escape.

The free trial runs through Stripe and needs a card (owner decision 2026-10-18),
so nothing here may suggest otherwise. Never mention reselling.
"""

# Used wherever {price} appears if the price couldn't be read from Stripe.
PRICE_FALLBACK = "the subscription price"


def price_text(label: str | None) -> str:
    return label or PRICE_FALLBACK


# The trial facts, worded once and shown before the Stripe link, in "How it works"
# and in /terms. {price} already includes the interval (e.g. "£2.99/month").
# With a checkout in hand the first-charge date is fixed (it is the trial_end baked into
# the Stripe session); in "How it works" and /terms there is no session yet, so the date
# is "if you start today".
TRIAL_TERMS_FIXED = (
    "• <b>{days} days free</b>, then <b>{price}</b>, charged automatically.\n"
    "• A card is required to start. You add it on Stripe's secure checkout page.\n"
    "• Your first payment is taken on <b>{charge_date}</b>. That date is fixed when this link is made, "
    "so it stays the same even if you finish checkout later.\n"
    "• Cancel any time before then with /manage and you won't be charged."
)
TRIAL_TERMS_GENERIC = (
    "• <b>{days} days free</b>, then <b>{price}</b>, charged automatically.\n"
    "• A card is required to start. You add it on Stripe's secure checkout page.\n"
    "• The {days} days count from when you tap the button. If you start today, your first payment "
    "is taken on <b>{charge_date}</b>.\n"
    "• Cancel any time before then with /manage and you won't be charged."
)


def trial_terms(price_label: str | None, days: int, charge_date: str, fixed: bool = False) -> str:
    template = TRIAL_TERMS_FIXED if fixed else TRIAL_TERMS_GENERIC
    return template.format(price=price_text(price_label), days=days, charge_date=charge_date)


# --- Welcome ---------------------------------------------------------------

WELCOME = (
    "👋 Welcome to <b>First In Exeter</b>.\n"
    "We alert you the second tickets drop on FIXR for TP and other Exeter nights, "
    "so you get yours before they sell out.\n"
    "{offer}"
)
OFFER_TRIAL = (
    "🎁 Your first {days} days are free, then {price}. A card is required to start, and you can "
    "cancel any time before your first payment with /manage."
)
OFFER_SUBSCRIBE = "Instant alerts are {price}. Cancel anytime."

HOW_IT_WORKS_TRIAL = (
    "<b>How it works</b>\n"
    "{terms}\n\n"
    "1. Tap <b>Start my free 2 weeks</b> and add your card on Stripe's secure page.\n"
    "2. Join the alerts channel and turn notifications on. Alerts arrive the second tickets drop.\n"
    "3. We message you {reminder_days} days before your first payment, and again a few hours before.\n"
    "Prefer not to pay? Follow the free channel (alerts arrive about 10 minutes later)."
)
# For people who've already had their free trial: they're charged straight away.
HOW_IT_WORKS_SUBSCRIBE = (
    "<b>How it works</b>\n"
    "1. Tap <b>Subscribe</b> and pay {price} on Stripe's secure checkout page.\n"
    "2. Join the alerts channel and turn notifications on. Alerts arrive the second tickets drop.\n"
    "3. Your subscription renews automatically until you cancel.\n"
    "4. Cancel anytime with /manage. You keep access until the end of the period you've paid for.\n"
    "Prefer not to pay? Follow the free channel (alerts arrive about 10 minutes later)."
)

UNAVAILABLE = "Sorry, this service isn't available for your account."

TRIAL_START_FIRST = "Tap /start first 👉 /start"

# --- Checkout ----------------------------------------------------------------

PAYMENTS_NOT_READY = (
    "Payments aren't set up yet, so the free trial and subscriptions can't start. "
    "Please check back soon."
)
STRIPE_ERROR = "Sorry, I couldn't reach the payment system just now. Please try again in a minute."

CHECKOUT_TRIAL = (
    "🎁 <b>Before you start your free trial</b>\n"
    "{terms}\n\n"
    "Nothing is charged today. Tap below to continue to Stripe."
)
CHECKOUT_SUBSCRIBE = "Tap below to subscribe for {price}. Cancel anytime with /manage."
CHECKOUT_AFTER = (
    "When you're done, come back here. I'll let you in automatically, usually within a minute, "
    "or tap <b>{done_button}</b>."
)

ALREADY_HAD_TRIAL = "You've already had your free trial. Subscribe to get instant alerts."
ALREADY_SUBSCRIBED = "You're already in ✅ Use /manage to see or cancel your subscription."
DISPUTED_NO_CHECKOUT = (
    "There's a payment dispute on your account, so I can't start a new subscription. {contact}"
)
PAYMENT_PROBLEM ="⚠️ Your last payment didn't go through. Update your card with /manage to keep your alerts."
PAID_NOT_SEEN = (
    "I can't see a finished checkout yet. If you've only just finished, give it a few seconds "
    "and tap the button again."
)

# --- After payment (sent once, by the payment poller) -------------------------

TRIAL_STARTED = (
    "✅ Your free trial is on until <b>{end_date}</b>.\n"
    "Your first payment of {price} is taken on that date. Cancel any time before then with /manage "
    "and you won't be charged."
)
PAID = "You're in ✅ Thanks for subscribing. Manage or cancel anytime with /manage."
JOIN_HINT = "Tap below to join the alerts channel. Turn notifications <b>on</b> for it so you don't miss a drop."

# --- Reminders and endings ---------------------------------------------------

TRIAL_REMINDER = (
    "⏳ Your free trial ends on <b>{end_date}</b>.\n"
    "After that, {price} is charged automatically to the card you added and your instant alerts carry on.\n"
    "Don't want to continue? Cancel any time before then with /manage and you won't be charged."
)
TRIAL_FINAL = (
    "⏰ Your free trial ends in a few hours, at <b>{end_date}</b>.\n"
    "Then {price} is charged automatically to your card.\n"
    "Want to stop? Cancel now with /manage and you won't be charged."
)
TRIAL_ENDED = (
    "Your free trial has ended, so you've been removed from the instant alerts.\n"
    "You can still follow the free channel (alerts arrive about 10 minutes later), "
    "or subscribe to get instant ones back."
)
SUB_ENDED = "Your subscription has ended, so you've been removed from instant alerts. Resubscribe anytime."
ACCESS_ENDED = (
    "Your free access has ended, so you've been removed from instant alerts. "
    "Subscribe anytime to get them back."
)

# --- Joining the paid channel --------------------------------------------------

JOIN_DECLINED = "To join the alerts channel, start your free trial or subscribe first 👉 /start"
JOIN_APPROVE_FAILED = (
    "Sorry, something went wrong letting you into the alerts channel. "
    "Please try the join link again in a few minutes."
)

# --- /manage -----------------------------------------------------------------

MANAGE_LINK = (
    "Tap below to manage your subscription: cancel, change your card or see invoices.\n"
    "Cancel during your free trial and you won't be charged. Cancel later and you keep access "
    "until the end of the period you've paid for."
)
MANAGE_NO_CUSTOMER = "You don't have a subscription to manage yet."
MANAGE_PORTAL_OFF = "Sorry, managing your subscription here isn't switched on yet. {contact}"
CONTACT_SUPPORT = "Please message {support} and we'll sort it."
CONTACT_LATER = "Please try again later."

# --- /status -----------------------------------------------------------------

STATUS_HEADER = "<b>Your First In status</b>"
STATUS_ACCESS_YES = "✅ You have access to the alerts channel."
STATUS_ACCESS_NO = "❌ You don't have access to the alerts channel right now."
STATUS_TRIALING = (
    "🎁 Free trial until <b>{end}</b>. After that, {price} is charged automatically. "
    "Cancel before then with /manage and you won't pay."
)
STATUS_TRIAL_CANCELLED = "🎁 Free trial until <b>{end}</b>. You've cancelled, so you won't be charged and access ends then."
STATUS_TRIALING_NO_DATE = "🎁 Your free trial is running. Cancel before it ends with /manage and you won't pay."
STATUS_ACTIVE = "Subscribed. Renews on <b>{end}</b> ({price}). Manage or cancel with /manage."
STATUS_ACTIVE_NO_DATE = "Subscribed. Manage or cancel with /manage."
STATUS_CANCELLED = "Subscription cancelled. You keep access until <b>{end}</b>."
STATUS_PAST_DUE = "⚠️ Your last payment didn't go through. Update your card with /manage to keep your alerts."
STATUS_PAST_DUE_UNTIL = (
    "⚠️ Your last payment didn't go through. Update your card with /manage before <b>{end}</b> "
    "to keep your alerts."
)
STATUS_DISPUTED = (
    "⚠️ There's a payment dispute on your account, so your access is on hold. "
    "{contact}"
)
STATUS_INCOMPLETE ="Your checkout didn't finish. Tap /subscribe to try again."
STATUS_GRANT = "🎟 Free access until <b>{end}</b>."
STATUS_ENDED = "Your subscription has ended. Tap /subscribe to get instant alerts back."
STATUS_NONE_TRIAL = "You haven't started your free trial yet. Tap /subscribe to start your free 2 weeks."
STATUS_NONE = "You don't have a subscription. Tap /subscribe to get instant alerts."

# --- /help and /terms ----------------------------------------------------------

HELP = (
    "<b>First In Exeter</b>\n"
    "/start: welcome and free trial\n"
    "/subscribe: start your free trial or subscribe\n"
    "/status: your trial or subscription\n"
    "/manage: cancel, change your card or see invoices\n"
    "/terms: the short version of the terms\n"
    "/help: this message"
)
# Added after HELP for ADMIN_IDS only. Everyone else sees exactly HELP.
HELP_ADMIN = (
    "<b>Admin</b>\n"
    "/stats: users, trials, paying members and sign-ups\n"
    "/user &lt;id or @username&gt;: everything about one user\n"
    "/grant &lt;id or @username&gt; &lt;days&gt;: free access for that many days (0 clears it)\n"
    "/revoke &lt;id or @username&gt;: clear a grant and remove them if nothing else gives access\n"
    "/ban &lt;id or @username&gt;: block them for good and remove them\n"
    "/unban &lt;id or @username&gt;: lift a ban\n"
    "/dryrun: who the removal sweep would remove, without removing anyone\n"
    "/confirmsweep: approve a sweep the mass-removal cap held back\n"
    "/pause: hold all removals and reminders (joins and payments keep working)\n"
    "/resume: switch removals and reminders back on"
)

# DRAFT: plain-English summary. The owner must check the final wording against
# UK consumer rules (subscription and refund rules) before go-live.
TERMS = (
    "<b>Terms (short version)</b>\n"
    "• You get instant Telegram alerts when tickets drop on FIXR for TP and other Exeter nights.\n"
    "{trial_terms}\n"
    "• The subscription then renews until you cancel.\n"
    "• We message you {reminder_days} days before your first payment, and again a few hours before.\n"
    "• Cancel during the trial and you keep access until the trial would have ended. Cancel later and you "
    "keep access until the end of the period you've paid for.\n"
    "• Alerts are best-effort and don't guarantee you get a ticket.\n"
    "• First In is not affiliated with FIXR or any venue.\n"
    "{support_line}"
)
TERMS_SUPPORT = "• Questions or problems: {support}"
