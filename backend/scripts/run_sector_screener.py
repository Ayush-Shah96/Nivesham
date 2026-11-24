from __future__ import annotations

"""Backfill the unified NSE Regulatory Activity feed.

Run from the backend root:
    python scripts/backfill_regulatory_desk_30d.py

Optional:
    python scripts/backfill_regulatory_desk_30d.py --days 30 --skip-market-prices

The default window is the latest 30 calendar days, inclusive of today's date
in Asia/Kolkata. It runs the existing Regulatory Activity service, which
collects Insider Trading, Bulk/Block Deals, QIP, and Preferential/Warrant
records and persists them into the existing unified event table.

This is not a dry run. Successful source categories are committed by the
existing service as they finish. Re-running is supported by source-key upserts.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import func, select  # noqa: E402

from apps.regulatory_activity.service import sync_regulatory_activity  # noqa: E402
from ii_platform.database.connection import dispose_engine, get_sessionmaker  # noqa: E402
from ii_platform.database.models import RegulatoryActivityEvent  # noqa: E402

logger = logging.getLogger("regulatory-activity-backfill")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the unified NSE Regulatory Activity feed "
            "(Insider, Bulk/Block, QIP, Preferential/Warrant)."
        )
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback calendar days inclusive of today in Asia/Kolkata (default: 30; max: 2557).",
    )
    parser.add_argument(
        "--skip-market-prices",
        action="store_true",
        help="Skip CMP enrichment to reduce external requests during the backfill.",
    )
    args = parser.parse_args()
    if not 1 <= args.days <= 2557:
        parser.error("--days must be between 1 and 2557")
    return args


async def run_backfill(days: int, refresh_market_prices: bool) -> int:
    try:
        session_factory = get_sessionmaker()
        async with session_factory() as db:
            result: dict[str, Any] = await sync_regulatory_activity(
                db,
                days=days,
                refresh_market_prices=refresh_market_prices,
            )
            # sync_regulatory_activity commits each successful source category;
            # this final commit also closes any remaining transaction cleanly.
            await db.commit()

            total_events = int(
                await db.scalar(
                    select(func.count()).select_from(RegulatoryActivityEvent)
                )
                or 0
            )
            rows_by_type = (
                await db.execute(
                    select(
                        RegulatoryActivityEvent.activity_type,
                        func.count(RegulatoryActivityEvent.id),
                    ).group_by(RegulatoryActivityEvent.activity_type)
                )
            ).all()

        print("\n=== REGULATORY DESK BACKFILL SUMMARY ===")
        print(f"requested_lookback_days={days}")
        print(f"window_start={result.get('window_start')}")
        print(f"window_end={result.get('window_end')}")
        print(f"status={result.get('status')}")
        print(f"complete={bool(result.get('complete'))}")
        print(f"received={int(result.get('received') or 0)}")
        print(f"inserted={int(result.get('inserted') or 0)}")
        print(f"updated={int(result.get('updated') or 0)}")
        print(f"rejected={int(result.get('rejected') or 0)}")
        print("categories=" + json.dumps(result.get("categories") or {}, default=str, sort_keys=True))
        print("failed_sources=" + json.dumps(result.get("failed_sources") or []))
        print("cmp_enrichment=" + json.dumps(result.get("cmp_enrichment") or {}, sort_keys=True))
        print(f"total_events_in_database={total_events}")
        print(
            "database_events_by_activity_type="
            + json.dumps({str(kind): int(count) for kind, count in rows_by_type}, sort_keys=True)
        )
        print("legacy_regulatory_signal_pipeline=not_run")
        print("document_archival=disabled")

        # Exit 2 signals that the run completed but one or more categories were
        # partial/failed. Successful categories have still been committed.
        return 0 if bool(result.get("complete")) else 2
    except Exception:
        logger.exception("Regulatory Desk backfill failed")
        return 1
    finally:
        await dispose_engine()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(
        run_backfill(
            days=args.days,
            refresh_market_prices=not args.skip_market_prices,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
