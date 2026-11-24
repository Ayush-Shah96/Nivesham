from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, or_, select

from ii_platform.database.connection import get_db
from ii_platform.database.models import (
    ApprovalStatus,
    Company,
    EarningsCalendarEvent,
    TargetCompany,
    User,
)
from infrastructure.security.auth import get_current_user

router = APIRouter(prefix="/earnings-calendar", tags=["Earnings Calendar"])


@router.get("")
async def list_earnings_calendar(
    start_date: date | None = None,
    end_date: date | None = None,
    event_type: Literal["Results", "Concall"] | None = None,
    analyst: str | None = None,
    search: str | None = None,
    db=Depends(get_db),
    _: User = Depends(get_current_user),
):
    start_date = start_date or (date.today().replace(day=1) - timedelta(days=30))
    end_date = end_date or (date.today() + timedelta(days=120))
    if end_date < start_date:
        return {"events": [], "count": 0, "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
    stmt = (
        select(EarningsCalendarEvent, Company, TargetCompany, User)
        .join(Company, Company.id == EarningsCalendarEvent.company_id)
        .join(TargetCompany, TargetCompany.company_id == Company.id)
        .outerjoin(User, User.id == TargetCompany.analyst_id)
        .where(
            TargetCompany.active.is_(True),
            TargetCompany.approval_status == ApprovalStatus.APPROVED,
            or_(
                EarningsCalendarEvent.event_date.is_(None),
                and_(EarningsCalendarEvent.event_date >= start_date, EarningsCalendarEvent.event_date <= end_date),
            ),
        )
    )
    if event_type:
        stmt = stmt.where(EarningsCalendarEvent.event_type == event_type)
    if analyst:
        stmt = stmt.where(User.username.ilike(analyst))
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where((Company.legal_name.ilike(term)) | (EarningsCalendarEvent.event_title.ilike(term)))
    stmt = stmt.order_by(EarningsCalendarEvent.event_date.asc().nulls_last(), Company.legal_name.asc(), EarningsCalendarEvent.event_type.asc())
    rows = (await db.execute(stmt)).all()
    events = []
    seen = set()
    for event, company, target, assigned_analyst in rows:
        # A company should only be in one active approved Coverage List row, but deduplicate defensively.
        if event.event_key in seen:
            continue
        seen.add(event.event_key)
        events.append({
            "id": event.id,
            "event_key": event.event_key,
            "date": event.event_date.isoformat() if event.event_date else None,
            "pending": event.event_date is None,
            "pending_label": event.pending_label or ("Date pending" if event.event_date is None else None),
            "company_id": company.id,
            "company": company.legal_name,
            "symbol": company.nse_symbol,
            "analyst": assigned_analyst.username if assigned_analyst else "Unassigned",
            "analyst_id": assigned_analyst.id if assigned_analyst else None,
            "track_mode": target.track_mode.value if target.track_mode else None,
            "type": event.event_type,
            "time": event.call_time or "",
            "title": event.event_title or company.legal_name,
            "source": event.source,
            "url": event.source_url,
        })
    return {"events": events, "count": len(events), "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "scope": "active_approved_coverage_list"}
