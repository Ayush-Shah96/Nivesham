
"""
Clear previous Debt Tracker runs.

Deletes:
    debt_tracker_jobs

Preserves:
    debt_tracker_mappings
    debt_tracker_mapping_history
    All other application tables

Run from the backend directory:
    python scripts/clear_debt_tracker_runs.py

Preview without deleting:
    python scripts/clear_debt_tracker_runs.py --dry-run
"""

import argparse
import asyncio
import sys
from pathlib import Path
from sqlalchemy import func, select, delete

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from apps.debt_tracker.models import DebtTrackerJob
from ii_platform.database.connection import (
    get_sessionmaker,
    dispose_engine,
)


async def clear_runs(dry_run: bool = False):
    session_factory = get_sessionmaker()

    async with session_factory() as db:
        result = await db.execute(
            select(func.count()).select_from(DebtTrackerJob)
        )
        count = result.scalar_one()

        print(f"\nDebt Tracker runs found: {count}")

        if dry_run:
            print("DRY RUN: No records deleted.")
            return

        if count == 0:
            print("Nothing to clear.")
            return

        confirmation = input(
            "\nType CLEAR DEBT RUNS to permanently delete these runs: "
        ).strip()

        if confirmation != "CLEAR DEBT RUNS":
            print("Cancelled. No records deleted.")
            return

        await db.execute(delete(DebtTrackerJob))
        await db.commit()

        print(f"\nSuccessfully deleted {count} Debt Tracker run records.")
        print("Approved mappings: preserved.")
        print("Mapping history: preserved.")
        print("Other application tables: untouched.")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count existing runs without deleting them.",
    )
    args = parser.parse_args()

    try:
        await clear_runs(dry_run=args.dry_run)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
