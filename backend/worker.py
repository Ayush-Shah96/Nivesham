from __future__ import annotations

"""Single durable owner of scheduled/background work.

Design goals:
- one startup refresh cycle immediately after process start;
- one complete refresh cycle every four hours for the existing master workflow;
- Solar captures are owned by a dedicated five-minute JSON-file cache scheduler;
- other Sector Screener captures retain their hourly JSON-file cache scheduler;
- no arbitrary wall-clock cancellation of a whole stage;
- per-source / per-request timeouts prevent one provider from hanging forever;
- Regulatory/Capital uses its own non-overlapping four-hour scheduler so NSE desks are never starved;
- Redis is optional and never part of the core batch path;
- the worker logs a stable code version so an old process cannot be mistaken for
  the current implementation.
"""

import asyncio
import logging
import os
import re
import signal
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable

from sqlalchemy import and_, func, or_, select, text

from infrastructure.config.settings import get_settings
from ii_platform.ai.gateway import get_ai_gateway
from ii_platform.database.connection import dispose_engine, get_sessionmaker
from ii_platform.database.models import (
    InsiderSignal, CapitalActionSignal, EventOutbox, Company, RegulatoryMarketPrice,
)
from ii_platform.events.event_bus import RedisEventBus
from ii_platform.cache.auth_cache import warm_auth_users
from ii_platform.events.outbox import outbox_worker
from ii_platform.pipelines.orchestrator import PipelineOrchestrator
from ii_platform.notifications.service import ensure_recent_ht_notifications
from ii_platform.cache.snapshot import set_snapshot
from ii_platform.events.schemas import Event, EventType
from apps.coverage_list import sync_coverage_workbook_if_changed, _grid_payload
from apps.newsalert.routes import ingest_configured_news
from apps.dashboard import warm_dashboard_snapshot
from apps.newsalert.workflows import process_news_ingest_event
from apps.regulatory_activity.legacy_routes import sync_regulatory_data, sync_pe_warrant_data
from apps.regulatory_activity.service import sync_regulatory_activity
from ii_platform.nse import NSEApiClient
from apps.pulse import refresh_pulse_data, close_pulse_data, is_pulse_polling_window, collector as pulse_market_collector
from apps.regulatory_activity.sync_state import CAPITAL_ACTION_DESK, REGULATORY_DESK, REGULATORY_ENGINE_VERSION, get_sync_state
from apps.market_intelligence.sector_screener_service import (
    run_periodic_refresh_loop as sector_screener_refresh_loop,
    run_solar_refresh_loop as solar_refresh_loop,
    run_tradestat_refresh_loop as tradestat_refresh_loop,
)
from apps.portfolio_comparison.service import portfolio_queue_loop
from apps.earnings_calendar.service import collect_earnings_calendar

logger = logging.getLogger("investment-worker")
settings = get_settings()

WORKER_CODE_VERSION = "UNIFIED-NEWSALERT-YOUTUBE-V41-WINDOWS-PLAYWRIGHT-PROACTOR-SUPERVISED-SECTOR-LOOPS"



async def _run_stage(
    name: str,
    func: Callable[[], Awaitable[object]],
) -> object:
    """Run a stage without a global cancellation deadline.

    The previous implementation wrapped the entire stage in asyncio.wait_for(...,
    210s). That cancelled valid work in the middle of DB commit/SSL connection setup.
    Each collector now owns its own bounded external operation instead.
    """
    started = time.monotonic()
    logger.info(
        "PIPELINE_STAGE_START stage=%s code_version=%s",
        name,
        WORKER_CODE_VERSION,
    )

    slow_task: asyncio.Task | None = None

    async def slow_watchdog() -> None:
        threshold = max(
            30.0,
            float(settings.pipeline_slow_warning_seconds),
        )
        try:
            await asyncio.sleep(threshold)
            while True:
                elapsed = time.monotonic() - started
                logger.warning(
                    "PIPELINE_STAGE_SLOW stage=%s elapsed_seconds=%.1f threshold_seconds=%s",
                    name,
                    elapsed,
                    int(threshold),
                )
                await asyncio.sleep(threshold)
        except asyncio.CancelledError:
            raise

    slow_task = asyncio.create_task(
        slow_watchdog(),
        name=f"slow-watchdog-{name}",
    )

    try:
        result = await func()
        logger.info(
            "PIPELINE_STAGE_DONE stage=%s elapsed_seconds=%.1f result=%s",
            name,
            time.monotonic() - started,
            _brief(result),
        )
        return result
    except asyncio.CancelledError:
        logger.warning(
            "PIPELINE_STAGE_CANCELLED stage=%s elapsed_seconds=%.1f",
            name,
            time.monotonic() - started,
        )
        raise
    except Exception:
        logger.exception(
            "PIPELINE_STAGE_FAILED stage=%s elapsed_seconds=%.1f",
            name,
            time.monotonic() - started,
        )
        return {
            "status": "failed",
            "stage": name,
        }
    finally:
        if slow_task is not None:
            slow_task.cancel()
            await asyncio.gather(
                slow_task,
                return_exceptions=True,
            )


def _brief(value: object) -> object:
    if isinstance(value, dict):
        keys = (
            "status",
            "created",
            "stored",
            "updated",
            "scanned",
            "failed",
            "rows",
            "companies",
            "sent",
            "skipped",
            "sources",
            "diagnostics",
            "complete",
            "coverage_companies_without_rows",
            "source_errors",
            "missing_nse_symbols",
            "collection_mode",
            "feed_complete",
            "symbols_with_announcements",
            "companies_scanned",
            "review_needed_count",
            "documents_failed",
            "snapshot_warm_status",
            "snapshot_warm_error",
        )
        return {
            key: value.get(key)
            for key in keys
            if key in value
        }
    if isinstance(value, list):
        return {"items": len(value)}
    return value


async def _warm_database() -> None:
    started = time.monotonic()
    try:
        async with get_sessionmaker()() as db:
            await db.execute(text("SELECT 1"))
        logger.info(
            "DATABASE_WARM_DONE elapsed_seconds=%.2f",
            time.monotonic() - started,
        )
    except Exception:
        logger.exception(
            "DATABASE_WARM_FAILED elapsed_seconds=%.2f",
            time.monotonic() - started,
        )


async def _coverage_stage() -> object:
    async with get_sessionmaker()() as db:
        result = await sync_coverage_workbook_if_changed(db)
        try:
            await _grid_payload(db)
        except Exception:
            logger.exception(
                "Coverage List grid warm-up failed"
            )
        await db.commit()
        return result


async def _news_stage() -> object:
    """Run NewsAlert v2, which writes directly to the canonical UI table."""
    async with get_sessionmaker()() as db:
        result = await ingest_configured_news(
            db,
            user_id=None,
            limit_per_feed=settings.news_cycle_limit_per_feed,
            # Respect the same YOUTUBE_ENABLED switch used by the NewsAlert UI.
            # Channel collection is enabled by default and can be paused explicitly
            # without changing the worker code.
            include_youtube=bool(settings.youtube_enabled),
            include_x_posts=True,
            include_stockscan=True,
        )
        await db.commit()
        if settings.snapshot_cache_warm_dashboard:
            try:
                result["dashboard_snapshot"] = await warm_dashboard_snapshot(db)
            except Exception:
                logger.exception("DASHBOARD_SNAPSHOT_WARM_FAILED")
        return result


async def _sync_history_days(db, desk: str, incremental_env: str) -> int:
    """Use a small incremental window by default; historical reconciliation is opt-in."""

    full_interval = max(3600, int(settings.regulatory_full_reconcile_interval_seconds))
    full_retry = max(3600, int(settings.regulatory_full_retry_interval_seconds))
    incremental_days = (
        int(settings.regulatory_incremental_history_days)
        if incremental_env == "REGULATORY_INCREMENTAL_HISTORY_DAYS"
        else int(settings.capital_action_incremental_history_days)
    )

    state = None
    try:
        state = await get_sync_state(db, desk)
    except Exception:
        logger.exception("Unable to read Regulatory sync state desk=%s; falling back to source-row bootstrap detection", desk)

    now = datetime.now(timezone.utc)

    def _ensure_utc_datetime(value: datetime | None) -> datetime | None:
        """Normalize DB timestamps to timezone-aware UTC datetimes.

        Some PostgreSQL/asyncpg paths can materialize legacy DateTime(timezone=True)
        values as naive datetimes. Treat those existing values as UTC so arithmetic
        remains safe and consistent with the worker clock.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if not settings.regulatory_auto_historical_backfill:
        logger.info(
            "REGULATORY_SYNC_PLAN desk=%s history_days=%s reason=incremental_only auto_full=false",
            desk, incremental_days,
        )
        return incremental_days

    if state is not None and getattr(state, "engine_version", None) != REGULATORY_ENGINE_VERSION:
        logger.info(
            "REGULATORY_SYNC_PLAN desk=%s history_days=2557 reason=engine_version_changed old=%s new=%s",
            desk, getattr(state, "engine_version", None), REGULATORY_ENGINE_VERSION,
        )
        return 2557
    if state is None:
        logger.info("REGULATORY_SYNC_PLAN desk=%s history_days=2557 reason=first_run_seven_year_backfill", desk)
        return 2557

    last_full_success_at = _ensure_utc_datetime(state.last_full_success_at)
    if last_full_success_at:
        age = (now - last_full_success_at).total_seconds()
        if age >= full_interval and settings.regulatory_auto_historical_backfill:
            logger.info(
                "REGULATORY_SYNC_PLAN desk=%s history_days=2557 reason=full_reconcile_due age_seconds=%.0f",
                desk, age,
            )
            return 2557
        logger.info(
            "REGULATORY_SYNC_PLAN desk=%s history_days=%s reason=incremental_since_full age_seconds=%.0f auto_full=%s",
            desk, incremental_days, age, settings.regulatory_auto_historical_backfill,
        )
        return incremental_days

    # The first run and incomplete historical runs stay on the seven-year window
    # until a complete sync is recorded. This keeps partial backfills resumable.
    if settings.regulatory_auto_historical_backfill:
        last_full_attempt_at = _ensure_utc_datetime(state.last_full_attempt_at)
        if last_full_attempt_at:
            age = (now - last_full_attempt_at).total_seconds()
            if age < full_retry:
                logger.info(
                    "REGULATORY_SYNC_PLAN desk=%s history_days=2557 reason=bootstrap_in_progress age_seconds=%.0f auto_full=true",
                    desk, age,
                )
                return 2557
        logger.info(
            "REGULATORY_SYNC_PLAN desk=%s history_days=2557 reason=bootstrap_not_completed auto_full=true",
            desk,
        )
        return 2557

    logger.info(
        "REGULATORY_SYNC_PLAN desk=%s history_days=%s reason=incremental_bootstrap auto_full=false",
        desk, incremental_days,
    )
    return incremental_days


async def _nse_company_universe_stage() -> object:
    """Refresh NSE companies using symbol/ISIN identity before weaker name matching.

    Company identifiers have UNIQUE constraints in PostgreSQL. Treat the exchange
    symbol as the primary row identity, then ISIN, and only then an unambiguous
    normalized legal name. Never guess across conflicting identities.
    """
    started = time.monotonic()

    def name_key(value: object) -> str:
        value = str(value or "").casefold().replace("&", " and ")
        value = re.sub(r"[^a-z0-9]+", " ", value)
        ignored = {"limited", "ltd", "private", "pvt", "company", "co", "inc", "corporation", "corp", "india"}
        return " ".join(token for token in value.split() if token not in ignored)

    try:
        async with get_sessionmaker()() as db:
            # get_equity_master reads the public archive CSV; the NSE client must not
            # bootstrap API cookies just to fetch this static file.
            async with NSEApiClient() as client:
                rows = await client.get_equity_master()

            companies = (await db.execute(select(Company))).scalars().all()
            by_symbol = {
                str(c.nse_symbol or "").strip().upper(): c
                for c in companies if str(c.nse_symbol or "").strip()
            }
            by_isin = {
                str(c.isin or "").strip().upper(): c
                for c in companies if str(c.isin or "").strip()
            }
            by_name: dict[str, list[Company]] = {}
            by_exact_name: dict[str, list[Company]] = {}
            for c in companies:
                exact_key = str(c.legal_name or "").strip().casefold()
                if exact_key:
                    by_exact_name.setdefault(exact_key, []).append(c)
                key = name_key(c.legal_name)
                if key:
                    by_name.setdefault(key, []).append(c)

            valid_rows: list[dict[str, str | None]] = []
            seen_symbols: set[str] = set()
            for row in rows:
                symbol = str(row.get("nse_symbol") or "").strip().upper()
                name = str(row.get("legal_name") or "").strip()
                isin = str(row.get("isin") or "").strip().upper() or None
                if not symbol or not name or symbol in seen_symbols:
                    continue
                seen_symbols.add(symbol)
                valid_rows.append({"nse_symbol": symbol, "legal_name": name[:255], "isin": isin[:20] if isin else None})
            source_symbols = {str(r["nse_symbol"]) for r in valid_rows}

            created = updated = 0
            isin_conflicts = ambiguous_names = duplicate_source_identities = 0
            skipped = 0

            for row in valid_rows:
                symbol = str(row["nse_symbol"])
                name = str(row["legal_name"])
                isin = row["isin"]
                key = name_key(name)

                symbol_owner = by_symbol.get(symbol)
                isin_owner = by_isin.get(isin) if isin else None
                name_candidates = by_name.get(key, []) if key else []
                name_owner = name_candidates[0] if len(name_candidates) == 1 else None

                # A symbol already known to the database wins. The name index is
                # deliberately not allowed to redirect it to a different company.
                if symbol_owner is not None:
                    company = symbol_owner
                elif isin_owner is not None:
                    # The ISIN already belongs to a database row. Reuse it only if
                    # it can represent this NSE symbol; the schema permits one NSE
                    # symbol per company. Do not create a second row with that ISIN.
                    old_symbol = str(isin_owner.nse_symbol or "").strip().upper()
                    if old_symbol and old_symbol != symbol and old_symbol in source_symbols:
                        duplicate_source_identities += 1
                        skipped += 1
                        logger.warning(
                            "NSE_COMPANY_IDENTITY_CONFLICT action=skip_duplicate_isin symbol=%s name=%s isin=%s existing_company_id=%s existing_symbol=%s",
                            symbol, name[:100], isin, isin_owner.id, old_symbol,
                        )
                        continue
                    company = isin_owner
                elif len(name_candidates) > 1:
                    ambiguous_names += 1
                    skipped += 1
                    logger.warning(
                        "NSE_COMPANY_IDENTITY_CONFLICT action=skip_ambiguous_name symbol=%s name=%s candidate_ids=%s isin=%s",
                        symbol, name[:100], [c.id for c in name_candidates[:8]], isin,
                    )
                    continue
                elif name_owner is not None:
                    # Legal-name matching is only used when unique. If that row
                    # already has another symbol that is still present in the NSE
                    # master, this is a genuine identity conflict, not a rename.
                    old_symbol = str(name_owner.nse_symbol or "").strip().upper()
                    if old_symbol and old_symbol != symbol and old_symbol in source_symbols:
                        ambiguous_names += 1
                        skipped += 1
                        logger.warning(
                            "NSE_COMPANY_IDENTITY_CONFLICT action=skip_name_symbol_conflict symbol=%s name=%s existing_company_id=%s existing_symbol=%s isin=%s",
                            symbol, name[:100], name_owner.id, old_symbol, isin,
                        )
                        continue
                    company = name_owner
                else:
                    # Never insert a row that would violate a unique identifier.
                    # These checks also protect against multiple worker processes
                    # that loaded stale in-memory indexes before this stage ran.
                    if isin and isin in by_isin:
                        isin_conflicts += 1
                        skipped += 1
                        logger.warning(
                            "NSE_COMPANY_IDENTITY_CONFLICT action=skip_create_duplicate_isin symbol=%s name=%s isin=%s existing_company_id=%s",
                            symbol, name[:100], isin, by_isin[isin].id,
                        )
                        continue
                    exact_name_matches = by_exact_name.get(name.casefold(), [])
                    if exact_name_matches:
                        skipped += 1
                        logger.warning(
                            "NSE_COMPANY_IDENTITY_CONFLICT action=skip_create_duplicate_name symbol=%s name=%s existing_company_ids=%s",
                            symbol, name[:100], [c.id for c in exact_name_matches[:8]],
                        )
                        continue
                    company = Company(legal_name=name[:255], nse_symbol=symbol[:40], isin=isin)
                    db.add(company)
                    companies.append(company)
                    by_symbol[symbol] = company
                    by_name.setdefault(key, []).append(company)
                    by_exact_name.setdefault(name.casefold(), []).append(company)
                    if isin:
                        by_isin[isin] = company
                    created += 1
                    continue

                changed = False
                # Company names can be updated only when this is an exact symbol/
                # ISIN identity. Avoid renaming a record that matched only by name.
                identity_confirmed = symbol_owner is company or isin_owner is company
                if identity_confirmed and name and company.legal_name != name:
                    duplicate_names = [c for c in by_exact_name.get(name.casefold(), []) if c is not company]
                    if not duplicate_names:
                        old_legal_name_key = str(company.legal_name or "").strip().casefold()
                        old_key = name_key(company.legal_name)
                        company.legal_name = name[:255]
                        if old_key in by_name:
                            by_name[old_key] = [c for c in by_name[old_key] if c is not company]
                        by_name.setdefault(key, []).append(company)
                        if old_legal_name_key in by_exact_name:
                            by_exact_name[old_legal_name_key] = [c for c in by_exact_name[old_legal_name_key] if c is not company]
                        by_exact_name.setdefault(name.casefold(), []).append(company)
                        changed = True

                old_symbol = str(company.nse_symbol or "").strip().upper()
                if not old_symbol or (old_symbol != symbol and old_symbol not in source_symbols and symbol_owner is None):
                    symbol_collision = by_symbol.get(symbol)
                    if symbol_collision is None or symbol_collision is company:
                        company.nse_symbol = symbol[:40]
                        by_symbol[symbol] = company
                        if old_symbol and old_symbol in by_symbol and by_symbol[old_symbol] is company:
                            by_symbol.pop(old_symbol, None)
                        changed = True
                    else:
                        logger.warning(
                            "NSE_COMPANY_IDENTITY_CONFLICT action=skip_symbol_assignment company_id=%s symbol=%s existing_company_id=%s",
                            company.id, symbol, symbol_collision.id,
                        )

                if isin:
                    current_isin = str(company.isin or "").strip().upper() or None
                    current_owner = by_isin.get(isin)
                    if current_isin is None:
                        if current_owner is None or current_owner is company:
                            company.isin = isin[:20]
                            by_isin[isin] = company
                            changed = True
                        else:
                            isin_conflicts += 1
                            logger.warning(
                                "NSE_COMPANY_IDENTITY_CONFLICT action=skip_isin_assignment company_id=%s symbol=%s isin=%s existing_company_id=%s",
                                company.id, symbol, isin, current_owner.id,
                            )
                    elif current_isin != isin:
                        # A changed ISIN is not auto-overwritten: that could corrupt
                        # issuer identity. Keep the current value and surface for review.
                        if current_owner is not company:
                            isin_conflicts += 1
                            logger.warning(
                                "NSE_COMPANY_IDENTITY_CONFLICT action=retain_existing_isin company_id=%s symbol=%s current_isin=%s source_isin=%s source_isin_owner_id=%s",
                                company.id, symbol, current_isin, isin,
                                getattr(current_owner, "id", None),
                            )

                if changed:
                    updated += 1

            await db.commit()
            result = {
                "status": "ok", "rows": len(valid_rows), "created": created,
                "updated": updated, "skipped": skipped,
                "isin_conflicts": isin_conflicts,
                "ambiguous_names": ambiguous_names,
                "duplicate_source_identities": duplicate_source_identities,
            }
            logger.info("NSE_COMPANY_UNIVERSE %s elapsed=%.1fs", result, time.monotonic() - started)
            return result
    except Exception as exc:
        # A transient source outage or a race against another worker must not disable
        # Regulatory/Capital ingestion when a cached universe exists.
        logger.exception("NSE_COMPANY_UNIVERSE_FAILED")
        try:
            async with get_sessionmaker()() as db:
                cached_count = int((await db.scalar(select(func.count(Company.id)))) or 0)
            if cached_count > 0:
                logger.warning(
                    "NSE_COMPANY_UNIVERSE_STALE_FALLBACK cached_companies=%s error=%s",
                    cached_count, str(exc)[:500],
                )
                return {
                    "status": "stale_fallback", "rows": cached_count,
                    "created": 0, "updated": 0, "source_error": str(exc)[:1000],
                }
        except Exception:
            logger.exception("NSE_COMPANY_UNIVERSE_FALLBACK_CHECK_FAILED")
        return {"status": "failed", "error": str(exc)[:1000]}


def _parse_market_trade_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = str(value or "").strip()
    if not text_value:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text_value, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


async def _nse_cmp_stage() -> object:
    """Refresh one exchange-wide NSE price snapshot for all known NSE symbols.

    This stage is worker-only. It never calls `/api/quote-equity` per company; one
    CM-UDiFF archive serves the whole requested universe and the API only consumes
    the resulting Redis snapshot.
    """
    try:
        async with get_sessionmaker()() as db:
            symbols = [
                str(value).strip().upper()
                for value in (await db.execute(select(Company.nse_symbol).where(Company.nse_symbol.is_not(None)))).scalars().all()
                if str(value or '').strip()
            ]
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return {"status": "empty", "requested": 0, "matched": 0}
        # CM-UDiFF is a public NSE archive. Do NOT enter the NSE JSON-session
        # context here: __aenter__ primes /get-quotes/equity, which can take
        # 60+ seconds or return 502 during anti-bot degradation even though the
        # public archive is healthy. Archive price collection must stay isolated
        # from the JSON API session/circuit breaker.
        client = NSEApiClient()
        try:
            prices = await client.get_latest_equity_prices(symbols, lookback_days=7)
        finally:
            await client.aclose()
        # Store a durable daily history independently of the Redis latest-price cache.
        # The feed is exchange-wide and can be re-fetched for the same trading date;
        # the unique symbol/date/source key makes each refresh idempotent.
        now = datetime.now(timezone.utc)
        normalized_prices: dict[str, tuple[date, dict]] = {}
        for symbol, quote in (prices or {}).items():
            if not isinstance(quote, dict):
                continue
            trade_day = _parse_market_trade_date(quote.get("trade_date"))
            if trade_day is None:
                logger.warning("NSE_CMP_ROW_SKIPPED symbol=%s reason=unparseable_trade_date value=%r", symbol, quote.get("trade_date"))
                continue
            normalized_prices[str(symbol).strip().upper()] = (trade_day, quote)
        if normalized_prices:
            async with get_sessionmaker()() as db:
                symbols_to_save = list(normalized_prices)
                min_trade_day = min(value[0] for value in normalized_prices.values())
                existing_rows = (await db.execute(
                    select(RegulatoryMarketPrice).where(
                        RegulatoryMarketPrice.nse_symbol.in_(symbols_to_save),
                        RegulatoryMarketPrice.trade_date >= min_trade_day,
                    )
                )).scalars().all()
                existing_by_key = {
                    (str(row.nse_symbol).upper(), row.trade_date, str(row.source)): row
                    for row in existing_rows
                }
                inserted_prices = updated_prices = 0
                for symbol, (trade_day, quote) in normalized_prices.items():
                    source = str(quote.get("source") or "NSE-CM-UDIFF-BHAVCOPY")[:80]
                    current = quote.get("last_price")
                    previous = quote.get("previous_close")
                    change = quote.get("change_percent")
                    def number(value):
                        try:
                            parsed = float(value) if value not in (None, "") else None
                            return parsed if parsed is None or (parsed == parsed and abs(parsed) != float("inf")) else None
                        except (TypeError, ValueError):
                            return None
                    current, previous, change = number(current), number(previous), number(change)
                    row = existing_by_key.get((symbol, trade_day, source))
                    if row is None:
                        row = RegulatoryMarketPrice(
                            nse_symbol=symbol, trade_date=trade_day, current_price=current,
                            previous_close=previous, change_percent=change, source=source,
                            source_url=str(quote.get("source_url") or "")[:1000] or None, collected_at=now,
                        )
                        db.add(row)
                        existing_by_key[(symbol, trade_day, source)] = row
                        inserted_prices += 1
                    else:
                        row.current_price = current
                        row.previous_close = previous
                        row.change_percent = change
                        row.source_url = str(quote.get("source_url") or "")[:1000] or row.source_url
                        row.collected_at = now
                        updated_prices += 1
                await db.commit()
        else:
            inserted_prices = updated_prices = 0

        payload = {
            "version": 1,
            "generated_at": now.isoformat(),
            "items": prices,
            "count": len(prices),
            "source": "NSE",
            "cadence_seconds": settings.master_pipeline_interval_seconds,
        }
        await set_snapshot("nse:equity-prices", payload)
        result = {
            "status": "ok", "requested": len(symbols), "matched": len(prices),
            "price_rows_inserted": inserted_prices, "price_rows_updated": updated_prices,
        }
        logger.info("NSE_CMP_SNAPSHOT %s", result)
        return result
    except Exception as exc:
        logger.warning("NSE_CMP_STAGE_FAILED error=%s", exc)
        return {"status": "failed", "error": str(exc)[:1000]}


async def _regulatory_stage() -> object:
    if not settings.regulatory_enabled:
        logger.info("REGULATORY_STAGE_SKIPPED reason=disabled")
        return {"status": "disabled", "source": "NSE"}
    async with get_sessionmaker()() as db:
        history_days = await _sync_history_days(db, REGULATORY_DESK, "REGULATORY_INCREMENTAL_HISTORY_DAYS")
        result = await sync_regulatory_data(db, history_days=history_days)
        await db.commit()
        logger.info(
            "REGULATORY_STAGE_RESULT status=%s history_days=%s source=%s scanned=%s stored=%s complete=%s errors=%s",
            result.get("status"), history_days, result.get("source"), result.get("scanned"),
            result.get("stored"), result.get("complete"), list((result.get("source_errors") or {}).keys()),
        )
        return result


async def _capital_stage() -> object:
    if not settings.capital_action_enabled:
        logger.info("CAPITAL_ACTION_STAGE_SKIPPED reason=disabled")
        return {"status": "disabled", "source": "NSE"}
    async with get_sessionmaker()() as db:
        history_days = await _sync_history_days(db, CAPITAL_ACTION_DESK, "CAPITAL_ACTION_INCREMENTAL_HISTORY_DAYS")
        result = await sync_pe_warrant_data(db, history_days=history_days)
        await db.commit()
        # Capital Ownership is served from normalized relational records directly.
        result["serving_store"] = "relational_database"
        logger.info(
            "CAPITAL_ACTION_STAGE_RESULT status=%s history_days=%s source=%s mode=%s scanned=%s created=%s complete=%s errors_count=%s errors=%s",
            result.get("status"), history_days, result.get("source"), result.get("collection_mode"),
            result.get("scanned"), result.get("created"), result.get("complete"),
            len(result.get("source_errors") or {}),
            list((result.get("source_errors") or {}).keys())[:25],
        )
        return result


def _historical_bootstrap_enabled() -> bool:
    return bool(settings.regulatory_auto_historical_backfill)


async def _notification_stage() -> object:
    async with get_sessionmaker()() as db:
        result = await ensure_recent_ht_notifications(db)
        await db.commit()
        return result


async def _startup_cycle(
    *,
    skip_nse_cmp: bool = False,
) -> None:
    cycle_started = time.monotonic()

    logger.info(
        "MASTER_PIPELINE_START startup=true code_version=%s",
        WORKER_CODE_VERSION,
    )

    # Coverage metadata is refreshed here. The dedicated Regulatory scheduler is
    # the sole owner of the NSE company-universe refresh; doing it here as well caused
    # two full EQUITY_L.csv downloads and competing updates during every worker startup.
    await _run_stage("coverage-sync", _coverage_stage)

    # The worker already primes the exchange-wide CMP snapshot before schedulers start.
    # Avoid immediately downloading the same BhavCopy a second time on the first
    # master cycle. Subsequent master cycles keep the normal refresh behavior.
    stages: list[tuple[str, Callable[[], Awaitable[object]]]] = [
        ("nse-cmp", _nse_cmp_stage),
    ]

    for name, func in stages:
        if name == "nse-cmp" and skip_nse_cmp:
            logger.info("PIPELINE_STAGE_SKIP stage=nse-cmp reason=already_bootstrapped_at_worker_start")
            continue
        await _run_stage(name, func)

    await _run_stage(
        "notifications",
        _notification_stage,
    )

    logger.info(
        "MASTER_PIPELINE_DONE startup=true elapsed_seconds=%.1f next_cycle_seconds=%s code_version=%s",
        time.monotonic() - cycle_started,
        settings.master_pipeline_interval_seconds,
        WORKER_CODE_VERSION,
    )


async def _newsalert_ten_minute_scheduler(stop_event: asyncio.Event) -> None:
    """Run all configured NewsAlert collectors every 10 minutes, without overlap."""
    interval = 10 * 60
    # Collect immediately after worker startup by default. Set
    # NEWSALERT_STARTUP_DELAY_SECONDS only when a deliberate startup stagger is needed.
    startup_delay = max(0, min(300, int(os.getenv("NEWSALERT_STARTUP_DELAY_SECONDS", "0"))))
    next_run = time.monotonic() + startup_delay
    logger.info(
        "NEWSALERT_SCHEDULER_STARTED cadence_seconds=%s startup_delay_seconds=%s "
        "youtube_enabled=%s pipeline=ingest_configured_news storage=news_alert_items",
        interval,
        startup_delay,
        bool(settings.youtube_enabled),
    )

    while not stop_event.is_set():
        wait_seconds = max(0.0, next_run - time.monotonic())
        if wait_seconds > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

        if stop_event.is_set():
            return

        cycle_started = time.monotonic()
        logger.info(
            "NEWSALERT_V2_SCHEDULED_RUN_START cadence_seconds=%s "
            "news_lookback_days=7 social_lookback_hours=48",
            interval,
        )
        result: object = {"status": "failed", "reason": "cycle_did_not_complete"}
        try:
            result = await _run_stage("newsalert-v2-cycle", _news_stage)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep the scheduler alive if an unexpected error escapes _run_stage.
            logger.exception("NEWSALERT_V2_SCHEDULED_RUN_UNEXPECTED_FAILURE")
        elapsed = time.monotonic() - cycle_started
        logger.info(
            "NEWSALERT_V2_SCHEDULED_RUN_DONE cadence_seconds=%s elapsed_seconds=%.1f result=%s",
            interval,
            elapsed,
            _brief(result),
        )

        # Schedule from the previous cycle's start. Skip missed ticks if collection
        # overruns; never launch overlapping catch-up runs.
        next_run = cycle_started + interval
        now = time.monotonic()
        while next_run <= now:
            next_run += interval


async def _earnings_calendar_stage(
    source_names: set[str] | None = None,
    *,
    refresh_coverage: bool = True,
) -> object:
    """Sync coverage on full runs; optionally retry only failed public sources."""
    if refresh_coverage:
        coverage_result = await _run_stage("earnings-calendar-coverage-sync", _coverage_stage)
        if isinstance(coverage_result, dict) and coverage_result.get("status") == "failed":
            logger.warning(
                "EARNINGS_CALENDAR_COVERAGE_SYNC_FAILED continuing_with_last_known_coverage result=%s",
                _brief(coverage_result),
            )
    async with get_sessionmaker()() as db:
        result = await collect_earnings_calendar(db, source_names=source_names)
        await db.commit()
        logger.info(
            "EARNINGS_CALENDAR_STAGE_RESULT status=%s coverage_companies=%s inserted=%s updated=%s unchanged=%s deduplicated=%s sources=%s",
            result.get("status"), result.get("coverage_companies"), result.get("inserted"),
            result.get("updated"), result.get("unchanged"), result.get("deduplicated_events"),
            result.get("source_stats"),
        )
        return result


async def _earnings_calendar_six_day_scheduler(stop_event: asyncio.Event) -> None:
    """Full Earnings Calendar crawl every six days, with bounded retries for failed sources.

    Failed sources are retried individually; a healthy source is not repeatedly fetched
    merely because another site timed out. The normal full-refresh cadence remains six days.
    """
    interval = 6 * 24 * 60 * 60
    startup_delay = max(0, min(600, int(os.getenv("EARNINGS_CALENDAR_STARTUP_DELAY_SECONDS", "45"))))
    retry_seconds = max(300, int(os.getenv("EARNINGS_CALENDAR_RETRY_SECONDS", "900")))
    max_source_retries = max(0, min(5, int(os.getenv("EARNINGS_CALENDAR_MAX_SOURCE_RETRIES", "3"))))
    next_full_run = time.monotonic() + startup_delay
    if startup_delay:
        logger.info("EARNINGS_CALENDAR_STARTUP_STAGGER delay_seconds=%s", startup_delay)
    next_retry_run: float | None = None
    retry_sources: set[str] = set()
    source_retry_count = 0

    while not stop_event.is_set():
        is_source_retry_pending = bool(retry_sources and next_retry_run is not None)
        next_run = min(next_full_run, next_retry_run) if is_source_retry_pending else next_full_run
        wait_seconds = max(0.0, next_run - time.monotonic())
        if wait_seconds > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
        if stop_event.is_set():
            return

        now = time.monotonic()
        is_source_retry = bool(
            retry_sources and next_retry_run is not None
            and next_retry_run <= next_full_run and now < next_full_run
        )
        attempted_retry_sources = sorted(retry_sources) if is_source_retry else []
        cycle_started = now
        if is_source_retry:
            logger.info(
                "EARNINGS_CALENDAR_SOURCE_RETRY_RUN_START retry=%s/%s retry_seconds=%s sources=%s full_cadence_seconds=%s",
                source_retry_count + 1, max_source_retries, retry_seconds,
                sorted(retry_sources), interval,
            )
            result = await _run_stage(
                "earnings-calendar-source-retry",
                lambda: _earnings_calendar_stage(
                    None if "__ALL__" in retry_sources else set(retry_sources),
                    refresh_coverage=False,
                ),
            )
            source_retry_count += 1
        else:
            # A normal full scrape resets the bounded retry budget and is scheduled
            # from this cycle's start; slow runs never overlap or cause catch-up bursts.
            logger.info(
                "EARNINGS_CALENDAR_SCHEDULED_RUN_START cadence_seconds=%s cadence_days=6 coverage_only=true retry_sources=%s",
                interval, sorted(retry_sources),
            )
            result = await _run_stage("earnings-calendar-scrape", _earnings_calendar_stage)
            next_full_run = cycle_started + interval
            while next_full_run <= time.monotonic():
                next_full_run += interval
            source_retry_count = 0

        source_stats = result.get("source_stats", []) if isinstance(result, dict) else []
        failed_sources = {
            str(item.get("source"))
            for item in source_stats
            if item.get("source") and (
                item.get("status") == "failed" or bool(item.get("parse_suspicious"))
            )
        }
        # If the stage failed before returning source-level stats, schedule one full
        # retry rather than silently waiting six days without a working scrape.
        if not isinstance(result, dict) or result.get("status") in {"failed", "error"} and not source_stats:
            failed_sources = {"__ALL__"}

        if failed_sources and source_retry_count < max_source_retries:
            retry_sources = failed_sources
            next_retry_run = min(time.monotonic() + retry_seconds, next_full_run)
            logger.warning(
                "EARNINGS_CALENDAR_SOURCE_RETRY_SCHEDULED sources=%s retry_seconds=%s retries_used=%s retries_limit=%s next_full_run_in_seconds=%.0f",
                sorted(retry_sources), retry_seconds, source_retry_count, max_source_retries,
                max(0.0, next_full_run - time.monotonic()),
            )
        else:
            if failed_sources and source_retry_count >= max_source_retries:
                logger.error(
                    "EARNINGS_CALENDAR_SOURCE_RETRIES_EXHAUSTED sources=%s retries=%s next_full_cadence_days=6",
                    sorted(failed_sources), source_retry_count,
                )
            retry_sources = set()
            next_retry_run = None

        if not is_source_retry:
            logger.info(
                "EARNINGS_CALENDAR_SCHEDULED_RUN_DONE cadence_seconds=%s cadence_days=6 status=%s next_full_run_in_seconds=%.0f",
                interval, result.get("status") if isinstance(result, dict) else "failed",
                max(0.0, next_full_run - time.monotonic()),
            )
        else:
            logger.info(
                "EARNINGS_CALENDAR_SOURCE_RETRY_RUN_DONE sources=%s status=%s",
                attempted_retry_sources, result.get("status") if isinstance(result, dict) else "failed",
            )


async def _regulatory_activity_stage() -> object:
    """Run the four-source normalized Regulatory Activity ingestion pipeline."""
    if not (settings.regulatory_enabled or settings.capital_action_enabled):
        logger.info("REGULATORY_ACTIVITY_STAGE_SKIPPED reason=disabled")
        return {"status": "disabled"}
    async with get_sessionmaker()() as db:
        days = max(1, min(int(settings.regulatory_incremental_history_days or 7), 2557))
        result = await sync_regulatory_activity(db, days=days)
        await db.commit()
        logger.info("REGULATORY_ACTIVITY_STAGE_RESULT status=%s complete=%s categories=%s", result.get("status"), result.get("complete"), list((result.get("categories") or {}).keys()))
        return result


async def _run_regulatory_pipeline() -> None:
    """Run the normalized Regulatory Activity pipeline on the four-hour cadence.

    Legacy Insider Buying/Capital Ownership jobs remain available for older API
    consumers, but the Regulatory Activity frontend is served exclusively from
    regulatory_activity_events and this unified collector.
    """
    universe = await _run_stage("regulatory-company-universe", _nse_company_universe_stage)
    if isinstance(universe, dict) and universe.get("status") == "stale_fallback":
        logger.warning("REGULATORY_PIPELINE_USING_STALE_COMPANY_UNIVERSE cached_companies=%s", universe.get("rows"))
    await _run_stage("regulatory-activity-unified", _regulatory_activity_stage)


async def _four_hour_regulatory_scheduler(stop_event: asyncio.Event) -> None:
    """Run regulatory ingestion immediately, then every four hours by default.

    REGULATORY_INTERVAL_SECONDS may override the interval for controlled operations;
    14400 seconds (four hours) is the production default. Each wait begins after the
    prior cycle finishes, so cycles never overlap.
    """
    interval_seconds = max(60, int(__import__("os").getenv("REGULATORY_INTERVAL_SECONDS", "14400")))
    run_immediately = __import__("os").getenv("REGULATORY_RUN_IMMEDIATELY", "true").lower() in {"1", "true", "yes", "on"}
    next_run = time.monotonic() if run_immediately else time.monotonic() + interval_seconds
    while not stop_event.is_set():
        wait_seconds = max(0.0, next_run - time.monotonic())
        if wait_seconds > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
                return
            except asyncio.TimeoutError:
                pass
        if stop_event.is_set():
            return
        cycle_started = time.monotonic()
        logger.info(
            "REGULATORY_FOUR_HOUR_RUN_START interval_seconds=%s incremental_history_days=%s capital_history_days=%s auto_historical_backfill=%s",
            interval_seconds,
            settings.regulatory_incremental_history_days,
            settings.capital_action_incremental_history_days,
            settings.regulatory_auto_historical_backfill,
        )
        try:
            await _run_regulatory_pipeline()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("REGULATORY_FOUR_HOUR_RUN_FAILED")
        # Keep start times four hours apart when a cycle fits. If a run overruns
        # one or more slots, skip missed ticks rather than overlap or burst-run.
        next_run = cycle_started + interval_seconds
        now = time.monotonic()
        while next_run <= now:
            next_run += interval_seconds


async def _repeating_master_cycle(
    stop_event: asyncio.Event,
) -> None:
    interval = max(
        300,
        settings.master_pipeline_interval_seconds,
    )
    first = True
    # _run_worker already refreshed CMP before tasks were created. Skip the duplicate
    # snapshot only when this first master cycle is configured to run immediately.
    skip_initial_cmp = bool(settings.master_pipeline_startup_immediate)

    next_run = time.monotonic()

    while not stop_event.is_set():
        if first:
            first = False
            if not settings.master_pipeline_startup_immediate:
                next_run += interval
        else:
            next_run = max(
                next_run,
                time.monotonic(),
            )

        wait_seconds = max(
            0.0,
            next_run - time.monotonic(),
        )

        if wait_seconds > 0:
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=wait_seconds,
                )
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

        if stop_event.is_set():
            return

        cycle_start = time.monotonic()
        await _startup_cycle(skip_nse_cmp=skip_initial_cmp)
        skip_initial_cmp = False
        next_run = cycle_start + interval



async def _supervised_master_cycle(stop_event: asyncio.Event) -> None:
    """Continuously own the four-hour cycle and restart after unexpected exits."""
    while not stop_event.is_set():
        try:
            await _repeating_master_cycle(stop_event)
            if stop_event.is_set():
                return
            logger.error("MASTER_CYCLE_EXITED_UNEXPECTEDLY; restarting_after_seconds=5")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MASTER_CYCLE_CRASHED; restarting_after_seconds=5")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise

async def _queued_news_job_loop(
    stop_event: asyncio.Event,
) -> None:
    """Durable DB fallback for manual NewsAlert refreshes.

    This is intentionally independent of Redis. A manual NewsAlert refresh should
    still execute even when Redis is not installed/running locally.
    """
    while not stop_event.is_set():
        try:
            async with get_sessionmaker()() as db:
                row = await db.scalar(
                    select(EventOutbox)
                    .where(
                        EventOutbox.event_type == EventType.NEWS_INGEST_REQUESTED.value,
                        or_(
                            EventOutbox.status == "pending",
                            and_(
                                EventOutbox.status == "processing",
                                EventOutbox.created_at < datetime.now(timezone.utc) - timedelta(minutes=30),
                            ),
                        ),
                    )
                    .order_by(EventOutbox.id)
                    .limit(1)
                    .with_for_update(skip_locked=True),
                )

                if row is not None:
                    # Materialize all scalar values BEFORE the network-heavy workflow.
                    # process_news_ingest_event() may flush/commit/rollback internally,
                    # and rollback can expire ORM attributes. Accessing row.attempts
                    # afterwards from an AsyncSession triggers MissingGreenlet because
                    # SQLAlchemy tries to lazy-load the expired attribute.
                    row_id = int(row.id)
                    row_event_id = str(row.event_id)
                    row_payload = dict(row.payload or {})
                    next_attempt = int(row.attempts or 0) + 1
                    event = Event.model_validate(row_payload)
                    # Mark the durable outbox row before the workflow's JSON branch
                    # commits to release its DB connection for source collection.
                    row.status = "processing"
                    await db.flush()
                    try:
                        await process_news_ingest_event(
                            event,
                            db,
                            None,
                        )

                        # Update by primary key after the workflow has finished. This
                        # deliberately avoids touching the possibly-expired ORM row.
                        await db.execute(
                            text(
                                "UPDATE event_outbox "
                                "SET status=:status, published_at=:published_at, "
                                "attempts=:attempts, last_error=:last_error "
                                "WHERE id=:id"
                            ),
                            {
                                "status": "processed",
                                "published_at": datetime.now(timezone.utc),
                                "attempts": next_attempt,
                                "last_error": None,
                                "id": row_id,
                            },
                        )
                        await db.commit()
                    except Exception as exc:
                        # The workflow may have rolled the session back. Start from a
                        # clean transaction, then update the outbox record with SQL so
                        # no expired ORM attribute is accessed.
                        try:
                            await db.rollback()
                        except Exception:
                            logger.exception(
                                "NewsAlert outbox rollback failed event_id=%s",
                                row_event_id,
                            )
                        try:
                            await db.execute(
                                text(
                                    "UPDATE event_outbox "
                                    "SET status=:status, attempts=:attempts, "
                                    "last_error=:last_error "
                                    "WHERE id=:id"
                                ),
                                {
                                    "status": "pending",
                                    "attempts": next_attempt,
                                    "last_error": str(exc)[:4000],
                                    "id": row_id,
                                },
                            )
                            await db.commit()
                        except Exception:
                            logger.exception(
                                "DB fallback NewsAlert outbox update failed event_id=%s attempts=%s",
                                row_event_id,
                                next_attempt,
                            )
                        else:
                            logger.exception(
                                "DB fallback NewsAlert job failed event_id=%s attempts=%s",
                                row_event_id,
                                next_attempt,
                            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            logger.warning("Queued NewsAlert DB poll timed out; disposing DB engine before retry: %s", exc)
            try:
                await dispose_engine()
            except Exception:
                logger.exception("DB engine dispose failed after timeout")
        except Exception:
            logger.exception(
                "Queued NewsAlert fallback poll failed"
            )

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.worker_queue_poll_seconds,
            )
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise


async def _portfolio_comparison_job_loop(
    stop_event: asyncio.Event,
) -> None:
    """Drain the Portfolio Manager DB queue with explicit heartbeat logging."""
    await portfolio_queue_loop(stop_event, owner="dedicated-worker")




async def _worker_heartbeat(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        logger.info("WORKER_HEARTBEAT alive=true")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            return
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise


async def _restartable_background_loop(
    name: str,
    factory: Callable[[asyncio.Event], Awaitable[object]],
    stop_event: asyncio.Event,
    *,
    restart_delay_seconds: float = 5.0,
) -> None:
    """Restart a critical scheduler if it exits unexpectedly.

    A source-level exception must not silently remove the Solar or Sector
    Screener scheduler from the long-lived worker. Individual cycles still
    own their normal retry/error policy; this supervisor is the outer guard.
    """
    while not stop_event.is_set():
        try:
            await factory(stop_event)
            if stop_event.is_set():
                return
            logger.error("WORKER_SCHEDULER_EXITED_UNEXPECTEDLY name=%s action=restart", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WORKER_SCHEDULER_CRASHED name=%s action=restart", name)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(1.0, restart_delay_seconds))
            return
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            raise


async def _pulse_cache_warm_loop(stop_event: asyncio.Event) -> None:
    """Own the one-minute Pulse scan; API requests read the shared JSON snapshot."""
    active_interval = max(60, int(os.getenv("PULSE_SCAN_INTERVAL_SECONDS", "60")))
    quiet_interval = max(300, int(os.getenv("PULSE_QUIET_SCAN_INTERVAL_SECONDS", "900")))
    logger.info("PULSE_SIGNAL_SCAN_STARTED active_interval=%s quiet_interval=%s", active_interval, quiet_interval)
    while not stop_event.is_set():
        interval = active_interval if is_pulse_polling_window() else quiet_interval
        started = time.monotonic()
        try:
            snapshot = await _run_stage(
                "pulse-movement-scan",
                lambda: refresh_pulse_data(force_refresh=True),
            )
            if isinstance(snapshot, dict):
                summary = snapshot.get("summary") or {}
                providers = snapshot.get("provider_status") or {}
                logger.info(
                    "PULSE_SIGNAL_SCAN_RESULT signals=%s sectors=%s stocks=%s quotes=%s/%s providers=%s",
                    summary.get("signals", 0), summary.get("sector_signals", 0),
                    summary.get("stock_signals", 0), summary.get("market_data_available", 0),
                    summary.get("market_data_requested", 0),
                    {key: value.get("status") for key, value in providers.items() if isinstance(value, dict)},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("PULSE_SIGNAL_SCAN_FAILED")
        remaining = max(1.0, interval - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=remaining)
            return
        except asyncio.TimeoutError:
            continue


async def _run() -> None:
    stop_event = asyncio.Event()
    tasks: list[asyncio.Task] = []
    event_bus: RedisEventBus | None = None
    orchestrator: PipelineOrchestrator | None = None

    loop = asyncio.get_running_loop()
    for sig in (
        signal.SIGINT,
        signal.SIGTERM,
    ):
        try:
            loop.add_signal_handler(
                sig,
                stop_event.set,
            )
        except (
            NotImplementedError,
            RuntimeError,
        ):
            pass

    logger.info(
        "WORKER_BOOT code_version=%s redis_consumers=%s master_pipeline=%s interval_seconds=%s",
        WORKER_CODE_VERSION,
        settings.worker_redis_consumers_enabled,
        settings.master_pipeline_enabled,
        settings.master_pipeline_interval_seconds,
    )

    # Pulse movement scanning is worker-owned. The frontend reads the shared
    # snapshot and does not create a second concurrent provider polling loop.
    tasks.append(asyncio.create_task(
        _pulse_cache_warm_loop(stop_event),
        name="pulse-movement-scan-one-minute-scheduler",
    ))
    # Solar is a dedicated worker-owned source mirror: warm one all-years
    # Cell snapshot and one all-years Module snapshot at startup, then refresh
    # each product snapshot on the five-minute source cadence. Dashboard year
    # and period changes are local projections over those persisted snapshots.
    logger.info("SOLAR_PARALLEL_COLLECTORS_CONFIGURED products=Cell,Module concurrency=2 refresh_seconds=300")
    tasks.append(asyncio.create_task(
        _restartable_background_loop(
            "solar-nise-refresh-five-minute-scheduler",
            lambda event: solar_refresh_loop(event, interval_seconds=5 * 60),
            stop_event,
        ),
        name="solar-nise-refresh-five-minute-scheduler",
    ))
    logger.info("PPAC_LPG_COLLECTOR_CONFIGURED source=PPAC_PETROLEUM_NATURAL_GAS startup_refresh=true api_fallback=true")
    tasks.append(asyncio.create_task(
        _restartable_background_loop(
            "sector-screener-refresh-hourly-scheduler",
            lambda event: sector_screener_refresh_loop(event, interval_seconds=60 * 60),
            stop_event,
        ),
        name="sector-screener-refresh-hourly-scheduler",
    ))
    logger.info("TRADESTAT_COLLECTOR_CONFIGURED source=MEIDB separate_import_export_watchlists=true cache_ttl_seconds=3600 startup_refresh=true pid=%s", os.getpid())
    logger.info("TRADESTAT_WORKER_TASK_CREATE scheduler=tradestat-meidb-hourly-scheduler interval_seconds=3600")
    tasks.append(asyncio.create_task(
        _restartable_background_loop(
            "tradestat-meidb-hourly-scheduler",
            lambda event: tradestat_refresh_loop(event, interval_seconds=60 * 60),
            stop_event,
        ),
        name="tradestat-meidb-hourly-scheduler",
    ))
    logger.info("TRADESTAT_WORKER_TASK_CREATED scheduler=tradestat-meidb-hourly-scheduler")

    await _warm_database()
    try:
        async with get_sessionmaker()() as db:
            count = await warm_auth_users(db)
            logger.info("AUTH_CACHE_WARM count=%s", count)
    except Exception:
        logger.exception("AUTH_CACHE_WARM_FAILED")

    # Prime shared NSE price data; news and regulatory event rows themselves are
    # always read from the relational database.
    await _run_stage("nse-cmp-bootstrap", _nse_cmp_stage)

    # Regulatory Signal and Capital Ownership read authoritative relational data
    # directly; no module-specific cached snapshots are built at startup.

    # Redis snapshot/auth caches are data-serving infrastructure. Redis event consumers
    # remain optional, but the same Redis instance is used for instant UI snapshots.
    if (
        settings.worker_redis_consumers_enabled
        and settings.event_bus_backend == "redis"
        and settings.redis_enabled
    ):
        try:
            event_bus = RedisEventBus()
            await event_bus.connect()
            orchestrator = PipelineOrchestrator(
                event_bus
            )
            await orchestrator.start()
            tasks.append(
                asyncio.create_task(
                    outbox_worker(
                        event_bus,
                        stop_event,
                    ),
                    name="event-outbox-worker",
                )
            )
            logger.info(
                "Redis event pipeline started"
            )
        except Exception:
            logger.exception(
                "Redis consumers disabled for this worker run; batch pipeline will continue"
            )
            if event_bus is not None:
                try:
                    await event_bus.close()
                except Exception:
                    pass
            event_bus = None
            orchestrator = None
    else:
        logger.info(
            "Redis event consumers are disabled; using durable DB workflow path"
        )

    if settings.master_pipeline_enabled:
        tasks.append(
            asyncio.create_task(
                _supervised_master_cycle(stop_event),
                name="master-four-hour-cycle",
            )
        )

    # NewsAlert has one canonical ingestion owner. The old NSE/BSE archive
    # scheduler wrote into the legacy Article path and is intentionally not started;
    # NewsAlert items are collected by the ten-minute v2 cycle into news_alert_items.
    tasks.append(asyncio.create_task(
        _newsalert_ten_minute_scheduler(stop_event),
        name="newsalert-v2-ten-minute-scheduler",
    ))

    # Regulatory Desk has an independent four-hour collection cadence.
    tasks.append(asyncio.create_task(
        _four_hour_regulatory_scheduler(stop_event),
        name="regulatory-signal-four-hour-scheduler",
    ))

    # Earnings Calendar has its own six-day cadence and only stores active, approved Coverage List companies.
    tasks.append(asyncio.create_task(
        _earnings_calendar_six_day_scheduler(stop_event),
        name="earnings-calendar-six-day-scheduler",
    ))

    # Always keep this alive so manual NewsAlert refreshes work even without Redis.
    tasks.append(
        asyncio.create_task(
            _queued_news_job_loop(stop_event),
            name="newsalert-db-job-fallback",
        )
    )

    # PMS portfolio uploads are durable DB jobs. The queue loop is independent of
    # the four-hour market/regulatory cycle so one slow portfolio PDF cannot delay
    # the core investment-intelligence refresh.
    tasks.append(
        asyncio.create_task(
            _portfolio_comparison_job_loop(stop_event),
            name="portfolio-comparison-db-job-worker",
        )
    )

    tasks.append(asyncio.create_task(_worker_heartbeat(stop_event), name="worker-heartbeat"))

    logger.info(
        "WORKER_READY code_version=%s tasks=%s",
        WORKER_CODE_VERSION,
        [task.get_name() for task in tasks],
    )

    try:
        await stop_event.wait()
    finally:
        for task in tasks:
            task.cancel()

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        if orchestrator is not None:
            try:
                await orchestrator.stop()
            except Exception:
                logger.debug(
                    "Pipeline orchestrator shutdown failed",
                    exc_info=True,
                )
        elif event_bus is not None:
            try:
                await event_bus.close()
            except Exception:
                logger.debug(
                    "Redis event bus shutdown failed",
                    exc_info=True,
                )

        try:
            await pulse_market_collector.aclose()
            await close_pulse_data()
        except Exception:
            logger.debug("Pulse collector shutdown failed", exc_info=True)
        await dispose_engine()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()