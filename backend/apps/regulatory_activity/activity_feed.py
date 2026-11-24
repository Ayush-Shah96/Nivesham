from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ii_platform.database.models import (
    Company,
    RegulatoryActivityEvent,
    RegulatoryCollectorRun,
)
from .collectors import collect_all_sources
from .normalization import normalized_key

logger = logging.getLogger(__name__)
INDIA_TZ = ZoneInfo("Asia/Kolkata")


def _company_indexes(companies: list[Company]):
    by_symbol: dict[str, Company] = {}
    by_isin: dict[str, Company] = {}
    by_bse: dict[str, Company] = {}
    by_name: dict[str, Company | None] = {}
    for company in companies:
        if company.nse_symbol:
            by_symbol[str(company.nse_symbol).upper()] = company
        if company.isin:
            by_isin[str(company.isin).upper()] = company
        if company.bse_code:
            by_bse[str(company.bse_code)] = company
        for name in [company.legal_name, *(company.aliases or [])]:
            key = normalized_key(name)
            if not key:
                continue
            if key in by_name and by_name[key] is not company:
                by_name[key] = None
            else:
                by_name[key] = company
    return by_symbol, by_isin, by_bse, by_name


def _match_company(event: dict[str, Any], indexes):
    by_symbol, by_isin, by_bse, by_name = indexes
    symbol = str(event.get("nse_symbol") or "").strip().upper()
    isin = str(event.get("isin") or "").strip().upper()
    bse_code = str(event.get("bse_code") or "").strip()
    if symbol and symbol in by_symbol:
        return by_symbol[symbol]
    if isin and isin in by_isin:
        return by_isin[isin]
    if bse_code and bse_code in by_bse:
        return by_bse[bse_code]
    company_name = normalized_key(event.get("company_name"))
    return by_name.get(company_name) if company_name else None


async def _persist_source_rows(
    db: AsyncSession,
    *,
    source: str,
    rows: list[dict[str, Any]],
    start_dt: datetime,
    end_dt: datetime,
) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    run = RegulatoryCollectorRun(
        source=source,
        window_start=start_dt,
        window_end=end_dt,
        started_at=now,
        status="RUNNING",
        records_received=len(rows),
    )
    db.add(run)
    await db.flush()

    companies = (await db.execute(select(Company))).scalars().all()
    company_indexes = _company_indexes(companies)

    # A single filing may repeat an identical table row or context. Collapse
    # duplicate source keys before insert to avoid violating the unique constraint.
    unique_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("source_key") or "").strip()
        if key:
            unique_rows[key] = row
    rows = list(unique_rows.values())
    source_keys = list(unique_rows)
    existing_rows: dict[str, RegulatoryActivityEvent] = {}
    # Keep the IN clauses bounded for databases with conservative parameter limits.
    for offset in range(0, len(source_keys), 400):
        chunk = source_keys[offset: offset + 400]
        matches = (await db.execute(
            select(RegulatoryActivityEvent).where(RegulatoryActivityEvent.source_key.in_(chunk))
        )).scalars().all()
        existing_rows.update({row.source_key: row for row in matches})

    inserted = updated = 0
    for raw in rows:
        source_key = str(raw.get("source_key") or "").strip()
        if not source_key:
            continue
        company = _match_company(raw, company_indexes)
        values = dict(raw)
        if company is not None:
            values["company_id"] = company.id
            # Prefer a canonical company master name when an exchange row only
            # supplied a symbol/code in its company-name field.
            if not values.get("company_name") or normalized_key(values["company_name"]) in {
                normalized_key(values.get("nse_symbol")), normalized_key(values.get("bse_code"))
            }:
                values["company_name"] = company.legal_name
            values["nse_symbol"] = values.get("nse_symbol") or company.nse_symbol
            values["bse_code"] = values.get("bse_code") or company.bse_code
            values["isin"] = values.get("isin") or company.isin
        values["last_seen_at"] = now
        existing = existing_rows.get(source_key)
        if existing is None:
            values.setdefault("first_seen_at", now)
            db.add(RegulatoryActivityEvent(**values))
            inserted += 1
        else:
            # Revisions update normalized facts and evidence without changing ID
            # or first_seen_at, making the source_key a stable event identity.
            for key, value in values.items():
                if key in {"id", "source_key", "first_seen_at"}:
                    continue
                setattr(existing, key, value)
            updated += 1

    run.records_inserted = inserted
    run.records_updated = updated
    run.status = "COMPLETED" if rows else "NO_DATA"
    run.finished_at = datetime.now(timezone.utc)
    run.duration_ms = max(0, int((run.finished_at - now).total_seconds() * 1000))
    await db.commit()
    return inserted, updated


async def _record_failed_run(
    db: AsyncSession,
    *,
    source: str,
    start_dt: datetime,
    end_dt: datetime,
    error: str,
):
    now = datetime.now(timezone.utc)
    db.add(RegulatoryCollectorRun(
        source=source,
        window_start=start_dt,
        window_end=end_dt,
        started_at=now,
        finished_at=now,
        status="FAILED",
        error_message=error[:10000],
    ))
    await db.commit()


async def sync_regulatory_activity(
    db: AsyncSession,
    *,
    history_days: int = 7,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    """Collect four Regulatory Desk categories and upsert the unified table.

    All collectors are independent and write only to regulatory_activity_events.
    Bulk and Block are one category with BULK/BLOCK subtype values.
    """
    today = datetime.now(INDIA_TZ).date()
    end = end_date or today
    days = max(1, min(int(history_days), 365))
    start = start_date or (end - timedelta(days=days - 1))
    if end < start:
        raise ValueError("end_date must be greater than or equal to start_date")
    start_dt = datetime.combine(start, time.min, tzinfo=INDIA_TZ)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=INDIA_TZ)

    source_results = await collect_all_sources(start, end)
    totals = {"inserted": 0, "updated": 0, "received": 0, "failed_sources": 0, "no_data_sources": 0}
    categories: dict[str, dict[str, Any]] = {}
    for source in (
        "INSIDER_TRADING",
        "BULK_DEAL",
        "QIP",
        "PREFERENTIAL_ISSUE",
    ):
        result = source_results.get(source, [])
        if isinstance(result, dict) and result.get("error"):
            totals["failed_sources"] += 1
            categories[source] = {"status": "failed", "error": result["error"]}
            await _record_failed_run(
                db,
                source=source,
                start_dt=start_dt,
                end_dt=end_dt,
                error=str(result["error"]),
            )
            continue
        rows = result if isinstance(result, list) else []
        try:
            inserted, updated = await _persist_source_rows(
                db,
                source=source,
                rows=rows,
                start_dt=start_dt,
                end_dt=end_dt,
            )
            totals["inserted"] += inserted
            totals["updated"] += updated
            totals["received"] += len(rows)
            if rows:
                category_status = "completed"
            else:
                category_status = "no_data"
                totals["no_data_sources"] += 1
                logger.warning(
                    "REGULATORY_SOURCE_NO_DATA source=%s window=%s..%s; check preceding source diagnostics before treating as a verified empty result",
                    source, start, end,
                )
            categories[source] = {
                "status": category_status,
                "received": len(rows),
                "inserted": inserted,
                "updated": updated,
            }
        except Exception as exc:
            await db.rollback()
            totals["failed_sources"] += 1
            categories[source] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:1000]}"}
            await _record_failed_run(
                db,
                source=source,
                start_dt=start_dt,
                end_dt=end_dt,
                error=categories[source]["error"],
            )
            logger.exception("REGULATORY_PERSIST_FAILED source=%s", source)

    status = "failed" if totals["failed_sources"] == len(categories) else "partial" if totals["failed_sources"] else "ok"
    result = {
        "status": status,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "inserted": totals["inserted"],
        "updated": totals["updated"],
        "received": totals["received"],
        "failed_sources": totals["failed_sources"],
        "no_data_sources": totals["no_data_sources"],
        "empty_categories": [key for key, value in categories.items() if value.get("status") == "no_data"],
        "nse": sum(1 for source_rows in source_results.values() if isinstance(source_rows, list) for row in source_rows if str(row.get("source_exchange") or "").upper() == "NSE"),
        "bse": sum(1 for source_rows in source_results.values() if isinstance(source_rows, list) for row in source_rows if str(row.get("source_exchange") or "").upper() == "BSE"),
        "categories": categories,
    }
    logger.info("REGULATORY_DESK_SYNC_RESULT %s", result)
    return result
