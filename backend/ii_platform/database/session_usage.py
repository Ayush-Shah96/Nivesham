"""Small helpers for persistent application usage stored in user_sessions.app_usage.

No separate usage table is required: each session row retains its own total
and compact per-day/per-application seconds inside the existing JSON column.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

USAGE_SCHEMA_VERSION = 2
REPORTING_TZ = ZoneInfo("Asia/Kolkata")


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalise_app_usage(raw: Any, active_seconds: int | float | None = 0) -> dict[str, Any]:
    """Return a mutable schema-v2 usage object, preserving legacy app totals.

    Legacy usage was stored as {"Settings": seconds, ...}. Since that shape did
    not include dates, it is preserved as an unbucketed baseline and never
    fabricated into daily history except when the reporting endpoint explicitly
    labels it as legacy/unbucketed data.
    """
    source = dict(raw) if isinstance(raw, dict) else {}
    if source.get("_schema") == USAGE_SCHEMA_VERSION:
        result = source
        result["by_app"] = dict(result.get("by_app") or {})
        result["by_day"] = dict(result.get("by_day") or {})
        result["_legacy_app_usage"] = dict(result.get("_legacy_app_usage") or {})
        result["_legacy_seconds"] = max(0, int(result.get("_legacy_seconds") or 0))
        result["_tracking"] = dict(result.get("_tracking") or {})
        return result

    legacy: dict[str, int] = {}
    for key, value in source.items():
        if str(key).startswith("_") or isinstance(value, bool):
            continue
        try:
            seconds = int(float(value))
        except (TypeError, ValueError, OverflowError):
            continue
        if seconds >= 0:
            legacy[str(key)[:120]] = seconds

    baseline = max(0, int(active_seconds or 0))
    return {
        "_schema": USAGE_SCHEMA_VERSION,
        "by_app": dict(legacy),
        "by_day": {},
        "_legacy_seconds": baseline,
        "_legacy_app_usage": dict(legacy),
        "_tracking": {},
    }


def credit_usage_interval(
    usage: dict[str, Any],
    app_name: str | None,
    interval_start: datetime,
    seconds: int,
) -> None:
    """Credit a bounded interval to both per-app and per-IST-day buckets."""
    amount = max(0, int(seconds))
    if amount <= 0:
        return

    app = (str(app_name or "Workspace").strip() or "Workspace")[:120]
    by_app = dict(usage.get("by_app") or {})
    by_app[app] = max(0, int(by_app.get(app, 0) or 0)) + amount
    usage["by_app"] = by_app

    by_day = dict(usage.get("by_day") or {})
    cursor = as_utc(interval_start)
    if cursor is None:
        return
    remaining = amount
    # Heartbeats are bounded to 90 seconds, so this loop is tiny even at midnight.
    while remaining > 0:
        local_cursor = cursor.astimezone(REPORTING_TZ)
        next_midnight = datetime.combine(
            local_cursor.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=REPORTING_TZ,
        )
        to_midnight = max(0.0, (next_midnight - local_cursor).total_seconds())
        if to_midnight <= 0:
            cursor = cursor + timedelta(seconds=1)
            continue
        portion = min(remaining, max(1, int(math.ceil(to_midnight))))
        day_key = local_cursor.date().isoformat()
        day_apps = dict(by_day.get(day_key) or {})
        day_apps[app] = max(0, int(day_apps.get(app, 0) or 0)) + portion
        by_day[day_key] = day_apps
        cursor += timedelta(seconds=portion)
        remaining -= portion
    usage["by_day"] = by_day
