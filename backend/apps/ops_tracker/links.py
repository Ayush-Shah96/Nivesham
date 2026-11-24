"""Map Ops Tracker members to system login users.

Links are stored in the backend's JSON link file (see link_store.py), so
neither Supabase nor the system database is changed. Only admins can link
or unlink members.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.auth import get_current_user
from ii_platform.database.connection import get_db
from ii_platform.database.models import User, UserRole
from infrastructure.security.permissions import require_role

from . import link_store
from . import service as sb

router = APIRouter(prefix="/ops-tracker", tags=["ops-tracker"])


class LinkIn(BaseModel):
    user_id: int | None = None


def _role(user: User) -> str:
    return user.role.value if hasattr(user.role, "value") else str(user.role)


async def _active_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.username))
    return list(result.scalars().all())


@router.get("/login-users")
async def login_users(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    return [{"id": u.id, "username": u.username, "role": _role(u)} for u in await _active_users(db)]


@router.post("/members/{member_id}/link")
async def link_member(
    member_id: str,
    payload: LinkIn,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_role(UserRole.ADMIN)),
):
    await sb.get_or_404(sb.MEMBERS, member_id, "Member")

    if payload.user_id is not None:
        user = await db.get(User, payload.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=404, detail="Login user not found")
        links = link_store.read_links()
        owner_id = next((mid for mid, uid in links.items() if uid == payload.user_id and mid != member_id), None)
        if owner_id:
            members = await sb.list_rows(sb.MEMBERS)
            owner_name = next((m.get("name") for m in members if str(m["id"]) == owner_id), "another member")
            raise HTTPException(status_code=409, detail=f"{user.username} is already linked to {owner_name}")

    await link_store.update_links({member_id: payload.user_id})
    return {"id": member_id, "user_id": payload.user_id}


@router.post("/members/auto-link")
async def auto_link_members(db: AsyncSession = Depends(get_db), _admin=Depends(require_role(UserRole.ADMIN))):
    """Link unlinked members whose name matches exactly one login username (case-insensitive)."""
    users = await _active_users(db)
    members = await sb.list_rows(sb.MEMBERS)
    links = link_store.read_links()
    taken = set(links.values())

    changes: dict[str, int] = {}
    linked, skipped = [], []
    for member in members:
        mid = str(member["id"])
        name = (member.get("name") or "").strip()
        if mid in links or not name:
            continue
        keys = {name.lower(), name.split()[0].lower(), name.replace(" ", "").lower()}
        matches = [u for u in users if u.username.lower() in keys]
        if len(matches) == 1 and matches[0].id not in taken:
            changes[mid] = matches[0].id
            taken.add(matches[0].id)
            linked.append(name)
        else:
            skipped.append(name)

    if changes:
        await link_store.update_links(changes)
    return {"linked": linked, "skipped": skipped}
