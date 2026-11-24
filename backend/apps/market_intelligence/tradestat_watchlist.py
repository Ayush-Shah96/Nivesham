from __future__ import annotations

"""Persistent per-user TradeStat watchlists with separate Import/Export sets."""

import json
import os
import re
import threading
import logging
import os
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
logger = logging.getLogger(__name__)
_CACHE_DIR = Path(os.getenv("SECTOR_SCREENER_CACHE_DIR") or (Path(__file__).resolve().parents[2] / "data" / "sector_screener_cache"))
_FILE = _CACHE_DIR / "tradestat_watchlists.json"
_ALLOWED_LENGTHS = {2, 4, 6, 8}
_ALLOWED_TRADES = {"import", "export"}
MAX_CODES_PER_TRADE = 30


def normalize_trade_type(value: str | None) -> str:
    trade = str(value or "").strip().casefold()
    if trade not in _ALLOWED_TRADES:
        raise ValueError("Trade type must be import or export")
    return trade


def _copy_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict) and item.get("hs_code")]


def _normalize_user_entry(entry: Any) -> dict[str, list[dict[str, Any]]]:
    # Backward-compatible migration from the old single-list structure.
    if isinstance(entry, list):
        legacy = _copy_items(entry)
        return {"imports": list(legacy), "exports": list(legacy)}
    if isinstance(entry, dict):
        return {
            "imports": _copy_items(entry.get("imports")),
            "exports": _copy_items(entry.get("exports")),
        }
    return {"imports": [], "exports": []}


def _read() -> dict[str, dict[str, list[dict[str, Any]]]]:
    try:
        payload = json.loads(_FILE.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {}
        return {str(user_id): _normalize_user_entry(entry) for user_id, entry in payload.items()}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _write(payload: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = _FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, _FILE)


def normalize_hs_code(value: str) -> str:
    code = re.sub(r"\D", "", str(value or ""))
    if len(code) not in _ALLOWED_LENGTHS:
        raise ValueError("HS code must contain 2, 4, 6 or 8 digits")
    return code


def get_watchlists(user_id: str | int) -> dict[str, list[dict[str, Any]]]:
    with _LOCK:
        entry = _normalize_user_entry(_read().get(str(user_id)))
        result = {"imports": [dict(item) for item in entry["imports"]], "exports": [dict(item) for item in entry["exports"]]}
        logger.info("TRADESTAT_WATCHLIST_READ pid=%s user_id=%s imports=%d exports=%d", os.getpid(), user_id, len(result["imports"]), len(result["exports"]))
        return result


def get_watchlist(user_id: str | int, trade_type: str = "import") -> list[dict[str, Any]]:
    trade = normalize_trade_type(trade_type)
    return get_watchlists(user_id)[f"{trade}s"]


def add_watchlist_item(
    user_id: str | int,
    hs_code: str,
    commodity: str | None = None,
    trade_type: str = "import",
) -> list[dict[str, Any]]:
    code = normalize_hs_code(hs_code)
    trade = normalize_trade_type(trade_type)
    description = re.sub(r"\s+", " ", str(commodity or "")).strip()[:160] or f"HS {code}"
    key = f"{trade}s"
    with _LOCK:
        payload = _read()
        entry = _normalize_user_entry(payload.get(str(user_id)))
        items = [dict(item) for item in entry[key]]
        if any(str(item.get("hs_code")) == code for item in items):
            return items
        if len(items) >= MAX_CODES_PER_TRADE:
            raise ValueError(f"A maximum of {MAX_CODES_PER_TRADE} watched HS codes is supported per {trade} watchlist")
        items.append({"hs_code": code, "commodity": description, "active": True, "trade_type": trade})
        entry[key] = items
        payload[str(user_id)] = entry
        _write(payload)
        logger.info("TRADESTAT_WATCHLIST_ADD pid=%s user_id=%s trade_type=%s hs_code=%s count=%d", os.getpid(), user_id, trade, code, len(items))
        return [dict(item) for item in items]


def remove_watchlist_item(user_id: str | int, hs_code: str, trade_type: str = "import") -> list[dict[str, Any]]:
    code = normalize_hs_code(hs_code)
    trade = normalize_trade_type(trade_type)
    key = f"{trade}s"
    with _LOCK:
        payload = _read()
        entry = _normalize_user_entry(payload.get(str(user_id)))
        entry[key] = [
            dict(item) for item in entry[key]
            if isinstance(item, dict) and str(item.get("hs_code")) != code
        ]
        payload[str(user_id)] = entry
        _write(payload)
        logger.info("TRADESTAT_WATCHLIST_DELETE pid=%s user_id=%s trade_type=%s hs_code=%s count=%d", os.getpid(), user_id, trade, code, len(entry[key]))
        return [dict(item) for item in entry[key]]


def get_all_watchlists() -> dict[str, dict[str, list[dict[str, Any]]]]:
    with _LOCK:
        payload = _read()
        return {
            str(user_id): {
                "imports": [dict(item) for item in entry["imports"]],
                "exports": [dict(item) for item in entry["exports"]],
            }
            for user_id, entry in payload.items()
        }
