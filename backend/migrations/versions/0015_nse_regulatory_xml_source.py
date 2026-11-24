"""Store NSE PIT source provenance and complete Details/XBRL payloads."""
from alembic import op
import sqlalchemy as sa

revision = "0015_nse_regulatory_xml_source"
down_revision = "0014_pipeline_news_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "insider_signals"
    columns = {column["name"] for column in inspector.get_columns(table)}

    additions = [
        ("source", sa.Column("source", sa.String(length=40), nullable=True)),
        ("source_key", sa.Column("source_key", sa.String(length=64), nullable=True)),
        ("source_url", sa.Column("source_url", sa.String(length=1000), nullable=True)),
        ("source_data", sa.Column("source_data", sa.JSON(), nullable=True)),
        ("regulation", sa.Column("regulation", sa.String(length=80), nullable=True)),
        ("submission_type", sa.Column("submission_type", sa.String(length=80), nullable=True)),
        ("revision_remarks", sa.Column("revision_remarks", sa.String(length=255), nullable=True)),
        ("broadcast_at", sa.Column("broadcast_at", sa.DateTime(timezone=True), nullable=True)),
        ("nse_symbol", sa.Column("nse_symbol", sa.String(length=40), nullable=True)),
        ("nse_pid", sa.Column("nse_pid", sa.String(length=80), nullable=True)),
        ("nse_did", sa.Column("nse_did", sa.String(length=80), nullable=True)),
        ("source_fetched_at", sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=True)),
    ]
    for name, column in additions:
        if name not in columns:
            op.add_column(table, column)

    # Preserve provenance of any pre-migration snapshot instead of labelling legacy
    # Trendlyne rows as NSE. The first full NSE refresh replaces the two-year snapshot.
    op.execute(
        sa.text(
            "UPDATE insider_signals "
            "SET source = 'Trendlyne' "
            "WHERE source IS NULL"
        )
    )

    existing_indexes = {index["name"] for index in inspector.get_indexes(table)}
    if "ix_insider_signals_source" not in existing_indexes:
        op.create_index("ix_insider_signals_source", table, ["source"], unique=False)
    if "ix_insider_signals_source_key" not in existing_indexes:
        op.create_index("ix_insider_signals_source_key", table, ["source_key"], unique=True)
    if "ix_insider_signals_broadcast_at" not in existing_indexes:
        op.create_index("ix_insider_signals_broadcast_at", table, ["broadcast_at"], unique=False)
    if "ix_insider_signals_nse_symbol" not in existing_indexes:
        op.create_index("ix_insider_signals_nse_symbol", table, ["nse_symbol"], unique=False)

    op.alter_column(
        table,
        "source",
        existing_type=sa.String(length=40),
        nullable=False,
        server_default="NSE",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "insider_signals"
    indexes = {index["name"] for index in inspector.get_indexes(table)}
    for name in (
        "ix_insider_signals_nse_symbol",
        "ix_insider_signals_broadcast_at",
        "ix_insider_signals_source_key",
        "ix_insider_signals_source",
    ):
        if name in indexes:
            op.drop_index(name, table_name=table)

    columns = {column["name"] for column in inspector.get_columns(table)}
    for name in (
        "source_fetched_at",
        "nse_did",
        "nse_pid",
        "nse_symbol",
        "broadcast_at",
        "revision_remarks",
        "submission_type",
        "regulation",
        "source_data",
        "source_url",
        "source_key",
        "source",
    ):
        if name in columns:
            op.drop_column(table, name)
