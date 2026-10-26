"""Admin lookups by ID or @username, and keeping stored usernames current so a
lookup never lands on someone who merely used to have that name."""
from datetime import datetime, timezone

from first_in_bot.access import handle_member_update
from first_in_bot.users import ensure_user, find_users, get_user, register_user
from tests.fakes import NOW, add_user, audit_actions

SEPT = datetime(2026, 9, 1, tzinfo=timezone.utc)
OCT = datetime(2026, 10, 1, tzinfo=timezone.utc)


async def ids(db, ref):
    return [u.telegram_id for u in await find_users(db, ref)]


async def test_find_users_by_id_or_username(db):
    await add_user(db, 5, username="Sam_X")
    assert await ids(db, "5") == [5]
    assert await ids(db, "@sam_x") == [5] and await ids(db, "SAM_X") == [5]
    assert await ids(db, "6") == [] and await ids(db, "@nobody") == [] and await ids(db, "@") == []


async def test_start_takes_the_username_off_a_stale_row(db):
    # A (October, paying) was @sam and renamed without the bot seeing it. B (September)
    # now has @sam and presses /start: @sam must mean B, never A.
    await add_user(db, 2, username="bob", created_at=SEPT)
    await add_user(db, 1, username="sam", created_at=OCT, sub_status="active")
    await register_user(db, 2, "Sam", "B", None, NOW)
    assert await ids(db, "@sam") == [2]
    assert (await get_user(db, 1)).username is None
    assert "username_cleared" in await audit_actions(db, 1)
    assert "username_cleared" not in await audit_actions(db, 2)


async def test_a_new_user_claims_a_username_too(db):
    await add_user(db, 1, username="sam")
    await register_user(db, 3, "sam", "C", None, NOW)
    assert await ids(db, "@sam") == [3]


async def test_member_updates_keep_names_current_and_claim_the_username(svc):
    await add_user(svc.db, 1, username="sam", created_at=OCT)
    await add_user(svc.db, 2, username="bob", created_at=SEPT)
    # A's own update shows the new name; B's shows they now have @sam.
    await handle_member_update(svc, 1, "sammy", "Sam", "member", None, NOW)
    await handle_member_update(svc, 2, "sam", "B", "member", None, NOW)
    assert (await get_user(svc.db, 1)).username == "sammy"
    assert (await get_user(svc.db, 2)).username == "sam" and (await get_user(svc.db, 2)).first_name == "B"
    assert await ids(svc.db, "@sam") == [2] and await ids(svc.db, "@sammy") == [1]


async def test_member_update_claims_from_a_row_it_never_heard_from(svc):
    await add_user(svc.db, 1, username="sam")
    await handle_member_update(svc, 2, "SAM", "B", "member", None, NOW)    # creates 2, case doesn't matter
    assert await ids(svc.db, "@sam") == [2] and (await get_user(svc.db, 1)).username is None


async def test_unchanged_names_write_no_audit_rows(db):
    await add_user(db, 1, username="sam")
    before = await audit_actions(db)
    await ensure_user(db, 1, "sam", "Name", NOW)
    await register_user(db, 1, "sam", "Name", None, NOW)
    assert await audit_actions(db) == before


async def test_lookup_returns_every_row_still_sharing_a_username(db):
    # Rows from before usernames were claimed can still share one: return them all.
    await add_user(db, 1, username="sam")
    await add_user(db, 2, username="bob")
    async with db.tx() as conn:
        await conn.execute("UPDATE users SET username='sam' WHERE telegram_id=2")
    assert await ids(db, "@sam") == [1, 2]
