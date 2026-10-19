from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .timeutil import from_iso


@dataclass(frozen=True)
class User:
    telegram_id: int
    username: str | None = None
    first_name: str | None = None
    source: str | None = None
    created_at: datetime | None = None
    trial_started_at: datetime | None = None
    trial_ends_at: datetime | None = None
    stripe_customer_id: str | None = None
    stripe_sub_id: str | None = None
    sub_status: str | None = None
    sub_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    manual_grant_until: datetime | None = None
    in_paid_channel: bool = False
    blocked_bot: bool = False
    banned: bool = False
    last_synced_at: datetime | None = None
    sub_trial_end: datetime | None = None
    sub_period_start: datetime | None = None
    past_due_since: datetime | None = None   # start of the first unpaid period while past_due

    @classmethod
    def from_row(cls, row: Mapping) -> "User":
        return cls(
            telegram_id=row["telegram_id"],
            username=row["username"],
            first_name=row["first_name"],
            source=row["source"],
            created_at=from_iso(row["created_at"]),
            trial_started_at=from_iso(row["trial_started_at"]),
            trial_ends_at=from_iso(row["trial_ends_at"]),
            stripe_customer_id=row["stripe_customer_id"],
            stripe_sub_id=row["stripe_sub_id"],
            sub_status=row["sub_status"],
            sub_period_end=from_iso(row["sub_period_end"]),
            cancel_at_period_end=bool(row["cancel_at_period_end"]),
            manual_grant_until=from_iso(row["manual_grant_until"]),
            in_paid_channel=bool(row["in_paid_channel"]),
            blocked_bot=bool(row["blocked_bot"]),
            banned=bool(row["banned"]),
            last_synced_at=from_iso(row["last_synced_at"]),
            sub_trial_end=from_iso(row["sub_trial_end"]),
            sub_period_start=from_iso(row["sub_period_start"]),
            past_due_since=from_iso(row["past_due_since"]),
        )
