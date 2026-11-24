from alembic import op


revision = "0011_dashboard_indexes"
down_revision = "0010_remove_legacy_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    print(">>> 0011 UPGRADE STARTED", flush=True)

    op.create_index(
        "ix_articles_published_at_id",
        "articles",
        ["published_at", "id"],
        unique=False,
        if_not_exists=True,
    )

    print(">>> articles index created", flush=True)

    op.create_index(
        "ix_target_companies_active_approval",
        "target_companies",
        ["active", "approval_status"],
        unique=False,
        if_not_exists=True,
    )

    print(">>> target_companies index created", flush=True)

    print(">>> 0011 UPGRADE FINISHED", flush=True)


def downgrade() -> None:
    op.drop_index(
        "ix_target_companies_active_approval",
        table_name="target_companies",
        if_exists=True,
    )

    op.drop_index(
        "ix_articles_published_at_id",
        table_name="articles",
        if_exists=True,
    )
