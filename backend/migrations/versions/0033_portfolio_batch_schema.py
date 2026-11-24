"""Restore the missing portfolio batch revision in Alembic history.

This is a history-repair placeholder, not the original schema migration.
It intentionally makes no database schema changes.

Revision ID: 0033_portfolio_batch_schema
Revises: 0032_debt_tracker
"""

revision = "0033_portfolio_batch_schema"
down_revision = "0032_debt_tracker"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass