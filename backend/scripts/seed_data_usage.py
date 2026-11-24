"""Idempotently seed the supplied App Usage matrix into user_sessions.app_usage.

Run from the backend directory with the same .env/DATABASE_URL as the API:
    python scripts/seed_app_usage.py --dry-run
    python scripts/seed_app_usage.py

Optional: create an active, zero-usage session for an existing user, for testing
that Admin Usage continues to show users who logged in but have no recorded use:
    python scripts/seed_app_usage.py --zero-usage-user some_existing_username

Only rows with deterministic `appusage-seed-*` session IDs are inserted/updated.
The script does not create users, alter their real sessions, or create any table.
The supplied minutes are demonstration/seed values, not retroactive telemetry.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Ensure package imports work when invoked as `python scripts/seed_app_usage.py`.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select

from ii_platform.database.connection import dispose_engine, session_scope
from ii_platform.database.models import User, UserSession

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
APP_NAME = "Workspace"
SEED_SOURCE = "manual_app_usage_seed"

# The order matches the requested matrix columns: 16 Oct, 15 Oct, 14 Oct, 13 Oct.
MATRIX: dict[str, dict[str, int | None]] = {
    "priyesh": {"2026-10-16": 12, "2026-10-15": 3,  "2026-10-14": 9,  "2026-10-13": None},
    "nayan":   {"2026-10-16": 8,  "2026-10-15": 18, "2026-10-14": 5,  "2026-10-13": 2},
    "romil":   {"2026-10-16": 17, "2026-10-15": 2,  "2026-10-14": 7,  "2026-10-13": None},
    "jainam":  {"2026-10-16": 11, "2026-10-15": 8,  "2026-10-14": 8,  "2026-10-13": 4},
    "ayush":   {"2026-10-16": 28, "2026-10-15": 32, "2026-10-14": 33, "2026-10-13": 43},
    "aashil":  {"2026-10-16": 13, "2026-10-15": 10, "2026-10-14": 14, "2026-10-13": 6},
    "miksha":  {"2026-10-16": 0,  "2026-10-15": 9,  "2026-10-14": None, "2026-10-13": 11},
}


def _stable_session_id(prefix: str, username: str) -> str:
    digest = hashlib.sha256(username.casefold().encode("utf-8")).hexdigest()[:24]
    return f"appusage-seed-{prefix}-{digest}"[:64]


def _local_instant(day: str, clock: time) -> datetime:
    d = date.fromisoformat(day)
    return datetime.combine(d, clock, tzinfo=IST).astimezone(UTC)


def _usage_payload(daily_minutes: dict[str, int | None], *, source: str) -> tuple[dict[str, Any], int]:
    by_day: dict[str, dict[str, int]] = {}
    by_app_seconds: dict[str, int] = {}
    total = 0
    for day_key, minutes in daily_minutes.items():
        # None means the supplied table showed “-”; it is not an invented 0m entry.
        if minutes is None or minutes <= 0:
            continue
        seconds = int(minutes) * 60
        by_day.setdefault(day_key, {})[APP_NAME] = seconds
        by_app_seconds[APP_NAME] = by_app_seconds.get(APP_NAME, 0) + seconds
        total += seconds

    payload: dict[str, Any] = {
        "_schema": 2,
        "by_app": by_app_seconds,
        "by_day": by_day,
        "_legacy_seconds": 0,
        "_legacy_app_usage": {},
        "_tracking": {
            "visible": False,
            "source": source,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    }
    return payload, total


async def _seed_matrix(*, dry_run: bool, zero_usage_user: str | None) -> None:
    async with session_scope() as db:
        users = (await db.execute(select(User))).scalars().all()
        by_name = {str(user.username).strip().casefold(): user for user in users}

        missing = [username for username in MATRIX if username.casefold() not in by_name]
        if missing:
            raise SystemExit(
                "These usernames do not exist in the configured database: " + ", ".join(missing)
                + ". Check DATABASE_URL; this script never creates user accounts."
            )
        if zero_usage_user and zero_usage_user.strip().casefold() not in by_name:
            raise SystemExit(
                f"--zero-usage-user {zero_usage_user!r} does not match an existing username."
            )

        report: list[dict[str, Any]] = []
        for username, daily_minutes in MATRIX.items():
            user = by_name[username.casefold()]
            session_id = _stable_session_id("matrix", user.username)
            row = await db.scalar(
                select(UserSession).where(UserSession.session_id == session_id)
            )
            payload, total_seconds = _usage_payload(daily_minutes, source=SEED_SOURCE)
            first_day = min(daily_minutes)
            last_day = max(daily_minutes)
            login_at = _local_instant(first_day, time(9, 0))
            logout_at = _local_instant(last_day, time(18, 0))
            last_seen_at = logout_at

            values = {
                "user_id": user.id,
                "login_at": login_at,
                "logout_at": logout_at,
                "last_seen_at": last_seen_at,
                "last_app": APP_NAME if total_seconds else None,
                "active_seconds": total_seconds,
                "app_usage": payload,
            }
            if row is None:
                row = UserSession(session_id=session_id, **values)
                db.add(row)
                action = "would insert" if dry_run else "inserted"
            else:
                for field, value in values.items():
                    setattr(row, field, value)
                action = "would update" if dry_run else "updated"

            report.append({"username": user.username, "action": action,
                           "total_minutes": total_seconds // 60,
                           "daily_minutes": daily_minutes})

        if zero_usage_user:
            user = by_name[zero_usage_user.strip().casefold()]
            session_id = _stable_session_id("zero", user.username)
            row = await db.scalar(
                select(UserSession).where(UserSession.session_id == session_id)
            )
            now = datetime.now(UTC)
            payload: dict[str, Any] = {
                "_schema": 2,
                "by_app": {},
                "by_day": {},
                "_legacy_seconds": 0,
                "_legacy_app_usage": {},
                "_tracking": {
                    "visible": True,
                    "source": SEED_SOURCE,
                    "updated_at": now.isoformat(),
                },
            }
            values = {
                "user_id": user.id,
                "login_at": now - timedelta(minutes=2),
                "logout_at": None,
                "last_seen_at": now,
                "last_app": None,
                "active_seconds": 0,
                "app_usage": payload,
            }
            if row is None:
                db.add(UserSession(session_id=session_id, **values))
                action = "would insert zero-usage signed-in session" if dry_run else "inserted zero-usage signed-in session"
            else:
                for field, value in values.items():
                    setattr(row, field, value)
                action = "would update zero-usage signed-in session" if dry_run else "updated zero-usage signed-in session"
            report.append({"username": user.username, "action": action,
                           "total_minutes": 0, "daily_minutes": {}})

        if dry_run:
            await db.rollback()
            print("DRY RUN — no changes committed")
        else:
            await db.commit()
            print("Seed committed to user_sessions.app_usage")

        print(json.dumps(report, indent=2, default=str))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Validate users and planned changes without committing")
    parser.add_argument(
        "--zero-usage-user", metavar="USERNAME", default=None,
        help="Optionally create an active session with zero usage for an existing user (testing only)",
    )
    args = parser.parse_args()
    try:
        await _seed_matrix(dry_run=args.dry_run, zero_usage_user=args.zero_usage_user)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
