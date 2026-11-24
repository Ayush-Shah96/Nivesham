from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apps.auth import get_current_user

from . import link_store
from . import service as sb

router = APIRouter(prefix="/ops-tracker", tags=["ops-tracker"])

PRIORITIES = {"low", "medium", "high"}


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TeamPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)


class MemberIn(BaseModel):
    team_id: str
    name: str = Field(min_length=1, max_length=120)
    level: int = Field(default=1, ge=0, le=9)
    extra_teams: list[str] = Field(default_factory=list)


class MemberPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    level: Optional[int] = Field(default=None, ge=0, le=9)
    team_id: Optional[str] = None
    extra_teams: Optional[list[str]] = None


class TaskIn(BaseModel):
    team_id: str
    name: str = Field(min_length=1, max_length=500)
    member_id: Optional[str] = None
    priority: str = "medium"
    due_date: Optional[date] = None
    notes: Optional[str] = None


class TaskPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=500)
    member_id: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[date] = None
    notes: Optional[str] = None
    done: Optional[bool] = None


# ── helpers ──────────────────────────────────────────────────────────────────

def _new_id() -> str:
    return uuid4().hex


def _sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:]


def _title(text: str) -> str:
    return " ".join(w[:1].upper() + w[1:].lower() for w in text.split())


def _check_priority(value: str) -> None:
    if value not in PRIORITIES:
        raise HTTPException(status_code=422, detail="priority must be low, medium or high")


def _extra_teams(value: Any) -> list[str]:
    # The old tracker stored extra_teams as a JSON string; accept both forms.
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [str(x) for x in value] if isinstance(value, list) else []


def _team_out(r: dict) -> dict:
    return {"id": str(r["id"]), "name": r.get("name") or ""}


def _member_out(r: dict) -> dict:
    return {
        "id": str(r["id"]),
        "team_id": str(r.get("team_id") or ""),
        "name": r.get("name") or "",
        "level": int(r.get("level") or 1),
        "extra_teams": _extra_teams(r.get("extra_teams")),
        "user_id": int(r["user_id"]) if r.get("user_id") is not None else None,
    }


def _task_out(r: dict) -> dict:
    priority = r.get("priority")
    return {
        "id": str(r["id"]),
        "team_id": str(r.get("team_id") or ""),
        "member_id": str(r["member_id"]) if r.get("member_id") else None,
        "name": r.get("name") or "",
        "notes": r.get("notes") or None,
        "priority": priority if priority in PRIORITIES else "medium",
        "due_date": str(r["due_date"])[:10] if r.get("due_date") else None,
        "done": bool(r.get("done")),
    }


# ── board ────────────────────────────────────────────────────────────────────

@router.get("/board")
async def get_board(_user=Depends(get_current_user)):
    teams = await sb.list_rows(sb.TEAMS, order="name.asc")
    members = await sb.list_rows(sb.MEMBERS, order="name.asc")
    tasks = await sb.list_rows(sb.TASKS, order="name.asc")
    links = await link_store.current_links({str(m["id"]) for m in members})
    return {
        "teams": [_team_out(r) for r in teams],
        "members": [{**_member_out(r), "user_id": links.get(str(r["id"]))} for r in members],
        "tasks": [_task_out(r) for r in tasks],
    }


# ── teams ────────────────────────────────────────────────────────────────────

@router.post("/teams", status_code=201)
async def create_team(payload: TeamIn, _user=Depends(get_current_user)):
    row = await sb.insert_row(sb.TEAMS, {"id": _new_id(), "name": _title(payload.name)})
    return _team_out(row)


@router.patch("/teams/{team_id}")
async def update_team(team_id: str, payload: TeamPatch, _user=Depends(get_current_user)):
    await sb.get_or_404(sb.TEAMS, team_id, "Team")
    patch = {}
    if payload.name is not None:
        patch["name"] = _title(payload.name)
    row = await sb.update_row(sb.TEAMS, team_id, patch) if patch else await sb.get_or_404(sb.TEAMS, team_id, "Team")
    return _team_out(row)


@router.delete("/teams/{team_id}")
async def delete_team(team_id: str, _user=Depends(get_current_user)):
    await sb.get_or_404(sb.TEAMS, team_id, "Team")
    # Remove dependants explicitly so this works whatever FK rules the table has.
    await sb.delete_rows(sb.TASKS, {"team_id": f"eq.{team_id}"})
    await sb.delete_rows(sb.MEMBERS, {"team_id": f"eq.{team_id}"})
    await sb.delete_rows(sb.TEAMS, {"id": f"eq.{team_id}"})
    return {"ok": True}


# ── members ──────────────────────────────────────────────────────────────────

@router.post("/members", status_code=201)
async def create_member(payload: MemberIn, _user=Depends(get_current_user)):
    await sb.get_or_404(sb.TEAMS, payload.team_id, "Team")
    row = await sb.insert_row(sb.MEMBERS, {
        "id": _new_id(),
        "team_id": payload.team_id,
        "name": _title(payload.name),
        "level": payload.level,
        "extra_teams": json.dumps(payload.extra_teams),
        "pin": "",  # PIN logins are replaced by the main app's auth; column kept for old rows
    })
    return _member_out(row)


@router.patch("/members/{member_id}")
async def update_member(member_id: str, payload: MemberPatch, _user=Depends(get_current_user)):
    current = await sb.get_or_404(sb.MEMBERS, member_id, "Member")
    data = payload.model_dump(exclude_unset=True)
    patch: dict = {}
    if data.get("name") is not None:
        patch["name"] = _title(data["name"])
    if data.get("level") is not None:
        patch["level"] = data["level"]
    if data.get("team_id") is not None:
        await sb.get_or_404(sb.TEAMS, data["team_id"], "Team")
        patch["team_id"] = data["team_id"]
    if data.get("extra_teams") is not None:
        patch["extra_teams"] = json.dumps(data["extra_teams"])
    row = await sb.update_row(sb.MEMBERS, member_id, patch) if patch else current
    return _member_out(row)


@router.delete("/members/{member_id}")
async def delete_member(member_id: str, _user=Depends(get_current_user)):
    await sb.get_or_404(sb.MEMBERS, member_id, "Member")
    await sb.update_rows(sb.TASKS, {"member_id": f"eq.{member_id}"}, {"member_id": None})
    await sb.delete_rows(sb.MEMBERS, {"id": f"eq.{member_id}"})
    return {"ok": True}


# ── tasks ────────────────────────────────────────────────────────────────────

@router.post("/tasks", status_code=201)
async def create_task(payload: TaskIn, _user=Depends(get_current_user)):
    _check_priority(payload.priority)
    await sb.get_or_404(sb.TEAMS, payload.team_id, "Team")
    if payload.member_id:
        await sb.get_or_404(sb.MEMBERS, payload.member_id, "Member")
    row = await sb.insert_row(sb.TASKS, {
        "id": _new_id(),
        "team_id": payload.team_id,
        "member_id": payload.member_id or None,
        "name": _sentence(payload.name),
        "notes": payload.notes or None,
        "priority": payload.priority,
        "due_date": payload.due_date.isoformat() if payload.due_date else None,
        "done": False,
    })
    return _task_out(row)


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, payload: TaskPatch, _user=Depends(get_current_user)):
    await sb.get_or_404(sb.TASKS, task_id, "Task")
    data = payload.model_dump(exclude_unset=True)
    patch: dict = {}

    if "name" in data:
        if data["name"] is None:
            raise HTTPException(status_code=422, detail="name cannot be empty")
        patch["name"] = _sentence(data["name"])
    if "member_id" in data:
        if data["member_id"]:
            await sb.get_or_404(sb.MEMBERS, data["member_id"], "Member")
        patch["member_id"] = data["member_id"] or None
    if data.get("priority") is not None:
        _check_priority(data["priority"])
        patch["priority"] = data["priority"]
    if "due_date" in data:
        patch["due_date"] = data["due_date"].isoformat() if data["due_date"] else None
    if "notes" in data:
        patch["notes"] = data["notes"] or None
    if data.get("done") is not None:
        patch["done"] = data["done"]

    row = await sb.update_row(sb.TASKS, task_id, patch) if patch else await sb.get_or_404(sb.TASKS, task_id, "Task")
    return _task_out(row)


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, _user=Depends(get_current_user)):
    await sb.get_or_404(sb.TASKS, task_id, "Task")
    await sb.delete_rows(sb.TASKS, {"id": f"eq.{task_id}"})
    return {"ok": True}
