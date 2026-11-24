"""Create canonical frontend-facing News Alert and Regulatory tables.

Revision ID: 0024_primary_signal_tables
Revises: 0023_regulatory_collector_audit
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_primary_signal_tables"
down_revision = "0023_regulatory_collector_audit"
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "news_alert_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("nse_symbol", sa.String(40), nullable=True),
        sa.Column("headline", sa.String(1000), nullable=False),
        sa.Column("url", sa.String(1500), nullable=False),
        sa.Column("source", sa.String(160), nullable=False),
        sa.Column("analyst", sa.String(120), nullable=True),
        sa.Column("content_type", sa.String(20), nullable=False, server_default="NEWS"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("author", sa.String(255), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("relevance_score", sa.Float(), nullable=True),
        sa.Column("materiality", sa.String(30), nullable=True),
        sa.Column("relevance_rationale", sa.Text(), nullable=True),
        sa.Column("source_item_id", sa.String(255), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("processing_status", sa.String(30), nullable=False, server_default="VISIBLE"),
        *_timestamps(),
    )
    for name, cols in {
        "ix_news_alert_items_company_id": ["company_id"],
        "ix_news_alert_items_company_name": ["company_name"],
        "ix_news_alert_items_nse_symbol": ["nse_symbol"],
        "ix_news_alert_items_source": ["source"],
        "ix_news_alert_items_analyst": ["analyst"],
        "ix_news_alert_items_content_type": ["content_type"],
        "ix_news_alert_items_published_at": ["published_at"],
        "ix_news_alert_items_source_item_id": ["source_item_id"],
        "ix_news_alert_items_content_hash": ["content_hash"],
        "ix_news_alert_items_dedupe_key": ["dedupe_key"],
        "ix_news_alert_items_processing_status": ["processing_status"],
        "ix_news_alert_company_published": ["company_id", "published_at"],
        "ix_news_alert_type_published": ["content_type", "published_at"],
    }.items():
        op.create_index(name, "news_alert_items", cols, unique=name == "ix_news_alert_items_dedupe_key")

    op.create_table(
        "insider_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("legacy_signal_id", sa.Integer(), nullable=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stock_name", sa.String(255), nullable=False),
        sa.Column("nse_symbol", sa.String(40), nullable=True),
        sa.Column("client_name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(120), nullable=True),
        sa.Column("action", sa.String(160), nullable=True),
        sa.Column("transaction_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("holding_before", sa.Float(), nullable=True),
        sa.Column("holding_after", sa.Float(), nullable=True),
        sa.Column("holding_change_pct", sa.Float(), nullable=True),
        sa.Column("average_price", sa.Float(), nullable=True),
        sa.Column("current_market_price", sa.Float(), nullable=True),
        sa.Column("cmp_vs_avg_transaction_price_pct", sa.Float(), nullable=True),
        sa.Column("daily_stock_change_pct", sa.Float(), nullable=True),
        sa.Column("total_transaction_value", sa.Float(), nullable=True),
        sa.Column("analyst", sa.String(120), nullable=True),
        sa.Column("source", sa.String(80), nullable=False, server_default="NSE_PIT"),
        sa.Column("source_url", sa.String(1500), nullable=True),
        sa.Column("source_filing_id", sa.String(255), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("legacy_signal_id", name="uq_insider_transactions_legacy_signal_id"),
    )
    for name, cols in {
        "ix_insider_transactions_company_id": ["company_id"],
        "ix_insider_transactions_stock_name": ["stock_name"],
        "ix_insider_transactions_nse_symbol": ["nse_symbol"],
        "ix_insider_transactions_client_name": ["client_name"],
        "ix_insider_transactions_action": ["action"],
        "ix_insider_transactions_transaction_date": ["transaction_date"],
        "ix_insider_transactions_analyst": ["analyst"],
        "ix_insider_transactions_source": ["source"],
        "ix_insider_transactions_source_filing_id": ["source_filing_id"],
        "ix_insider_transactions_source_hash": ["source_hash"],
        "ix_insider_transactions_dedupe_key": ["dedupe_key"],
        "ix_insider_transactions_company_date": ["company_id", "transaction_date"],
        "ix_insider_transactions_symbol_date": ["nse_symbol", "transaction_date"],
    }.items():
        op.create_index(name, "insider_transactions", cols, unique=name == "ix_insider_transactions_dedupe_key")

    op.create_table(
        "capital_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("legacy_signal_id", sa.Integer(), nullable=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event", sa.String(80), nullable=False),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("nse_symbol", sa.String(40), nullable=True),
        sa.Column("investor_allottee", sa.String(500), nullable=True),
        sa.Column("subtype", sa.String(120), nullable=True),
        sa.Column("shares_warrants", sa.Float(), nullable=True),
        sa.Column("issue_transaction_price", sa.Float(), nullable=True),
        sa.Column("value_issue_size", sa.Float(), nullable=True),
        sa.Column("ownership_pct", sa.Float(), nullable=True),
        sa.Column("cmp", sa.Float(), nullable=True),
        sa.Column("importance", sa.String(40), nullable=True),
        sa.Column("importance_score", sa.Float(), nullable=True),
        sa.Column("event_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("analyst", sa.String(120), nullable=True),
        sa.Column("source", sa.String(120), nullable=False, server_default="NSE_CA"),
        sa.Column("source_url", sa.String(1500), nullable=True),
        sa.Column("source_announcement_id", sa.String(255), nullable=True),
        sa.Column("transaction_status", sa.String(24), nullable=False, server_default="REVIEW"),
        sa.Column("classification_status", sa.String(24), nullable=False, server_default="REVIEW"),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("legacy_signal_id", name="uq_capital_actions_legacy_signal_id"),
    )
    for name, cols in {
        "ix_capital_actions_company_id": ["company_id"],
        "ix_capital_actions_event": ["event"],
        "ix_capital_actions_company_name": ["company_name"],
        "ix_capital_actions_nse_symbol": ["nse_symbol"],
        "ix_capital_actions_event_date": ["event_date"],
        "ix_capital_actions_analyst": ["analyst"],
        "ix_capital_actions_source": ["source"],
        "ix_capital_actions_source_announcement_id": ["source_announcement_id"],
        "ix_capital_actions_transaction_status": ["transaction_status"],
        "ix_capital_actions_classification_status": ["classification_status"],
        "ix_capital_actions_source_hash": ["source_hash"],
        "ix_capital_actions_dedupe_key": ["dedupe_key"],
        "ix_capital_actions_company_date": ["company_id", "event_date"],
        "ix_capital_actions_type_date": ["event", "event_date"],
        "ix_capital_actions_symbol_date": ["nse_symbol", "event_date"],
    }.items():
        op.create_index(name, "capital_actions", cols, unique=name == "ix_capital_actions_dedupe_key")


def downgrade() -> None:
    for table, indexes in {
        "capital_actions": ["ix_capital_actions_company_id", "ix_capital_actions_event", "ix_capital_actions_company_name", "ix_capital_actions_nse_symbol", "ix_capital_actions_event_date", "ix_capital_actions_analyst", "ix_capital_actions_source", "ix_capital_actions_source_announcement_id", "ix_capital_actions_transaction_status", "ix_capital_actions_classification_status", "ix_capital_actions_source_hash", "ix_capital_actions_dedupe_key", "ix_capital_actions_company_date", "ix_capital_actions_type_date", "ix_capital_actions_symbol_date"],
        "insider_transactions": ["ix_insider_transactions_company_id", "ix_insider_transactions_stock_name", "ix_insider_transactions_nse_symbol", "ix_insider_transactions_client_name", "ix_insider_transactions_action", "ix_insider_transactions_transaction_date", "ix_insider_transactions_analyst", "ix_insider_transactions_source", "ix_insider_transactions_source_filing_id", "ix_insider_transactions_source_hash", "ix_insider_transactions_dedupe_key", "ix_insider_transactions_company_date", "ix_insider_transactions_symbol_date"],
        "news_alert_items": ["ix_news_alert_items_company_id", "ix_news_alert_items_company_name", "ix_news_alert_items_nse_symbol", "ix_news_alert_items_source", "ix_news_alert_items_analyst", "ix_news_alert_items_content_type", "ix_news_alert_items_published_at", "ix_news_alert_items_source_item_id", "ix_news_alert_items_content_hash", "ix_news_alert_items_dedupe_key", "ix_news_alert_items_processing_status", "ix_news_alert_company_published", "ix_news_alert_type_published"],
    }.items():
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)