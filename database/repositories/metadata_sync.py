"""Metadata job queues and aggregate state. All SQL transactions are short.

Field deadlines derive from their own observation time and the live interval.
Legacy checked_at is only a scheduling baseline, never a network timestamp.
"""

from datetime import UTC, datetime

from database.repositories.metadata_acquisitions import MetadataStore
from metadata_types import MetadataError

LEADER_KEY = ("internal", "metadata-sync", "original")
CONTROL_ERRORS = {
    MetadataError.LEASE,
    MetadataError.BUSY,
    MetadataError.BUDGET,
    MetadataError.PAUSED,
    MetadataError.DISABLED,
    MetadataError.BACKOFF,
    MetadataError.CAPACITY,
}


def seed_known_videos(store: MetadataStore, now: float) -> None:
    with store._db(write=True) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO metadata_acquisitions
                (site, video_id, language, queued_at, failure_count, retry_at)
            SELECT v.site, v.video_id, l.language, ?, COALESCE(m.failure_count, 0),
                   CASE WHEN m.failure_count>0 THEN (julianday(m.next_check_at)-2440587.5)*86400 END
            FROM (SELECT DISTINCT site, video_id FROM cached_videos WHERE site='youtube') v
            CROSS JOIN (SELECT 'original' AS language UNION ALL SELECT 'fr' UNION ALL SELECT 'en') l
            LEFT JOIN feed_video_metadata m USING(site, video_id, language)
        """,
            (now,),
        )
        conn.execute("UPDATE metadata_acquisitions SET queued_at=? WHERE site='youtube' AND queued_at=0", (now,))


def _queue_sql() -> str:
    return """
        WITH videos AS (
            SELECT site, video_id, MIN(channel_id) AS channel_id FROM cached_videos
            WHERE site='youtube' GROUP BY site, video_id
        ), fields AS (
            SELECT v.site, v.video_id, l.language, v.channel_id, a.owner, a.expires_at,
                COALESCE(a.generation, 0) AS generation,
                COALESCE(NULLIF(a.queued_at, 0), :now) AS queued_at, a.last_attempt_at,
                COALESCE(a.failure_count, m.failure_count, 0) AS failure_count,
                COALESCE(a.retry_at, CASE WHEN a.video_id IS NULL AND m.failure_count>0
                    THEN (julianday(m.next_check_at)-2440587.5)*86400 END) AS retry_at,
                CASE WHEN COALESCE(a.title, CASE WHEN l.language!='original' OR m.source='innertube:player'
                                      THEN m.title END) IS NOT NULL THEN 1 END AS effective_title,
                CASE WHEN COALESCE(a.description, CASE WHEN l.language!='original' OR m.source='innertube:player'
                                            THEN m.description END) IS NOT NULL THEN 1 END AS effective_description,
                COALESCE(a.title_acquired_at, (julianday(m.checked_at)-2440587.5)*86400) AS title_time,
                COALESCE(a.description_acquired_at, (julianday(m.checked_at)-2440587.5)*86400) AS desc_time,
                CASE WHEN a.title_acquired_at IS NULL THEN MIN(:interval, 86400) ELSE :interval END AS title_interval,
                CASE WHEN a.description_acquired_at IS NULL THEN MIN(:interval, 86400)
                     ELSE :interval END AS desc_interval,
                CASE WHEN a.title_acquired_at IS NULL
                     THEN (julianday(m.next_check_at)-2440587.5)*86400 END AS legacy_title_due,
                CASE WHEN a.description_acquired_at IS NULL
                     THEN (julianday(m.next_check_at)-2440587.5)*86400 END AS legacy_desc_due
            FROM videos v
            CROSS JOIN (SELECT 'original' AS language UNION ALL SELECT 'fr' UNION ALL SELECT 'en') l
            LEFT JOIN metadata_acquisitions a USING(site, video_id, language)
            LEFT JOIN feed_video_metadata m USING(site, video_id, language)
        ), deadlines AS (
            SELECT *, MIN(CASE WHEN effective_title IS NULL OR title_time IS NULL
                              THEN queued_at ELSE MIN(title_time+title_interval,
                                                       COALESCE(legacy_title_due, 1e20)) END,
                          CASE WHEN effective_description IS NULL OR desc_time IS NULL
                              THEN queued_at ELSE MIN(desc_time+desc_interval,
                                                       COALESCE(legacy_desc_due, 1e20)) END) AS due
            FROM fields
        ), categorized AS (
            SELECT *, CASE WHEN failure_count>0 THEN 'retry'
                           WHEN effective_title IS NULL OR effective_description IS NULL THEN 'missing'
                           ELSE 'renew' END AS category,
                      MAX(due, COALESCE(retry_at, 0)) AS eligible_at
            FROM deadlines
        ) SELECT * FROM categorized
    """


def queue_snapshot(store: MetadataStore, interval_hours: int, now: float) -> list[dict]:
    with store._db() as conn:
        return [dict(row) for row in conn.execute(_queue_sql(), {"interval": interval_hours * 3600, "now": now})]


def select_batch(store: MetadataStore, interval_hours: int, now: float, limit=30) -> list[dict]:
    rows = [
        row
        for row in queue_snapshot(store, interval_hours, now)
        if row["eligible_at"] <= now and (row["owner"] is None or row["expires_at"] <= now)
    ]
    queues = {}
    for category in ("missing", "renew", "retry"):
        group = [row for row in rows if row["category"] == category]
        # Persistent FIFO within deadline; ties rotate across channels/languages.
        group.sort(
            key=lambda r: (
                r["eligible_at"],
                r["last_attempt_at"] or 0,
                r["queued_at"],
                r["channel_id"],
                r["video_id"],
                r["language"],
            )
        )
        ranks = {}
        for row in group:
            key = row["channel_id"], row["language"]
            row["rotation"] = ranks.get(key, 0)
            ranks[key] = row["rotation"] + 1
        group.sort(
            key=lambda r: (
                r["eligible_at"],
                r["rotation"],
                r["last_attempt_at"] or 0,
                ("original", "fr", "en").index(r["language"]),
                r["channel_id"],
                r["video_id"],
            )
        )
        queues[category] = group
    result = []
    # A repeated ten-slot pattern also protects renewals/retries if a cycle is
    # interrupted early. Empty categories lend their unused slots immediately.
    pattern = ("missing", "renew", "missing", "renew", "missing", "retry", "missing", "renew", "missing", "renew")
    with store._db() as conn:
        offset = conn.execute("SELECT dispatch_cursor FROM metadata_sync_state WHERE id=1").fetchone()[0]
    for index in range(limit):
        preferred = pattern[(offset + index) % 10]
        queue = queues[preferred]
        if not queue:
            queue = next((queues[k] for k in ("renew", "retry", "missing") if queues[k]), [])
        if not queue:
            break
        result.append(queue.pop(0))
    return result


def record_result(conn, lease, result, now: float) -> None:
    """Called inside META-02's fenced finish transaction, before lease release."""
    if result.from_cache or result.error in CONTROL_ERRORS:
        return
    complete = result.outcome == "complete"
    count = conn.execute(
        "SELECT failure_count FROM metadata_acquisitions WHERE site=? AND video_id=? AND language=?", lease.key
    ).fetchone()[0]
    failures = 0 if complete else count + 1
    retry_at = None if complete else now + min(900 * 2 ** min(count, 7), 86400)
    conn.execute(
        """
        UPDATE metadata_acquisitions SET last_attempt_at=?, failure_count=?, retry_at=?
        WHERE site=? AND video_id=? AND language=?
    """,
        (now, failures, retry_at, *lease.key),
    )
    usable = result.outcome != "failed"
    conn.execute(
        """
        UPDATE metadata_sync_state SET successes=successes+?, failures=failures+?, partials=partials+?,
            last_success=CASE WHEN ? THEN ? ELSE last_success END WHERE id=1
    """,
        (int(usable), int(not usable), int(result.outcome == "partial"), int(usable), now),
    )


def mark_run(store: MetadataStore, now: float) -> None:
    with store._db(write=True) as conn:
        conn.execute("UPDATE metadata_sync_state SET last_run=? WHERE id=1", (now,))


def maintenance_due(store: MetadataStore, now: float) -> bool:
    with store._db() as conn:
        last = conn.execute("SELECT last_cleanup FROM metadata_sync_state WHERE id=1").fetchone()[0]
        return last is None or last <= now - 3600


def prune_journal(store: MetadataStore, now: float) -> None:
    """Hourly maintenance, or bounded catch-up batches; never video metadata.

    Retain seven days (well beyond the rolling budget), including crash records.
    """
    with store._db(write=True) as conn:
        last = conn.execute("SELECT last_cleanup FROM metadata_sync_state WHERE id=1").fetchone()[0]
        if last is not None and last > now - 3600:
            return
        deleted = conn.execute(
            """DELETE FROM metadata_http_attempts WHERE id IN (
            SELECT id FROM metadata_http_attempts WHERE started_at<? ORDER BY started_at LIMIT 1000
        )""",
            (now - 7 * 86400,),
        )
        if deleted.rowcount < 1000:
            conn.execute("UPDATE metadata_sync_state SET last_cleanup=? WHERE id=1", (now,))


def get_status(settings, store=None, *, now=None) -> dict:
    store = store or MetadataStore()
    stamp = datetime.now(UTC).timestamp() if now is None else now
    rows = queue_snapshot(store, settings.metadata_sync_interval_hours, stamp)
    pending = [r for r in rows if r["due"] <= stamp]
    with store._db() as conn:
        state = dict(conn.execute("SELECT * FROM metadata_sync_state WHERE id=1").fetchone())
        pause = conn.execute("SELECT paused_until FROM metadata_provider_pauses WHERE provider='innertube'").fetchone()
        active = (
            conn.execute(
                """SELECT 1 FROM metadata_acquisitions
            WHERE site=? AND video_id=? AND language=? AND owner IS NOT NULL AND expires_at>?""",
                (*LEADER_KEY, stamp),
            ).fetchone()
            is not None
        )
        budget = conn.execute(
            """SELECT COUNT(*), MIN(started_at) FROM metadata_http_attempts
            WHERE started_at>?""",
            (stamp - 3600,),
        ).fetchone()
        budget_resume = None
        if budget[0] >= settings.metadata_sync_max_http_per_hour:
            budget_resume = (
                conn.execute(
                    """SELECT started_at FROM metadata_http_attempts WHERE started_at>?
                   ORDER BY started_at LIMIT 1 OFFSET ?""",
                    (stamp - 3600, budget[0] - settings.metadata_sync_max_http_per_hour),
                ).fetchone()[0]
                + 3600
            )
    pause_until = pause[0] if pause and pause[0] > stamp else None
    enabled = settings.metadata_sync_enabled
    suspended = enabled and (not settings.innertube_enabled or pause_until is not None or budget_resume is not None)
    ready_at = min((r["eligible_at"] for r in rows), default=None)
    resume = max(stamp, ready_at or stamp, pause_until or 0, budget_resume or 0)

    def iso(value):
        return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None

    return {
        "enabled": enabled,
        "state": "suspended" if suspended else "running" if active and enabled else "idle",
        "last_run": iso(state["last_run"]),
        "last_success": iso(state["last_success"]),
        "successes": state["successes"],
        "failures": state["failures"],
        "partials": state["partials"],
        "pending": len(pending),
        "oldest_due": iso(min((r["due"] for r in pending), default=None)),
        "http_used": budget[0],
        "http_limit": settings.metadata_sync_max_http_per_hour,
        "provider_paused_until": iso(pause_until),
        "next_resume": iso(resume) if enabled and settings.innertube_enabled and rows else None,
    }
