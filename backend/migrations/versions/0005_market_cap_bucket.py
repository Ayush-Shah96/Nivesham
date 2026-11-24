"""Add market-cap bucket to shared company master list."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("companies", sa.Column("market_cap_bucket", sa.String(length=20), nullable=True))


def downgrade():
    op.drop_column("companies", "market_cap_bucket")
