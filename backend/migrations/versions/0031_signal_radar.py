"""Create Signal Radar source records and collection runs.

Revision ID: 0031_signal_radar
Revises: 0030_remove_benpos_financial_results
"""
from alembic import op
import sqlalchemy as sa

revision = "0031_signal_radar"
down_revision = "0030_remove_benpos"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "signal_radar_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(80), nullable=False),
        sa.Column("source_name", sa.String(160), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False, server_default="unclassified"),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("source_url", sa.String(1500), nullable=False),
        sa.Column("publication_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_name", sa.String(300), nullable=True),
        sa.Column("location", sa.String(300), nullable=True),
        sa.Column("estimated_investment", sa.String(120), nullable=True),
        sa.Column("estimated_capacity", sa.String(120), nullable=True),
        sa.Column("current_status", sa.String(60), nullable=False, server_default="needs_review"),
        sa.Column("potential_impact", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.25"),
        sa.Column("extraction_method", sa.String(60), nullable=False, server_default="public_html_link_discovery"),
        sa.Column("raw_metadata", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "source_url", name="uq_signal_radar_source_url"),
    )
    for name, cols in [("ix_signal_radar_records_source_id", ["source_id"]), ("ix_signal_radar_records_category", ["category"]), ("ix_signal_radar_records_event_type", ["event_type"]), ("ix_signal_radar_records_publication_date", ["publication_date"]), ("ix_signal_radar_records_company_name", ["company_name"]), ("ix_signal_radar_records_current_status", ["current_status"]), ("ix_signal_radar_records_content_hash", ["content_hash"]), ("ix_signal_radar_category_pub", ["category", "publication_date"])]:
        op.create_index(name, "signal_radar_records", cols)
    op.create_table(
        "signal_radar_source_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(80), nullable=False),
        sa.Column("source_name", sa.String(160), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("discovered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("diagnostics", sa.JSON(), nullable=False),
    )
    op.create_index("ix_signal_radar_source_runs_source_id", "signal_radar_source_runs", ["source_id"])
    op.create_index("ix_signal_radar_source_runs_status", "signal_radar_source_runs", ["status"])


def downgrade():
    op.drop_table("signal_radar_source_runs")
    op.drop_table("signal_radar_records")
