"""Store detailed live VAHAN dashboard observations.

Revision ID: 0018_vahan_observations
Revises: 0017_regulatory_nse_ui_engine
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_vahan_observations"
down_revision = "0017_regulatory_nse_ui_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vahan_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column("scrape_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("view", sa.String(length=40), nullable=False),
        sa.Column("display_type", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=160), nullable=False),
        sa.Column("rto", sa.String(length=240), nullable=False),
        sa.Column("year_type", sa.String(length=40), nullable=False),
        sa.Column("year", sa.String(length=20), nullable=False),
        sa.Column("filter_dimension", sa.String(length=80), nullable=True),
        sa.Column("filter_value", sa.String(length=255), nullable=True),
        sa.Column("filters", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("y_axis", sa.String(length=80), nullable=False),
        sa.Column("y_value", sa.String(length=255), nullable=False),
        sa.Column("x_axis", sa.String(length=80), nullable=False),
        sa.Column("x_value", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(length=40), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=False),
        sa.Column("source_sheet", sa.String(length=160), nullable=True),
        sa.Column("row_number", sa.Integer(), nullable=True),
        sa.Column("raw_row", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "query_hash", "scrape_date", "view", "y_axis", "y_value", "x_axis", "x_value",
            name="uq_vahan_observation_snapshot",
        ),
    )
    for name, cols in (
        ("ix_vahan_observations_query_hash", ["query_hash"]),
        ("ix_vahan_observations_scrape_date", ["scrape_date"]),
        ("ix_vahan_observations_view", ["view"]),
        ("ix_vahan_observations_state", ["state"]),
        ("ix_vahan_observations_rto", ["rto"]),
        ("ix_vahan_observations_year", ["year"]),
        ("ix_vahan_observations_y_axis", ["y_axis"]),
        ("ix_vahan_observations_y_value", ["y_value"]),
        ("ix_vahan_observations_x_axis", ["x_axis"]),
        ("ix_vahan_observations_x_value", ["x_value"]),
        ("ix_vahan_query_xy", ["query_hash", "y_axis", "x_axis"]),
        ("ix_vahan_state_year", ["state", "year"]),
    ):
        op.create_index(name, "vahan_observations", cols)


def downgrade() -> None:
    for name in (
        "ix_vahan_state_year",
        "ix_vahan_query_xy",
        "ix_vahan_observations_x_value",
        "ix_vahan_observations_x_axis",
        "ix_vahan_observations_y_value",
        "ix_vahan_observations_y_axis",
        "ix_vahan_observations_year",
        "ix_vahan_observations_rto",
        "ix_vahan_observations_state",
        "ix_vahan_observations_view",
        "ix_vahan_observations_scrape_date",
        "ix_vahan_observations_query_hash",
    ):
        op.drop_index(name, table_name="vahan_observations")
    op.drop_table("vahan_observations")
