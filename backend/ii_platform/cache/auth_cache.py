from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from infrastructure.config.settings import get_settings

logger = logging.getLogger(__name__)
_MEMORY: dict[str, tuple[float, dict[str, Any]]] = {}
_MEMORY_LOCK = asyncio.Lock()


def _key(username: str) -> str:
    settings = get_settings()
    return f"{settings.redis_stream_prefix}:auth:user:{str(username).strip().casefold()}"


async def _client():
    settings = get_settings()
    if not settings.redis_enabled or not settings.auth_cache_enabled:
        return None
    try:
        from redis.asyncio import Redis
    except ImportError:
        return None
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=15,
        socket_keepalive=True,
        retry_on_timeout=True,
    )


async def get_cached_user(username: str) -> dict[str, Any] | None:
    settings = get_settings()
    key = _key(username)
    now = time.time()

    # Web-process memory fallback keeps authentication fast even when Redis is
    # temporarily unavailable. The worker/web startup warm-up populates this.
    async with _MEMORY_LOCK:
        item = _MEMORY.get(key)
        if item is not None:
            expires_at, payload = item
            if expires_at > now:
                return payload
            _MEMORY.pop(key, None)

    client = None
    try:
        client = await _client()
        if client is None:
            return None
        value = await client.get(key)
        payload = json.loads(value) if value else None
        if payload:
            async with _MEMORY_LOCK:
                _MEMORY[key] = (now + settings.auth_cache_ttl_seconds, payload)
        return payload
    except Exception:
        logger.debug("Auth cache read failed username=%s", username, exc_info=True)
        return None
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


async def set_cached_user(
    user_id: int,
    username: str,
    role: str,
    password_hash: str,
    is_active: bool = True,
) -> bool:
    settings = get_settings()
    key = _key(username)
    payload = {
        "id": int(user_id),
        "username": str(username),
        "role": str(role),
        "password_hash": str(password_hash),
        "is_active": bool(is_active),
    }

    async with _MEMORY_LOCK:
        _MEMORY[key] = (time.time() + settings.auth_cache_ttl_seconds, payload)

    client = None
    try:
        client = await _client()
        if client is None:
            return True
        await client.set(
            key,
            json.dumps(payload, separators=(",", ":")),
            ex=settings.auth_cache_ttl_seconds,
        )
        return True
    except Exception:
        logger.debug("Auth Redis cache write failed username=%s", username, exc_info=True)
        # Memory cache is still valid; a Redis outage must not make login slow.
        return True
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


async def warm_auth_users(db) -> int:
    from sqlalchemy import select
    from ii_platform.database.models import User

    rows = (await db.execute(select(User))).scalars().all()
    count = 0
    for user in rows:
        if await set_cached_user(
            user.id,
            user.username,
            user.role.value,
            user.password_hash,
            user.is_active,
        ):
            count += 1
    return count


async def clear_auth_cache() -> None:
    async with _MEMORY_LOCK:
        _MEMORY.clear()
