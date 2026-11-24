from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select

from apps.regulatory_activity.normalization import norm
from apps.regulatory_activity.service import sync_regulatory_activity
from infrastructure.security.permissions import require_role
from ii_platform.database.connection import get_db
from ii_platform.database.models import (
    Company, RegulatoryActivityEvent, RegulatoryCollectorRun, TargetCompany, User, UserRole,
)

router = APIRouter(prefix="/regulatory-activity", tags=["Regulatory Activity"])

_NSE_TIMEZONE = ZoneInfo("Asia/Kolkata")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _stock_quote_url(symbol: Any) -> str | None:
    clean = "".join(str(symbol or "").split()).upper()
    return f"https://www.nseindia.com/get-quotes/equity?{urlencode({'symbol': clean})}" if clean else None


def _record_payload(row: RegulatoryActivityEvent) -> dict[str, Any]:
    quantity = float(row.quantity) if row.quantity is not None else None
    price = float(row.price) if row.price is not None else None
    value = float(row.value_inr) if row.value_inr is not None else None
    # Some exchange disclosures publish only two of quantity, price and value.
    # Derive the third for display without changing the source record in storage.
    if price is None and quantity is not None and quantity > 0 and value is not None:
        price = value / quantity
    if value is None and quantity is not None and quantity > 0 and price is not None:
        value = quantity * price
    return {
        "id": row.id,
        "company_id": row.company_id,
        "date": _as_utc(row.event_date),
        "disclosure_date": _as_utc(row.disclosure_date),
        "company": row.company_name,
        "symbol": row.symbol,
        "isin": row.isin,
        "activity_type": row.activity_type,
        "activity_sub_type": row.activity_sub_type,
        "person_allottee_client": row.person_allottee_client,
        "action": row.action,
        "quantity": quantity,
        "price": price,
        "value": value,
        "holding_before": row.holding_before,
        "holding_after": row.holding_after,
        "current_market_price": row.current_market_price,
        "market_price_change_percent": row.market_price_change_percent,
        "status": row.status,
        "source_exchange": row.source_exchange,
        "source_url": row.source_url,
        "stock_url": _stock_quote_url(row.symbol),
        "coverage_mode": "OTHERS",
        "analyst": None,
    }


def _company_key(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


async def _coverage_lookup(db) -> dict[str, dict[Any, dict[str, Any]]]:
    """Build event-to-Coverage-List metadata from the shared company master."""
    result = await db.execute(
        select(Company, TargetCompany, User)
        .outerjoin(TargetCompany, TargetCompany.company_id == Company.id)
        .outerjoin(User, User.id == TargetCompany.analyst_id)
    )
    indexes: dict[str, dict[Any, dict[str, Any]]] = {
        "id": {}, "symbol": {}, "isin": {}, "name": {},
    }
    for company, target, analyst in result.all():
        mode = str(getattr(target.track_mode, "value", target.track_mode) or "OTHERS").upper() if target else "OTHERS"
        if mode not in {"PO", "HT", "LT"}:
            mode = "OTHERS"
        payload = {
            "coverage_mode": mode,
            "analyst": analyst.username if analyst else None,
        }
        indexes["id"][company.id] = payload
        if company.nse_symbol:
            indexes["symbol"][_company_key(company.nse_symbol)] = payload
        if company.isin:
            indexes["isin"][_company_key(company.isin)] = payload
        if company.legal_name:
            indexes["name"][_company_key(company.legal_name)] = payload
        for alias in company.aliases or []:
            if alias:
                indexes["name"].setdefault(_company_key(alias), payload)
    return indexes


def _apply_coverage_metadata(rows: list[dict[str, Any]], indexes: dict[str, dict[Any, dict[str, Any]]]) -> None:
    for row in rows:
        metadata = (
            indexes["id"].get(row.get("company_id"))
            or indexes["symbol"].get(_company_key(row.get("symbol")))
            or indexes["isin"].get(_company_key(row.get("isin")))
            or indexes["name"].get(_company_key(row.get("company")))
        )
        if metadata:
            row.update(metadata)


def _transaction_group_key(row: RegulatoryActivityEvent) -> tuple[Any, ...] | None:
    """Group only identifiable same-day trades; leave partial/unknown trades separate."""
    activity = str(row.activity_type or "").upper()
    if activity not in {"INSIDER_TRADING", "BULK_DEAL"}:
        return None
    if str(row.status or "").upper() == "FAILED":
        return None
    metadata = row.source_metadata if isinstance(row.source_metadata, dict) else {}
    # A filing-date fallback is not a reliable trade date. Keep those partial
    # rows separate instead of accidentally combining distinct transactions.
    if activity == "INSIDER_TRADING" and metadata.get("event_date_basis") == "filing_date_fallback":
        return None
    person = norm(row.person_allottee_client)
    action = norm(row.action)
    if not person or not action or row.event_date is None:
        return None

    company_identity: tuple[str, str]
    if row.company_id is not None:
        company_identity = ("company_id", str(row.company_id))
    elif row.symbol:
        company_identity = ("symbol", norm(row.symbol))
    elif row.isin:
        company_identity = ("isin", norm(row.isin))
    else:
        company_identity = ("company", norm(row.company_name))

    event_date = _as_utc(row.event_date)
    assert event_date is not None
    trade_day = event_date.astimezone(_NSE_TIMEZONE).date().isoformat()
    # Bulk and Block are distinct markets; never combine them. The action is
    # also part of the key, so a Buy never offsets or merges with a Sell.
    return (activity, str(row.activity_sub_type or "").upper(), company_identity, trade_day, person, action)


def _aggregate_transaction_group(rows: list[RegulatoryActivityEvent]) -> dict[str, Any]:
    """Build one dashboard row while preserving each source transaction as evidence.

    Raw transaction records remain unchanged in PostgreSQL. This is a read-side
    projection, so existing historical rows are grouped immediately without a
    destructive data migration or loss of filing-level provenance.
    """
    ordered = sorted(
        rows,
        key=lambda item: (
            _as_utc(item.disclosure_date) or _as_utc(item.event_date) or datetime.min.replace(tzinfo=timezone.utc),
            int(item.id or 0),
        ),
    )
    first = ordered[0]
    quantity_total = 0.0
    quantity_seen = False
    value_total = 0.0
    value_seen = False
    weighted_value = 0.0
    weighted_quantity = 0.0
    weighted_price_complete = True

    transaction_evidence: list[dict[str, Any]] = []

    for item in ordered:
        quantity = float(item.quantity) if item.quantity is not None else None
        price = float(item.price) if item.price is not None else None
        value = float(item.value_inr) if item.value_inr is not None else None
        if value is None and quantity is not None and price is not None:
            value = quantity * price

        if quantity is not None:
            quantity_total += quantity
            quantity_seen = True
        if value is not None:
            value_total += value
            value_seen = True
        if quantity is not None and quantity > 0 and value is not None:
            weighted_value += value
            weighted_quantity += quantity
        else:
            weighted_price_complete = False

        if price is None and quantity is not None and quantity > 0 and value is not None:
            price = value / quantity
        source_url = item.source_url
        # Expose only normalized transaction fields to the grouped-row UI.
        # Raw HTML/XBRL/API JSON remains stored for audit/debugging but is not
        # serialized into the dashboard's expandable transaction list.
        transaction_evidence.append({
            "record_id": item.id,
            "date": item.event_date,
            "disclosure_date": item.disclosure_date,
            "person": item.person_allottee_client,
            "action": item.action,
            "quantity": quantity,
            "price": price,
            "value": value,
            "holding_before": item.holding_before,
            "holding_after": item.holding_after,
            "status": item.status,
            "source_url": source_url,
        })

    price_average = (
        weighted_value / weighted_quantity
        if weighted_price_complete and weighted_quantity > 0
        else None
    )
    status_values = {str(item.status or "PARTIAL").upper() for item in ordered}
    status = "DETAILED" if status_values == {"DETAILED"} else "LEGACY" if status_values == {"LEGACY"} else "PARTIAL"
    before_values = [item.holding_before for item in ordered if item.holding_before is not None]
    after_values = [item.holding_after for item in ordered if item.holding_after is not None]
    disclosures = [item.disclosure_date for item in ordered if item.disclosure_date is not None]
    quote_rows = [item for item in ordered if item.current_market_price is not None]
    quote_row = quote_rows[-1] if quote_rows else first

    first_event_utc = _as_utc(first.event_date)
    return {
        "id": min(int(item.id or 0) for item in ordered),
        "company_id": first.company_id,
        "date": first_event_utc,
        "disclosure_date": _as_utc(max(disclosures, key=lambda value: _as_utc(value))) if disclosures else None,
        "company": first.company_name,
        "symbol": first.symbol,
        "isin": first.isin,
        "activity_type": first.activity_type,
        "activity_sub_type": first.activity_sub_type,
        "person_allottee_client": first.person_allottee_client,
        "action": first.action,
        "quantity": quantity_total if quantity_seen else None,
        "price": price_average,
        "value": value_total if value_seen else None,
        # Holding values describe the person's position, not transaction flow.
        # Preserve first/last observations rather than summing them.
        "holding_before": before_values[0] if before_values else None,
        "holding_after": after_values[-1] if after_values else None,
        "current_market_price": quote_row.current_market_price,
        "market_price_change_percent": quote_row.market_price_change_percent,
        "status": status,
        "source_exchange": first.source_exchange,
        "source_url": next((item.source_url for item in reversed(ordered) if item.source_url), None),
        "stock_url": _stock_quote_url(first.symbol),
        "coverage_mode": "OTHERS",
        "analyst": None,
        "transaction_count": len(ordered),
        # The UI receives normalized underlying transactions only. Raw NSE
        # filing facts, scraped JSON, parser diagnostics, and aggregation
        # metadata remain stored server-side and are not exposed as dropdown
        # content.
        "details": {"transactions": transaction_evidence},
    }


def _display_rows(rows: list[RegulatoryActivityEvent]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[RegulatoryActivityEvent]] = {}
    output: list[dict[str, Any]] = []
    for row in rows:
        key = _transaction_group_key(row)
        if key is None:
            output.append(_record_payload(row))
        else:
            grouped.setdefault(key, []).append(row)
    for members in grouped.values():
        if len(members) > 1:
            output.append(_aggregate_transaction_group(members))
        else:
            output.append(_record_payload(members[0]))
    return output


def _sort_display_rows(rows: list[dict[str, Any]], sort_by: str, sort_dir: str) -> list[dict[str, Any]]:
    field = {
        "date": "date", "company": "company", "quantity": "quantity",
        "price": "price", "value": "value", "cmp": "current_market_price",
        "cmp_change": "market_price_change_percent", "coverage": "coverage_mode",
        "analyst": "analyst", "action": "action", "activity": "activity_type",
    }.get(sort_by, "date")

    def sortable(value: Any) -> Any:
        if isinstance(value, datetime):
            normalized = _as_utc(value)
            return normalized.timestamp() if normalized else None
        if isinstance(value, str):
            return value.casefold()
        return value

    present = [row for row in rows if row.get(field) is not None]
    missing = [row for row in rows if row.get(field) is None]
    present.sort(key=lambda row: sortable(row.get(field)), reverse=sort_dir.lower() != "asc")
    return present + missing


def _in_range(value: Any, minimum: float | None, maximum: float | None) -> bool:
    if minimum is None and maximum is None:
        return True
    if value is None:
        return False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return (minimum is None or parsed >= minimum) and (maximum is None or parsed <= maximum)


def _filter_display_rows(
    rows: list[dict[str, Any]], *, activity_types: list[str], actions: list[str],
    statuses: list[str], coverage_modes: list[str], analysts: list[str], person: str,
    quantity_min: float | None, quantity_max: float | None,
    cmp_min: float | None, cmp_max: float | None,
    value_min_crore: float | None, value_max_crore: float | None,
    price_min: float | None, price_max: float | None,
    change_min: float | None, change_max: float | None,
    holding_min: float | None, holding_max: float | None,
) -> list[dict[str, Any]]:
    activities = {value.upper() for value in activity_types if value}
    action_set = {value.upper() for value in actions if value}
    status_set = {value.upper() for value in statuses if value}
    coverage_set = {value.upper() for value in coverage_modes if value}
    analyst_set = {_company_key(value) for value in analysts if value}
    person_needle = _company_key(person)
    filtered = []
    for row in rows:
        if activities and str(row.get("activity_type") or "").upper() not in activities:
            continue
        if action_set and str(row.get("action") or "").upper() not in action_set:
            continue
        if status_set and str(row.get("status") or "PARTIAL").upper() not in status_set:
            continue
        if coverage_set and str(row.get("coverage_mode") or "OTHERS").upper() not in coverage_set:
            continue
        if analyst_set and _company_key(row.get("analyst") or "Unassigned") not in analyst_set:
            continue
        if person_needle and person_needle not in _company_key(row.get("person_allottee_client")):
            continue
        if not _in_range(row.get("quantity"), quantity_min, quantity_max):
            continue
        if not _in_range(row.get("current_market_price"), cmp_min, cmp_max):
            continue
        # RegulatoryActivityEvent.value is stored as INR; filter input is Crores.
        value_crore = (float(row["value"]) / 10_000_000) if row.get("value") is not None else None
        if not _in_range(value_crore, value_min_crore, value_max_crore):
            continue
        if not _in_range(row.get("price"), price_min, price_max):
            continue
        if not _in_range(row.get("market_price_change_percent"), change_min, change_max):
            continue
        if not _in_range(row.get("holding_before"), holding_min, holding_max):
            continue
        filtered.append(row)
    return filtered


def _parse_multi(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


@router.get("")
async def list_regulatory_activity(
    activity_type: str | None = None,
    activity_types: str | None = None,
    activity_sub_type: str | None = None,
    company: str | None = None,
    person: str | None = None,
    action: str | None = None,
    actions: str | None = None,
    status: str | None = None,
    statuses: str | None = None,
    coverage_modes: str | None = None,
    analysts: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    quantity_min: float | None = None,
    quantity_max: float | None = None,
    cmp_min: float | None = None,
    cmp_max: float | None = None,
    value_min_crore: float | None = None,
    value_max_crore: float | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    change_min: float | None = None,
    change_max: float | None = None,
    holding_min: float | None = None,
    holding_max: float | None = None,
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 100,
    offset: int = 0,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    stmt = select(RegulatoryActivityEvent)
    selected_types = _parse_multi(activity_types)
    if activity_type:
        selected_types = selected_types or [activity_type]
    if selected_types:
        stmt = stmt.where(RegulatoryActivityEvent.activity_type.in_([value.upper() for value in selected_types]))
    if activity_sub_type:
        stmt = stmt.where(RegulatoryActivityEvent.activity_sub_type == activity_sub_type.strip().upper())
    if company:
        needle = f"%{company.strip()}%"
        stmt = stmt.where(or_(
            RegulatoryActivityEvent.company_name.ilike(needle),
            func.coalesce(RegulatoryActivityEvent.symbol, "").ilike(needle),
        ))
    if from_date:
        stmt = stmt.where(RegulatoryActivityEvent.event_date >= datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc))
    if to_date:
        stmt = stmt.where(RegulatoryActivityEvent.event_date < datetime.combine(to_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))

    records = (await db.execute(
        stmt.order_by(RegulatoryActivityEvent.event_date.desc(), RegulatoryActivityEvent.id.desc())
    )).scalars().all()
    display_rows = _display_rows(records)
    _apply_coverage_metadata(display_rows, await _coverage_lookup(db))
    selected_actions = _parse_multi(actions)
    if action:
        selected_actions = selected_actions or [action]
    display_rows = _filter_display_rows(
        display_rows,
        activity_types=[], actions=selected_actions,
        statuses=_parse_multi(statuses) or ([status] if status else []),
        coverage_modes=_parse_multi(coverage_modes), analysts=_parse_multi(analysts),
        person=person or "",
        quantity_min=quantity_min, quantity_max=quantity_max,
        cmp_min=cmp_min, cmp_max=cmp_max,
        value_min_crore=value_min_crore, value_max_crore=value_max_crore,
        price_min=price_min, price_max=price_max,
        change_min=change_min, change_max=change_max,
        holding_min=holding_min, holding_max=holding_max,
    )
    display_rows = _sort_display_rows(display_rows, sort_by, sort_dir)
    page_limit = min(max(int(limit), 1), 250)
    page_offset = max(0, int(offset))
    return display_rows[page_offset:page_offset + page_limit]


@router.get("/filter-options")
async def regulatory_activity_filter_options(
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    records = (await db.execute(select(RegulatoryActivityEvent))).scalars().all()
    rows = _display_rows(records)
    _apply_coverage_metadata(rows, await _coverage_lookup(db))

    def values(field: str, fallback: str | None = None) -> list[str]:
        found = {str(row.get(field) or fallback or "").strip() for row in rows}
        return sorted((value for value in found if value), key=str.casefold)

    return {
        "activities": values("activity_type"),
        "actions": values("action"),
        "statuses": values("status", "PARTIAL"),
        "coverage_modes": [mode for mode in ("PO", "HT", "LT", "OTHERS") if mode in set(values("coverage_mode", "OTHERS"))],
        "analysts": values("analyst", "Unassigned"),
    }


@router.get("/summary")
async def regulatory_activity_summary(
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST, UserRole.INVESTMENT)),
):
    counts = (await db.execute(
        select(RegulatoryActivityEvent.activity_type, func.count(RegulatoryActivityEvent.id))
        .group_by(RegulatoryActivityEvent.activity_type)
    )).all()
    latest_runs = (await db.execute(
        select(RegulatoryCollectorRun).where(RegulatoryCollectorRun.source.in_(
            ["NSE_INSIDER", "NSE_BULK_BLOCK", "NSE_QIP", "NSE_PREF_WARRANT"]
        )).order_by(RegulatoryCollectorRun.created_at.desc()).limit(20)
    )).scalars().all()
    by_type = {str(kind): int(count) for kind, count in counts}
    by_source: dict[str, dict[str, Any]] = {}
    for run in latest_runs:
        by_source.setdefault(run.source, {
            "status": run.status, "last_run": run.finished_at or run.started_at,
            "received": run.records_received, "inserted": run.records_inserted,
            "updated": run.records_updated, "error": run.error_message,
        })
    return {"counts": by_type, "sources": by_source}


@router.post("/sync")
async def sync_now(
    days: int = 7,
    db=Depends(get_db),
    _: User = Depends(require_role(UserRole.ADMIN, UserRole.ANALYST)),
):
    try:
        return await sync_regulatory_activity(db, days=max(1, min(int(days), 2557)))
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=f"Regulatory collection failed: {type(exc).__name__}: {str(exc)[:500]}") from exc
