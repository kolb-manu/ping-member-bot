"""Admin commands: /stats, /report, /user, /grant, /revoke, /ban, /unban, /dryrun, /confirmsweep,
/pause and /resume. Only for ADMIN_IDS; anyone else gets no reply at all.

A @username that several stored users share is refused with the list of them,
so a command never lands on someone who merely used to have that name."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .. import admin
from ..models import User
from ..report import report_text
from ..scheduler import JobHealthStore
from ..services import Services
from ..sweep import _fit, confirm_sweep, format_report, offer_in_reply, sweep_removals
from ..timeutil import format_local
from ..users import find_users

router = Router(name="admin")
router.message.filter(F.chat.type == "private")

NO_SUCH_USER = "No user found for {ref}. They need to /start the bot first."


def is_admin(message: Message, svc: Services) -> bool:
    return message.from_user is not None and message.from_user.id in svc.config.admin_ids


async def _one_user(message: Message, svc: Services, ref: str) -> User | None:
    """The one user ref names, or None once the admin has been told why not
    (nobody has it, or several stored users share that @username)."""
    users = await find_users(svc.db, ref)
    if not users:
        await message.answer(NO_SUCH_USER.format(ref=html.escape(ref)))
        return None
    if len(users) > 1:
        await message.answer(admin.ambiguous_text(ref, users, svc.config.timezone))
        return None
    return users[0]


@router.message(Command("stats"))
async def cmd_stats(message: Message, svc: Services) -> None:
    if not is_admin(message, svc):
        return
    await message.answer(await admin.stats_text(svc.db, svc.now()))


@router.message(Command("report"))
async def cmd_report(message: Message, svc: Services) -> None:
    """The daily report on demand. Unlike the 09:00 one it resets no failure counts."""
    if not is_admin(message, svc):
        return
    health = await JobHealthStore(svc.db).snapshot()
    await message.answer(_fit(await report_text(svc, svc.now(), health)))


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, svc: Services) -> None:
    if not is_admin(message, svc):
        return
    ref = (command.args or "").strip()
    if not ref:
        await message.answer("Usage: /user &lt;telegram_id or @username&gt;")
        return
    user = await _one_user(message, svc, ref)
    if user is None:
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
    user = await _one_user(message, svc, parts[0])
    if user is None:
        return
    try:
        user = await admin.grant(svc.db, user, int(parts[1]), svc.now(), by=message.from_user.id)
    except ValueError as exc:
        await message.answer(html.escape(str(exc)))
        return
    name = html.escape(admin.who(user))
    if user.manual_grant_until is None:
        await message.answer(f"Grant cleared for {name}.")
    else:
        until = format_local(user.manual_grant_until, svc.config.timezone)
        await message.answer(f"✅ {name} has free access until {until}.")


async def _target_command(message: Message, command: CommandObject, svc: Services, name: str, action) -> None:
    """Shared shape of /revoke, /ban and /unban: <id or @username>, then action(svc, user, now, by)."""
    if not is_admin(message, svc) or message.from_user is None:
        return
    ref = (command.args or "").strip()
    if not ref or len(ref.split()) != 1:
        await message.answer(f"Usage: /{name} &lt;telegram_id or @username&gt;")
        return
    user = await _one_user(message, svc, ref)
    if user is None:
        return
    try:
        text = await action(svc, user, svc.now(), message.from_user.id)
    except admin.AdminTarget as exc:
        text = str(exc)
    await message.answer(html.escape(text, quote=False))


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, svc: Services) -> None:
    await _target_command(message, command, svc, "revoke", admin.revoke)


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, svc: Services) -> None:
    await _target_command(message, command, svc, "ban", admin.ban)


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, svc: Services) -> None:
    await _target_command(message, command, svc, "unban", admin.unban)


@router.message(Command("dryrun"))
async def cmd_dryrun(message: Message, svc: Services) -> None:
    if not is_admin(message, svc):
        return
    report = await sweep_removals(svc, svc.now(), force_dry_run=True)
    await message.answer(format_report(report))


@router.message(Command("confirmsweep"))
async def cmd_confirmsweep(message: Message, svc: Services) -> None:
    if not is_admin(message, svc) or message.from_user is None:
        return
    result = await confirm_sweep(svc, svc.now(), by=message.from_user.id)
    await message.answer(result.text)
    if result.offer is not None:
        # Only once the owner has the new report: an approval must match what they saw.
        await offer_in_reply(svc, result.offer, svc.now())


@router.message(Command("pause"))
async def cmd_pause(message: Message, svc: Services) -> None:
    if not is_admin(message, svc) or message.from_user is None:
        return
    await message.answer(await admin.pause(svc, svc.now(), by=message.from_user.id))


@router.message(Command("resume"))
async def cmd_resume(message: Message, svc: Services) -> None:
    if not is_admin(message, svc) or message.from_user is None:
        return
    await message.answer(await admin.resume(svc, svc.now(), by=message.from_user.id))
