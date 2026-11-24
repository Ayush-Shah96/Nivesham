"""Add PE / warrant capital-action signals."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "capital_action_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("stock_name", sa.String(length=255), nullable=False),
        sa.Column("investor_name", sa.String(length=255), nullable=True),
        sa.Column("investor_category", sa.String(length=120), nullable=True),
        sa.Column("action", sa.String(length=160), nullable=True),
        sa.Column("event_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("amount_inr", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("title", sa.String(length=1000), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_capital_action_signals_company_id", "capital_action_signals", ["company_id"])
    op.create_index("ix_capital_action_signals_event_type", "capital_action_signals", ["event_type"])
    op.create_index("ix_capital_action_signals_event_date", "capital_action_signals", ["event_date"])
    op.create_index("ix_capital_action_signals_source_key", "capital_action_signals", ["source_key"], unique=True)
    op.create_index("ix_capital_action_type_date", "capital_action_signals", ["event_type", "event_date"])
    op.create_index("ix_capital_action_company_date", "capital_action_signals", ["company_id", "event_date"])


def downgrade():
    op.drop_index("ix_capital_action_company_date", table_name="capital_action_signals")
    op.drop_index("ix_capital_action_type_date", table_name="capital_action_signals")
    op.drop_index("ix_capital_action_signals_source_key", table_name="capital_action_signals")
    op.drop_index("ix_capital_action_signals_event_date", table_name="capital_action_signals")
    op.drop_index("ix_capital_action_signals_event_type", table_name="capital_action_signals")
    op.drop_index("ix_capital_action_signals_company_id", table_name="capital_action_signals")
    op.drop_table("capital_action_signals")
