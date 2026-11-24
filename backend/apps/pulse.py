from __future__ import annotations

"""Pulse API orchestration: detect unusual movements and investigate possible catalysts.

Scraping and provider-specific HTTP work lives in ii_platform/web/collectors.
This module owns thresholds, signal generation, event history and API contracts.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Path as ApiPath, Query

from infrastructure.security.permissions import require_role
from ii_platform.database.models import User, UserRole
from ii_platform.database.connection import get_sessionmaker
from apps.coverage_list import get_pulse_coverage_rows
from ii_platform.web.collectors.pulse_market_collector import collector as legacy_collector
from ii_platform.web.collectors.pulse_market_data_collector import (
    pulse_market_data_collector,
    coverage_sector_for,
)
from ii_platform.web.collectors.pulse_news_collector import pulse_news_collector
from ii_platform.web.collectors.pulse_exchange_filings_collector import pulse_exchange_filings_collector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pulse", tags=["Pulse"])
IST = ZoneInfo("Asia/Kolkata")

# Backward-compatible export: main.py from the original project closes this
# collector on shutdown. It is no longer used as a market-data source by Pulse.
collector = legacy_collector

CACHE_SECONDS = max(5, int(os.getenv("PULSE_DASHBOARD_CACHE_SECONDS", "15")))
EVENT_LIMIT = max(100, int(os.getenv("PULSE_EVENT_HISTORY_LIMIT", "500")))
STORE_PATH = Path(os.getenv("PULSE_EVENT_STORE_PATH", str(Path.cwd() / "data" / "pulse" / "movement_events.json")))
SNAPSHOT_PATH = Path(os.getenv("PULSE_SNAPSHOT_STORE_PATH", str(STORE_PATH.with_name("current_dashboard.json"))))
SHARED_SNAPSHOT_MAX_AGE = max(30, int(os.getenv("PULSE_SHARED_SNAPSHOT_MAX_AGE_SECONDS", "75")))
STOCK_MOVE_5M = float(os.getenv("PULSE_STOCK_MOVE_5M_PCT", "0.80"))
STOCK_MOVE_15M = float(os.getenv("PULSE_STOCK_MOVE_15M_PCT", "1.50"))
SECTOR_MOVE_5M = float(os.getenv("PULSE_SECTOR_MOVE_5M_PCT", "0.45"))
SECTOR_MOVE_15M = float(os.getenv("PULSE_SECTOR_MOVE_15M_PCT", "0.90"))
VOLUME_SPIKE_RATIO = float(os.getenv("PULSE_VOLUME_SPIKE_RATIO", "2.50"))
MAX_SIGNAL_AGE_SECONDS = max(60, int(os.getenv("PULSE_MAX_SIGNAL_AGE_SECONDS", "180")))

_cache_lock = asyncio.Lock()
_event_lock = asyncio.Lock()
_dashboard_cache: dict[str, Any] | None = None
_dashboard_cached_at = 0.0
_last_good_dashboard: dict[str, Any] | None = None
_coverage_watchlist_cache: list[dict[str, Any]] = []
_coverage_watchlist_cached_at = 0.0

# Maps official sector-index keys to the business classifications already
# assigned to instruments in Coverage_List.xlsx. The workbook does not contain
# official index membership, so these are research-theme associations only.
SECTOR_COVERAGE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "BANK": ("Banking",),
    "IT": ("Technology & Electronics", "Electricals & Cables"),
    "AUTO": ("Automobile",),
    "PHARMA": ("Pharmaceuticals",),
    "FMCG": ("Beverages & Consumer",),
    "METAL": ("Metals", "Metals & Recycling"),
    "ENERGY": ("Energy",),
    "FINANCE": ("Financial Services", "Banking"),
}

# The Coverage List contains no real-estate developers/REITs, so NIFTY Realty
# has no direct mapped coverage names. These companies operate in adjacent
# construction/building-products supply chains and are shown separately as
# related research leads, never as NIFTY Realty constituents.
REALTY_ADJACENT_COVERAGE_SYMBOLS = {
    "INTERARCH", "EPACKPEB", "APLAPOLLO", "HITECH", "VENUSPIPES"
}


def _now() -> str:
    return datetime.now(IST).isoformat()


def _is_market_hours() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) < (15, 30)


def is_pulse_polling_window() -> bool:
    """Poll before and during the cash session; reduce calls overnight/weekends."""
    now = datetime.now(IST)
    return now.weekday() < 5 and (now.hour, now.minute) >= (8, 45) and (now.hour, now.minute) < (16, 15)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _coverage_basket_series(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a normalized equal-weighted intraday line from Coverage List bars.

    The first available bar for each member is its own base (100). Each later
    timestamp is the mean percentage return of members that have a bar at that
    timestamp. This is a research basket, not an official exchange index.
    """
    member_series: list[tuple[float, dict[int, dict[str, Any]]]] = []
    for member in members:
        points = member.get("series") or []
        parsed: dict[int, dict[str, Any]] = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            epoch = _number(point.get("epoch"))
            price = _number(point.get("price"))
            if epoch is None or price is None or price <= 0:
                continue
            parsed[int(epoch)] = point
        if parsed:
            first_epoch = min(parsed)
            base = _number(parsed[first_epoch].get("price"))
            if base and base > 0:
                member_series.append((base, parsed))

    if not member_series:
        return []

    returns_by_epoch: dict[int, list[float]] = {}
    timestamps: dict[int, str] = {}
    for base, points in member_series:
        for epoch, point in points.items():
            price = _number(point.get("price"))
            if price is None:
                continue
            returns_by_epoch.setdefault(epoch, []).append(((price / base) - 1.0) * 100.0)
            timestamps[epoch] = str(point.get("timestamp") or datetime.fromtimestamp(epoch, IST).isoformat())

    output = []
    for epoch in sorted(returns_by_epoch)[-120:]:
        values = returns_by_epoch[epoch]
        if not values:
            continue
        average_return = sum(values) / len(values)
        output.append({
            "timestamp": timestamps.get(epoch),
            "epoch": epoch,
            "price": round(100.0 * (1.0 + average_return / 100.0), 4),
            "contributors": len(values),
        })
    return output


def _instrument_signal(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("status") not in {"available", "eod_fallback"}:
        return None
    age_seconds = _number(row.get("age_seconds"))
    if row.get("freshness") == "stale" or age_seconds is None or age_seconds > MAX_SIGNAL_AGE_SECONDS:
        return None
    kind = row.get("kind")
    is_sector_or_index = kind in {"sector", "index"}
    move_5m = _number(row.get("change_5m_pct"))
    move_15m = _number(row.get("change_15m_pct"))
    day_move = _number(row.get("change_pct"))
    volume_ratio = _number(row.get("volume_ratio"))
    move_limit_5m = SECTOR_MOVE_5M if is_sector_or_index else STOCK_MOVE_5M
    move_limit_15m = SECTOR_MOVE_15M if is_sector_or_index else STOCK_MOVE_15M
    reasons: list[str] = []
    if move_5m is not None and abs(move_5m) >= move_limit_5m:
        reasons.append(f"{abs(move_5m):.2f}% move over 5 minutes")
    if move_15m is not None and abs(move_15m) >= move_limit_15m:
        reasons.append(f"{abs(move_15m):.2f}% move over 15 minutes")
    if volume_ratio is not None and volume_ratio >= VOLUME_SPIKE_RATIO and day_move is not None and abs(day_move) >= 0.50:
        reasons.append(f"volume running at {volume_ratio:.1f}x the recent minute-bar baseline")
    if not reasons or row.get("data_type") == "end_of_day":
        return None
    strongest = max(abs(value) for value in (move_5m, move_15m, 0.0) if value is not None)
    severity = "critical" if strongest >= 1.75 or (volume_ratio or 0) >= 4.0 else "high" if strongest >= 1.0 or (volume_ratio or 0) >= 3.0 else "notable"
    direction = "up" if (move_5m if move_5m is not None else move_15m or 0) >= 0 else "down"
    return {
        "kind": kind,
        "instrument_type": kind,
        "symbol": row.get("symbol"),
        "name": row.get("name") or row.get("symbol"),
        "sector": row.get("sector"),
        "direction": direction,
        "severity": severity,
        "price": row.get("price"),
        "change_pct": day_move,
        "change_5m_pct": move_5m,
        "change_15m_pct": move_15m,
        "volume_ratio": volume_ratio,
        "timestamp": row.get("timestamp"),
        "freshness": row.get("freshness"),
        "data_type": row.get("data_type"),
        "source": row.get("source"),
        "reasons": reasons,
        "explanation_status": "uninvestigated",
    }


async def _read_events() -> list[dict[str, Any]]:
    async with _event_lock:
        try:
            raw = await asyncio.to_thread(STORE_PATH.read_text, encoding="utf-8")
            payload = json.loads(raw)
            return payload if isinstance(payload, list) else []
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError):
            logger.warning("Pulse event history could not be read path=%s", STORE_PATH, exc_info=True)
            return []


async def _write_events(events: list[dict[str, Any]]) -> None:
    async with _event_lock:
        temporary: Path | None = None
        try:
            await asyncio.to_thread(STORE_PATH.parent.mkdir, parents=True, exist_ok=True)
            temporary = STORE_PATH.with_suffix(".tmp")
            content = json.dumps(events[:EVENT_LIMIT], ensure_ascii=False, separators=(",", ":"))
            await asyncio.to_thread(temporary.write_text, content, encoding="utf-8")
            await asyncio.to_thread(temporary.replace, STORE_PATH)
        except OSError:
            logger.warning("Pulse event history could not be persisted path=%s", STORE_PATH, exc_info=True)
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def _event_identity(signal: dict[str, Any]) -> str:
    # Coalesce repeated detections into a single event during a 10-minute window.
    timestamp = signal.get("timestamp") or _now()
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        bucket = int(dt.timestamp() // 600)
    except ValueError:
        bucket = int(time.time() // 600)
    return f"{signal.get('kind')}:{signal.get('symbol')}:{signal.get('direction')}:{bucket}"


async def _record_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = await _read_events()
    by_id = {str(item.get("event_id")): item for item in existing if item.get("event_id")}
    for signal in signals:
        event_id = _event_identity(signal)
        current = by_id.get(event_id)
        if current:
            current.update(signal)
            current["last_seen"] = _now()
            current["occurrences"] = int(current.get("occurrences", 1)) + 1
        else:
            by_id[event_id] = {
                **signal,
                "event_id": event_id,
                "first_detected": _now(),
                "last_seen": _now(),
                "occurrences": 1,
            }
    ordered = sorted(by_id.values(), key=lambda item: str(item.get("last_seen") or item.get("first_detected") or ""), reverse=True)[:EVENT_LIMIT]
    await _write_events(ordered)
    return ordered


async def _coverage_workbook_watchlist() -> list[dict[str, Any]]:
    """Load the stock universe directly from the Coverage List workbook."""
    global _coverage_watchlist_cache, _coverage_watchlist_cached_at
    if _coverage_watchlist_cached_at and time.monotonic() - _coverage_watchlist_cached_at < 1800:
        return _coverage_watchlist_cache
    try:
        async with get_sessionmaker()() as db:
            companies = await get_pulse_coverage_rows(db)
        rows = []
        for company in companies:
            symbol = str(company.get("symbol") or "").strip().upper()
            exchange = str(company.get("exchange") or "NSE").strip().upper()
            if not symbol or exchange not in {"NSE", "BSE"}:
                continue
            name = str(company.get("name") or symbol).strip()
            rows.append({
                **company,
                "symbol": symbol,
                "name": name,
                "sector": coverage_sector_for(symbol, name),
                "exchange": exchange,
                "yahoo": str(company.get("yahoo") or (f"{symbol}.NS" if exchange == "NSE" else f"{symbol}.BO")),
                "watchlist_source": "coverage_list",
            })
        _coverage_watchlist_cache = sorted(rows, key=lambda row: (row["exchange"], row["symbol"]))
        _coverage_watchlist_cached_at = time.monotonic()
        logger.info(
            "PULSE_COVERAGE_UNIVERSE_LOADED source=workbook instruments=%s sectors=%s",
            len(_coverage_watchlist_cache),
            len({row.get("sector") for row in _coverage_watchlist_cache}),
        )
    except Exception:
        logger.exception("Pulse could not load the Coverage List workbook; stock heatmap will not fall back to unrelated symbols")
        if not _coverage_watchlist_cache:
            _coverage_watchlist_cache = []
        # Retry soon after a workbook path/permissions issue rather than caching
        # an empty universe for the full 30-minute metadata interval.
        _coverage_watchlist_cached_at = time.monotonic() - 25 * 60
    return _coverage_watchlist_cache


async def refresh_pulse_data(force_refresh: bool = True) -> dict[str, Any]:
    """Worker entry point; orchestration remains here, scraping stays in collectors."""
    global _dashboard_cache, _dashboard_cached_at, _last_good_dashboard
    async with _cache_lock:
        if not force_refresh and _dashboard_cache and time.monotonic() - _dashboard_cached_at < CACHE_SECONDS:
            return _dashboard_cache
        coverage_watchlist = await _coverage_workbook_watchlist()
        market = await pulse_market_data_collector.market_universe(extra_stocks=coverage_watchlist)
        instruments = market.get("instruments", [])
        sector_rows = [row for row in instruments if row.get("kind") in {"sector", "index"}]
        stock_rows = [row for row in instruments if row.get("kind") == "stock"]
        signals = [signal for row in instruments if (signal := _instrument_signal(row)) is not None]
        signals.sort(key=lambda item: ({"critical": 0, "high": 1, "notable": 2}.get(item["severity"], 3), -max(abs(item.get("change_5m_pct") or 0), abs(item.get("change_15m_pct") or 0))))
        events = await _record_signals(signals)
        successful = int(market.get("market_data_count", 0))
        total = sum(1 for row in instruments if not row.get("is_synthetic"))
        snapshot = {
            "as_of": market.get("as_of") or _now(),
            "stock_universe_source": "coverage_list_workbook",
            "coverage_list_count": len(coverage_watchlist),
            "market_session": "open" if _is_market_hours() else "closed_or_preopen",
            "freshness_policy": "1-minute polling target; provider delivery delay is not independently verified",
            "summary": {
                "signals": len(signals),
                "critical": sum(1 for item in signals if item.get("severity") == "critical"),
                "high": sum(1 for item in signals if item.get("severity") == "high"),
                "sector_signals": sum(1 for item in signals if item.get("kind") == "sector"),
                "stock_signals": sum(1 for item in signals if item.get("kind") == "stock"),
                "index_signals": sum(1 for item in signals if item.get("kind") == "index"),
                "market_data_available": successful,
                "market_data_requested": total,
                "coverage_stocks_requested": (market.get("watchlist") or {}).get("coverage_stocks_requested", 0),
                "coverage_stocks_available": market.get("stock_data_count", 0),
                "watchlist_stocks_requested": (market.get("watchlist") or {}).get("requested_stocks", 0),
            },
            "sectors": sector_rows,
            "stocks": stock_rows,
            "macro": market.get("macro", {}),
            "signals": signals,
            "recent_events": events[:60],
            "provider_status": market.get("provider_status", {}),
            "watchlist": market.get("watchlist", {}),
            "market_data_note": "Yahoo Finance chart data is unofficial and its delay is not guaranteed. Use signals as research prompts, not trading instructions.",
        }
        _dashboard_cache = snapshot
        _dashboard_cached_at = time.monotonic()
        if successful:
            _last_good_dashboard = snapshot
        else:
            if not _last_good_dashboard:
                previous_snapshot = await _read_shared_snapshot(max_age=8 * 60 * 60)
                previous_stocks = previous_snapshot.get("stocks", []) if isinstance(previous_snapshot, dict) else []
                previous_is_coverage_only = isinstance(previous_snapshot, dict) and previous_snapshot.get("stock_universe_source") == "coverage_list_workbook" and all(
                    row.get("watchlist_source") == "coverage_list" for row in previous_stocks
                )
                if previous_is_coverage_only and (previous_snapshot.get("summary") or {}).get("market_data_available", 0) > 0:
                    _last_good_dashboard = previous_snapshot
        if not successful and _last_good_dashboard:
            stale = dict(_last_good_dashboard)
            # Keep the independently collected currency/oil banner current even
            # when every equity/index quote failed in this scan.
            if market.get("macro"):
                stale["macro"] = market.get("macro")
            stale["provider_status"] = market.get("provider_status", {})
            stale["stale"] = True
            stale["stale_reason"] = "Current provider scan returned no usable market prices; showing the last successful scan."
            stale["as_of"] = _now()
            _dashboard_cache = stale
        await _write_shared_snapshot(_dashboard_cache)
        return _dashboard_cache


async def _read_shared_snapshot(max_age: int | None = None) -> dict[str, Any] | None:
    try:
        raw = await asyncio.to_thread(SNAPSHOT_PATH.read_text, encoding="utf-8")
        wrapper = json.loads(raw)
        saved_at = float(wrapper.get("saved_at_epoch", 0))
        data = wrapper.get("snapshot")
        if time.time() - saved_at <= (max_age or SHARED_SNAPSHOT_MAX_AGE) and isinstance(data, dict):
            return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


async def _write_shared_snapshot(snapshot: dict[str, Any]) -> None:
    temporary = SNAPSHOT_PATH.with_suffix(".tmp")
    try:
        await asyncio.to_thread(SNAPSHOT_PATH.parent.mkdir, parents=True, exist_ok=True)
        payload = json.dumps({"saved_at_epoch": time.time(), "snapshot": snapshot}, ensure_ascii=False, separators=(",", ":"))
        await asyncio.to_thread(temporary.write_text, payload, encoding="utf-8")
        await asyncio.to_thread(temporary.replace, SNAPSHOT_PATH)
    except OSError:
        logger.warning("Pulse shared snapshot could not be persisted path=%s", SNAPSHOT_PATH, exc_info=True)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


async def get_pulse_dashboard(force_refresh: bool = False) -> dict[str, Any]:
    global _dashboard_cache, _dashboard_cached_at
    if not force_refresh:
        if _dashboard_cache and time.monotonic() - _dashboard_cached_at < CACHE_SECONDS:
            return _dashboard_cache
        snapshot_max_age = SHARED_SNAPSHOT_MAX_AGE if is_pulse_polling_window() else 8 * 60 * 60
        shared = await _read_shared_snapshot(max_age=snapshot_max_age)
        shared_stocks = shared.get("stocks", []) if isinstance(shared, dict) else []
        shared_is_coverage_only = isinstance(shared, dict) and shared.get("stock_universe_source") == "coverage_list_workbook" and all(
            row.get("watchlist_source") == "coverage_list" for row in shared_stocks
        )
        if shared and shared_is_coverage_only and isinstance(shared.get("macro"), dict) and {"usd_inr", "crude_oil"}.issubset(shared.get("macro", {})):
            _dashboard_cache = shared
            _dashboard_cached_at = time.monotonic()
            return shared
        # Ignore an older persisted snapshot created before the banner KPIs
        # were added; the next request performs one compatible refresh.
    return await refresh_pulse_data(force_refresh=force_refresh)


async def get_pulse_events(limit: int = 100, kind: str | None = None) -> dict[str, Any]:
    events = await _read_events()
    if kind in {"sector", "stock"}:
        events = [item for item in events if item.get("kind") == kind]
    return {"as_of": _now(), "events": events[:max(1, min(limit, EVENT_LIMIT))], "count": len(events)}


async def investigate_instrument(instrument: str, kind: str = "stock") -> dict[str, Any]:
    normalized = str(instrument or "").strip().upper()
    if not normalized or len(normalized) > 40:
        raise ValueError("A valid instrument identifier is required")
    dashboard = await get_pulse_dashboard(force_refresh=False)
    sectors = dashboard.get("sectors", [])
    stocks = dashboard.get("stocks", [])
    if kind in {"sector", "index"}:
        row = next((item for item in sectors if item.get("kind") == kind and normalized in {str(item.get("key", "")).upper(), str(item.get("symbol", "")).upper(), str(item.get("name", "")).upper()}), None)
        if not row:
            raise ValueError(f"Unknown sector/index: {normalized}")
        sector_key = str(row.get("key") or "").upper()
        if kind == "sector" and sector_key.startswith("COVERAGE-"):
            coverage_categories = (str(row.get("sector") or row.get("name") or ""),)
        else:
            coverage_categories = SECTOR_COVERAGE_CATEGORIES.get(sector_key, (str(row.get("sector") or ""),))

        direct_stocks = [
            item for item in stocks
            if item.get("watchlist_source") == "coverage_list"
            and str(item.get("sector") or "") in coverage_categories
        ]
        related_stock_mode = "coverage_sector_mapping"
        related_stocks = direct_stocks
        if kind == "sector" and sector_key == "REALTY" and not direct_stocks:
            related_stocks = [
                item for item in stocks
                if item.get("watchlist_source") == "coverage_list"
                and str(item.get("symbol") or "").upper() in REALTY_ADJACENT_COVERAGE_SYMBOLS
            ]
            related_stock_mode = "adjacent_building_supply_chain" if related_stocks else "no_coverage_matches"
        elif not related_stocks:
            related_stock_mode = "no_coverage_matches"

        related_stocks = [
            {**item, "sector_match_type": "adjacent" if related_stock_mode == "adjacent_building_supply_chain" else "coverage_theme"}
            for item in related_stocks
        ]
        related_stocks.sort(
            key=lambda item: (
                abs(_number(item.get("change_5m_pct")) or 0.0),
                abs(_number(item.get("change_15m_pct")) or 0.0),
                abs(_number(item.get("change_pct")) or 0.0),
                _number(item.get("volume_ratio")) or 0.0,
            ),
            reverse=True,
        )
        symbol = row.get("symbol") or row.get("yahoo")
        company = row.get("name") or normalized
        sector_name = row.get("sector") if kind == "sector" else None
        if row.get("is_synthetic"):
            chart = {
                "status": row.get("status", "unavailable"),
                "price": None,
                "change_pct": row.get("change_pct"),
                "change_5m_pct": row.get("change_5m_pct"),
                "change_15m_pct": row.get("change_15m_pct"),
                "volume_ratio": row.get("volume_ratio"),
                "timestamp": row.get("timestamp"),
                "freshness": row.get("freshness"),
                "data_type": row.get("data_type"),
                "source": row.get("source"),
                "series": _coverage_basket_series(related_stocks),
                "reason": None if row.get("priced_constituent_count") else "No coverage-list member quotes are available for this industry basket.",
            }
            if chart["series"]:
                chart["source"] = f"Equal-weighted Coverage List basket for {sector_name or company} (normalized to 100)"
                chart["data_type"] = "intraday_indicative"
                chart["freshness"] = "intraday_unverified"
        else:
            chart = await pulse_market_data_collector._yahoo_chart(str(row.get("yahoo") or symbol))
        news_symbol = row.get("name") or normalized
    else:
        row = next((item for item in stocks if str(item.get("symbol", "")).upper() == normalized), None)
        if not row or row.get("watchlist_source") != "coverage_list":
            raise ValueError(f"Stock {normalized} is not present in the current Coverage List workbook")
        symbol = row.get("symbol") or normalized
        company = row.get("name") or normalized
        sector_name = row.get("sector")
        related_stocks = []
        chart = await pulse_market_data_collector._yahoo_chart(str(row.get("yahoo") or f"{symbol}.NS"))
        # If the intraday provider has no quote, reuse the opt-in TejHQ EOD
        # snapshot for this stock. Keep it explicitly marked EOD and never
        # present it as an intraday movement series.
        if chart.get("status") != "available":
            exchange = str(row.get("exchange") or "NSE").upper()
            eod_rows = await pulse_market_data_collector._get_tejhq_snapshot()
            eod = eod_rows.get(f"{exchange}:{str(symbol).upper()}") or (eod_rows.get(str(symbol).upper()) if exchange == "NSE" else None)
            if eod:
                chart = {
                    **eod,
                    "status": "eod_fallback",
                    "freshness": "eod",
                    "data_type": "end_of_day",
                    "delay_verified": False,
                    "source": f"TejHQ {exchange} end-of-day snapshot",
                    "source_url": "https://tejhq.dev/docs",
                    "reason": None,
                }
        news_symbol = symbol
    source_context = await pulse_news_collector.investigation_sources(symbol=str(news_symbol), company=str(company), sector=sector_name)
    if kind == "sector" and related_stocks:
        filing_results = await asyncio.gather(*(
            pulse_exchange_filings_collector.search(symbol=str(stock.get("symbol")), company=str(stock.get("name")), limit=10)
            for stock in related_stocks[:8]
        ), return_exceptions=True)
        official_items = []
        official_statuses = []
        for result in filing_results:
            if isinstance(result, dict):
                official_items.extend(result.get("items", []))
                official_statuses.append(result.get("status"))
        seen_filing_urls = set()
        unique_official_items = []
        for filing in official_items:
            identity = str(filing.get("url") or filing.get("title") or "").casefold()
            if identity and identity not in seen_filing_urls:
                seen_filing_urls.add(identity)
                unique_official_items.append(filing)
        official_filings = {"status": "ok" if unique_official_items else ("unavailable" if "unavailable" in official_statuses else "empty"), "items": unique_official_items}
    else:
        official_filings = await pulse_exchange_filings_collector.search(symbol=str(news_symbol), company=str(company), limit=35)
    event_history = await _read_events()
    related_symbols = {str(stock.get("symbol") or "").upper() for stock in related_stocks}
    related_events = [
        item for item in event_history
        if str(item.get("symbol", "")).upper() == str(symbol).upper()
        or (kind in {"sector", "index"} and str(item.get("symbol", "")).upper() in related_symbols)
        or (kind == "sector" and item.get("sector") == sector_name)
    ][:20]
    news_items = list(official_filings.get("items", [])) + list(source_context.get("items", []))
    seen_information = set()
    deduped_information = []
    for item in news_items:
        identity = str(item.get("url") or item.get("title") or "").casefold()
        if identity and identity not in seen_information:
            seen_information.add(identity)
            deduped_information.append(item)
    news_items = sorted(deduped_information, key=lambda item: str(item.get("published_at") or ""), reverse=True)[:50]
    explanation = {
        "status": "evidence_pending" if not news_items else "potential_catalysts_found",
        "headline": "No verified catalyst found yet" if not news_items else f"{len(news_items)} potentially relevant information item(s) found",
        "summary": "Pulse detected a price/volume pattern. Nearby news and filing-search results are leads only; publication time and the original source must be checked before attributing the move.",
        "causality_confirmed": False,
    }
    return {
        "as_of": _now(),
        "instrument": {"kind": kind, "symbol": symbol, "name": company, "sector": sector_name},
        "quote": {key: chart.get(key) for key in ("price", "previous_close", "change_pct", "change_5m_pct", "change_15m_pct", "volume_ratio", "timestamp", "freshness", "data_type", "source", "status", "reason")},
        "series": chart.get("series", []),
        "related_stocks": related_stocks,
        "related_stock_count": len(related_stocks),
        "related_stock_mode": related_stock_mode if kind in {"sector", "index"} else None,
        "related_stock_note": (
            "Showing Coverage List companies mapped to this sector theme. These are not a verified list of official index constituents."
            if kind in {"sector", "index"} and related_stock_mode == "coverage_sector_mapping" and related_stocks
            else "The Coverage List has no direct real-estate developers or REITs. These adjacent construction and building-products companies are shown as research leads, not NIFTY Realty constituents."
            if kind == "sector" and related_stock_mode == "adjacent_building_supply_chain"
            else "No companies in the current Coverage List are mapped to this sector. No unrelated stocks have been inserted."
            if kind in {"sector", "index"} else None
        ),
        "related_events": related_events,
        "information": news_items,
        "provider_status": {"market": chart.get("status"), "news": source_context.get("news_status"), "filing_search": official_filings.get("status"), "google_filing_search": source_context.get("filings_search_status"), "sector_news": source_context.get("sector_news_status")},
        "explanation": explanation,
        "disclaimer": "NSE official RSS and best-effort BSE public announcements are queried alongside Google News. Feed coverage and publication timing can vary; confirm the original exchange disclosure. A nearby filing or article is not proof that it caused the price movement.",
    }


@router.get("")
async def get_pulse_snapshot(
    refresh: bool = Query(default=False),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
) -> dict[str, Any]:
    try:
        return await get_pulse_dashboard(force_refresh=refresh)
    except Exception as exc:
        logger.exception("Pulse dashboard orchestration failed")
        raise HTTPException(status_code=502, detail="Pulse market scan is temporarily unavailable") from exc


@router.get("/commodities")
async def get_commodities(
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
) -> dict[str, Any]:
    """On-demand global commodity quote list for the Crude Oil banner KPI."""
    try:
        return await pulse_market_data_collector.commodity_prices()
    except Exception as exc:
        logger.exception("Pulse commodity board failed")
        raise HTTPException(status_code=502, detail="Commodity prices are temporarily unavailable") from exc


@router.get("/events")
async def get_events(
    limit: int = Query(default=100, ge=1, le=500),
    kind: str | None = Query(default=None),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
) -> dict[str, Any]:
    return await get_pulse_events(limit=limit, kind=kind)


@router.get("/investigate/{instrument}")
async def get_investigation(
    instrument: str = ApiPath(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9&._^ -]+$"),
    kind: str = Query(default="stock", pattern="^(stock|sector|index)$"),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
) -> dict[str, Any]:
    try:
        return await investigate_instrument(instrument, kind=kind)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Pulse investigation failed instrument=%s", instrument)
        raise HTTPException(status_code=502, detail="Instrument investigation is temporarily unavailable") from exc


@router.get("/stock/{symbol}")
async def get_legacy_stock_context(
    symbol: str = ApiPath(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9&._-]+$"),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
) -> dict[str, Any]:
    try:
        return await investigate_instrument(symbol, kind="stock")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/movers")
async def get_legacy_movers(user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT))) -> dict[str, Any]:
    dashboard = await get_pulse_dashboard(force_refresh=False)
    return {"signals": dashboard.get("signals", []), "as_of": dashboard.get("as_of"), "provider_status": dashboard.get("provider_status", {})}


@router.get("/health")
async def get_pulse_health(user: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT))) -> dict[str, Any]:
    dashboard = await get_pulse_dashboard(force_refresh=False)
    return {"as_of": dashboard.get("as_of"), "summary": dashboard.get("summary"), "provider_status": dashboard.get("provider_status"), "market_session": dashboard.get("market_session"), "freshness_policy": dashboard.get("freshness_policy")}


async def close_pulse_data() -> None:
    await pulse_market_data_collector.aclose()
    await pulse_news_collector.aclose()
    await pulse_exchange_filings_collector.aclose()


def get_router() -> APIRouter:
    return router
