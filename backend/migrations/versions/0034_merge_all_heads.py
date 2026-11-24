"""Merge the regulatory/debt and portfolio batch migration branches.

Revision ID: 0034_merge_all_heads
Revises: 0033_merge_regulatory_deals_heads,
         0033_portfolio_batch_schema
"""

revision = "0034_merge_all_heads"
down_revision = (
    "0033_merge_reg_deals",
    "0033_portfolio_batch_schema",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass