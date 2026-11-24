"""File-backed store for Ops Tracker member-to-login links.

The mapping lives in a small JSON file, so neither Supabase nor the system
database needs a schema change. Shape: {"<member_id>": <user_id>}.

Path: backend/data/ops_tracker_links.json, or OPS_TRACKER_LINKS_PATH if set.
Writes are atomic and serialised with a lock. This suits a single backend
process. Keep the data folder on persistent storage (not a temporary container).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "ops_tracker_links.json"
_lock = asyncio.Lock()


def _path() -> Path:
    return Path(os.getenv("OPS_TRACKER_LINKS_PATH") or _DEFAULT_PATH)


def read_links() -> dict[str, int]:
    path = _path()
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"Ops Tracker link file is not valid JSON: {path}") from exc
    return {str(k): int(v) for k, v in data.items() if v is not None}


def _write(links: dict[str, int]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(links, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


async def update_links(changes: dict[str, int | None]) -> dict[str, int]:
    """Apply changes; a value of None removes that member's link."""
    async with _lock:
        links = read_links()
        for member_id, user_id in changes.items():
            if user_id is None:
                links.pop(member_id, None)
            else:
                links[member_id] = user_id
        _write(links)
        return links


async def current_links(valid_member_ids: set[str]) -> dict[str, int]:
    """Return links, dropping any that point at members that no longer exist."""
    links = read_links()
    stale = {mid: None for mid in links if mid not in valid_member_ids}
    if stale:
        return await update_links(stale)
    return links
