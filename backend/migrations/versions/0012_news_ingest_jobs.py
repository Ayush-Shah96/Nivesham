"""Store NewsAlert ingestion job results for asynchronous refreshes.

Revision ID: 0012_news_ingest_jobs
Revises: 0011_dashboard_performance_indexes
Create Date: 2026-09-28
"""
from alembic import op
import sqlalchemy as sa


revision = "0012_news_ingest_jobs"
down_revision = "0011_dashboard_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processing_jobs",
        sa.Column("result", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("processing_jobs", "result")