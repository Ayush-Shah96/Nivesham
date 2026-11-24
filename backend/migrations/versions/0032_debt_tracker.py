"""Create Debt Tracker jobs and persistent approved mappings.

Revision ID: 0032_debt_tracker
Revises: 0031_signal_radar
"""
from alembic import op
import sqlalchemy as sa

revision = "0032_debt_tracker"
down_revision = "0031_signal_radar"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "debt_tracker_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="processing"),
        sa.Column("input_files", sa.JSON(), nullable=False),
        sa.Column("report_data", sa.JSON(), nullable=True),
        sa.Column("output_key", sa.String(length=1000), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_debt_tracker_jobs_requested_by", "debt_tracker_jobs", ["requested_by"])
    op.create_index("ix_debt_tracker_jobs_status", "debt_tracker_jobs", ["status"])
    op.create_index("ix_debt_tracker_jobs_created_at", "debt_tracker_jobs", ["created_at"])

    op.create_table(
        "debt_tracker_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mapping_type", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False),
        sa.Column("source_normalized", sa.String(length=512), nullable=False),
        sa.Column("target_key", sa.String(length=512), nullable=False),
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("mapping_type", "source_normalized", name="uq_debt_tracker_mapping_source"),
    )
    op.create_index("ix_debt_tracker_mappings_mapping_type", "debt_tracker_mappings", ["mapping_type"])
    op.create_index("ix_debt_tracker_mapping_type_target", "debt_tracker_mappings", ["mapping_type", "target_key"])

    op.create_table(
        "debt_tracker_mapping_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mapping_type", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False),
        sa.Column("previous_target", sa.String(length=512), nullable=True),
        sa.Column("new_target", sa.String(length=512), nullable=False),
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_debt_tracker_mapping_history_mapping_type", "debt_tracker_mapping_history", ["mapping_type"])
    op.create_index("ix_debt_tracker_mapping_history_created_at", "debt_tracker_mapping_history", ["created_at"])


def downgrade():
    op.drop_index("ix_debt_tracker_mapping_history_created_at", table_name="debt_tracker_mapping_history")
    op.drop_index("ix_debt_tracker_mapping_history_mapping_type", table_name="debt_tracker_mapping_history")
    op.drop_table("debt_tracker_mapping_history")
    op.drop_index("ix_debt_tracker_mapping_type_target", table_name="debt_tracker_mappings")
    op.drop_index("ix_debt_tracker_mappings_mapping_type", table_name="debt_tracker_mappings")
    op.drop_table("debt_tracker_mappings")
    op.drop_index("ix_debt_tracker_jobs_created_at", table_name="debt_tracker_jobs")
    op.drop_index("ix_debt_tracker_jobs_status", table_name="debt_tracker_jobs")
    op.drop_index("ix_debt_tracker_jobs_requested_by", table_name="debt_tracker_jobs")
    op.drop_table("debt_tracker_jobs")