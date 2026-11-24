"""Add Coverage List track modes and allow unassigned coverage rows."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE trackmode ADD VALUE IF NOT EXISTS 'NT'")
        op.execute("ALTER TYPE trackmode ADD VALUE IF NOT EXISTS 'PO'")

    op.alter_column(
        "target_companies",
        "analyst_id",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade():
    # PostgreSQL enum values cannot be removed safely with a simple ALTER TYPE.
    # Existing NT/PO rows must be converted before a downgrade.
    bind = op.get_bind()
    op.execute("UPDATE target_companies SET track_mode = 'LT' WHERE track_mode IN ('NT', 'PO')")
    op.alter_column(
        "target_companies",
        "analyst_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
