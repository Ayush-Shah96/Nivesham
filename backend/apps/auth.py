from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from pydantic import BaseModel

from infrastructure.config.settings import get_settings
from infrastructure.security.auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
)
from ii_platform.database.connection import get_db, get_sessionmaker
from ii_platform.database.models import AuditLog, UserSession, UserRole
from ii_platform.database.session_usage import as_utc, credit_usage_interval, normalise_app_usage
from ii_platform.cache.auth_cache import get_cached_user, set_cached_user


router = APIRouter(prefix="/auth", tags=["auth"])


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    session_id: str | None = None


class UserOut(BaseModel):
    id: int
    username: str
    role: str


def _credentials_from_mapping(values) -> tuple[str, str]:
    username = str(
        values.get("username")
        or values.get("email")
        or values.get("user")
        or ""
    ).strip()
    password = str(
        values.get("password")
        or values.get("pass")
        or ""
    )
    return username, password


async def _extract_login_fields(request: Request) -> tuple[str, str]:
    """Parse login credentials without trusting Content-Type to be correct.

    The React client sends application/x-www-form-urlencoded, but accepting JSON as
    well makes the endpoint robust to direct API clients and older frontend builds.
    We read the raw body once and choose the parser based on the body itself, which
    avoids the recurring empty-body JSON parsing failure seen in the browser.
    """
    raw = await request.body()
    text = raw.decode("utf-8", errors="replace").strip()
    content_type = (request.headers.get("content-type") or "").lower()

    username = ""
    password = ""

    if text:
        parsed = None

        # Prefer JSON when the body is actually JSON, regardless of a stale/wrong
        # Content-Type header.
        try:
            candidate = json.loads(text)
            if isinstance(candidate, dict):
                parsed = candidate
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None

        if parsed is not None:
            username, password = _credentials_from_mapping(parsed)
        else:
            # Standard OAuth2 form payload: username=...&password=...
            values = parse_qs(text, keep_blank_values=True)
            normalized = {
                key: (items[0] if items else "")
                for key, items in values.items()
            }
            username, password = _credentials_from_mapping(normalized)

    if not username or not password:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Username and password are required.",
                "content_type": content_type or None,
                "body_received": bool(raw),
                "received_fields": {
                    "username": bool(username),
                    "password": bool(password),
                },
            },
        )

    return username, password


async def _persist_login_event(user_id: int, username: str, session_id: str, issued_at: datetime) -> None:
    """Best-effort session/audit persistence after the token has been returned."""
    try:
        async with get_sessionmaker()() as session_db:
            existing_session = await session_db.scalar(
                select(UserSession).where(UserSession.session_id == session_id)
            )
            if existing_session is None:
                session_db.add(UserSession(
                    session_id=session_id,
                    user_id=user_id,
                    login_at=issued_at,
                    last_seen_at=issued_at,
                    active_seconds=0,
                    app_usage={},
                ))
            session_db.add(AuditLog(
                user_id=user_id,
                action="login",
                resource="auth",
                resource_id=session_id,
                detail={"session_id": session_id, "username": username},
                created_at=issued_at,
            ))
            await session_db.commit()
    except Exception:
        # Authentication must never be held hostage by analytics/session writes.
        import logging
        logging.getLogger(__name__).warning(
            "LOGIN_SESSION_PERSIST_FAILED username=%s session_id=%s",
            username,
            session_id,
            exc_info=True,
        )


@router.post("/token", response_model=Token)
async def token(
    request: Request,
    background_tasks: BackgroundTasks,
):
    username, password = await _extract_login_fields(request)
    normalized_username = username.strip().casefold()

    user = None
    cached = await get_cached_user(normalized_username) if get_settings().auth_cache_enabled else None
    if cached and cached.get("is_active"):
        try:
            role = UserRole(str(cached.get("role") or "ANALYST"))
            from infrastructure.security.auth import verify_password
            verified = await asyncio.to_thread(verify_password, password, str(cached.get("password_hash") or ""))
            if verified:
                user = type("CachedLoginUser", (), {
                    "id": int(cached["id"]),
                    "username": str(cached.get("username") or username),
                    "role": role,
                    "is_active": True,
                    "password_hash": str(cached.get("password_hash") or ""),
                })()
        except Exception:
            user = None

    if user is None:
        try:
            async with get_sessionmaker()() as auth_db:
                user = await asyncio.wait_for(authenticate_user(auth_db, username, password), timeout=4.0)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication database request timed out. Please retry.",
            ) from exc
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
        if get_settings().auth_cache_enabled:
            await set_cached_user(user.id, user.username, user.role.value, user.password_hash, user.is_active)

    session_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    background_tasks.add_task(_persist_login_event, int(user.id), str(user.username), session_id, now)

    return Token(
        access_token=create_access_token(user, session_id=session_id),
        session_id=session_id,
    )


@router.post("/logout")
async def logout(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Close the current persisted session; logging out never deletes history."""
    now = datetime.now(timezone.utc)
    if user.session_id:
        session = await db.scalar(
            select(UserSession).where(
                UserSession.session_id == user.session_id,
                UserSession.user_id == user.id,
            ).with_for_update()
        )
        if session and session.logout_at is None:
            usage = normalise_app_usage(session.app_usage, session.active_seconds)
            tracking = dict(usage.get("_tracking") or {})
            previous_visible = bool(tracking.get("visible", True))
            last_seen = as_utc(session.last_seen_at)
            if previous_visible and last_seen is not None:
                gap = (now - last_seen).total_seconds()
                # Credit only the last bounded interval. A long delay may mean the
                # browser was asleep or offline, so never fabricate the full gap.
                if 0 < gap <= 90:
                    credited = int(gap)
                    credit_usage_interval(usage, session.last_app or "Workspace", last_seen, credited)
                    session.active_seconds = max(0, int(session.active_seconds or 0)) + credited
            tracking["visible"] = False
            tracking["updated_at"] = now.isoformat()
            usage["_tracking"] = tracking
            session.app_usage = usage
            session.logout_at = now
            # last_seen_at remains the last visible browser heartbeat, not the logout
            # request timestamp; admins can distinguish activity from sign-out.
            db.add(AuditLog(
                user_id=user.id,
                action="logout",
                resource="auth",
                resource_id=user.session_id,
                detail={"session_id": user.session_id},
                created_at=now,
            ))
            await db.commit()
    return {"status": "logged_out"}


@router.get("/me", response_model=UserOut)
async def me(user=Depends(get_current_user)):
    return UserOut(
        id=user.id,
        username=user.username,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
    )
