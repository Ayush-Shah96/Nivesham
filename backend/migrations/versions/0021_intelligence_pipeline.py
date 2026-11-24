"""Add relational run, scoring, and capital-assessment records.

Revision ID: 0021_intelligence_pipeline
Revises: 0020_portfolio_comparison
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_intelligence_pipeline"
down_revision = "0020_portfolio_comparison"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "news_source_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("articles_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("articles_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("lookback_days", sa.Integer(), nullable=False, server_default="2557"),
    )
    op.create_index("ix_news_source_runs_source_key", "news_source_runs", ["source_key"])
    op.create_index("ix_news_source_runs_status", "news_source_runs", ["status"])
    op.create_index("ix_news_source_runs_started_at", "news_source_runs", ["started_at"])
    op.create_index("ix_news_source_runs_started_status", "news_source_runs", ["started_at", "status"])

    op.create_table(
        "news_article_analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False, server_default="heuristic"),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("prompt_version", sa.String(40), nullable=False, server_default="news-relevance-v1"),
        sa.Column("relevance_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("materiality", sa.String(30), nullable=False, server_default="unknown"),
        sa.Column("event_type", sa.String(40), nullable=False, server_default="NEWS"),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_news_article_analyses_article_id", "news_article_analyses", ["article_id"])
    op.create_index("ix_news_article_analyses_created_at", "news_article_analyses", ["created_at"])
    op.create_index("ix_news_article_analyses_article_created", "news_article_analyses", ["article_id", "created_at"])

    op.create_table(
        "capital_action_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("capital_action_id", sa.Integer(), sa.ForeignKey("capital_action_signals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False, server_default="heuristic"),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("assessment_version", sa.String(40), nullable=False, server_default="capital-assessment-v1"),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("assessment", sa.String(40), nullable=False, server_default="REVIEW"),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("missing_information", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_capital_action_assessments_capital_action_id", "capital_action_assessments", ["capital_action_id"])
    op.create_index("ix_capital_action_assessments_created_at", "capital_action_assessments", ["created_at"])
    op.create_index("ix_capital_assessments_event_created", "capital_action_assessments", ["capital_action_id", "created_at"])

    op.create_index("ix_insider_signals_transaction_date_id", "insider_signals", ["transaction_date", "id"], unique=False)
    op.create_index("ix_capital_action_event_date_id", "capital_action_signals", ["event_date", "id"], unique=False)

def downgrade() -> None:
    op.drop_index("ix_capital_action_event_date_id", table_name="capital_action_signals")
    op.drop_index("ix_insider_signals_transaction_date_id", table_name="insider_signals")
    op.drop_table("capital_action_assessments")
    op.drop_table("news_article_analyses")
    op.drop_table("news_source_runs")
