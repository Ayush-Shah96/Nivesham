"""Merge the exchange-deals and Signal Radar/Debt Tracker migration branches."""

revision = "0033_merge_reg_deals"
down_revision = ("0030_nse_exchange_deals", "0032_debt_tracker")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
