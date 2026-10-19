"""Config loading and validation. Reads a mapping (os.environ after dotenv),
collects every problem, and raises one ConfigError listing them all.

Secrets are never included in error messages or repr output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    bot_username: str
    admin_ids: tuple[int, ...]
    paid_channel_id: int
    paid_join_request_link: str
    free_channel_link: str
    trial_days: int
    trial_reminder_days_before: int
    past_due_grace: timedelta
    removals_enabled: bool
    max_removals_per_sweep: int
    stripe_api_key: str = field(repr=False)
    stripe_price_id: str
    stripe_success_url: str
    stripe_cancel_url: str
    timezone: ZoneInfo
    support_contact: str
    db_path: str

    @property
    def stripe_mode(self) -> str:
        if not self.stripe_api_key:
            return "not configured"
        return "TEST"

    @property
    def bot_link(self) -> str:
        return f"https://t.me/{self.bot_username}"


_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_TME_RE = re.compile(r"^https://t\.me/\S+$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def load_config(env: Mapping[str, str]) -> Config:
    problems: list[str] = []

    def get(key: str, default: str = "") -> str:
        return (env.get(key) or default).strip()

    def required(key: str, hint: str) -> str:
        value = get(key)
        if not value:
            problems.append(f"{key} is missing ({hint})")
        return value

    def integer(key: str, default: int, minimum: int) -> int:
        raw = get(key)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            problems.append(f"{key} must be a whole number")
            return default
        if value < minimum:
            problems.append(f"{key} must be at least {minimum}")
            return default
        return value

    bot_token = required("BOT_TOKEN", "the token from @BotFather")
    if bot_token and not _TOKEN_RE.match(bot_token):
        problems.append("BOT_TOKEN doesn't look like a Telegram bot token (format 123456:ABC...)")

    bot_username = required("BOT_USERNAME", "the bot's @username without the @").lstrip("@")
    if bot_username and not _USERNAME_RE.match(bot_username):
        problems.append("BOT_USERNAME must be 5-32 letters, digits or underscores")

    admin_ids: list[int] = []
    raw_admins = required("ADMIN_IDS", "comma-separated Telegram user IDs, owner first")
    for part in filter(None, (p.strip() for p in raw_admins.split(","))):
        try:
            admin_ids.append(int(part))
        except ValueError:
            problems.append("ADMIN_IDS must be comma-separated numeric Telegram user IDs")
            break

    paid_channel_id = 0
    raw_channel = required("PAID_CHANNEL_ID", "starts with -100")
    if raw_channel:
        try:
            paid_channel_id = int(raw_channel)
            if not str(paid_channel_id).startswith("-100"):
                problems.append("PAID_CHANNEL_ID should start with -100")
        except ValueError:
            problems.append("PAID_CHANNEL_ID must be a number like -1001234567890")

    paid_link = required("PAID_JOIN_REQUEST_LINK", "invite link with join requests ON")
    if paid_link and not _TME_RE.match(paid_link):
        problems.append("PAID_JOIN_REQUEST_LINK must start with https://t.me/")

    free_link = required("FREE_CHANNEL_LINK", "public free channel link")
    if free_link and not _TME_RE.match(free_link):
        problems.append("FREE_CHANNEL_LINK must start with https://t.me/")

    trial_days = integer("TRIAL_DAYS", 14, 1)
    reminder_days = integer("TRIAL_REMINDER_DAYS_BEFORE", 3, 1)
    grace_hours = integer("PAST_DUE_GRACE_HOURS", 72, 0)
    max_removals = integer("MAX_REMOVALS_PER_SWEEP", 5, 1)

    removals_raw = get("REMOVALS_ENABLED", "false").lower()
    if removals_raw in _TRUE:
        removals_enabled = True
    elif removals_raw in _FALSE:
        removals_enabled = False
    else:
        problems.append("REMOVALS_ENABLED must be true or false")
        removals_enabled = False

    stripe_key = get("STRIPE_API_KEY")
    if stripe_key and not stripe_key.startswith(("sk_test_", "rk_test_")):
        problems.append(
            "STRIPE_API_KEY must be a TEST-mode key (sk_test_... or rk_test_...). "
            "Live keys are not accepted until go-live."
        )
    price_id = get("STRIPE_PRICE_ID")
    if price_id and not price_id.startswith("price_"):
        problems.append("STRIPE_PRICE_ID must start with price_")

    default_link = f"https://t.me/{bot_username}" if bot_username else ""
    success_url = get("STRIPE_SUCCESS_URL", default_link)
    cancel_url = get("STRIPE_CANCEL_URL", default_link)

    tz_name = get("TIMEZONE", "Europe/London")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"TIMEZONE '{tz_name}' is not a valid timezone name")
        tz = ZoneInfo("UTC")

    if problems:
        raise ConfigError(problems)

    return Config(
        bot_token=bot_token,
        bot_username=bot_username,
        admin_ids=tuple(admin_ids),
        paid_channel_id=paid_channel_id,
        paid_join_request_link=paid_link,
        free_channel_link=free_link,
        trial_days=trial_days,
        trial_reminder_days_before=reminder_days,
        past_due_grace=timedelta(hours=grace_hours),
        removals_enabled=removals_enabled,
        max_removals_per_sweep=max_removals,
        stripe_api_key=stripe_key,
        stripe_price_id=price_id,
        stripe_success_url=success_url,
        stripe_cancel_url=cancel_url,
        timezone=tz,
        support_contact=get("SUPPORT_CONTACT"),
        db_path=get("DB_PATH", "data/first-in.db"),
    )
