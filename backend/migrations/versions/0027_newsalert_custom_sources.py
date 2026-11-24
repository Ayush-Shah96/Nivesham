"""Persist user-managed NewsAlert source websites, YouTube channels and X handles.

Revision ID: 0027_newsalert_custom_sources
Revises: 0026_earnings_calendar
"""
from alembic import op
import sqlalchemy as sa

revision = "0027_newsalert_custom_sources"
down_revision = "0026_earnings_calendar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "news_alert_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("value", sa.String(length=1500), nullable=False),
        sa.Column("normalized_value", sa.String(length=1500), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_type", "normalized_value", name="uq_news_alert_source_type_value"),
    )
    op.create_index("ix_news_alert_sources_source_type", "news_alert_sources", ["source_type"])
    op.create_index("ix_news_alert_sources_enabled", "news_alert_sources", ["enabled"])
    op.create_index("ix_news_alert_sources_type_enabled", "news_alert_sources", ["source_type", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_news_alert_sources_type_enabled", table_name="news_alert_sources")
    op.drop_index("ix_news_alert_sources_enabled", table_name="news_alert_sources")
    op.drop_index("ix_news_alert_sources_source_type", table_name="news_alert_sources")
    op.drop_table("news_alert_sources")
