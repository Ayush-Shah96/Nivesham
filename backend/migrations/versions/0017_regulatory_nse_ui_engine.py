
"""Track the Regulatory Desk ingestion engine version.

Revision ID: 0017_regulatory_nse_ui_engine
Revises: 0016_regulatory_sync_state
"""

from alembic import op
import sqlalchemy as sa

revision = "0017_regulatory_nse_ui_engine"
down_revision = "0016_regulatory_sync_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "regulatory_sync_state",
        sa.Column("engine_version", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("regulatory_sync_state", "engine_version")
