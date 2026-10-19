"""Add opt-in metadata coordination, field observations and HTTP accounting.

Revision ID: 016
Revises: 015
"""

from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deliberately separate from legacy Feed rows: their checked_at does not
    # establish an actual network acquisition time for each individual field.
    op.execute("""
        CREATE TABLE metadata_acquisitions (
            site TEXT NOT NULL,
            video_id TEXT NOT NULL,
            language TEXT NOT NULL CHECK (language IN ('original', 'fr', 'en')),
            owner TEXT,
            expires_at REAL,
            generation INTEGER NOT NULL DEFAULT 0,
            title TEXT, title_source TEXT, title_acquired_at REAL,
            description TEXT, description_source TEXT, description_acquired_at REAL,
            last_outcome TEXT, last_error TEXT, last_http_status INTEGER,
            last_retry_after TEXT, last_finished_at REAL, last_acquired_at REAL,
            PRIMARY KEY (site, video_id, language)
        )
    """)
    op.execute("""
        CREATE TABLE metadata_http_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site TEXT NOT NULL, video_id TEXT NOT NULL, language TEXT NOT NULL,
            generation INTEGER NOT NULL, endpoint TEXT NOT NULL,
            started_at REAL NOT NULL, finished_at REAL,
            http_status INTEGER, error TEXT, retry_after TEXT
        )
    """)
    op.execute("CREATE INDEX idx_metadata_http_time ON metadata_http_attempts(started_at)")
    op.execute("""
        CREATE TABLE metadata_provider_pauses (
            provider TEXT PRIMARY KEY,
            paused_until REAL NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE metadata_provider_pauses")
    op.execute("DROP TABLE metadata_http_attempts")
    op.execute("DROP TABLE metadata_acquisitions")
