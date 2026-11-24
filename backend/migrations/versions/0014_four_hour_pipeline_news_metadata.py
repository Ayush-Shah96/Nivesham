"""Ensure NewsAlert metadata and add only indexes not already owned by prior revisions.

Revision ID: 0014_pipeline_news_meta
Revises: 0013_newsalert_perf
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_pipeline_news_meta"
down_revision = "0013_newsalert_perf"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _ensure_index(name: str, table: str, columns: list[str]) -> None:
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, unique=False)


def upgrade() -> None:
    """Repair NewsAlert fields if absent, without duplicating 0012/0013 objects."""
    article_columns = _columns("articles")
    if "content_type" not in article_columns:
        op.add_column(
            "articles",
            sa.Column("content_type", sa.String(length=20), nullable=False, server_default="NEWS"),
        )
    if "matched_company_id" not in article_columns:
        op.add_column(
            "articles",
            sa.Column("matched_company_id", sa.Integer(), nullable=True),
        )
    if "news_alert_eligible" not in article_columns:
        op.add_column(
            "articles",
            sa.Column("news_alert_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    # processing_jobs.result is owned by 0012. Keep the guard for installations
    # whose physical schema drifted, but do not claim ownership of it here.
    if "result" not in _columns("processing_jobs"):
        op.add_column("processing_jobs", sa.Column("result", sa.JSON(), nullable=True))

    # The matched-company FK is owned by 0013. This is a repair guard only.
    fk_names = {fk.get("name") for fk in sa.inspect(op.get_bind()).get_foreign_keys("articles")}
    if "fk_articles_matched_company_id_companies" not in fk_names:
        op.create_foreign_key(
            "fk_articles_matched_company_id_companies",
            "articles",
            "companies",
            ["matched_company_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 0013 already creates equivalent indexes for content_type, matched_company_id,
    # processing_jobs, and audit_logs. Do not create duplicate indexes under new names.
    _ensure_index(
        "ix_articles_news_alert_eligible_published",
        "articles",
        ["news_alert_eligible", "published_at", "id"],
    )

    for table, name, columns in (
        ("insider_signals", "ix_insider_signals_company_date", ["company_id", "transaction_date", "id"]),
        ("capital_action_signals", "ix_capital_action_company_date_id", ["company_id", "event_date", "id"]),
        ("financial_results", "ix_financial_results_company_published_id", ["company_id", "published_at", "id"]),
        ("market_intelligence_metrics", "ix_market_metrics_recorded_id", ["recorded_at", "id"]),
        ("notifications", "ix_notifications_user_read_created", ["user_id", "read_at", "created_at", "id"]),
    ):
        _ensure_index(name, table, columns)


def downgrade() -> None:
    """Remove only 0014-owned objects; retain fields/indexes owned by 0012/0013."""
    for table, name in (
        ("notifications", "ix_notifications_user_read_created"),
        ("market_intelligence_metrics", "ix_market_metrics_recorded_id"),
        ("financial_results", "ix_financial_results_company_published_id"),
        ("capital_action_signals", "ix_capital_action_company_date_id"),
        ("insider_signals", "ix_insider_signals_company_date"),
        ("articles", "ix_articles_news_alert_eligible_published"),
    ):
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
        if name in indexes:
            op.drop_index(name, table_name=table)

    if "news_alert_eligible" in _columns("articles"):
        op.drop_column("articles", "news_alert_eligible")

    # Deliberately do not drop articles.content_type, articles.matched_company_id,
    # their foreign key, or processing_jobs.result: those belong to 0013/0012.
