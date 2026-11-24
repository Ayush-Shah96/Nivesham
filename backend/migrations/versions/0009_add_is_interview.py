"""Add is_interview field to articles table.

Revision ID: 0009_add_is_interview
Revises: 0008_pe_warrant_signals
Create Date: 2024-09-25 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0009_add_is_interview'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add is_interview column to articles table
    op.add_column('articles', sa.Column('is_interview', sa.Boolean(), nullable=False, server_default='false'))
    
    # Update existing interviews based on title patterns
    op.execute("""
        UPDATE articles 
        SET is_interview = true 
        WHERE title ILIKE '%interview%' 
           OR title ILIKE '%interviewed%'
           OR title ILIKE '%interview with%'
           OR title ILIKE '%conversation%'
           OR title ILIKE '%fireside%'
           OR title ILIKE '%q&a%'
           OR title ILIKE '%qa session%'
    """)


def downgrade() -> None:
    # Remove is_interview column
    op.drop_column('articles', 'is_interview')