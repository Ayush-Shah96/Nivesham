from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ii_platform.database.session_usage import as_utc, normalise_app_usage, REPORTING_TZ

from infrastructure.security.permissions import require_role
from infrastructure.security.auth import hash_password
from ii_platform.database.connection import get_db
from ii_platform.database.models import User, UserRole, UserSession

router = APIRouter(prefix="/admin", tags=["admin"])


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=6, max_length=255)
    role: UserRole = UserRole.ANALYST


class UserUpdate(BaseModel):
    role: UserRole | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=6, max_length=255)


@router.get("/users")
async def list_users(
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN)),
):
    users = (await db.execute(select(User).order_by(User.id))).scalars().all()
    return [
        {"id": u.id, "username": u.username, "role": u.role.value, "is_active": u.is_active}
        for u in users
    ]


@router.post("/users")
async def create_user(
    data: UserCreate,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN)),
):
    username = data.username.strip()
    if await db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(409, "Username already exists")
    u = User(username=username, password_hash=hash_password(data.password), role=data.role, is_active=True)
    db.add(u)
    await db.flush()
    await db.commit()
    return {"id": u.id, "username": u.username, "role": u.role.value, "is_active": u.is_active}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    data: UserUpdate,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    if data.role is not None:
        u.role = data.role
    if data.is_active is not None:
        if u.id == admin.id and data.is_active is False:
            raise HTTPException(400, "You cannot deactivate your own admin account")
        u.is_active = data.is_active
        if data.is_active is False:
            now = datetime.now(timezone.utc)
            open_sessions = (await db.execute(
                select(UserSession).where(
                    UserSession.user_id == u.id,
                    UserSession.logout_at.is_(None),
                )
            )).scalars().all()
            for session in open_sessions:
                session.logout_at = now
                usage_data = normalise_app_usage(session.app_usage, session.active_seconds)
                tracking = dict(usage_data.get("_tracking") or {})
                tracking["visible"] = False
                tracking["updated_at"] = now.isoformat()
                usage_data["_tracking"] = tracking
                session.app_usage = usage_data
    if data.password:
        u.password_hash = hash_password(data.password)
    await db.commit()
    return {"id": u.id, "username": u.username, "role": u.role.value, "is_active": u.is_active}


@router.delete("/users/{user_id}")
async def deactivate_user(
    user_id: int,
    db=Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
):
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    if u.id == admin.id:
        raise HTTPException(400, "You cannot remove your own admin account")
    u.is_active = False
    now = datetime.now(timezone.utc)
    open_sessions = (await db.execute(
        select(UserSession).where(
            UserSession.user_id == u.id,
            UserSession.logout_at.is_(None),
        )
    )).scalars().all()
    for session in open_sessions:
        session.logout_at = now
        usage_data = normalise_app_usage(session.app_usage, session.active_seconds)
        tracking = dict(usage_data.get("_tracking") or {})
        tracking["visible"] = False
        tracking["updated_at"] = now.isoformat()
        usage_data["_tracking"] = tracking
        session.app_usage = usage_data
    await db.commit()
    return {"id": u.id, "username": u.username, "status": "deactivated"}


SESSION_STALE_SECONDS = 90


def _active_now(session: UserSession, now: datetime) -> bool:
    usage = normalise_app_usage(session.app_usage, session.active_seconds)
    tracking = usage.get("_tracking") or {}
    last_seen = as_utc(session.last_seen_at)
    if session.logout_at is not None or tracking.get("visible") is False or last_seen is None:
        return False
    gap = (now - last_seen).total_seconds()
    return 0 <= gap <= SESSION_STALE_SECONDS


def _minutes(seconds: int | float | None) -> float:
    return round(max(float(seconds or 0), 0.0) / 60.0, 1)


def _sum_app_seconds(apps: dict) -> int:
    total = 0
    for value in (apps or {}).values():
        try:
            total += max(0, int(float(value or 0)))
        except (TypeError, ValueError, OverflowError):
            continue
    return total


@router.get("/usage")
async def usage(
    days: int = Query(default=30, ge=1, le=365),
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN)),
):
    """Return full user history and date-aware usage from user_sessions only.

    Every registered user is returned, including users with no sessions and users
    who are logged out. Per-day usage is read from the existing app_usage JSON.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    today_local = now.astimezone(REPORTING_TZ).date()
    first_day = today_local - timedelta(days=days - 1)
    first_day_key = first_day.isoformat()
    today_key = today_local.isoformat()

    actors = (await db.execute(select(User).order_by(User.username))).scalars().all()
    # Do not filter sessions by login_at before aggregation: older, long-running
    # sessions can contain usage inside the selected window, and last-login/logout
    # must remain visible after a user signs out.
    records = (await db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .order_by(UserSession.login_at.desc(), UserSession.id.desc())
    )).all()

    by_user: dict[int, dict] = {
        actor.id: {
            "id": actor.id,
            "username": actor.username,
            "role": actor.role.value if hasattr(actor.role, "value") else str(actor.role),
            "is_active": actor.is_active,
            "sessions_count": 0,
            "total_active_minutes": 0.0,
            "_period_active_seconds": 0,
            "_app_usage_seconds": {},
            "lifetime_active_minutes": 0.0,
            "last_login_at": None,
            "last_logout_at": None,
            "last_seen_at": None,
            "currently_active": False,
            "current_app": None,
            "current_session_login_at": None,
            "current_session_active_minutes": 0.0,
            "app_usage": {},
        }
        for actor in actors
    }
    recent_sessions: list[dict] = []
    in_window_session_count = 0

    for session, actor in records:
        item = by_user.get(actor.id)
        if item is None:
            continue

        login_at = as_utc(session.login_at)
        logout_at = as_utc(session.logout_at)
        last_seen_at = as_utc(session.last_seen_at)
        item["lifetime_active_minutes"] += _minutes(session.active_seconds)
        if login_at and (item["last_login_at"] is None or login_at > item["last_login_at"]):
            item["last_login_at"] = login_at
        if logout_at and (item["last_logout_at"] is None or logout_at > item["last_logout_at"]):
            item["last_logout_at"] = logout_at
        if last_seen_at and (item["last_seen_at"] is None or last_seen_at > item["last_seen_at"]):
            item["last_seen_at"] = last_seen_at

        raw_usage = session.app_usage if isinstance(session.app_usage, dict) else {}
        has_v2 = raw_usage.get("_schema") == 2
        usage_data = normalise_app_usage(raw_usage, session.active_seconds)
        by_day = usage_data.get("by_day") or {}
        selected_daily: dict[str, dict[str, int]] = {}
        period_app_seconds: dict[str, int] = {}
        period_daily_seconds = 0

        for day_key, apps in by_day.items():
            day_text = str(day_key)
            if day_text < first_day_key or day_text > today_key or not isinstance(apps, dict):
                continue
            cleaned_apps: dict[str, int] = {}
            for app_name, value in apps.items():
                try:
                    app_seconds = max(0, int(float(value or 0)))
                except (TypeError, ValueError, OverflowError):
                    continue
                if app_seconds <= 0:
                    continue
                app_key = str(app_name or "Workspace")[:120]
                cleaned_apps[app_key] = app_seconds
                period_app_seconds[app_key] = period_app_seconds.get(app_key, 0) + app_seconds
                period_daily_seconds += app_seconds
            if cleaned_apps:
                selected_daily[day_text] = cleaned_apps

        # Existing sessions predate day-bucket telemetry. Their historical totals
        # cannot be reconstructed by day; keep them visible when their login falls
        # in the selected window and label them as unbucketed for the matrix.
        legacy_seconds = 0
        legacy_apps: dict[str, int] = {}
        if login_at and login_at >= since:
            legacy_seconds = max(0, int(usage_data.get("_legacy_seconds") or 0))
            raw_legacy_apps = usage_data.get("_legacy_app_usage") or {}
            if isinstance(raw_legacy_apps, dict):
                for app_name, value in raw_legacy_apps.items():
                    try:
                        app_seconds = max(0, int(float(value or 0)))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if app_seconds:
                        legacy_apps[str(app_name or "Workspace")[:120]] = app_seconds

        active = _active_now(session, now)
        gap_seconds = 0
        if active and last_seen_at:
            gap = max(0.0, (now - last_seen_at).total_seconds())
            # A small provisional live value makes current usage feel real-time;
            # the next persisted heartbeat replaces this estimate rather than adding
            # on top of it.
            gap_seconds = min(30, int(gap))
            if gap_seconds:
                live_app = str(session.last_app or "Workspace")[:120]
                period_app_seconds[live_app] = period_app_seconds.get(live_app, 0) + gap_seconds
                if first_day_key <= today_key:
                    selected_daily.setdefault(today_key, {})
                    selected_daily[today_key][live_app] = selected_daily[today_key].get(live_app, 0) + gap_seconds

        period_seconds = period_daily_seconds + legacy_seconds + gap_seconds
        period_app_seconds_with_legacy = dict(period_app_seconds)
        for app_name, value in legacy_apps.items():
            period_app_seconds_with_legacy[app_name] = period_app_seconds_with_legacy.get(app_name, 0) + value

        in_window = bool(
            (login_at and login_at >= since)
            or period_seconds > 0
            or active
        )
        if in_window:
            in_window_session_count += 1
            item["sessions_count"] += 1
            item["_period_active_seconds"] += period_seconds
            for app_name, seconds in period_app_seconds_with_legacy.items():
                app_seconds = item["_app_usage_seconds"].get(app_name, 0)
                item["_app_usage_seconds"][app_name] = app_seconds + max(0, int(seconds))

        if active and (
            item["current_session_login_at"] is None
            or (login_at and login_at > item["current_session_login_at"])
        ):
            item["currently_active"] = True
            item["current_app"] = session.last_app
            item["current_session_login_at"] = login_at
            item["current_session_active_minutes"] = _minutes(
                max(0, int(session.active_seconds or 0)) + gap_seconds
            )

        if in_window:
            recent_sessions.append({
                "id": session.id,
                "session_id": session.session_id,
                "user_id": actor.id,
                "username": actor.username,
                "role": actor.role.value if hasattr(actor.role, "value") else str(actor.role),
                "login_at": login_at,
                "logout_at": logout_at,
                "last_seen_at": last_seen_at,
                "active": active,
                "current_app": session.last_app,
                "active_minutes": _minutes(period_seconds),
                "daily_app_usage_seconds": selected_daily,
                "legacy_unbucketed_seconds": legacy_seconds,
                "usage_schema_version": 2 if has_v2 else 1,
            })

    user_rows = []
    total_active_seconds = 0
    for actor in actors:
        item = by_user[actor.id]
        period_active_seconds = max(0, int(item.pop("_period_active_seconds", 0) or 0))
        total_active_seconds += period_active_seconds
        item["total_active_minutes"] = _minutes(period_active_seconds)
        item["lifetime_active_minutes"] = round(item["lifetime_active_minutes"], 1)
        app_usage_minutes = {
            app_name: _minutes(seconds)
            for app_name, seconds in (item.pop("_app_usage_seconds", {}) or {}).items()
        }
        item["app_usage"] = dict(sorted(app_usage_minutes.items(), key=lambda pair: pair[1], reverse=True))
        item["top_app"] = next(iter(item["app_usage"]), None)
        item["last_seen_at"] = item["last_seen_at"]
        user_rows.append(item)

    recent_sessions.sort(key=lambda row: row.get("login_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    active_user_ids = {row["user_id"] for row in recent_sessions if row["active"]}
    total_active_minutes = _minutes(total_active_seconds)
    unique_users = sum(1 for row in user_rows if row["sessions_count"] > 0 or row["currently_active"])

    return {
        "period": {"days": days, "since": since, "until": now, "timezone": "Asia/Kolkata"},
        "metrics": {
            "total_users": len(actors),
            "unique_users": unique_users,
            "active_users_now": len(active_user_ids),
            "sessions": in_window_session_count,
            "total_active_minutes": total_active_minutes,
        },
        "users": user_rows,
        "sessions": recent_sessions[:500],
    }

