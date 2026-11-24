from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, String, select, delete
from sqlalchemy.orm import Mapped, mapped_column

from ii_platform.database.models import Base, RegulatorySyncError, utcnow


class RegulatorySyncState(Base):
    __tablename__ = "regulatory_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    desk: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="never_run")
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_full_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_full_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    history_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    to_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scanned: Mapped[int] = mapped_column(Integer, default=0)
    stored: Mapped[int] = mapped_column(Integer, default=0)
    complete: Mapped[bool] = mapped_column(default=False)
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


REGULATORY_DESK = "REGULATORY"
CAPITAL_ACTION_DESK = "CAPITAL_ACTIONS"
REGULATORY_ENGINE_VERSION = "nse-pit-capital-primary-table-six-day-v3"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_sync_state(db, desk: str) -> RegulatorySyncState | None:
    return await db.scalar(
        select(RegulatorySyncState).where(RegulatorySyncState.desk == desk)
    )


async def mark_running(
    db,
    desk: str,
    *,
    history_days: int,
    from_date: str | None = None,
    to_date: str | None = None,
) -> RegulatorySyncState:
    state = await get_sync_state(db, desk)
    if state is None:
        state = RegulatorySyncState(desk=desk)
        db.add(state)
    now = _now()
    state.status = "running"
    state.engine_version = REGULATORY_ENGINE_VERSION
    state.last_attempted_at = now
    if history_days >= 2557:
        state.last_full_attempt_at = now
    state.history_days = int(history_days)
    state.from_date = _as_datetime(from_date)
    state.to_date = _as_datetime(to_date, end_of_day=True)
    state.updated_at = now
    await db.flush()
    return state


async def mark_finished(
    db,
    desk: str,
    *,
    status: str,
    history_days: int,
    from_date: str | None,
    to_date: str | None,
    scanned: int,
    stored: int,
    complete: bool,
    errors: dict[str, Any] | None = None,
) -> RegulatorySyncState:
    state = await get_sync_state(db, desk)
    if state is None:
        state = RegulatorySyncState(desk=desk)
        db.add(state)
    now = _now()
    state.status = status
    state.engine_version = REGULATORY_ENGINE_VERSION
    state.history_days = int(history_days)
    state.from_date = _as_datetime(from_date)
    state.to_date = _as_datetime(to_date, end_of_day=True)
    state.scanned = int(scanned or 0)
    state.stored = int(stored or 0)
    state.complete = bool(complete)
    await db.execute(delete(RegulatorySyncError).where(RegulatorySyncError.desk == desk))
    for error_key, message in list((errors or {}).items())[:100]:
        db.add(RegulatorySyncError(desk=desk, error_key=str(error_key)[:255], message=str(message)[:4000]))
    if complete:
        state.last_success_at = now
        if history_days >= 2557:
            state.last_full_success_at = now
    state.updated_at = now
    await db.flush()
    return state


def _as_datetime(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed