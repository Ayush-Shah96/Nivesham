"""Clear debt tracker run history and stored job files.

Usage (from the backend root):
    python scripts/clear_debt_tracker.py --storage "<DEBT_TRACKER_STORAGE_DIR>" --yes
    python scripts/clear_debt_tracker.py --storage "<dir>" --yes --drop-folio-mapping

What it does:
  1. Makes a timestamped backup of jobs.sqlite3 (kept in the same folder).
  2. Deletes every row in the jobs table (run history).
  3. Deletes every per-run folder under jobs/ (uploaded workbooks and outputs).
  4. Keeps the folio_brokers table unless --drop-folio-mapping is given.

Stop the backend before running this, so no run is writing while files are removed.
Without --yes the script only prints what it would delete.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--storage", required=True, help="Debt tracker storage directory (DEBT_TRACKER_STORAGE_DIR)")
    parser.add_argument("--drop-folio-mapping", action="store_true", help="Also empty the folio_brokers table")
    parser.add_argument("--yes", action="store_true", help="Actually delete. Without this, nothing is changed.")
    args = parser.parse_args()

    storage = Path(args.storage)
    database = storage / "jobs.sqlite3"
    jobs_root = storage / "jobs"
    if not database.exists():
        print(f"No database found at {database}. Nothing to clear.")
        return 0

    with sqlite3.connect(database, timeout=30) as connection:
        job_count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        folio_count = 0
        has_folio = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='folio_brokers'"
        ).fetchone()
        if has_folio and args.drop_folio_mapping:
            folio_count = connection.execute("SELECT COUNT(*) FROM folio_brokers").fetchone()[0]
    run_dirs = [p for p in jobs_root.iterdir() if p.is_dir()] if jobs_root.exists() else []

    print(f"Storage: {storage}")
    print(f"  Run history rows to delete : {job_count}")
    print(f"  Run folders to delete      : {len(run_dirs)}")
    if args.drop_folio_mapping:
        print(f"  Folio mapping rows to delete: {folio_count}")
    else:
        print("  Folio mapping              : kept")

    if not args.yes:
        print("\nDry run. Add --yes to delete.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = storage / f"jobs-backup-{stamp}.sqlite3"
    shutil.copy2(database, backup)
    print(f"\nBackup written: {backup}")

    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("DELETE FROM jobs")
        if has_folio and args.drop_folio_mapping:
            connection.execute("DELETE FROM folio_brokers")
        connection.commit()
        connection.execute("VACUUM")

    for run_dir in run_dirs:
        shutil.rmtree(run_dir, ignore_errors=True)

    print("Debt tracker history cleared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())