from __future__ import annotations

import logging
import re
import time
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.regulatory_activity.collectors import collect_all
from apps.regulatory_activity.normalization import norm, validate_event
from ii_platform.cache.snapshot import get_snapshot
from ii_platform.database.models import Company, RegulatoryActivityEvent, RegulatoryCollectorRun, RegulatoryMarketPrice

logger = logging.getLogger(__name__)


def _utc_floor(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _company_index(companies: list[Company]) -> tuple[dict[str, Company], dict[str, list[Company]]]:
    by_symbol: dict[str, Company] = {}
    by_isin: dict[str, Company] = {}
    by_name: dict[str, list[Company]] = {}
    for company in companies:
        if company.nse_symbol:
            by_symbol[norm(company.nse_symbol)] = company
        if company.isin:
            by_isin[norm(company.isin)] = company
        terms = [company.legal_name, *(company.aliases or [])]
        for term in terms:
            key = norm(term)
            if key:
                by_name.setdefault(key, []).append(company)
    return {**by_symbol, **{f"isin:{k}": v for k, v in by_isin.items()}}, by_name


def _match_company(event: dict[str, Any], by_key: dict[str, Company], by_name: dict[str, list[Company]]) -> Company | None:
    symbol = norm(event.get("symbol"))
    if symbol and symbol in by_key:
        return by_key[symbol]
    isin = norm(event.get("isin"))
    if isin and f"isin:{isin}" in by_key:
        return by_key[f"isin:{isin}"]
    name = norm(event.get("company_name"))
    exact = by_name.get(name) or []
    if len(exact) == 1:
        return exact[0]
    matches = []
    for key, companies in by_name.items():
        if len(key) >= 7 and (key in name or name in key):
            matches.extend(companies)
    unique = {item.id: item for item in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


async def _market_prices(db: AsyncSession, symbols: set[str]) -> dict[str, dict[str, Any]]:
    symbols = {s.upper() for s in symbols if s}
    if not symbols:
        return {}
    latest = (
        select(RegulatoryMarketPrice.nse_symbol.label("symbol"), func.max(RegulatoryMarketPrice.trade_date).label("trade_date"))
        .where(RegulatoryMarketPrice.nse_symbol.in_(symbols))
        .group_by(RegulatoryMarketPrice.nse_symbol)
        .subquery()
    )
    rows = (await db.execute(
        select(RegulatoryMarketPrice).join(
            latest,
            and_(RegulatoryMarketPrice.nse_symbol == latest.c.symbol,
                 RegulatoryMarketPrice.trade_date == latest.c.trade_date),
        )
    )).scalars().all()
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = row.nse_symbol.upper()
        if row.current_price is not None and float(row.current_price) > 0:
            output[symbol] = {
                "price": row.current_price,
                "change_percent": row.change_percent,
                "trade_date": row.trade_date.isoformat() if row.trade_date else None,
                "source": row.source or "regulatory_market_prices",
                "source_url": row.source_url,
                "collected_at": row.collected_at,
            }
    # Prefer an available shared NSE equity quote snapshot for current CMP; retain
    # the daily persisted price as a fallback. This makes CMP enrichment best-effort
    # without introducing one network request per deal symbol.
    try:
        snapshot, meta = await get_snapshot("nse:equity-prices", allow_stale=True)
        items = snapshot.get("items") if isinstance(snapshot, dict) else None
        if isinstance(items, dict):
            for symbol in symbols:
                item = items.get(symbol)
                if not isinstance(item, dict):
                    continue
                live_price = _positive_float(item.get("last_price") or item.get("lastPrice") or item.get("ltp"))
                live_change = _finite_float(item.get("change_percent") or item.get("changePercent"))
                if live_price is not None or live_change is not None:
                    previous = output.get(symbol, {})
                    output[symbol] = {
                        "price": live_price if live_price is not None else previous.get("price"),
                        "change_percent": live_change if live_change is not None else previous.get("change_percent"),
                        "trade_date": (snapshot.get("generated_at") if isinstance(snapshot, dict) else None) or previous.get("trade_date"),
                        "source": f"nse_equity_snapshot:{(meta if isinstance(meta, dict) else {}).get('source', 'shared_cache')}",
                        "source_url": item.get("source_url") or previous.get("source_url"),
                        "collected_at": (snapshot.get("generated_at") if isinstance(snapshot, dict) else None),
                    }
    except Exception as exc:
        logger.info("REGULATORY_ACTIVITY_CMP_SNAPSHOT_UNAVAILABLE error=%s", str(exc)[:180])

    # CMP used to depend entirely on an optional, separately-run backfill plus
    # a Redis snapshot.  The regulatory backfill command does not run that
    # worker stage, so a fresh database/cache commonly left every event CMP
    # blank.  Fill missing symbols directly from NSE's exchange-wide CM-UDiFF
    # bhavcopy archive (official latest close = EOD CMP); one set of CSV files
    # serves all symbols and does not issue one quote request per event.
    now_utc = datetime.now(timezone.utc)
    nse_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    refresh_before = now_utc - timedelta(hours=6)
    missing: set[str] = set()
    for symbol in symbols:
        quote = output.get(symbol) or {}
        if _positive_float(quote.get("price")) is None:
            missing.add(symbol)
            continue
        quote_day = _market_trade_date(quote.get("trade_date"))
        collected_at = quote.get("collected_at")
        if isinstance(collected_at, str):
            try:
                collected_at = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
            except ValueError:
                collected_at = None
        if isinstance(collected_at, datetime) and collected_at.tzinfo is None:
            collected_at = collected_at.replace(tzinfo=timezone.utc)
        is_stale = quote_day is None or quote_day < nse_today
        refresh_is_due = not isinstance(collected_at, datetime) or collected_at.astimezone(timezone.utc) <= refresh_before
        if is_stale and refresh_is_due:
            missing.add(symbol)
    if missing:
        archive_quotes: dict[str, dict[str, Any]] = {}
        client = None
        try:
            from ii_platform.nse.client import NSEApiClient as NSEPriceClient

            # Keep this archive path independent from JSON session priming and
            # without a request timeout. NSE may serve its public bhavcopy even
            # when the cookie-based quote APIs are degraded.
            client = NSEPriceClient(timeout=httpx.Timeout(None))
            archive_quotes = await client.get_latest_equity_prices(
                sorted(missing), lookback_days=10,
            )
        except Exception as exc:
            logger.warning(
                "REGULATORY_ACTIVITY_CMP_ARCHIVE_FAILED requested=%s error=%s: %s",
                len(missing), type(exc).__name__, str(exc)[:240],
            )
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    logger.debug("REGULATORY_ACTIVITY_CMP_CLIENT_CLOSE_FAILED", exc_info=True)

        normalized_quotes: dict[str, tuple[date, dict[str, Any]]] = {}
        for raw_symbol, quote in (archive_quotes or {}).items():
            symbol = str(raw_symbol or "").strip().upper()
            if symbol not in missing or not isinstance(quote, dict):
                continue
            price = _positive_float(quote.get("last_price") or quote.get("price"))
            trade_day = _market_trade_date(quote.get("trade_date"))
            if price is None or trade_day is None:
                continue
            quote = {**quote, "last_price": price}
            normalized_quotes[symbol] = (trade_day, quote)
            output[symbol] = {
                "price": price,
                "change_percent": _finite_float(quote.get("change_percent")),
                "trade_date": trade_day.isoformat(),
                "source": str(quote.get("source") or "NSE-CM-UDIFF-BHAVCOPY"),
                "source_url": quote.get("source_url"),
            }

        # Persist official archive observations so the next run does not have
        # to download bhavcopy again.  This reuses the existing daily-price
        # table; the events table remains the only Regulatory Activity serving
        # table and no migration is introduced.
        if normalized_quotes:
            min_trade_day = min(day for day, _quote in normalized_quotes.values())
            existing_rows = (await db.execute(
                select(RegulatoryMarketPrice).where(
                    RegulatoryMarketPrice.nse_symbol.in_(list(normalized_quotes)),
                    RegulatoryMarketPrice.trade_date >= min_trade_day,
                )
            )).scalars().all()
            existing_by_key = {
                (row.nse_symbol.upper(), row.trade_date, row.source): row
                for row in existing_rows
            }
            now = datetime.now(timezone.utc)
            for symbol, (trade_day, quote) in normalized_quotes.items():
                source = str(quote.get("source") or "NSE-CM-UDIFF-BHAVCOPY")[:80]
                key = (symbol, trade_day, source)
                price = _positive_float(quote.get("last_price"))
                previous = _positive_float(quote.get("previous_close"))
                change = _finite_float(quote.get("change_percent"))
                row = existing_by_key.get(key)
                if row is None:
                    row = RegulatoryMarketPrice(
                        nse_symbol=symbol,
                        trade_date=trade_day,
                        current_price=price,
                        previous_close=previous,
                        change_percent=change,
                        source=source,
                        source_url=str(quote.get("source_url") or "")[:1000] or None,
                        collected_at=now,
                    )
                    db.add(row)
                    existing_by_key[key] = row
                else:
                    row.current_price = price
                    row.previous_close = previous
                    row.change_percent = change
                    row.source_url = str(quote.get("source_url") or "")[:1000] or row.source_url
                    row.collected_at = now
            await db.flush()

        logger.info(
            "REGULATORY_ACTIVITY_CMP_REFRESH requested=%s matched=%s missing=%s source=NSE-CM-UDIFF-BHAVCOPY",
            len(missing), len(normalized_quotes), len(missing - set(normalized_quotes)),
        )
    return output


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return number if number is not None and math.isfinite(number) else None


def _positive_float(value: Any) -> float | None:
    number = _finite_float(value)
    return number if number is not None and number > 0 else None


def _market_trade_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


async def _persist_source(
    db: AsyncSession,
    source_name: str,
    result: dict[str, Any],
    company_indexes: tuple[dict[str, Company], dict[str, list[Company]]],
    prices: dict[str, dict[str, Any]],
    start: date,
    end: date,
) -> dict[str, int]:
    received = inserted = updated = rejected = partial_events = 0
    started = datetime.now(timezone.utc)
    by_key, by_name = company_indexes
    events = result.get("events") if isinstance(result.get("events"), list) else []
    for event in events:
        received += 1
        valid, reason = validate_event(event)
        if not valid:
            rejected += 1
            logger.warning("REGULATORY_ACTIVITY_EVENT_REJECTED source=%s reason=%s", source_name, reason)
            continue
        if str(event.get("status") or "PARTIAL").upper() != "DETAILED":
            partial_events += 1
        company = _match_company(event, by_key, by_name)
        symbol = str(event.get("symbol") or (company.nse_symbol if company else "") or "").upper()
        quote = prices.get(symbol) or {}
        values = {
            "source_exchange": event.get("source_exchange") or "NSE",
            "activity_type": event.get("activity_type"),
            "activity_sub_type": event.get("activity_sub_type"),
            "company_id": company.id if company else None,
            "company_name": (
                company.legal_name
                if company and norm(event.get("company_name")) == norm(symbol)
                else event.get("company_name")
            ),
            "symbol": symbol or None,
            "isin": event.get("isin") or (company.isin if company else None),
            "event_date": event.get("event_date"),
            "disclosure_date": event.get("disclosure_date"),
            "person_allottee_client": event.get("person_allottee_client"),
            "action": event.get("action"),
            "quantity": event.get("quantity"),
            "price": event.get("price"),
            "value_inr": event.get("value_inr"),
            "holding_before": event.get("holding_before"),
            "holding_after": event.get("holding_after"),
            "current_market_price": event.get("current_market_price") if event.get("current_market_price") is not None else quote.get("price"),
            "market_price_change_percent": event.get("market_price_change_percent") if event.get("market_price_change_percent") is not None else quote.get("change_percent"),
            "status": event.get("status") or "PARTIAL",
            "source_url": event.get("source_url"),
            "details_json": event.get("details_json") or {},
            "source_metadata": {
                **(event.get("source_metadata") or {}),
                "market_price_trade_date": quote.get("trade_date"),
                "market_price_source": quote.get("source"),
                "market_price_source_url": quote.get("source_url"),
            },
        }
        existing = await db.scalar(select(RegulatoryActivityEvent).where(RegulatoryActivityEvent.source_key == event["source_key"]))
        # The issue parser now uses a stable filing/allottee identity instead of
        # including mutable quantity/price/value fields. Upgrade a legacy row's
        # source_key when its filing URL identifies exactly one matching event;
        # this prevents a re-sync from duplicating QIP/PREF rows after parser fixes.
        if existing is None and values["activity_type"] in {"QIP", "PREFERENTIAL_WARRANT"} and values["source_url"] and values["symbol"]:
            issue_filters = [
                RegulatoryActivityEvent.source_url == values["source_url"],
                RegulatoryActivityEvent.activity_type == values["activity_type"],
                RegulatoryActivityEvent.activity_sub_type == values["activity_sub_type"],
                RegulatoryActivityEvent.symbol == values["symbol"],
            ]
            # Prefer a precise person/action match when a filing contains
            # multiple allottee-level records.
            if values["person_allottee_client"]:
                precise = (await db.execute(
                    select(RegulatoryActivityEvent).where(
                        *issue_filters,
                        RegulatoryActivityEvent.person_allottee_client == values["person_allottee_client"],
                        RegulatoryActivityEvent.action == values["action"],
                    ).limit(2)
                )).scalars().all()
                if len(precise) == 1:
                    existing = precise[0]
            if existing is None:
                possible = (await db.execute(
                    select(RegulatoryActivityEvent).where(*issue_filters).limit(2)
                )).scalars().all()
                if len(possible) == 1:
                    existing = possible[0]

        if existing is None:
            db.add(RegulatoryActivityEvent(source_key=event["source_key"], **values))
            inserted += 1
        else:
            changed = existing.source_key != event["source_key"] or any(
                getattr(existing, key) != value for key, value in values.items()
            )
            if changed:
                existing.source_key = event["source_key"]
                for key, value in values.items():
                    setattr(existing, key, value)
                existing.updated_at = datetime.now(timezone.utc)
                updated += 1
    raw_status = str(result.get("status") or "FAILED").upper()
    error = result.get("error")
    detail_failed = int(result.get("details_failed") or 0)
    discovered = int(result.get("discovered") or 0)
    if raw_status in {"FAILED", "EMPTY_UNVERIFIED"} or error:
        status = "FAILED" if not received else "PARTIAL"
    elif raw_status == "EMPTY":
        status = "EMPTY"
    elif raw_status == "PARTIAL" or rejected or detail_failed or partial_events or (discovered and not received):
        status = "PARTIAL"
    else:
        status = "SUCCESS"
    db.add(RegulatoryCollectorRun(
        source=source_name[:40], window_start=_utc_floor(start), window_end=_utc_floor(end),
        started_at=started, finished_at=datetime.now(timezone.utc), status=status,
        http_status=200 if status in {"SUCCESS", "EMPTY", "PARTIAL"} else None,
        records_received=received, records_inserted=inserted, records_updated=updated,
        content_hash=None, duration_ms=None,
        error_message=(str(error) if error else (f"discovered={discovered}; rejected={rejected}; partial_events={partial_events}; details_failed={detail_failed}" if rejected or detail_failed or partial_events or (discovered and not received) else None)),
        created_at=datetime.now(timezone.utc),
    ))
    await db.flush()
    return {"received": received, "inserted": inserted, "updated": updated, "rejected": rejected,
            "discovered": discovered, "partial_events": partial_events,
            "details_ok": int(result.get("details_ok") or 0),
            "details_failed": detail_failed, "status": status}


async def sync_regulatory_activity(
    db: AsyncSession, *, days: int = 7, refresh_market_prices: bool = True,
) -> dict[str, Any]:
    """Collect the official NSE sources independently and upsert normalized alert rows."""
    days = max(1, min(int(days), 2557))
    # NSE listing and disclosure dates are Indian-market dates, not UTC dates.
    end = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    start = end - timedelta(days=days - 1)
    companies = (await db.execute(select(Company))).scalars().all()
    company_indexes = _company_index(companies)
    source_results = await collect_all(start, end)
    event_symbols = {
        str(event.get("symbol") or "").upper()
        for result in source_results.values()
        for event in (result.get("events") or [])
        if isinstance(event, dict) and event.get("symbol")
    }
    # Price only companies that actually appear in this run's regulatory
    # events. Resolve missing filing symbols through the existing company
    # master so those events can still receive CMP without downloading prices
    # for the entire unrelated company universe.
    cmp_symbols = set(event_symbols)
    for result in source_results.values():
        for event in (result.get("events") or []):
            if not isinstance(event, dict) or event.get("symbol"):
                continue
            matched_company = _match_company(event, *company_indexes)
            if matched_company and matched_company.nse_symbol:
                cmp_symbols.add(str(matched_company.nse_symbol).upper())
    prices = await _market_prices(db, cmp_symbols) if refresh_market_prices else {}
    totals = {"received": 0, "inserted": 0, "updated": 0, "rejected": 0}
    categories: dict[str, dict[str, Any]] = {}
    for key, result in source_results.items():
        source_name = {
            "INSIDER_TRADING": "NSE_INSIDER",
            "BULK_DEAL": "NSE_BULK_BLOCK",
            "QIP": "NSE_QIP",
            "PREFERENTIAL_WARRANT": "NSE_PREF_WARRANT",
        }.get(key, key)
        try:
            stats = await _persist_source(db, source_name, result, company_indexes, prices, start, end)
            categories[key] = stats
            for name in totals:
                totals[name] += stats.get(name, 0)
            await db.commit()  # keep successful categories durable if a later source fails
        except Exception as exc:
            await db.rollback()
            categories[key] = {"status": "FAILED", "error": f"{type(exc).__name__}: {str(exc)[:1000]}"}
            logger.exception("REGULATORY_ACTIVITY_PERSIST_FAILED category=%s", key)
    failed = [key for key, value in categories.items() if value.get("status") not in {"SUCCESS", "EMPTY"}]
    complete = len(categories) == 4 and not failed
    status = "completed" if complete else "partial" if totals["received"] else "failed"
    summary = {
        "status": status, "window_start": start.isoformat(), "window_end": end.isoformat(),
        **totals, "failed_sources": failed, "categories": categories,
        "cmp_enrichment": {
            "enabled": bool(refresh_market_prices),
            "symbols_requested": len(cmp_symbols) if refresh_market_prices else 0,
            "symbols_priced": sum(1 for quote in prices.values() if _positive_float(quote.get("price")) is not None),
            "event_symbols_requested": len(event_symbols) if refresh_market_prices else 0,
            "event_symbols_priced": sum(1 for symbol in event_symbols if _positive_float(prices.get(symbol, {}).get("price")) is not None) if refresh_market_prices else 0,
            "event_symbols_unpriced_count": sum(
                1 for symbol in event_symbols
                if _positive_float(prices.get(symbol, {}).get("price")) is None
            ) if refresh_market_prices else 0,
            "event_symbols_unpriced_sample": sorted(
                symbol for symbol in event_symbols
                if _positive_float(prices.get(symbol, {}).get("price")) is None
            )[:50] if refresh_market_prices else [],
        },
        "complete": complete,
    }
    logger.info("REGULATORY_ACTIVITY_SYNC_RESULT %s", summary)
    return summary
