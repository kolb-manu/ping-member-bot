"""Admin commands: /stats, /user, /grant, /dryrun. Only for ADMIN_IDS; anyone
else gets no reply at all."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .. import admin
from ..services import Services
from ..sweep import format_report, sweep_removals
from ..timeutil import format_local
from ..users import find_user

router = Router(name="admin")
router.message.filter(F.chat.type == "private")

NO_SUCH_USER = "No user found for {ref}. They need to /start the bot first."


def is_admin(message: Message, svc: Services) -> bool:
    return message.from_user is not None and message.from_user.id in svc.config.admin_ids


@router.message(Command("stats"))
async def cmd_stats(message: Message, svc: Services) -> None:
    if not is_admin(message, svc):
        return
    await message.answer(await admin.stats_text(svc.db, svc.now()))


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, svc: Services) -> None:
    if not is_admin(message, svc):
        return
    ref = (command.args or "").strip()
    if not ref:
        await message.answer("Usage: /user &lt;telegram_id or @username&gt;")
        return
    user = await find_user(svc.db, ref)
    if user is None:
        await message.answer(NO_SUCH_USER.format(ref=html.escape(ref)))
        return
    await message.answer(await admin.user_text(svc.db, user, svc.config.timezone))


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, svc: Services) -> None:
    if not is_admin(message, svc) or message.from_user is None:
        return
    parts = (command.args or "").split()
    usage = "Usage: /grant &lt;telegram_id or @username&gt; &lt;days&gt; (0 clears)"
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(usage)
        return
    user = await find_user(svc.db, parts[0])
    if user is None:
        await message.answer(NO_SUCH_USER.format(ref=html.escape(parts[0])))
        return
    try:
        user = await admin.grant(svc.db, user, int(parts[1]), svc.now(), by=message.from_user.id)
    except ValueError as exc:
        await message.answer(html.escape(str(exc)))
        return
    if user.manual_grant_until is None:
        await message.answer(f"Grant cleared for {user.telegram_id}.")
    else:
        until = format_local(user.manual_grant_until, svc.config.timezone)
        await message.answer(f"✅ {user.telegram_id} has free access until {until}.")


@router.message(Command("dryrun"))
async def cmd_dryrun(message: Message, svc: Services) -> None:
    if not is_admin(message, svc):
        return
    report = await sweep_removals(svc, svc.now(), force_dry_run=True)
    await message.answer(format_report(report))
