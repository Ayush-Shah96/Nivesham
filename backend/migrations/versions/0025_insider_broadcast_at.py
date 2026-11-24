"""Add disclosure date to the existing canonical Insider Buying table.

Revision ID: 0025_insider_broadcast_at
Revises: 0024_primary_signal_tables

This migration DOES NOT create a table. `insider_signals.broadcast_at` is already
provided by migration 0015. The canonical frontend-facing `insider_transactions`
table (created in migration 0024) needs the same field for disclosure-window
filtering, so this migration alters that existing table only.
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_insider_broadcast_at"
down_revision = "0024_primary_signal_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "insider_transactions"

    columns = {column["name"] for column in inspector.get_columns(table)}
    if "broadcast_at" not in columns:
        with op.batch_alter_table(table, recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column("broadcast_at", sa.DateTime(timezone=True), nullable=True)
            )

    indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
    if "ix_insider_transactions_broadcast_at" not in indexes:
        op.create_index(
            "ix_insider_transactions_broadcast_at",
            table,
            ["broadcast_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "insider_transactions"
    indexes = {index["name"] for index in inspector.get_indexes(table)}
    if "ix_insider_transactions_broadcast_at" in indexes:
        op.drop_index("ix_insider_transactions_broadcast_at", table_name=table)

    columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
    if "broadcast_at" in columns:
        with op.batch_alter_table(table, recreate="auto") as batch_op:
            batch_op.drop_column("broadcast_at")
