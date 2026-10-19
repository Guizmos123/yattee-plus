"""Bound admission of new non-Feed metadata, preserving every existing row.

Revision ID: 018
Revises: 017
"""

from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE metadata_acquisitions ADD COLUMN on_demand_entry INTEGER NOT NULL DEFAULT 0")
    op.execute("CREATE INDEX idx_metadata_on_demand ON metadata_acquisitions(on_demand_entry)")


def downgrade() -> None:
    op.execute("DROP INDEX idx_metadata_on_demand")
    op.execute("ALTER TABLE metadata_acquisitions DROP COLUMN on_demand_entry")
