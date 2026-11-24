"""Repair adaptive Portfolio Comparison PMS tables on existing databases.

The original 0020_portfolio_comparison revision was updated in-place during
feature development to include the adaptive PMS registry and monthly tables.
Databases that had already applied the earlier version of 0020 will not replay
that revision, and the historical 0033_portfolio_batch_schema revision is a
no-op placeholder. This forward migration ensures the current PMS tables exist
without dropping or rewriting any existing portfolio data.

Revision ID: 0035_repair_portfolio_pms_schema
Revises: 0034_merge_all_heads
"""

from alembic import op

from apps.portfolio_comparison import models as portfolio_models


revision = "0035_repair_portfolio_pms_schema"
down_revision = "0034_merge_all_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create only missing adaptive PMS tables, in foreign-key order.

    checkfirst=True makes this safe in both cases:
    - older databases where 0020 was already recorded but its PMS tables are
      missing; and
    - fresh databases where the current 0020 migration already created them.
    """
    bind = op.get_bind()

    for model in (
        portfolio_models.PMSRegistry,
        portfolio_models.PMSMonthlyFile,
        portfolio_models.PMSMonthlyHolding,
    ):
        model.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    """Intentionally retain the repaired tables and user data on downgrade.

    This is a compatibility repair for schema objects that were missing from
    some already-deployed databases. Dropping them on downgrade would delete
    uploaded PMS history, so this revision is deliberately non-destructive.
    """
    pass
