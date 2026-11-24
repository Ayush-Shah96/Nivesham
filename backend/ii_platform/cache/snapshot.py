from __future__ import annotations

import asyncio
import json
import logging
import math
import numbers
import time
import zlib
import re
from decimal import Decimal
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from infrastructure.config.settings import get_settings

logger = logging.getLogger(__name__)

_MAGIC = b"II-ZLIB-JSON-v1\n"
_MEMORY: dict[str, tuple[float, Any]] = {}
_MEMORY_LOCK = asyncio.Lock()


def snapshot_key(name: str) -> str:
    settings = get_settings()
    return f"{settings.redis_stream_prefix}:snapshot:{str(name).strip(':')}"


def _disk_path(name: str) -> Path:
    settings = get_settings()
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(name).strip(":"))
    return Path(settings.storage_path) / "snapshot-cache" / f"{safe}.json.zlib"


def _sanitize_json_value(value: Any, path: str = "$", invalid_paths: list[str] | None = None) -> Any:
    """Return a JSON-safe copy, replacing non-finite numbers with null.

    We keep ``allow_nan=False`` below as a final guard.  Silently storing Infinity
    or NaN makes snapshots unreadable by strict JSON clients, so invalid paths are
    reported and the affected measurement becomes JSON null instead.
    """
    if invalid_paths is None:
        invalid_paths = []

    if isinstance(value, Decimal):
        if not value.is_finite():
            invalid_paths.append(path)
            return None
        return int(value) if value == value.to_integral_value() else float(value)

    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        try:
            if not math.isfinite(float(value)):
                invalid_paths.append(path)
                return None
        except (TypeError, ValueError, OverflowError):
            invalid_paths.append(path)
            return None
        # Normalize non-builtin numeric scalar types to a JSON-native number.
        if type(value) not in (int, float):
            return float(value)
        return value

    if isinstance(value, Mapping):
        return {str(key): _sanitize_json_value(item, f"{path}.{key}", invalid_paths) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_value(item, f"{path}[{index}]", invalid_paths) for index, item in enumerate(value)]
    return value


def _encode(value: Any) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _MAGIC + zlib.compress(raw, level=6)


def _decode(raw: bytes | str | None) -> Any:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    try:
        payload = zlib.decompress(raw[len(_MAGIC):] if raw.startswith(_MAGIC) else raw)
        return json.loads(payload.decode("utf-8"))
    except Exception:
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            logger.warning("Invalid Redis snapshot payload", exc_info=True)
            return None


async def _redis_client():
    settings = get_settings()
    if not settings.redis_enabled or not settings.snapshot_cache_enabled:
        return None
    try:
        from redis.asyncio import Redis
    except ImportError:
        logger.warning("redis package is unavailable; snapshot cache will use process memory only")
        return None
    return Redis.from_url(
        settings.redis_url,
        decode_responses=False,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=15,
        socket_keepalive=True,
        retry_on_timeout=True,
    )


async def set_snapshot(name: str, payload: Mapping[str, Any] | list[Any], ttl_seconds: int | None = None) -> bool:
    settings = get_settings()
    key = snapshot_key(name)
    ttl = int(ttl_seconds or settings.snapshot_cache_ttl_seconds)
    invalid_paths: list[str] = []
    safe_payload = _sanitize_json_value(payload, invalid_paths=invalid_paths)
    if invalid_paths:
        logger.warning(
            "SNAPSHOT_NONFINITE_SANITIZED key=%s count=%s sample_paths=%s",
            key, len(invalid_paths), invalid_paths[:20],
        )
    encoded = _encode(safe_payload)
    now = time.time()
    async with _MEMORY_LOCK:
        # Memory, disk and Redis must expose the same sanitized representation.
        _MEMORY[key] = (now, safe_payload)
    try:
        disk = _disk_path(name)
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_bytes(encoded)
    except Exception:
        logger.debug("Snapshot disk fallback write failed key=%s", key, exc_info=True)
    client = None
    try:
        client = await _redis_client()
        if client is None:
            return True
        await client.set(key, encoded, ex=max(60, ttl))
        return True
    except Exception:
        logger.warning("Redis snapshot write failed key=%s; memory snapshot retained", key, exc_info=True)
        return False
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


async def get_snapshot(name: str, allow_stale: bool = True) -> tuple[Any | None, dict[str, Any]]:
    settings = get_settings()
    key = snapshot_key(name)
    now = time.time()

    client = None
    try:
        client = await _redis_client()
        if client is not None:
            raw = await client.get(key)
            decoded = _decode(raw)
            if decoded is not None:
                await client.aclose()
                return decoded, {"source": "redis", "stale": False, "key": key}
    except Exception:
        logger.warning("Redis snapshot read failed key=%s", key, exc_info=True)
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    try:
        disk = _disk_path(name)
        if disk.exists():
            decoded = _decode(disk.read_bytes())
            if decoded is not None:
                return decoded, {"source": "disk", "stale": False, "key": key}
    except Exception:
        logger.debug("Snapshot disk fallback read failed key=%s", key, exc_info=True)

    async with _MEMORY_LOCK:
        memory = _MEMORY.get(key)
    if memory is not None:
        created_at, payload = memory
        age = max(0.0, now - created_at)
        if age <= settings.snapshot_cache_memory_max_age_seconds or allow_stale:
            return payload, {"source": "memory", "stale": age > settings.snapshot_cache_ttl_seconds, "age_seconds": age, "key": key}

    return None, {"source": "miss", "stale": True, "key": key}


async def clear_snapshot(name: str) -> None:
    key = snapshot_key(name)
    async with _MEMORY_LOCK:
        _MEMORY.pop(key, None)
    try:
        disk = _disk_path(name)
        if disk.exists():
            disk.unlink()
    except Exception:
        pass
    client = None
    try:
        client = await _redis_client()
        if client is not None:
            await client.delete(key)
    except Exception:
        logger.warning("Redis snapshot delete failed key=%s", key, exc_info=True)
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass