"""Run the canonical NewsAlert ingestion pipeline on demand.

This command is retained for operational compatibility, but it no longer writes
legacy Article rows or projects them into news_alert_items. Every selected source
runs through apps.newsalert.pipeline and persists directly to news_alert_items.

Examples (run from backend/):
    python scripts/backfill_news_alert.py --source all
    python scripts/backfill_news_alert.py --source x
    python scripts/backfill_news_alert.py --source youtube
    python scripts/backfill_news_alert.py --source stockscan
    python scripts/backfill_news_alert.py --source cnbc,moneycontrol

Public source access may be limited by the publisher. This command never bypasses
login gates, CAPTCHAs, or other access controls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from apps.newsalert.routes import ingest_configured_news  # noqa: E402
from ii_platform.database.connection import dispose_engine, get_sessionmaker  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("newsalert.canonical-backfill")

SPECIAL_SOURCES = {
    "rss", "financial_news", "financial-news", "news", "google_news", "googlenews",
    "youtube", "youtube_interviews", "stockscan", "stockscans", "x", "twitter", "x_posts", "nse", "nse_india",
    "regulatory", "all",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run NewsAlert ingestion into news_alert_items")
    parser.add_argument(
        "--source", default="all",
        help="all or comma-separated sources (rss, google_news, youtube, stockscan, x, nse, or registry source keys)",
    )
    parser.add_argument("--limit-windows", type=int, default=1,
                        help="Accepted for compatibility with the old command; legacy historical windows are no longer used")
    parser.add_argument("--limit-per-feed", type=int, default=30)
    parser.add_argument(
        "--youtube-lookback-days", type=int, default=2,
        help="YouTube lookback window for this run (1-30 days; default: 2)",
    )
    parser.add_argument(
        "--stockscan-lookback-days", type=int, default=30,
        help="StockScans Interview Scans lookback window (1-30 days; default: 30)",
    )
    return parser.parse_args()


def source_plan(raw_source: str) -> dict[str, object]:
    tokens = [part.strip().casefold() for part in str(raw_source or "all").split(",") if part.strip()]
    if not tokens or "all" in tokens:
        return {
            "source_ids": None,
            "include_rss": True,
            "include_google_news": True,
            "include_nse": True,
            "include_youtube": True,
            "include_x_posts": True,
            "include_stockscan": False,
        }

    x_tokens = {"x", "twitter", "x_posts"}
    youtube_tokens = {"youtube", "youtube_interviews"}
    stockscan_tokens = {"stockscan", "stockscans"}
    nse_tokens = {"nse", "nse_india", "regulatory"}
    google_tokens = {"google_news", "googlenews"}
    generic_rss_tokens = {"rss", "financial_news", "financial-news", "news"}
    named_rss = [token for token in tokens if token not in SPECIAL_SOURCES]
    want_rss = bool(named_rss or set(tokens) & generic_rss_tokens)
    return {
        "source_ids": named_rss or None,
        "include_rss": want_rss,
        "include_google_news": bool(set(tokens) & google_tokens),
        "include_nse": bool(set(tokens) & nse_tokens),
        "include_youtube": bool(set(tokens) & youtube_tokens),
        "include_x_posts": bool(set(tokens) & x_tokens),
        "include_stockscan": bool(set(tokens) & stockscan_tokens),
    }


async def async_main(args: argparse.Namespace) -> int:
    if args.limit_windows < 0:
        raise ValueError("--limit-windows must be >= 0")
    if not 1 <= int(args.youtube_lookback_days) <= 30:
        raise ValueError("--youtube-lookback-days must be between 1 and 30")
    if not 1 <= int(args.stockscan_lookback_days) <= 30:
        raise ValueError("--stockscan-lookback-days must be between 1 and 30")
    # Keep StockScans history independent from the YouTube discovery window.
    os.environ["NEWSALERT_YOUTUBE_LOOKBACK_DAYS"] = str(int(args.youtube_lookback_days))
    os.environ["NEWSALERT_STOCKSCAN_LOOKBACK_DAYS"] = str(int(args.stockscan_lookback_days))
    # Collector has legacy module-level constants used by some fallback methods.
    from ii_platform.web.collectors import youtube_news_collector  # noqa: E402
    youtube_news_collector.MAX_LOOKBACK_DAYS = int(args.youtube_lookback_days)
    youtube_news_collector.MAX_LOOKBACK_HOURS = int(args.youtube_lookback_days) * 24
    limit_per_feed = max(5, min(int(args.limit_per_feed), 100))
    plan = source_plan(args.source)
    logger.info(
        "NEWSALERT_CANONICAL_RUN_START sources=%s limit_per_feed=%s youtube_lookback_days=%s stockscan_lookback_days=%s legacy_windows_ignored=%s",
        args.source, limit_per_feed, args.youtube_lookback_days, args.stockscan_lookback_days, args.limit_windows,
    )
    try:
        async with get_sessionmaker()() as db:
            try:
                result = await ingest_configured_news(
                    db,
                    user_id=None,
                    limit_per_feed=limit_per_feed,
                    include_regulatory=("nse" in str(args.source).casefold() or "regulatory" in str(args.source).casefold()),
                    **plan,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        logger.info(
            "NEWSALERT_CANONICAL_RUN_DONE status=%s fetched=%s created=%s sources=%s",
            result.get("status"), result.get("items_fetched"), result.get("created"),
            json.dumps(result.get("sources", {}), ensure_ascii=False, default=str),
        )
        if result.get("status") == "failed" or int(result.get("failed") or 0) > 0:
            return 2
        return 0
    finally:
        await dispose_engine()


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
