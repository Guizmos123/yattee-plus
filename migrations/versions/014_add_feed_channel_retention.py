"""Add configurable retention for watched channels no longer requested.

Revision ID: 014
Revises: 013
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: Union[str, Sequence[str], None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists() -> bool:
    result = op.get_bind().execute(sa.text("PRAGMA table_info(settings)"))
    return any(row[1] == "feed_channel_retention_days" for row in result)


def upgrade() -> None:
    if not _column_exists():
        op.execute("ALTER TABLE settings ADD COLUMN feed_channel_retention_days INTEGER NOT NULL DEFAULT 14")


def downgrade() -> None:
    if _column_exists():
        op.execute("ALTER TABLE settings DROP COLUMN feed_channel_retention_days")
