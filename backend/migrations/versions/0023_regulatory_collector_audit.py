"""Add raw NSE filings, per-window collection runs, market prices and review states.

Revision ID: 0023_regulatory_collector_audit
Revises: 0022_database_first_intelligence
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_regulatory_collector_audit"
down_revision = "0022_database_first_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "regulatory_filings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("source_filing_id", sa.String(length=255), nullable=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("nse_symbol", sa.String(length=40), nullable=True),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column("filing_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_status", sa.String(length=24), nullable=False),
        sa.Column("classification_status", sa.String(length=24), nullable=False),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("source_key", name="uq_regulatory_filings_source_key"),
    )
    op.create_index("ix_regulatory_filings_source", "regulatory_filings", ["source"])
    op.create_index("ix_regulatory_filings_source_key", "regulatory_filings", ["source_key"])
    op.create_index("ix_regulatory_filings_source_filing_id", "regulatory_filings", ["source_filing_id"])
    op.create_index("ix_regulatory_filings_company_id", "regulatory_filings", ["company_id"])
    op.create_index("ix_regulatory_filings_nse_symbol", "regulatory_filings", ["nse_symbol"])
    op.create_index("ix_regulatory_filings_filing_date", "regulatory_filings", ["filing_date"])
    op.create_index("ix_regulatory_filings_content_hash", "regulatory_filings", ["content_hash"])
    op.create_index("ix_regulatory_filings_processing_status", "regulatory_filings", ["processing_status"])
    op.create_index("ix_regulatory_filings_classification_status", "regulatory_filings", ["classification_status"])
    op.create_index("ix_regulatory_filings_source_date", "regulatory_filings", ["source", "filing_date"])
    op.create_index("ix_regulatory_filings_symbol_date", "regulatory_filings", ["nse_symbol", "filing_date"])

    op.create_table(
        "regulatory_collector_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("records_received", sa.Integer(), nullable=False),
        sa.Column("records_inserted", sa.Integer(), nullable=False),
        sa.Column("records_updated", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_regulatory_collector_runs_source", "regulatory_collector_runs", ["source"])
    op.create_index("ix_regulatory_collector_runs_window_start", "regulatory_collector_runs", ["window_start"])
    op.create_index("ix_regulatory_collector_runs_window_end", "regulatory_collector_runs", ["window_end"])
    op.create_index("ix_regulatory_collector_runs_started_at", "regulatory_collector_runs", ["started_at"])
    op.create_index("ix_regulatory_collector_runs_status", "regulatory_collector_runs", ["status"])
    op.create_index("ix_regulatory_collector_runs_created_at", "regulatory_collector_runs", ["created_at"])

    op.create_table(
        "regulatory_market_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nse_symbol", sa.String(length=40), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("previous_close", sa.Float(), nullable=True),
        sa.Column("change_percent", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("nse_symbol", "trade_date", "source", name="uq_regulatory_market_price_symbol_date_source"),
    )
    op.create_index("ix_regulatory_market_prices_nse_symbol", "regulatory_market_prices", ["nse_symbol"])
    op.create_index("ix_regulatory_market_prices_trade_date", "regulatory_market_prices", ["trade_date"])
    op.create_index("ix_regulatory_market_prices_trade_date_symbol", "regulatory_market_prices", ["trade_date", "nse_symbol"])

    # Batch alters are required for SQLite deployments while remaining valid on
    # PostgreSQL. The project supports both local SQLite and server database URLs.
    with op.batch_alter_table("insider_signals", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("regulatory_filing_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_insider_signals_regulatory_filing_id", "regulatory_filings", ["regulatory_filing_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index("ix_insider_signals_regulatory_filing_id", ["regulatory_filing_id"])

    with op.batch_alter_table("capital_action_signals", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("regulatory_filing_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("transaction_status", sa.String(length=24), nullable=False, server_default="REVIEW"))
        batch_op.add_column(sa.Column("classification_status", sa.String(length=24), nullable=False, server_default="CLASSIFIED"))
        batch_op.add_column(sa.Column("review_reason", sa.Text(), nullable=True))
        batch_op.create_foreign_key("fk_capital_action_signals_regulatory_filing_id", "regulatory_filings", ["regulatory_filing_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index("ix_capital_action_signals_regulatory_filing_id", ["regulatory_filing_id"])
        batch_op.create_index("ix_capital_action_signals_transaction_status", ["transaction_status"])
        batch_op.create_index("ix_capital_action_signals_classification_status", ["classification_status"])


def downgrade() -> None:
    with op.batch_alter_table("capital_action_signals", recreate="auto") as batch_op:
        batch_op.drop_index("ix_capital_action_signals_classification_status")
        batch_op.drop_index("ix_capital_action_signals_transaction_status")
        batch_op.drop_index("ix_capital_action_signals_regulatory_filing_id")
        batch_op.drop_constraint("fk_capital_action_signals_regulatory_filing_id", type_="foreignkey")
        batch_op.drop_column("review_reason")
        batch_op.drop_column("classification_status")
        batch_op.drop_column("transaction_status")
        batch_op.drop_column("regulatory_filing_id")

    with op.batch_alter_table("insider_signals", recreate="auto") as batch_op:
        batch_op.drop_index("ix_insider_signals_regulatory_filing_id")
        batch_op.drop_constraint("fk_insider_signals_regulatory_filing_id", type_="foreignkey")
        batch_op.drop_column("regulatory_filing_id")

    op.drop_index("ix_regulatory_market_prices_trade_date_symbol", table_name="regulatory_market_prices")
    op.drop_index("ix_regulatory_market_prices_trade_date", table_name="regulatory_market_prices")
    op.drop_index("ix_regulatory_market_prices_nse_symbol", table_name="regulatory_market_prices")
    op.drop_table("regulatory_market_prices")

    for name in ("source", "window_start", "window_end", "started_at", "finished_at", "status", "created_at"):
        op.drop_index(f"ix_regulatory_collector_runs_{name}", table_name="regulatory_collector_runs")
    op.drop_table("regulatory_collector_runs")

    for name in ("source", "source_key", "source_filing_id", "company_id", "nse_symbol", "filing_date", "content_hash", "processing_status", "classification_status"):
        op.drop_index(f"ix_regulatory_filings_{name}", table_name="regulatory_filings")
    op.drop_index("ix_regulatory_filings_source_date", table_name="regulatory_filings")
    op.drop_index("ix_regulatory_filings_symbol_date", table_name="regulatory_filings")
    op.drop_table("regulatory_filings")
