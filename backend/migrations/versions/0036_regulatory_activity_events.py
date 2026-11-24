"""Create the unified Regulatory Activity event table.

This revision intentionally follows the existing portfolio repair migration.
The project already uses revision 0035_repair_portfolio_pms_schema, so the
Regulatory Activity table must not introduce a second 0035 revision or a
parallel migration head.

Revision ID: 0036_regulatory_activity_events
Revises: 0035_repair_portfolio_pms_schema
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0036_regulatory_activity_events"
down_revision = "0035_repair_portfolio_pms_schema"
branch_labels = None
depends_on = None

TABLE = "regulatory_activity_events"

INDEXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ix_regulatory_activity_events_source_exchange", ("source_exchange",)),
    ("ix_regulatory_activity_events_activity_type", ("activity_type",)),
    ("ix_regulatory_activity_events_activity_sub_type", ("activity_sub_type",)),
    ("ix_regulatory_activity_events_company_id", ("company_id",)),
    ("ix_regulatory_activity_events_company_name", ("company_name",)),
    ("ix_regulatory_activity_events_symbol", ("symbol",)),
    ("ix_regulatory_activity_events_isin", ("isin",)),
    ("ix_regulatory_activity_events_event_date", ("event_date",)),
    ("ix_regulatory_activity_events_disclosure_date", ("disclosure_date",)),
    ("ix_regulatory_activity_events_status", ("status",)),
    ("ix_reg_activity_type_date", ("activity_type", "event_date")),
    ("ix_reg_activity_symbol_date", ("symbol", "event_date")),
    ("ix_reg_activity_exchange_date", ("source_exchange", "event_date")),
)


def _create_table_if_missing(bind) -> None:
    inspector = sa.inspect(bind)
    if TABLE in inspector.get_table_names():
        return

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("source_exchange", sa.String(length=12), nullable=False, server_default="NSE"),
        sa.Column("activity_type", sa.String(length=40), nullable=False),
        sa.Column("activity_sub_type", sa.String(length=40), nullable=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=True),
        sa.Column("isin", sa.String(length=24), nullable=True),
        sa.Column("event_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disclosure_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("person_allottee_client", sa.String(length=500), nullable=True),
        sa.Column("action", sa.String(length=160), nullable=True),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("value_inr", sa.Float(), nullable=True),
        sa.Column("holding_before", sa.Float(), nullable=True),
        sa.Column("holding_after", sa.Float(), nullable=True),
        sa.Column("current_market_price", sa.Float(), nullable=True),
        sa.Column("market_price_change_percent", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="PARTIAL"),
        sa.Column("source_url", sa.String(length=1500), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("source_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_key", name="uq_regulatory_activity_source_key"),
    )


def _create_indexes_if_missing(bind) -> None:
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        raise RuntimeError(f"{TABLE} was not created; refusing to continue migration")
    existing = {item["name"] for item in inspector.get_indexes(TABLE)}
    for name, columns in INDEXES:
        if name not in existing:
            op.create_index(name, TABLE, list(columns), unique=False)


def _copy_legacy_rows(bind, table: str, statement: str) -> None:
    """Copy old normalized rows if their source table exists; preserve idempotency."""
    if table not in sa.inspect(bind).get_table_names():
        return
    op.execute(statement)


def upgrade() -> None:
    bind = op.get_bind()
    _create_table_if_missing(bind)
    _create_indexes_if_missing(bind)

    # Existing normalized insider records. The prefixed source keys keep the
    # migrated records distinct from newly scraped NSE filing keys.
    _copy_legacy_rows(bind, "insider_transactions", sa.text("""
        INSERT INTO regulatory_activity_events
        (source_key, source_exchange, activity_type, activity_sub_type, company_id,
         company_name, symbol, event_date, disclosure_date, person_allottee_client,
         action, quantity, price, value_inr, holding_before, holding_after,
         current_market_price, market_price_change_percent, status, source_url,
         details_json, source_metadata)
        SELECT ('I' || substr(dedupe_key, 1, 63)), 'NSE', 'INSIDER_TRADING', NULL, company_id,
               COALESCE(stock_name, 'Unknown'), nse_symbol, transaction_date, broadcast_at,
               client_name, action, quantity, average_price, total_transaction_value,
               holding_before, holding_after, current_market_price, daily_stock_change_pct,
               'LEGACY', source_url, metadata, '{"migrated_from":"insider_transactions"}'
        FROM insider_transactions
        WHERE dedupe_key IS NOT NULL AND transaction_date IS NOT NULL
        ON CONFLICT (source_key) DO NOTHING
    """))

    _copy_legacy_rows(bind, "capital_actions", sa.text("""
        INSERT INTO regulatory_activity_events
        (source_key, source_exchange, activity_type, activity_sub_type, company_id,
         company_name, symbol, event_date, person_allottee_client, action, quantity,
         price, value_inr, current_market_price, status, source_url, details_json, source_metadata)
        SELECT ('P' || substr(dedupe_key, 1, 63)), 'NSE', 'PREFERENTIAL_WARRANT',
               CASE WHEN upper(event) LIKE '%WARRANT%' THEN 'WARRANT' ELSE 'PREFERENTIAL' END,
               company_id, COALESCE(company_name, 'Unknown'), nse_symbol, event_date,
               investor_allottee, subtype, shares_warrants, issue_transaction_price,
               value_issue_size, cmp, 'LEGACY', source_url,
               CASE
                   WHEN details IS NULL OR btrim(details) = '' THEN NULL
                   ELSE json_build_object('legacy_details', details)
               END,
               '{"migrated_from":"capital_actions"}'
        FROM capital_actions
        WHERE dedupe_key IS NOT NULL AND event_date IS NOT NULL
          AND upper(event) IN ('WARRANT','WARRANT_ISSUE','PREFERENTIAL_ISSUE','PREFERENTIAL','PREFERENTIAL_ALLOTMENT')
        ON CONFLICT (source_key) DO NOTHING
    """))

    _copy_legacy_rows(bind, "nse_exchange_deals", sa.text("""
        INSERT INTO regulatory_activity_events
        (source_key, source_exchange, activity_type, activity_sub_type, company_name,
         symbol, event_date, person_allottee_client, action, quantity, price,
         value_inr, status, source_url, details_json, source_metadata)
        SELECT ('B' || substr(source_key, 1, 63)), 'NSE', 'BULK_DEAL',
               CASE WHEN upper(event_type) LIKE '%BLOCK%' THEN 'BLOCK' ELSE 'BULK' END,
               COALESCE(stock_name, 'Unknown'), nse_symbol, transaction_date,
               client_name, action, quantity, average_price, total_value, 'LEGACY',
               source_url, NULL, '{"migrated_from":"nse_exchange_deals"}'
        FROM nse_exchange_deals
        WHERE source_key IS NOT NULL AND transaction_date IS NOT NULL
        ON CONFLICT (source_key) DO NOTHING
    """))


def downgrade() -> None:
    # Downgrading removes the new unified event store (including migrated rows).
    # Source legacy tables are not modified.
    op.drop_table(TABLE)
