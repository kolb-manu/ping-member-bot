"""Join requests and member updates for the paid channel. Updates about any
other chat are ignored."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import ChatJoinRequest, ChatMemberUpdated

from ..access import handle_join_request, handle_member_update
from ..services import Services

router = Router(name="channel")


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, svc: Services) -> None:
    if request.chat.id != svc.config.paid_channel_id:
        return
    await handle_join_request(svc, request.from_user.id, request.user_chat_id, svc.now())


@router.chat_member()
async def on_chat_member(update: ChatMemberUpdated, svc: Services) -> None:
    if update.chat.id != svc.config.paid_channel_id:
        return
    member = update.new_chat_member
    if member.user.is_bot:
        return  # e.g. the alert bot; bots aren't customers
    status = getattr(member.status, "value", member.status)
    await handle_member_update(
        svc,
        member.user.id,
        member.user.username,
        member.user.first_name,
        str(status),
        getattr(member, "is_member", None),
        svc.now(),
    )
