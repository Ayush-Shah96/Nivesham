"""Ops Tracker API backed by the existing Supabase tables.

Authentication is shared with the Intelligence System. ADMIN users are Team
Leads; ANALYST and INVESTMENT users are Team Members. Member/login links live
in the persistent JSON link store, so this module requires no DB migration.
"""
from __future__ import annotations

import calendar
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.auth import get_current_user
from apps.ops_tracker import link_store
from ii_platform.database.connection import get_db
from ii_platform.database.models import User, UserRole
from infrastructure.security.permissions import require_role

router = APIRouter(prefix="/ops-tracker", tags=["ops-tracker"])

# This is the public Supabase anon key already used by the supplied Ops Tracker
# HTML. Environment overrides are supported for deployment configuration.
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://fzqwjcuxrpdhtqwmjkzu.supabase.co").rstrip("/")
SUPABASE_API_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_ANON_KEY")
    or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZ6cXdqY3V4cnBkaHRxd21qa3p1Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQwMjEwODEsImV4cCI6MjA4OTU5NzA4MX0.UYnszIP5NkJ7u5ih26jq41zjcrJd_U31YOnbi773UV8"
).strip()


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TeamPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)


class MemberIn(BaseModel):
    team_id: str
    name: str = Field(min_length=1, max_length=120)
    level: int = Field(default=1, ge=0, le=9)
    extra_teams: list[str] = Field(default_factory=list)


class MemberPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    level: int | None = Field(default=None, ge=0, le=9)
    team_id: str | None = None
    extra_teams: list[str] | None = None


class TaskIn(BaseModel):
    team_id: str
    name: str = Field(min_length=1, max_length=500)
    member_id: str | None = None
    backup_member_id: str | None = None
    notes: str | None = None
    due_date: date | None = None
    freq: str = "once"
    priority: bool = False
    approval_mode: str = "lead"


class TaskPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    member_id: str | None = None
    backup_member_id: str | None = None
    notes: str | None = None
    due_date: date | None = None
    freq: str | None = None
    priority: bool | None = None
    done: bool | None = None
    pending_approval: bool | None = None
    approval_stage: int | None = None


class LinkIn(BaseModel):
    user_id: int | None = None


def _is_lead(user: User) -> bool:
    return user.role == UserRole.ADMIN


def _role_label(user: User) -> str:
    return "Team Lead" if _is_lead(user) else "Team Member"


def _clean_extra(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    return [str(x) for x in value if x] if isinstance(value, list) else []


def _title(value: str) -> str:
    return " ".join(part[:1].upper() + part[1:] for part in value.strip().split())


def _uid() -> str:
    return uuid4().hex


async def _supabase(method: str, table: str, *, params: dict[str, str] | None = None,
                    payload: dict | list | None = None) -> Any:
    if not SUPABASE_URL or not SUPABASE_API_KEY:
        raise HTTPException(status_code=503, detail="Ops Tracker Supabase configuration is missing")
    headers = {
        "apikey": SUPABASE_API_KEY,
        "Authorization": f"Bearer {SUPABASE_API_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as client:
            response = await client.request(
                method,
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers=headers,
                params=params or {},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Ops Tracker database connection failed: {exc}") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("message") or response.json().get("details") or response.text
        except Exception:
            detail = response.text
        raise HTTPException(status_code=502, detail=f"Ops Tracker database error: {detail[:500]}")
    if not response.content:
        return []
    try:
        return response.json()
    except ValueError:
        return []


async def _rows(table: str, **params: str) -> list[dict]:
    # Match the original tracker pagination so larger workspaces are not silently
    # truncated at PostgREST's default 1,000-row response limit.
    if "limit" in params:
        return await _supabase("GET", table, params={"select": "*", **params})
    result: list[dict] = []
    for offset in range(0, 100_000, 1_000):
        batch = await _supabase(
            "GET", table,
            params={"select": "*", **params, "limit": "1000", "offset": str(offset)},
        )
        result.extend(batch or [])
        if len(batch or []) < 1_000:
            break
    return result


async def _one(table: str, row_id: str) -> dict:
    rows = await _rows(table, id=f"eq.{row_id}", limit="1")
    if not rows:
        raise HTTPException(status_code=404, detail=f"{table[:-1].title()} not found")
    return rows[0]


async def _insert(table: str, row: dict) -> dict:
    rows = await _supabase("POST", table, payload=row)
    if not rows:
        raise HTTPException(status_code=502, detail=f"Could not create {table[:-1]}")
    return rows[0]


async def _patch(table: str, row_id: str, patch: dict) -> dict:
    rows = await _supabase("PATCH", table, params={"id": f"eq.{row_id}"}, payload=patch)
    if not rows:
        raise HTTPException(status_code=404, detail=f"{table[:-1].title()} not found")
    return rows[0]


async def _delete(table: str, **filters: str) -> None:
    await _supabase("DELETE", table, params=filters)


def _member_teams(member: dict) -> list[str]:
    return list(dict.fromkeys([*([str(member.get("team_id"))] if member.get("team_id") else []), *_clean_extra(member.get("extra_teams"))]))


async def _users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.username))
    return list(result.scalars().all())


async def _ensure_member_link(user: User, db: AsyncSession, members: list[dict] | None = None) -> str | None:
    """Auto-link a non-admin login to one unambiguous existing member row."""
    if _is_lead(user):
        return None
    links = link_store.read_links()
    existing = next((mid for mid, uid in links.items() if uid == user.id), None)
    if existing:
        return existing
    members = members if members is not None else await _rows("members")
    users = await _users(db)
    taken = set(links.values())
    by_username = {u.username.strip().casefold(): u for u in users if u.role != UserRole.ADMIN}
    # Only exact username/name matches are automatic; ambiguous names are left for admin review.
    matches = [m for m in members if (m.get("name") or "").strip().casefold() == user.username.strip().casefold()]
    if len(matches) == 1 and user.id not in taken:
        member_id = str(matches[0]["id"])
        if member_id not in links and by_username.get(user.username.strip().casefold()):
            await link_store.update_links({member_id: user.id})
            return member_id
    return None


async def _auto_link(db: AsyncSession) -> dict:
    users = [u for u in await _users(db) if u.role != UserRole.ADMIN]
    members = await _rows("members")
    links = link_store.read_links()
    taken = set(links.values())
    linked: list[str] = []
    skipped: list[str] = []
    changes: dict[str, int] = {}
    member_name_counts: dict[str, int] = {}
    user_name_counts: dict[str, int] = {}
    for member in members:
        key = (member.get("name") or "").strip().casefold()
        if key:
            member_name_counts[key] = member_name_counts.get(key, 0) + 1
    for login in users:
        key = login.username.strip().casefold()
        user_name_counts[key] = user_name_counts.get(key, 0) + 1
    for member in members:
        member_id = str(member["id"])
        name = (member.get("name") or "").strip().casefold()
        if member_id in links or not name:
            continue
        matches = [u for u in users if u.username.strip().casefold() == name and u.id not in taken]
        if len(matches) == 1 and member_name_counts.get(name) == 1 and user_name_counts.get(name) == 1:
            changes[member_id] = matches[0].id
            taken.add(matches[0].id)
            linked.append(member.get("name") or name)
        elif any(u.username.strip().casefold() == name for u in users):
            skipped.append(member.get("name") or name)
    if changes:
        await link_store.update_links(changes)
    return {"linked": linked, "skipped": skipped}


def _team_out(row: dict) -> dict:
    return {"id": str(row["id"]), "name": row.get("name") or "", "sort_order": row.get("sort_order") or 0}


def _member_out(row: dict, user_id: int | None = None) -> dict:
    return {
        "id": str(row["id"]), "team_id": str(row.get("team_id") or ""),
        "name": row.get("name") or "", "level": int(row.get("level") or 1),
        "extra_teams": _clean_extra(row.get("extra_teams")), "user_id": user_id,
        "pending_name": row.get("pending_name"),
    }


def _task_out(row: dict) -> dict:
    return {
        "id": str(row["id"]), "team_id": str(row.get("team_id") or ""),
        "member_id": str(row["member_id"]) if row.get("member_id") else None,
        "backup_member_id": str(row["backup_member_id"]) if row.get("backup_member_id") else None,
        "name": row.get("name") or "", "notes": row.get("notes") or "",
        "due_date": str(row["due_date"]).split("T")[0] if row.get("due_date") else None,
        "freq": row.get("freq") or "once", "priority": bool(row.get("priority")),
        "done": bool(row.get("done")), "pending_approval": bool(row.get("pending_approval")),
        "approval_mode": row.get("approval_mode") or "lead", "approval_stage": int(row.get("approval_stage") or 0),
        "completed_at": row.get("completed_at"), "pending_due": row.get("pending_due"),
        "is_personal": bool(row.get("is_personal")), "owner_id": row.get("owner_id"),
        "created_by_member_id": row.get("created_by_member_id"), "sort_order": row.get("sort_order") or 0,
    }


async def _identity(user: User, db: AsyncSession) -> tuple[str, str | None]:
    if _is_lead(user):
        return "Team Lead", None
    members = await _rows("members")
    member_id = await _ensure_member_link(user, db, members)
    if not member_id:
        raise HTTPException(
            status_code=403,
            detail="Your Intelligence System account is not linked to an Ops Tracker member. Ask an admin to link your existing member record.",
        )
    return "Team Member", member_id


@router.get("/me")
async def ops_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    role, member_id = await _identity(user, db)
    return {"user_id": user.id, "username": user.username, "system_role": user.role.value, "ops_role": role, "member_id": member_id}


@router.get("/board")
async def get_board(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    role, member_id = await _identity(user, db)
    teams = await _rows("teams", order="sort_order.asc.nullslast,name.asc")
    members = await _rows("members", order="name.asc")
    tasks = await _rows("tasks", order="created_at.desc.nullslast")
    if role == "Team Lead":
        await _auto_link(db)
    links = await link_store.current_links({str(m["id"]) for m in members})
    member_by_id = {str(m["id"]): m for m in members}
    if role == "Team Member":
        own = member_by_id.get(str(member_id))
        if not own:
            raise HTTPException(status_code=403, detail="Your linked Ops Tracker member record no longer exists.")
        team_ids = set(_member_teams(own))
        teams = [t for t in teams if str(t["id"]) in team_ids]
        visible_members = [m for m in members if set(_member_teams(m)) & team_ids]
        tasks = [t for t in tasks if str(t.get("team_id") or "") in team_ids]
        tasks += [t for t in await _rows("tasks", is_personal="eq.true", owner_id=f"eq.{member_id}") if str(t.get("id")) not in {str(x.get("id")) for x in tasks}]
        members = visible_members
    return {
        "role": role,
        "current_member_id": member_id,
        "teams": [_team_out(t) for t in teams],
        "members": [_member_out(m, links.get(str(m["id"]))) for m in members],
        "tasks": [_task_out(t) for t in tasks],
    }


@router.get("/login-users")
async def login_users(db: AsyncSession = Depends(get_db), _lead: User = Depends(require_role(UserRole.ADMIN))):
    return [
        {"id": u.id, "username": u.username, "role": u.role.value, "ops_role": "Team Member"}
        for u in await _users(db) if u.role != UserRole.ADMIN
    ]


@router.post("/members/auto-link")
async def auto_link_members(db: AsyncSession = Depends(get_db), _lead: User = Depends(require_role(UserRole.ADMIN))):
    return await _auto_link(db)


@router.post("/members/{member_id}/link")
async def link_member(member_id: str, payload: LinkIn, db: AsyncSession = Depends(get_db), _lead: User = Depends(require_role(UserRole.ADMIN))):
    await _one("members", member_id)
    if payload.user_id is not None:
        user = await db.get(User, payload.user_id)
        if user is None or not user.is_active or user.role == UserRole.ADMIN:
            raise HTTPException(status_code=404, detail="Active Team Member login not found")
        links = link_store.read_links()
        owner = next((mid for mid, uid in links.items() if uid == payload.user_id and mid != member_id), None)
        if owner:
            raise HTTPException(status_code=409, detail=f"This login is already linked to another Ops Tracker member ({owner}).")
    await link_store.update_links({member_id: payload.user_id})
    return {"id": member_id, "user_id": payload.user_id}


# Team management is Team Lead-only.
@router.post("/teams", status_code=201)
async def create_team(payload: TeamIn, _lead: User = Depends(require_role(UserRole.ADMIN))):
    return _team_out(await _insert("teams", {"id": _uid(), "name": _title(payload.name), "sort_order": 0}))


@router.patch("/teams/{team_id}")
async def update_team(team_id: str, payload: TeamPatch, _lead: User = Depends(require_role(UserRole.ADMIN))):
    await _one("teams", team_id)
    patch = {"name": _title(payload.name)} if payload.name is not None else {}
    return _team_out(await _patch("teams", team_id, patch)) if patch else _team_out(await _one("teams", team_id))


@router.delete("/teams/{team_id}")
async def delete_team(team_id: str, _lead: User = Depends(require_role(UserRole.ADMIN))):
    await _one("teams", team_id)
    # Preserve members that belong to other teams; remove only this association.
    members = await _rows("members")
    for member in members:
        extras = _clean_extra(member.get("extra_teams"))
        if str(member.get("team_id") or "") == team_id:
            if extras:
                await _patch("members", str(member["id"]), {"team_id": extras[0], "extra_teams": json.dumps(extras[1:])})
            else:
                await _patch("members", str(member["id"]), {"team_id": None, "extra_teams": "[]"})
        elif team_id in extras:
            await _patch("members", str(member["id"]), {"extra_teams": json.dumps([x for x in extras if x != team_id])})
    await _delete("tasks", team_id=f"eq.{team_id}")
    await _delete("teams", id=f"eq.{team_id}")
    return {"ok": True}


@router.post("/members", status_code=201)
async def create_member(payload: MemberIn, _lead: User = Depends(require_role(UserRole.ADMIN))):
    await _one("teams", payload.team_id)
    return _member_out(await _insert("members", {
        "id": _uid(), "team_id": payload.team_id, "name": _title(payload.name),
        "level": payload.level, "extra_teams": json.dumps(payload.extra_teams),
    }))


@router.patch("/members/{member_id}")
async def update_member(member_id: str, payload: MemberPatch, _lead: User = Depends(require_role(UserRole.ADMIN))):
    current = await _one("members", member_id)
    data = payload.model_dump(exclude_unset=True)
    patch: dict[str, Any] = {}
    if data.get("name") is not None:
        patch["name"] = _title(data["name"])
    if data.get("level") is not None:
        patch["level"] = data["level"]
    if data.get("team_id") is not None:
        await _one("teams", data["team_id"])
        patch["team_id"] = data["team_id"]
    if data.get("extra_teams") is not None:
        patch["extra_teams"] = json.dumps(data["extra_teams"])
    row = await _patch("members", member_id, patch) if patch else current
    links = link_store.read_links()
    return _member_out(row, links.get(member_id))


@router.delete("/members/{member_id}")
async def delete_member(member_id: str, _lead: User = Depends(require_role(UserRole.ADMIN))):
    await _one("members", member_id)
    await _supabase("PATCH", "tasks", params={"member_id": f"eq.{member_id}"}, payload={"member_id": None})
    await _supabase("PATCH", "tasks", params={"backup_member_id": f"eq.{member_id}"}, payload={"backup_member_id": None})
    await _delete("members", id=f"eq.{member_id}")
    await link_store.update_links({member_id: None})
    return {"ok": True}


def _next_due(due: str | None, freq: str) -> str | None:
    if not due:
        return date.today().isoformat()
    current = date.fromisoformat(str(due)[:10])
    if freq in {"daily", "weekly", "biweekly"}:
        days = {"daily": 1, "weekly": 7, "biweekly": 14}[freq]
        return (current + timedelta(days=days)).isoformat()
    months = {"monthly": 1, "quarterly": 3, "annual": 12}.get(freq)
    if not months:
        return None
    month_index = current.month - 1 + months
    year = current.year + month_index // 12
    month = month_index % 12 + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


async def _spawn_recurring(task: dict) -> None:
    freq = task.get("freq") or "once"
    if freq == "once" or task.get("spawned_child_id"):
        return
    next_due = _next_due(task.get("due_date"), freq)
    if not next_due:
        return
    child_id = _uid()
    await _insert("tasks", {
        "id": child_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "team_id": task.get("team_id"), "member_id": task.get("member_id"),
        "backup_member_id": task.get("backup_member_id"), "name": task.get("name"),
        "notes": task.get("notes"), "due_date": next_due, "freq": freq,
        "is_personal": bool(task.get("is_personal")), "owner_id": task.get("owner_id"),
        "created_by_member_id": task.get("created_by_member_id"), "done": False,
        "pending_approval": False, "approval_mode": task.get("approval_mode") or "lead",
        "approval_stage": 0, "priority": bool(task.get("priority")), "sort_order": task.get("sort_order") or 0,
    })
    await _patch("tasks", str(task["id"]), {"spawned_child_id": child_id})


@router.post("/tasks", status_code=201)
async def create_task(payload: TaskIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    role, member_id = await _identity(user, db)
    if role != "Team Lead":
        raise HTTPException(status_code=403, detail="Only Team Leads can create team tasks.")
    await _one("teams", payload.team_id)
    if payload.member_id:
        await _one("members", payload.member_id)
    if payload.backup_member_id:
        await _one("members", payload.backup_member_id)
    row = await _insert("tasks", {
        "id": _uid(), "created_at": datetime.now(timezone.utc).isoformat(),
        "team_id": payload.team_id, "member_id": payload.member_id,
        "backup_member_id": payload.backup_member_id, "name": payload.name.strip(),
        "notes": payload.notes or None, "due_date": payload.due_date.isoformat() if payload.due_date else None,
        "freq": payload.freq or "once", "priority": payload.priority, "is_personal": False,
        "done": False, "pending_approval": False, "approval_mode": payload.approval_mode or "lead",
        "approval_stage": 0, "created_by_member_id": None, "sort_order": 0,
    })
    return _task_out(row)


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, payload: TaskPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    role, member_id = await _identity(user, db)
    current = await _one("tasks", task_id)
    data = payload.model_dump(exclude_unset=True)
    patch: dict[str, Any] = {}
    if role == "Team Member":
        if str(current.get("member_id") or "") != str(member_id) and str(current.get("backup_member_id") or "") != str(member_id):
            raise HTTPException(status_code=403, detail="You can only update tasks assigned to you or assigned to you as backup.")
        forbidden = set(data) - {"done"}
        if forbidden:
            raise HTTPException(status_code=403, detail="Team Members can only submit task completion for approval.")
        if data.get("done") is True:
            if current.get("done"):
                return _task_out(current)
            patch.update({"pending_approval": True, "approval_stage": 1, "done": False})
        elif data.get("done") is False:
            raise HTTPException(status_code=403, detail="Only a Team Lead can reopen a completed task.")
    else:
        if "name" in data:
            if not data["name"]:
                raise HTTPException(status_code=422, detail="Task name cannot be empty")
            patch["name"] = data["name"].strip()
        for field in ("member_id", "backup_member_id"):
            if field in data:
                if data[field]:
                    await _one("members", data[field])
                patch[field] = data[field] or None
        for field in ("notes", "freq", "priority", "pending_approval", "approval_stage"):
            if field in data:
                patch[field] = data[field]
        if "due_date" in data:
            patch["due_date"] = data["due_date"].isoformat() if data["due_date"] else None
        if data.get("done") is True:
            patch.update({"done": True, "pending_approval": False, "approval_stage": 0,
                          "completed_at": datetime.now(timezone.utc).isoformat(), "pending_due": None})
        elif data.get("done") is False:
            patch.update({"done": False, "pending_approval": False, "approval_stage": 0, "completed_at": None})
        if data.get("pending_approval") is False:
            patch.update({"pending_approval": False, "approval_stage": 0})
    row = await _patch("tasks", task_id, patch) if patch else current
    if role == "Team Lead" and patch.get("done") is True:
        await _spawn_recurring(row)
        row = await _one("tasks", task_id)
    return _task_out(row)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, _lead: User = Depends(require_role(UserRole.ADMIN))):
    await _one("tasks", task_id)
    await _delete("tasks", id=f"eq.{task_id}")
    return {"ok": True}
