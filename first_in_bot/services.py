"""The shared pieces every job and handler needs, bundled into one object."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import Config
from .db import Database
from .entitlement import is_entitled
from .models import User
from .stripe_gateway import StripeGateway
from .telegram_gateway import TelegramGateway
from .timeutil import utcnow


@dataclass(frozen=True)
class Services:
    db: Database
    config: Config
    tg: TelegramGateway
    stripe: StripeGateway | None = None   # None when Stripe isn't configured
    price_label: str | None = None        # e.g. "£2.99/month", read from Stripe at startup
    clock: Callable[[], datetime] = utcnow  # tests pin this

    def now(self) -> datetime:
        return self.clock()

    @property
    def owner_id(self) -> int | None:
        return self.config.admin_ids[0] if self.config.admin_ids else None

    def entitled(self, user: User, now: datetime) -> bool:
        return is_entitled(user, now, self.config.past_due_grace)
