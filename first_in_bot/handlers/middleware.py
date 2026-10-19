"""Outer middleware for private messages and button presses."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from ..users import clear_blocked_bot


class UnblockMiddleware(BaseMiddleware):
    """A 403 sets users.blocked_bot and stops every DM to them, including the
    pre-charge trial reminder. Anyone who writes to us or presses a button in a
    private chat has clearly unblocked the bot, so clear it here, not only on /start.
    If they're somehow still blocked, the next 403 sets the flag again."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        svc = data.get("svc")
        if user is not None and chat is not None and chat.type == "private" and svc is not None:
            await clear_blocked_bot(svc.db, user.id, svc.now())
        return await handler(event, data)
