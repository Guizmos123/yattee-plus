"""Persist Feed video metadata independently of channel cache replacements.

Revision ID: 015
Revises: 014
"""

from typing import Sequence, Union

from alembic import op

revision: str = "015"
down_revision: Union[str, Sequence[str], None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No foreign key to cached_videos: its rows are deleted/reinserted on sync.
    op.execute("""
        CREATE TABLE IF NOT EXISTS feed_video_metadata (
            site TEXT NOT NULL,
            video_id TEXT NOT NULL,
            language TEXT NOT NULL CHECK (language IN ('original', 'fr', 'en')),
            title TEXT,
            description TEXT,
            source TEXT,
            checked_at TIMESTAMP,
            next_check_at TIMESTAMP,
            failure_count INTEGER NOT NULL DEFAULT 0
                CHECK (typeof(failure_count) = 'integer' AND failure_count >= 0),
            observed_title TEXT,
            observed_source TEXT,
            PRIMARY KEY (site, video_id, language)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_feed_metadata_due ON feed_video_metadata(site, language, next_check_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_cached_videos_site_video ON cached_videos(site, video_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_cached_videos_site_video")
    op.execute("DROP TABLE IF EXISTS feed_video_metadata")
