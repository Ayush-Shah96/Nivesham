"""Compatibility marker for the previously applied legacy-signals migration.

The original 0010_remove_legacy_signals.py was applied to some databases and later
removed from the repository. Those databases still record this revision in
alembic_version, so Alembic cannot traverse the history without a script carrying the
same revision id. The current baseline metadata no longer creates the legacy signal
objects, so there is intentionally no operation here.
"""

revision = "0010_remove_legacy_signals"
down_revision = "0009_add_is_interview"
branch_labels = None
depends_on = None

def upgrade():
    pass

def downgrade():
    pass
