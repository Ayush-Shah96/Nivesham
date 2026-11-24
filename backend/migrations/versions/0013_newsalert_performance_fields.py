"""Persist NewsAlert classification/matches and ProcessingJob results."""
from alembic import op
import sqlalchemy as sa

revision = "0013_newsalert_perf"
down_revision = "0012_news_ingest_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Persist the source-driven News / Interview / Post classification.
    op.add_column(
        "articles",
        sa.Column("content_type", sa.String(length=20), nullable=True)
    )
    op.execute(
        """
        UPDATE articles
        SET content_type = CASE
            WHEN lower(coalesce(source, '')) LIKE 'youtube%'
              OR lower(coalesce(source, '')) LIKE '%youtube ·%'
                THEN 'INTERVIEW'
            WHEN lower(coalesce(source, '')) LIKE 'x / twitter%'
              OR lower(coalesce(source, '')) LIKE '%twitter%'
              OR lower(coalesce(source, '')) LIKE '%x.com%'
                THEN 'POST'
            ELSE 'NEWS'
        END
        WHERE content_type IS NULL
        """
    )
    op.alter_column(
        "articles",
        "content_type",
        existing_type=sa.String(length=20),
        nullable=False,
        server_default="NEWS",
    )

    op.add_column(
        "articles",
        sa.Column("matched_company_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_articles_matched_company_id_companies",
        "articles",
        "companies",
        ["matched_company_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_index(
        "ix_articles_content_type_published_id",
        "articles",
        ["content_type", "published_at", "id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_articles_matched_company_published_id",
        "articles",
        ["matched_company_id", "published_at", "id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_processing_jobs_app_stage_status_id",
        "processing_jobs",
        ["application", "workflow_stage", "status", "id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_target_companies_active_approval_track_analyst",
        "target_companies",
        ["active", "approval_status", "track_mode", "analyst_id", "company_id"],
        unique=False,
        if_not_exists=True,
    )
    # Composite read-path indexes for the pages that sort/filter by source/date.
    op.create_index(
        "ix_market_metrics_source_recorded_id",
        "market_intelligence_metrics",
        ["source_name", "recorded_at", "id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_crisp_notes_created_id",
        "crisp_investor_notes",
        ["created_at", "id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_audit_logs_created_id",
        "audit_logs",
        ["created_at", "id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_audit_logs_user_created_id",
        "audit_logs",
        ["user_id", "created_at", "id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_benpos_records_created_id",
        "benpos_records",
        ["created_at", "id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_financial_results_published_id",
        "financial_results",
        ["published_at", "id"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_financial_results_published_id", table_name="financial_results", if_exists=True)
    op.drop_index("ix_benpos_records_created_id", table_name="benpos_records", if_exists=True)
    op.drop_index("ix_audit_logs_user_created_id", table_name="audit_logs", if_exists=True)
    op.drop_index("ix_audit_logs_created_id", table_name="audit_logs", if_exists=True)
    op.drop_index("ix_crisp_notes_created_id", table_name="crisp_investor_notes", if_exists=True)
    op.drop_index("ix_market_metrics_source_recorded_id", table_name="market_intelligence_metrics", if_exists=True)
    op.drop_index("ix_target_companies_active_approval_track_analyst", table_name="target_companies", if_exists=True)
    op.drop_index("ix_processing_jobs_app_stage_status_id", table_name="processing_jobs", if_exists=True)
    op.drop_index("ix_articles_matched_company_published_id", table_name="articles", if_exists=True)
    op.drop_index("ix_articles_content_type_published_id", table_name="articles", if_exists=True)
    op.drop_constraint("fk_articles_matched_company_id_companies", "articles", type_="foreignkey")
    op.drop_column("articles", "matched_company_id")
    op.drop_column("articles", "content_type")
