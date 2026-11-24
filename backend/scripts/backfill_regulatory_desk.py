from __future__ import annotations

"""Backfill the unified Regulatory Activity feed only.

Usage from backend root:
    python scripts/backfill_regulatory_activity.py --days 7

No attachment documents are archived. Original exchange URLs are retained on
normalized event records.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import func, select  # noqa: E402

from apps.regulatory_activity.service import sync_regulatory_activity  # noqa: E402
from ii_platform.database.connection import dispose_engine, get_sessionmaker  # noqa: E402
from ii_platform.database.models import RegulatoryActivityEvent  # noqa: E402

LOG = logging.getLogger("regulatory-activity-backfill")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Collect the unified NSE Regulatory Activity event feed.")
    parser.add_argument("--days", type=int, default=7, help="Lookback calendar days (1–2557; default: 7).")
    parser.add_argument(
        "--skip-market-prices", action="store_true",
        help="Skip CMP enrichment. By default CMP is fetched during the regulatory sync.",
    )
    # Retain the old flag for existing operator commands. CMP refresh is now
    # enabled by default inside sync_regulatory_activity; this flag is a no-op.
    parser.add_argument("--with-market-prices", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--price-sessions", type=int, default=6, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.days <= 2557:
        parser.error("--days must be between 1 and 2557")
    if args.skip_market_prices and args.with_market_prices:
        parser.error("--skip-market-prices cannot be combined with --with-market-prices")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    session_factory = get_sessionmaker()
    try:
        async with session_factory() as db:
            result = await sync_regulatory_activity(
                db, days=args.days, refresh_market_prices=not args.skip_market_prices,
            )
            await db.commit()
            total = int(await db.scalar(select(func.count()).select_from(RegulatoryActivityEvent)) or 0)
            grouped = (await db.execute(
                select(RegulatoryActivityEvent.activity_type, func.count(RegulatoryActivityEvent.id))
                .group_by(RegulatoryActivityEvent.activity_type)
            )).all()
        print("\n=== REGULATORY ACTIVITY BACKFILL SUMMARY ===")
        print("result=" + json.dumps(result, default=str, sort_keys=True))
        print(f"regulatory_activity_events_total={total}")
        print("regulatory_activity_events_by_type=" + json.dumps({str(k): int(v) for k, v in grouped}, sort_keys=True))
        print("cmp_enrichment=" + json.dumps(result.get("cmp_enrichment") or {}, sort_keys=True))
        print("DOCUMENT_ARCHIVAL=disabled")
        print("LEGACY_NEWSALERT_PIPELINE=not_run")
        complete = bool(result.get("complete"))
        return 0 if complete else 2
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
