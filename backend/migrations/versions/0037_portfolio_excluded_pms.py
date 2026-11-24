"""Persist PMS reports explicitly marked as not received on each upload batch.

Revision ID: 0037_portfolio_excluded_pms
Revises: 0036_regulatory_activity_events
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_portfolio_excluded_pms"
down_revision = "0036_regulatory_activity_events"
branch_labels = None
depends_on = None

TABLE = "portfolio_comparison_upload_batches"
COLUMN = "excluded_pms"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns(TABLE)}
        if COLUMN not in columns:
            op.add_column(TABLE, sa.Column(COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns(TABLE)}
        if COLUMN in columns:
            op.drop_column(TABLE, COLUMN)
