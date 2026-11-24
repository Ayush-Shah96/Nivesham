"""Store folio number to broker mapping on portfolio accounts.

Each folio in the client's mapping workbook becomes an account under its broker.
folio_key is the whitespace-free match key; it is unique across all brokers, so a
folio can belong to only one broker.

Revision ID: 0029_folio_broker_mapping
Revises: 0028_crisp_note_title
"""
from alembic import op
import sqlalchemy as sa

revision = "0029_folio_broker_mapping"
down_revision = "0028_crisp_note_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "portfolio_comparison_accounts",
        sa.Column("folio_number", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "portfolio_comparison_accounts",
        sa.Column("folio_key", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_portfolio_comparison_account_folio_key",
        "portfolio_comparison_accounts",
        ["folio_key"],
        unique=True,
        postgresql_where=sa.text("folio_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_portfolio_comparison_account_folio_key", table_name="portfolio_comparison_accounts")
    op.drop_column("portfolio_comparison_accounts", "folio_key")
    op.drop_column("portfolio_comparison_accounts", "folio_number")