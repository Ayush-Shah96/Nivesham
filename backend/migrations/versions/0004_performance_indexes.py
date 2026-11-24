"""Add performance indexes to InsiderSignal table.

Revision ID: 0004
Revises: 0003_regulatory_signal_columns
Create Date: 2026-01-16 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003_regulatory_signal_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add index on company_id for faster joins
    op.create_index(
        "ix_insider_signals_company_id",
        "insider_signals",
        ["company_id"],
        unique=False
    )
    
    # Add index on transaction_date for faster sorting and filtering
    op.create_index(
        "ix_insider_signals_transaction_date",
        "insider_signals",
        ["transaction_date"],
        unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_insider_signals_transaction_date", table_name="insider_signals")
    op.drop_index("ix_insider_signals_company_id", table_name="insider_signals")