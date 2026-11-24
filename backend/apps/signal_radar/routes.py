from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from infrastructure.security.auth import get_current_user
from ii_platform.database.connection import get_db
from ii_platform.database.models import User, UserRole
from .models import SignalRadarRecord, SignalRadarSourceRun
from .collectors.source_registry import SOURCES
from .collector_service import run_signal_radar_collection

router = APIRouter(prefix="/signal-radar", tags=["Signal Radar"])


def _record(row: SignalRadarRecord):
    return {"id": row.id, "source_id": row.source_id, "source_name": row.source_name, "category": row.category, "event_type": row.event_type, "title": row.title, "summary": row.summary, "source_url": row.source_url, "publication_date": row.publication_date, "retrieved_at": row.retrieved_at, "company_name": row.company_name, "location": row.location, "estimated_investment": row.estimated_investment, "estimated_capacity": row.estimated_capacity, "current_status": row.current_status, "potential_impact": row.potential_impact, "confidence": row.confidence, "extraction_method": row.extraction_method, "raw_metadata": row.raw_metadata}


@router.get("/sources")
async def get_sources(user: User = Depends(get_current_user)):
    return [{**{k: v for k, v in s.items() if k != "keywords"}, "collector_enabled": bool(s["enabled"])} for s in SOURCES]


@router.get("/signals")
async def get_signals(category: str | None = None, source_id: str | None = None, status: str | None = None, q: str | None = Query(default=None, max_length=200), limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0), db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(SignalRadarRecord)
    if category: stmt = stmt.where(SignalRadarRecord.category == category)
    if source_id: stmt = stmt.where(SignalRadarRecord.source_id == source_id)
    if status: stmt = stmt.where(SignalRadarRecord.current_status == status)
    if q: stmt = stmt.where(SignalRadarRecord.title.ilike(f"%{q}%") | SignalRadarRecord.summary.ilike(f"%{q}%"))
    rows = (await db.scalars(stmt.order_by(desc(SignalRadarRecord.retrieved_at)).offset(offset).limit(limit))).all()
    return {"items": [_record(r) for r in rows], "limit": limit, "offset": offset}


@router.get("/runs")
async def get_runs(limit: int = Query(default=100, ge=1, le=500), db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.scalars(select(SignalRadarSourceRun).order_by(desc(SignalRadarSourceRun.started_at)).limit(limit))).all()
    return [{"id": r.id, "source_id": r.source_id, "source_name": r.source_name, "started_at": r.started_at, "completed_at": r.completed_at, "status": r.status, "http_status": r.http_status, "discovered_count": r.discovered_count, "new_count": r.new_count, "error_message": r.error_message, "diagnostics": r.diagnostics} for r in rows]


@router.post("/collect")
async def collect_now(source_id: str | None = None, user: User = Depends(get_current_user)):
    if getattr(user, "role", None) not in (UserRole.ADMIN, "ADMIN"):
        raise HTTPException(status_code=403, detail="Administrator access required to trigger source collection")
    return await run_signal_radar_collection([source_id] if source_id else None)
