# Add coverage-scoped earnings calendar events.
# Revision ID: 0026_earnings_calendar
# Revises: 0025_insider_broadcast_at

from alembic import op
import sqlalchemy as sa

revision = "0026_earnings_calendar"
down_revision = "0025_insider_broadcast_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "earnings_calendar_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("pending_label", sa.String(length=100), nullable=True),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("call_time", sa.String(length=40), nullable=True),
        sa.Column("event_title", sa.String(length=1000), nullable=True),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("source_url", sa.String(length=1500), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.UniqueConstraint(
            "event_key", name="uq_earnings_calendar_event_key"
        ),
    )

    op.create_index(
        "ix_earnings_calendar_events_company_id",
        "earnings_calendar_events",
        ["company_id"],
    )
    op.create_index(
        "ix_earnings_calendar_events_event_date",
        "earnings_calendar_events",
        ["event_date"],
    )
    op.create_index(
        "ix_earnings_calendar_events_event_type",
        "earnings_calendar_events",
        ["event_type"],
    )
    op.create_index(
        "ix_earnings_calendar_date_type",
        "earnings_calendar_events",
        ["event_date", "event_type"],
    )
    op.create_index(
        "ix_earnings_calendar_company_date",
        "earnings_calendar_events",
        ["company_id", "event_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_earnings_calendar_company_date",
        table_name="earnings_calendar_events",
    )
    op.drop_index(
        "ix_earnings_calendar_date_type",
        table_name="earnings_calendar_events",
    )
    op.drop_index(
        "ix_earnings_calendar_events_event_type",
        table_name="earnings_calendar_events",
    )
    op.drop_index(
        "ix_earnings_calendar_events_event_date",
        table_name="earnings_calendar_events",
    )
    op.drop_index(
        "ix_earnings_calendar_events_company_id",
        table_name="earnings_calendar_events",
    )
    op.drop_table("earnings_calendar_events")
