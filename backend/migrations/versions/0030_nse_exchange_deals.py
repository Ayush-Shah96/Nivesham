"""Persist NSE bulk and block deal disclosures.

Revision ID: 0030_nse_exchange_deals
Revises: 0029_folio_broker_mapping
"""
from alembic import op
import sqlalchemy as sa

revision = "0030_nse_exchange_deals"
down_revision = "0029_folio_broker_mapping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nse_exchange_deals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("nse_symbol", sa.String(length=40), nullable=False),
        sa.Column("stock_name", sa.String(length=255), nullable=False),
        sa.Column("client_name", sa.String(length=500), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=True),
        sa.Column("transaction_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("average_price", sa.Float(), nullable=True),
        sa.Column("total_value", sa.Float(), nullable=True),
        sa.Column("remarks", sa.Text(), nullable=True),
        sa.Column("source_url", sa.String(length=1500), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_nse_exchange_deals_source_key", "nse_exchange_deals", ["source_key"], unique=True)
    op.create_index("ix_nse_exchange_deals_source_hash", "nse_exchange_deals", ["source_hash"])
    op.create_index("ix_nse_exchange_deals_event_type", "nse_exchange_deals", ["event_type"])
    op.create_index("ix_nse_exchange_deals_nse_symbol", "nse_exchange_deals", ["nse_symbol"])
    op.create_index("ix_nse_exchange_deals_client_name", "nse_exchange_deals", ["client_name"])
    op.create_index("ix_nse_exchange_deals_transaction_date", "nse_exchange_deals", ["transaction_date"])
    op.create_index("ix_nse_exchange_deals_symbol_date", "nse_exchange_deals", ["nse_symbol", "transaction_date"])
    op.create_index("ix_nse_exchange_deals_type_date", "nse_exchange_deals", ["event_type", "transaction_date"])


def downgrade() -> None:
    op.drop_index("ix_nse_exchange_deals_type_date", table_name="nse_exchange_deals")
    op.drop_index("ix_nse_exchange_deals_symbol_date", table_name="nse_exchange_deals")
    op.drop_index("ix_nse_exchange_deals_transaction_date", table_name="nse_exchange_deals")
    op.drop_index("ix_nse_exchange_deals_client_name", table_name="nse_exchange_deals")
    op.drop_index("ix_nse_exchange_deals_nse_symbol", table_name="nse_exchange_deals")
    op.drop_index("ix_nse_exchange_deals_event_type", table_name="nse_exchange_deals")
    op.drop_index("ix_nse_exchange_deals_source_hash", table_name="nse_exchange_deals")
    op.drop_index("ix_nse_exchange_deals_source_key", table_name="nse_exchange_deals")
    op.drop_table("nse_exchange_deals")
