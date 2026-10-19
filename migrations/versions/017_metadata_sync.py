"""Add autonomous metadata settings and persistent scheduling state.

Revision ID: 017
Revises: 016
"""

from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE settings ADD COLUMN metadata_sync_enabled INTEGER NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE settings ADD COLUMN metadata_sync_interval_hours INTEGER NOT NULL DEFAULT 24")
    op.execute("ALTER TABLE settings ADD COLUMN metadata_sync_max_http_per_hour INTEGER NOT NULL DEFAULT 150")
    op.execute("ALTER TABLE metadata_acquisitions ADD COLUMN queued_at REAL NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE metadata_acquisitions ADD COLUMN last_attempt_at REAL")
    op.execute("ALTER TABLE metadata_acquisitions ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE metadata_acquisitions ADD COLUMN retry_at REAL")
    # Recover technical failures from META-02 without inventing field observations.
    op.execute("""UPDATE metadata_acquisitions SET
        failure_count=1, retry_at=last_finished_at+900, last_attempt_at=last_finished_at
        WHERE last_outcome IN ('failed', 'partial') AND last_error IN
            ('rate_limited', 'timeout', 'server', 'client', 'network', 'invalid_response', 'partial')
    """)
    op.execute("""CREATE TABLE metadata_sync_state (
        id INTEGER PRIMARY KEY CHECK (id=1), last_run REAL, last_success REAL,
        successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
        partials INTEGER NOT NULL DEFAULT 0, last_cleanup REAL,
        dispatch_cursor INTEGER NOT NULL DEFAULT 0
    )""")
    op.execute("INSERT INTO metadata_sync_state(id) VALUES (1)")


def downgrade() -> None:
    op.execute("DROP TABLE metadata_sync_state")
    for name in ("queued_at", "last_attempt_at", "failure_count", "retry_at"):
        op.execute(f"ALTER TABLE metadata_acquisitions DROP COLUMN {name}")
    for name in ("metadata_sync_enabled", "metadata_sync_interval_hours", "metadata_sync_max_http_per_hour"):
        op.execute(f"ALTER TABLE settings DROP COLUMN {name}")
