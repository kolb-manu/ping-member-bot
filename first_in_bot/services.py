"""The shared pieces every job and handler needs, bundled into one object."""
from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import datetime
from typing import Callable

from .config import Config
from .db import Database
from .entitlement import is_entitled
from .models import User
from .stripe_gateway import StripeGateway
from .telegram_gateway import TelegramGateway
from .timeutil import utcnow


class PriceLabel:
    """The price as users see it, e.g. "£2.99/month", or None while it couldn't be
    read from Stripe. Services is frozen, so this small holder is the one thing that
    changes: billing.refresh_price fills it in later without a restart, and every
    reader (handlers included) goes through svc.price_label to see the new value."""

    __slots__ = ("label",)

    def __init__(self, label: str | None = None) -> None:
        self.label = label


@dataclass(frozen=True)
class Services:
    db: Database
    config: Config
    tg: TelegramGateway
    stripe: StripeGateway | None = None   # None when Stripe isn't configured
    # The price read at startup. Only a constructor argument: it seeds `price`, and
    # svc.price_label (defined below the class) always reads the holder.
    price_label: InitVar[str | None] = None
    clock: Callable[[], datetime] = utcnow  # tests pin this
    price: PriceLabel = field(default=None, compare=False)  # type: ignore[assignment]  # set in __post_init__

    def __post_init__(self, price_label: str | None) -> None:
        if self.price is None:   # dataclasses.replace() passes the existing holder on
            object.__setattr__(self, "price", PriceLabel(price_label))

    def now(self) -> datetime:
        return self.clock()

    @property
    def owner_id(self) -> int | None:
        return self.config.admin_ids[0] if self.config.admin_ids else None

    def entitled(self, user: User, now: datetime) -> bool:
        return is_entitled(user, now, self.config.past_due_grace)


# Outside the class body: inside it, the dataclass would take this property for the
# default value of the price_label argument.
Services.price_label = property(  # type: ignore[assignment, method-assign]
    lambda self: self.price.label,
    doc='The current price label, e.g. "£2.99/month"; None while it is unknown.',
)
