from alembic import op
import sqlalchemy as sa

revision = "0003_regulatory_signal_columns"
down_revision = "0002_architecture_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("insider_signals", sa.Column("client_category", sa.String(120), nullable=True))
    op.add_column("insider_signals", sa.Column("action", sa.String(120), nullable=True))
    op.add_column("insider_signals", sa.Column("quantity", sa.Float(), nullable=True))
    op.add_column("insider_signals", sa.Column("post_transaction_holding", sa.Float(), nullable=True))
    op.add_column("insider_signals", sa.Column("traded_percent", sa.Float(), nullable=True))
    op.add_column("insider_signals", sa.Column("period", sa.String(120), nullable=True))


def downgrade():
    for name in ["period", "traded_percent", "post_transaction_holding", "quantity", "action", "client_category"]:
        op.drop_column("insider_signals", name)
