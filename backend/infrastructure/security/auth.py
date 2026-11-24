from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select

from infrastructure.config.settings import get_settings
from ii_platform.database.models import User, UserRole

try:
    from pwdlib import PasswordHash

    _password_hash = PasswordHash.recommended()
except ImportError:
    _password_hash = None

settings = get_settings()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_prefix}/auth/token")


def hash_password(password: str) -> str:
    if _password_hash is not None:
        return _password_hash.hash(password)
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return "pbkdf2_sha256$600000$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, hashed: str) -> bool:
    if _password_hash is not None:
        return _password_hash.verify(password, hashed)
    try:
        algo, iters, salt_b64, digest_b64 = hashed.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def create_access_token(user: User, session_id: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role.value,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
    }
    if session_id:
        payload["sid"] = str(session_id)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


@dataclass(frozen=True)
class AuthenticatedUser:
    """Lightweight authenticated identity derived from the signed JWT.

    Protected endpoints only need id/username/role. The old implementation hit
    PostgreSQL for every authenticated request, which made even local navigation
    wait on the remote database. Token signature + expiry validation is sufficient
    for request authentication; admin/user-management endpoints still query the
    database when they need current user records.
    """

    id: int
    username: str
    role: UserRole
    is_active: bool = True
    session_id: str | None = None
    issued_at: datetime | None = None


async def get_current_user(
    token: str = Depends(oauth2_scheme),
) -> AuthenticatedUser:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        user_id = int(payload["sub"])
        username = str(payload.get("username") or "").strip()
        role = UserRole(str(payload.get("role") or "ANALYST"))
        session_id = str(payload.get("sid") or "").strip() or None
        raw_iat = payload.get("iat")
        issued_at = None
        if raw_iat is not None:
            try:
                issued_at = datetime.fromtimestamp(float(raw_iat), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                issued_at = None
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc

    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )

    return AuthenticatedUser(
        id=user_id,
        username=username,
        role=role,
        session_id=session_id,
        issued_at=issued_at,
    )


async def authenticate_user(db, username, password):
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not user or not user.is_active:
        return None
    verified = await asyncio.to_thread(verify_password, password, user.password_hash)
    if not verified:
        return None
    return user
