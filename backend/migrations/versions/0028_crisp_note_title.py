"""Add a user-editable title to Crisp Notes transcripts.

Revision ID: 0028_crisp_note_title
Revises: 0027_newsalert_custom_sources
"""
from alembic import op
import sqlalchemy as sa

revision = "0028_crisp_note_title"
down_revision = "0027_newsalert_custom_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crisp_investor_notes",
        sa.Column("title", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crisp_investor_notes", "title")