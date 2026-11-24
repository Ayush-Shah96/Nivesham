"""Persist four-hour Regulatory Desk synchronization state."""

from alembic import op
import sqlalchemy as sa

revision = "0016_regulatory_sync_state"
down_revision = "0015_nse_regulatory_xml_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "regulatory_sync_state" in inspector.get_table_names():
        return

    op.create_table(
        "regulatory_sync_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("desk", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="never_run"),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_full_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_full_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("history_days", sa.Integer(), nullable=True),
        sa.Column("from_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("to_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("errors", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("desk", name="uq_regulatory_sync_state_desk"),
    )
    op.create_index(
        "ix_regulatory_sync_state_desk",
        "regulatory_sync_state",
        ["desk"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "regulatory_sync_state" not in inspector.get_table_names():
        return
    indexes = {idx["name"] for idx in inspector.get_indexes("regulatory_sync_state")}
    if "ix_regulatory_sync_state_desk" in indexes:
        op.drop_index("ix_regulatory_sync_state_desk", table_name="regulatory_sync_state")
    op.drop_table("regulatory_sync_state")
