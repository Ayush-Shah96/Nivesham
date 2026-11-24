"""Remove retired BENPOS and Financial Results tables.

Revision ID: 0030_remove_benpos_financial_results
Revises: 0029_folio_broker_mapping
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0030_remove_benpos"
down_revision = "0029_folio_broker_mapping"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _table_names()
    # Drop only the two tables explicitly authorized by the conversion brief.
    # Keep existing email records and Telegram identifiers untouched: they are
    # existing database data, even though their retired runtime integrations are
    # no longer exposed by the application.
    if "benpos_records" in existing:
        op.drop_table("benpos_records")
    if "financial_results" in existing:
        op.drop_table("financial_results")


def downgrade() -> None:
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import ENUM
        approval_status_type = ENUM(
            "PENDING_AASHIL_APPROVAL",
            "PENDING_APPROVAL",
            "APPROVED",
            "REJECTED",
            name="approvalstatus",
            create_type=False,
        )
    else:
        approval_status_type = sa.Enum(
            "PENDING_AASHIL_APPROVAL",
            "PENDING_APPROVAL",
            "APPROVED",
            "REJECTED",
            name="approvalstatus",
            native_enum=False,
        )

    op.create_table(
        "financial_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period", sa.String(length=30), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("status", approval_status_type, nullable=False),
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
    )
    op.create_index("ix_financial_results_status", "financial_results", ["status"], unique=False)
    op.create_index("ix_financial_results_published_id", "financial_results", ["published_at", "id"], unique=False)
    op.create_index(
        "ix_financial_results_company_published_id",
        "financial_results",
        ["company_id", "published_at", "id"],
        unique=False,
    )

    op.create_table(
        "benpos_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email_id", sa.Integer(), sa.ForeignKey("emails.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("holder_name", sa.String(length=255), nullable=True),
        sa.Column("previous_quantity", sa.Float(), nullable=True),
        sa.Column("current_quantity", sa.Float(), nullable=True),
        sa.Column("change_quantity", sa.Float(), nullable=True),
        sa.Column("source_uri", sa.String(length=1000), nullable=True),
        sa.Column("processing_status", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_benpos_records_email_id", "benpos_records", ["email_id"], unique=False)
    op.create_index("ix_benpos_records_created_id", "benpos_records", ["created_at", "id"], unique=False)
