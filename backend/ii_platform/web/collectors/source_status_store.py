from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from infrastructure.config.settings import get_settings
from ii_platform.web.collectors.news_source_registry import enabled_sources

_MEMORY: dict[str, dict[str, Any]] = {}


def _key(source_key: str) -> str:
    settings = get_settings()
    return f"{settings.redis_stream_prefix}:news:source-status:{source_key}"


def _default(source_key: str, source_name: str) -> dict[str, Any]:
    return {
        "key": source_key,
        "source": source_name,
        "status": "never_run",
        "items_found": 0,
        "matched": 0,
        "created": 0,
        "skipped": 0,
        "attempted_urls": [],
        "errors": [],
        "last_attempted_at": None,
        "last_finished_at": None,
        "duration_ms": None,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _redis_client():
    settings = get_settings()
    if not getattr(settings, "redis_status_enabled", False):
        return None
    try:
        from redis.asyncio import Redis
        client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
            health_check_interval=30,
        )
        await client.ping()
        return client
    except Exception:
        return None


async def mark_running(source_key: str, source_name: str) -> dict[str, Any]:
    payload = _default(source_key, source_name)
    payload["status"] = "running"
    payload["last_attempted_at"] = _now()
    _MEMORY[source_key] = payload
    client = await _redis_client()
    if client is None:
        return payload
    try:
        await client.set(_key(source_key), json.dumps(payload), ex=7 * 24 * 3600)
    finally:
        await client.aclose()
    return payload


async def save_result(
    source_key: str,
    source_name: str,
    *,
    status: str,
    items_found: int,
    created: int,
    skipped: int,
    matched: int | None = None,
    attempted_urls: list[str],
    errors: list[str] | list[dict[str, Any]],
    duration_ms: int,
) -> dict[str, Any]:
    payload = _default(source_key, source_name)
    payload.update(
        {
            "status": status,
            "items_found": int(items_found),
            "matched": int(matched if matched is not None else created),
            "created": int(created),
            "skipped": int(skipped),
            "attempted_urls": attempted_urls[-50:],
            "errors": errors[-20:],
            "last_attempted_at": _MEMORY.get(source_key, {}).get("last_attempted_at") or _now(),
            "last_finished_at": _now(),
            "duration_ms": int(duration_ms),
        }
    )
    _MEMORY[source_key] = payload
    client = await _redis_client()
    if client is None:
        return payload
    try:
        await client.set(_key(source_key), json.dumps(payload), ex=7 * 24 * 3600)
    finally:
        await client.aclose()
    return payload


async def get_statuses() -> list[dict[str, Any]]:
    sources = enabled_sources()
    payloads = {s.key: _default(s.key, s.name) for s in sources}
    payloads.setdefault("youtube", _default("youtube", "YouTube"))
    payloads.setdefault("x_posts", _default("x_posts", "X / Twitter Posts"))

    client = await _redis_client()
    if client is not None:
        try:
            for source_key in [s.key for s in sources] + ["youtube", "x_posts"]:
                raw = await client.get(_key(source_key))
                source = next((s for s in sources if s.key == source_key), None)
                if raw:
                    try:
                        value = json.loads(raw)
                        if isinstance(value, dict):
                            payloads[source_key].update(value)
                    except json.JSONDecodeError:
                        pass
        finally:
            await client.aclose()

    for key, value in _MEMORY.items():
        if key in payloads:
            payloads[key].update(value)

    return list(payloads.values())
