"""Add per-user ownership to Crisp Notes while preserving legacy records.

Revision ID: 0038_crisp_notes_user_ownership
Revises: 0037_portfolio_excluded_pms
"""
from alembic import op
import sqlalchemy as sa

revision = "0038_crisp_notes_user_ownership"
down_revision = "0037_portfolio_excluded_pms"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "crisp_investor_notes" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("crisp_investor_notes")}
    if "user_id" not in columns:
        op.add_column("crisp_investor_notes", sa.Column("user_id", sa.Integer(), nullable=True))
        op.create_foreign_key(
            "fk_crisp_investor_notes_user_id_users",
            "crisp_investor_notes", "users", ["user_id"], ["id"], ondelete="SET NULL"
        )
        op.create_index("ix_crisp_investor_notes_user_id", "crisp_investor_notes", ["user_id"])

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "crisp_investor_notes" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("crisp_investor_notes")}
    if "user_id" in columns:
        op.drop_index("ix_crisp_investor_notes_user_id", table_name="crisp_investor_notes")
        op.drop_constraint("fk_crisp_investor_notes_user_id_users", "crisp_investor_notes", type_="foreignkey")
        op.drop_column("crisp_investor_notes", "user_id")
