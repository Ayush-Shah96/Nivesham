from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ii_platform.database.models import Base, utcnow


# ---------------------------------------------------------------------------
# Legacy snapshot/account tables
# ---------------------------------------------------------------------------
# These classes are intentionally retained because migration 0020 created them
# and an existing deployment may still contain historical data. The adaptive
# PMS workflow below does not depend on broker/account assignment.


class PortfolioBroker(Base):
    __tablename__ = "portfolio_comparison_brokers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    parser_key: Mapped[str] = mapped_column(String(80), default="generic")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortfolioAccount(Base):
    __tablename__ = "portfolio_comparison_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_id: Mapped[int] = mapped_column(ForeignKey("portfolio_comparison_brokers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("broker_id", "name", name="uq_portfolio_comparison_account"),
        Index("ix_portfolio_comparison_accounts_broker", "broker_id"),
    )


class PortfolioUploadBatch(Base):
    __tablename__ = "portfolio_comparison_upload_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    comparison_month: Mapped[str] = mapped_column(String(7), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    parsed_rows: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_count: Mapped[int] = mapped_column(Integer, default=0)
    report_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Adaptive workflow fields created by consolidated migration 0020.
    archive_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archive_object_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    upload_type: Mapped[str] = mapped_column(String(30), default="monthly", server_default="monthly")
    excluded_pms: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    pms_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    active_pms_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    inactive_pms_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class PortfolioSourceFile(Base):
    __tablename__ = "portfolio_comparison_source_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("portfolio_comparison_upload_batches.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    broker_name: Mapped[str] = mapped_column(String(160))
    account_name: Mapped[str] = mapped_column(String(200))
    object_key: Mapped[str] = mapped_column(String(1000))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    parsed_rows: Mapped[int] = mapped_column(Integer, default=0)
    parser_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_comparison_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("portfolio_comparison_accounts.id", ondelete="CASCADE"), index=True)
    source_batch_id: Mapped[str] = mapped_column(ForeignKey("portfolio_comparison_upload_batches.id", ondelete="CASCADE"), index=True)
    snapshot_month: Mapped[str] = mapped_column(String(7), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", index=True)
    total_investment_value: Mapped[float] = mapped_column(Float, default=0.0)
    total_current_value: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("account_id", "snapshot_month", "version", name="uq_portfolio_snapshot_version"),
        Index("ix_portfolio_snapshot_current_month", "snapshot_month", "is_current"),
        Index("ix_portfolio_snapshot_account_month", "account_id", "snapshot_month", "is_current"),
    )


class PortfolioHolding(Base):
    __tablename__ = "portfolio_comparison_holdings"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("portfolio_comparison_snapshots.id", ondelete="CASCADE"), index=True)
    isin: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    security_name: Mapped[str] = mapped_column(String(255))
    normalized_security_name: Mapped[str] = mapped_column(String(255), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    average_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    investment_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sector: Mapped[str] = mapped_column(String(160), default="Unmapped")
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    identity_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("portfolio_comparison_source_files.id", ondelete="SET NULL"), nullable=True)
    source_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_row: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_portfolio_holding_snapshot_isin", "snapshot_id", "isin"),
        Index("ix_portfolio_holding_snapshot_name", "snapshot_id", "normalized_security_name"),
    )


# ---------------------------------------------------------------------------
# Adaptive PMS registry / monthly file store
# ---------------------------------------------------------------------------


class PMSRegistry(Base):
    """Learned identity and expected file format for each PMS."""

    __tablename__ = "portfolio_comparison_pms_registry"

    id: Mapped[int] = mapped_column(primary_key=True)
    pms_name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    normalized_name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    format: Mapped[str] = mapped_column(String(20))  # EXCEL / PDF
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    first_seen_month: Mapped[str] = mapped_column(String(7))
    last_seen_month: Mapped[str | None] = mapped_column(String(7), nullable=True, index=True)
    last_processed_month: Mapped[str | None] = mapped_column(String(7), nullable=True, index=True)
    last_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parser_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_portfolio_pms_registry_status", "status"),
        Index("ix_portfolio_pms_registry_last_seen", "last_seen_month"),
    )


class PMSMonthlyFile(Base):
    """One accepted source file for a PMS/month, versioned for safe re-runs."""

    __tablename__ = "portfolio_comparison_pms_monthly_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    pms_id: Mapped[int] = mapped_column(ForeignKey("portfolio_comparison_pms_registry.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("portfolio_comparison_upload_batches.id", ondelete="CASCADE"), index=True)
    comparison_month: Mapped[str] = mapped_column(String(7), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    filename_key: Mapped[str] = mapped_column(String(255), index=True)
    format: Mapped[str] = mapped_column(String(20))
    object_key: Mapped[str] = mapped_column(String(1000))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    parser_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parsed_rows: Mapped[int] = mapped_column(Integer, default=0)
    previous_file_id: Mapped[int | None] = mapped_column(ForeignKey("portfolio_comparison_pms_monthly_files.id", ondelete="SET NULL"), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (
        UniqueConstraint("pms_id", "comparison_month", "version", name="uq_portfolio_pms_monthly_file_version"),
        Index("ix_portfolio_pms_monthly_file_current", "pms_id", "comparison_month", "is_current"),
        Index("ix_portfolio_pms_monthly_file_filename_key", "filename_key", "comparison_month"),
    )


class PMSMonthlyHolding(Base):
    """Normalized holdings belonging to an individual PMS/monthly source file."""

    __tablename__ = "portfolio_comparison_pms_monthly_holdings"

    id: Mapped[int] = mapped_column(primary_key=True)
    monthly_file_id: Mapped[int] = mapped_column(ForeignKey("portfolio_comparison_pms_monthly_files.id", ondelete="CASCADE"), index=True)
    isin: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    security_name: Mapped[str] = mapped_column(String(255))
    normalized_security_name: Mapped[str] = mapped_column(String(255), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    measurement_type: Mapped[str] = mapped_column(String(30), default="QUANTITY", server_default="QUANTITY")
    average_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    investment_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sector: Mapped[str] = mapped_column(String(160), default="Unmapped")
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    identity_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_reference: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_row: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_portfolio_pms_monthly_holding_file_isin", "monthly_file_id", "isin"),
        Index("ix_portfolio_pms_monthly_holding_file_name", "monthly_file_id", "normalized_security_name"),
    )
