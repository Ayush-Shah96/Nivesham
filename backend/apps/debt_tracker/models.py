from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ii_platform.database.models import Base, utcnow


class DebtTrackerJob(Base):
    __tablename__ = "debt_tracker_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="processing", index=True)
    input_files: Mapped[dict] = mapped_column(JSON, default=dict)
    report_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DebtTrackerMapping(Base):
    """Persistent, explicitly approved fund and security/group aliases."""

    __tablename__ = "debt_tracker_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_type: Mapped[str] = mapped_column(String(32), index=True)
    source_key: Mapped[str] = mapped_column(String(512))
    source_normalized: Mapped[str] = mapped_column(String(512))
    target_key: Mapped[str] = mapped_column(String(512))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("mapping_type", "source_normalized", name="uq_debt_tracker_mapping_source"),
        Index("ix_debt_tracker_mapping_type_target", "mapping_type", "target_key"),
    )


class DebtTrackerMappingHistory(Base):
    __tablename__ = "debt_tracker_mapping_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_type: Mapped[str] = mapped_column(String(32), index=True)
    source_key: Mapped[str] = mapped_column(String(512))
    previous_target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    new_target: Mapped[str] = mapped_column(String(512))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
