#!/usr/bin/env python3
"""Safely clear ONLY the regulatory_activity_events table.

Run from the backend directory so the application's backend/.env and
DATABASE_URL are picked up by ii_platform.database.connection:

    python scripts/clear_regulatory_activity_events.py --confirm regulatory_activity_events

Without the exact --confirm value, the script performs a read-only row count.
It deliberately uses DELETE (not TRUNCATE CASCADE), so unrelated tables and
foreign-key-linked data are not cascaded/deleted. It does not clear the
regulatory source-run audit/history table.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import text

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from ii_platform.database.connection import get_engine

TABLE_NAME = "regulatory_activity_events"
CONFIRM_VALUE = "regulatory_activity_events"


async def _run(confirm: str | None) -> int:
    engine = get_engine()
    try:
        async with engine.connect() as connection:
            dialect = connection.dialect.name
            if dialect == "postgresql":
                target_result = await connection.execute(
                    text("SELECT current_database() AS database_name, current_schema() AS schema_name")
                )
                target: Any = target_result.mappings().one()
                database_name = target["database_name"]
                schema_name = target["schema_name"]
            else:
                database_name = "(database name unavailable)"
                schema_name = "main" if dialect == "sqlite" else "(default)"

            exists_result = await connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = :schema_name AND table_name = :table_name"
                ) if dialect == "postgresql" else text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = :table_name"
                ),
                {"schema_name": schema_name, "table_name": TABLE_NAME},
            )
            if int(exists_result.scalar_one()) == 0:
                print(f"ERROR: {schema_name}.{TABLE_NAME} does not exist in {database_name}.")
                return 2

            count_result = await connection.execute(
                text(f'SELECT COUNT(*) FROM "{TABLE_NAME}"')
            )
            before = int(count_result.scalar_one())
            print(f"Database : {database_name}")
            print(f"Schema   : {schema_name}")
            print(f"Table    : {TABLE_NAME}")
            print(f"Rows now : {before}")

        if confirm != CONFIRM_VALUE:
            print("\nDRY RUN ONLY — nothing was deleted.")
            print(
                "To clear this table, rerun with: "
                "--confirm regulatory_activity_events"
            )
            return 0

        # Use a transaction and DELETE only this table. No CASCADE and no other
        # tables are touched. If deletion fails, SQLAlchemy rolls it back.
        async with engine.begin() as connection:
            result = await connection.execute(
                text(f'DELETE FROM "{TABLE_NAME}"')
            )
            deleted = int(result.rowcount or 0)
            after_result = await connection.execute(
                text(f'SELECT COUNT(*) FROM "{TABLE_NAME}"')
            )
            after = int(after_result.scalar_one())
            if after != 0:
                raise RuntimeError(
                    f"Clear did not complete: {after} rows remain in {TABLE_NAME}."
                )

        print("\nSUCCESS")
        print(f"Rows deleted: {deleted}")
        print(f"Rows remaining: {after}")
        print("Other tables and regulatory source-run history were not changed.")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Clear only the {TABLE_NAME} table (safe dry-run by default)."
    )
    parser.add_argument(
        "--confirm",
        default=None,
        help=f"Required exact confirmation value: {CONFIRM_VALUE}",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args.confirm))
    except KeyboardInterrupt:
        print("Cancelled; no further action was taken.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
