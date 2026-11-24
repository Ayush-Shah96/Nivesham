from __future__ import annotations

import json
import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from ii_platform.database.models import User, UserRole
from infrastructure.security.permissions import require_role

from .scrapper.sector_screener import SECTORS
from .sector_screener_service import (
    CACHE_TTL_SECONDS,
    cache_ttl_seconds,
    get_report,
    get_solar_company_series,
    get_tradestat_monitor_report,
)
from .scrapper.sector_screener import automotive
from .tradestat_watchlist import add_watchlist_item, get_watchlists, normalize_hs_code, normalize_trade_type, remove_watchlist_item

router = APIRouter(tags=["Sector Screener"])
logger = logging.getLogger(__name__)
READ_ROLES = (UserRole.ADMIN, UserRole.INVESTMENT, UserRole.ANALYST)


@router.get("/catalog")
async def sector_screener_catalog(user: User = Depends(require_role(*READ_ROLES))):
    return {
        "sectors": [
            {
                "key": key,
                "name": sector["name"],
                "source": sector["source"],
                "source_url": sector["source_url"],
                "description": sector.get("description", ""),
                "cache_ttl_seconds": cache_ttl_seconds(key),
                "cache_ttl_minutes": cache_ttl_seconds(key) // 60,
                "reports": [{"key": report_key, **report} for report_key, report in sector["reports"].items()],
            }
            for key, sector in SECTORS.items()
        ],
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "cache_ttl_minutes": CACHE_TTL_SECONDS // 60,
        "cache_ttl_by_sector": {key: cache_ttl_seconds(key) for key in SECTORS},
    }


@router.get("/reports/automotive/overview/makers")
async def automotive_maker_options(
    search: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(30, ge=1, le=50),
    user: User = Depends(require_role(*READ_ROLES)),
):
    try:
        return {"options": await automotive.search_makers(search, limit=limit)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to load Parivahan maker options: {type(exc).__name__}") from exc


@router.get("/reports/solar/overview/company-series")
async def solar_company_series(
    product: str = Query("Cell", pattern="^(Cell|Module)$"),
    agency_ids: str = Query(..., min_length=1, max_length=2000),
    period: str = Query("monthly", pattern="^(monthly|yearly)$"),
    user: User = Depends(require_role(*READ_ROLES)),
):
    """Return cached selected-company history and queue missing monthly data."""
    requested_ids = list(dict.fromkeys(value.strip() for value in agency_ids.split(",") if value.strip()))[:12]
    if not requested_ids:
        raise HTTPException(status_code=422, detail="At least one NISE manufacturer ID is required")
    try:
        return await get_solar_company_series(product=product, agency_ids=requested_ids, period=period)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to load NISE company manufacturing history: {type(exc).__name__}") from exc




class TradeStatWatchlistInput(BaseModel):
    hs_code: str = Field(min_length=2, max_length=12)
    commodity: str | None = Field(default=None, max_length=160)
    trade_type: str = Field(default="import", pattern="^(import|export)$")


@router.get("/tradestat/watchlist")
async def tradestat_get_watchlist(user: User = Depends(require_role(*READ_ROLES))):
    started = time.monotonic()
    lists = get_watchlists(user.id)
    logger.info("TRADESTAT_ROUTE_WATCHLIST_GET pid=%s user_id=%s imports=%d exports=%d", os.getpid(), user.id, len(lists["imports"]), len(lists["exports"]))
    return {"imports": lists["imports"], "exports": lists["exports"], "limits": {"imports": 30, "exports": 30}}


@router.post("/tradestat/watchlist")
async def tradestat_add_watchlist(payload: TradeStatWatchlistInput, user: User = Depends(require_role(*READ_ROLES))):
    started = time.monotonic()
    logger.info("TRADESTAT_ROUTE_WATCHLIST_ADD_START pid=%s user_id=%s trade_type=%s hs_code=%s", os.getpid(), user.id, payload.trade_type, payload.hs_code)
    try:
        trade = normalize_trade_type(payload.trade_type)
        items = add_watchlist_item(user.id, payload.hs_code, payload.commodity, trade_type=trade)
        logger.info("TRADESTAT_ROUTE_WATCHLIST_ADD_DONE pid=%s user_id=%s trade_type=%s hs_code=%s count=%d duration_ms=%.1f", os.getpid(), user.id, trade, payload.hs_code, len(items), (time.monotonic()-started)*1000)
        return {"items": items, "trade_type": trade, "added": normalize_hs_code(payload.hs_code)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/tradestat/watchlist/{hs_code}")
async def tradestat_delete_watchlist(
    hs_code: str,
    trade_type: str = Query("import", pattern="^(import|export)$"),
    user: User = Depends(require_role(*READ_ROLES)),
):
    started = time.monotonic()
    logger.info("TRADESTAT_ROUTE_WATCHLIST_DELETE_START pid=%s user_id=%s trade_type=%s hs_code=%s", os.getpid(), user.id, trade_type, hs_code)
    try:
        trade = normalize_trade_type(trade_type)
        items = remove_watchlist_item(user.id, hs_code, trade_type=trade)
        logger.info("TRADESTAT_ROUTE_WATCHLIST_DELETE_DONE pid=%s user_id=%s trade_type=%s hs_code=%s count=%d duration_ms=%.1f", os.getpid(), user.id, trade, hs_code, len(items), (time.monotonic()-started)*1000)
        return {"items": items, "trade_type": trade}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/tradestat/monitor")
async def tradestat_monitor(refresh: bool = Query(False), user: User = Depends(require_role(*READ_ROLES))):
    started = time.monotonic()
    lists = get_watchlists(user.id)
    filters = {
        "import_hs_codes": [item.get("hs_code") for item in lists["imports"] if item.get("hs_code")],
        "export_hs_codes": [item.get("hs_code") for item in lists["exports"] if item.get("hs_code")],
    }
    logger.info("TRADESTAT_ROUTE_MONITOR_START pid=%s user_id=%s refresh=%s imports=%d exports=%d import_codes=%s export_codes=%s", os.getpid(), user.id, refresh, len(filters["import_hs_codes"]), len(filters["export_hs_codes"]), filters["import_hs_codes"], filters["export_hs_codes"])
    report = await get_tradestat_monitor_report(
        import_codes=filters["import_hs_codes"],
        export_codes=filters["export_hs_codes"],
        force_refresh=refresh,
    )
    logger.info("TRADESTAT_ROUTE_MONITOR_DONE pid=%s user_id=%s refresh=%s status=%s refresh_started=%s refresh_in_progress=%s directions=%s age=%s duration_ms=%.1f error=%s", os.getpid(), user.id, refresh, report.get("status"), report.get("refresh_started"), report.get("refresh_in_progress"), {key: value.get("status") for key, value in (report.get("direction_status") or {}).items()}, report.get("age_seconds"), (time.monotonic()-started)*1000, report.get("error"))
    return {"watchlists": lists, "report": report}


@router.get("/tradestat/commodity/{hs_code}")
async def tradestat_commodity_history(
    hs_code: str,
    trade_type: str = Query("import", pattern="^(import|export)$"),
    refresh: bool = Query(False),
    user: User = Depends(require_role(*READ_ROLES)),
):
    started = time.monotonic()
    logger.info("TRADESTAT_ROUTE_HISTORY_START pid=%s user_id=%s hs_code=%s trade_type=%s refresh=%s", os.getpid(), user.id, hs_code, trade_type, refresh)
    try:
        code = normalize_hs_code(hs_code)
        trade = normalize_trade_type(trade_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    lists = get_watchlists(user.id)
    selected = lists[f"{trade}s"]
    if not any(str(item.get("hs_code")) == code for item in selected):
        raise HTTPException(status_code=404, detail=f"HS code is not in your {trade} TradeStat watchlist")
    filters = {
        "import_hs_codes": [code] if trade == "import" else [],
        "export_hs_codes": [code] if trade == "export" else [],
        "trade_type": trade,
        "hs_codes": [code],
        "focus_code": code,
        "history": True,
    }
    report = await get_report("tradestat", "overview", force_refresh=refresh, filters=filters)
    logger.info("TRADESTAT_ROUTE_HISTORY_DONE pid=%s user_id=%s hs_code=%s trade_type=%s status=%s refresh_started=%s refresh_in_progress=%s age=%s duration_ms=%.1f error=%s", os.getpid(), user.id, code, trade, report.get("status"), report.get("refresh_started"), report.get("refresh_in_progress"), report.get("age_seconds"), (time.monotonic()-started)*1000, report.get("error"))
    return report


@router.get("/reports/{sector_key}/{report_key}")
async def sector_screener_report(
    sector_key: str,
    report_key: str = "overview",
    refresh: bool = Query(False),
    category: str | None = Query(None, max_length=80),
    fuel: str | None = Query(None, max_length=40),
    maker: str | None = Query(None, max_length=160),
    makers: str | None = Query(None, max_length=2000),
    timeline: str = Query("monthly", pattern="^(monthly|quarterly|yearly)$"),
    product: str = Query("Cell", pattern="^(Cell|Module)$"),
    period: str = Query("monthly", pattern="^(monthly|yearly)$"),
    source_filters: str | None = Query(None, max_length=6000),
    user: User = Depends(require_role(*READ_ROLES)),
):
    sector_key = sector_key.strip().lower()
    report_key = report_key.strip().lower()
    sector = SECTORS.get(sector_key)
    if sector is None:
        raise HTTPException(status_code=404, detail="Unknown Sector Screener sector")
    if report_key not in sector["reports"]:
        raise HTTPException(status_code=404, detail="Unknown report for selected sector")
    filters = None
    if sector_key == "automotive":
        filters = {"category": category, "fuel": fuel, "maker": maker, "makers": makers, "timeline": timeline}
    elif sector_key == "solar":
        parsed_source_filters: dict[str, str] = {}
        if source_filters:
            try:
                decoded = json.loads(source_filters)
                if isinstance(decoded, dict):
                    parsed_source_filters = {
                        str(key)[:240]: str(value)[:300]
                        for key, value in list(decoded.items())[:30]
                        if key is not None and value is not None and str(key).strip() and str(value).strip()
                    }
                else:
                    raise ValueError("source_filters must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="Invalid Solar source_filters JSON object") from exc
        filters = {"product": product, "period": period, "source_filters": parsed_source_filters}
    return await get_report(sector_key, report_key, force_refresh=refresh, filters=filters)
