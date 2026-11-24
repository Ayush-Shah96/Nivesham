"""Compatibility-only table definitions for immutable pre-cleanup Alembic revisions.

The active application models intentionally omit the retired legacy modules. Older
migration revisions, however, build the baseline schema from ``Base.metadata`` and
then add indexes to their tables. This helper attaches those historical definitions
only inside Alembic's migration process. The cleanup migration at revision 0030 drops
them, and the application itself never imports this helper.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Table,
    Text,
)


def attach_legacy_tables(metadata) -> None:
    """Add retired schema objects so unchanged historical migrations still replay."""
    users = metadata.tables.get("users")
    if users is not None and "telegram_user_id" not in users.c:
        users.append_column(Column("telegram_user_id", Integer, nullable=True, unique=True))

    if "emails" not in metadata.tables:
        Table(
            "emails",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("email_id", String(255), nullable=False, unique=True),
            Column("from_address", String(255), nullable=False),
            Column("to_address", String(255), nullable=False),
            Column("subject", String(1000), nullable=False),
            Column("body", Text, nullable=False),
            Column("received_at", DateTime(timezone=True), nullable=False),
            Column("attachments_count", Integer, nullable=False),
            Column("attachment_ids", JSON, nullable=False),
            Column("processing_status", String(50), nullable=False),
            Column("extracted_data", JSON, nullable=False),
        )

    if "financial_results" not in metadata.tables:
        # Reuse the historical approval enum type used by target_companies.
        approval_type = metadata.tables["target_companies"].c.approval_status.type
        financial_results = Table(
            "financial_results",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("company_id", Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            Column("period", String(30), nullable=False),
            Column("published_at", DateTime(timezone=True), nullable=False),
            Column("source_url", String(1000), nullable=False),
            Column("metrics", JSON, nullable=False),
            Column("status", approval_type, nullable=False),
            Column("approved_by", Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            Column("approved_at", DateTime(timezone=True), nullable=True),
            Column("rejection_reason", Text, nullable=True),
        )
        Index("ix_financial_results_status", financial_results.c.status)

    if "benpos_records" not in metadata.tables:
        benpos_records = Table(
            "benpos_records",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("email_id", Integer, ForeignKey("emails.id", ondelete="CASCADE"), nullable=False),
            Column("company_id", Integer, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
            Column("holder_name", String(255), nullable=True),
            Column("previous_quantity", Float, nullable=True),
            Column("current_quantity", Float, nullable=True),
            Column("change_quantity", Float, nullable=True),
            Column("source_uri", String(1000), nullable=True),
            Column("processing_status", String(50), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        Index("ix_benpos_records_email_id", benpos_records.c.email_id)
