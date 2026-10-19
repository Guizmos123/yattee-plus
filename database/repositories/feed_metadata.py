"""Persistent Feed texts and refresh bookkeeping; no acquisition or scheduling."""

import sqlite3
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from database.connection import get_connection

_LANGUAGES = ("original", "fr", "en")


def _validate_language(language: str) -> None:
    if language not in _LANGUAGES:
        raise ValueError("Feed metadata language must be 'original', 'fr' or 'en'")


def _timestamp(value: datetime) -> str:
    """Use one UTC representation so SQLite text comparisons order correctly."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Feed metadata timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def get_feed_video_metadata(
    video_ids: List[str], site: str = "youtube", languages: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Read variants in batches, including explicit NULL and empty text values."""
    selected_languages = _LANGUAGES if languages is None else tuple(dict.fromkeys(languages))
    for language in selected_languages:
        _validate_language(language)
    if not video_ids or not selected_languages:
        return []

    unique_ids = list(dict.fromkeys(video_ids))
    rows = []
    with get_connection() as conn:
        # Stay below the 999-variable limit of older SQLite installations.
        for offset in range(0, len(unique_ids), 400):
            batch = unique_ids[offset : offset + 400]
            video_placeholders = ",".join("?" for _ in batch)
            language_placeholders = ",".join("?" for _ in selected_languages)
            cursor = conn.execute(
                f"""
                SELECT * FROM feed_video_metadata
                WHERE site = ? AND video_id IN ({video_placeholders})
                  AND language IN ({language_placeholders})
                ORDER BY video_id, language
                """,
                [site, *batch, *selected_languages],
            )
            rows.extend(dict(row) for row in cursor.fetchall())
            # New observations are exposed only through this internal reader;
            # public response shapes and the no-lang path remain unchanged.
            fresh = conn.execute(
                f"""SELECT * FROM metadata_acquisitions
                    WHERE site=? AND video_id IN ({video_placeholders})
                      AND language IN ({language_placeholders})
                      AND (title_acquired_at IS NOT NULL OR description_acquired_at IS NOT NULL)""",
                [site, *batch, *selected_languages],
            ).fetchall()
            interval = conn.execute("SELECT metadata_sync_interval_hours FROM settings WHERE id=1").fetchone()
            interval = interval[0] if interval else 24
            indexed = {(r["video_id"], r["language"]): r for r in rows}
            for observation in fresh:
                row = indexed.get((observation["video_id"], observation["language"]))
                if row is None:
                    row = {"site": site, "video_id": observation["video_id"], "language": observation["language"]}
                    rows.append(row)
                field_metadata = {}
                for field in ("title", "description"):
                    acquired = observation[f"{field}_acquired_at"]
                    if observation[field] is None or acquired is None:
                        continue
                    if row.get("checked_at") is not None:
                        try:
                            # A later legacy control date wins conservatively;
                            # neither date is a YouTube modification timestamp.
                            controlled = datetime.fromisoformat(row["checked_at"])
                            if controlled.tzinfo is not None and controlled.timestamp() > acquired:
                                continue
                        except (ValueError, TypeError):
                            pass
                    field_metadata[field] = {
                        "value": observation[field],
                        "source": observation[f"{field}_source"],
                        "acquired_at": datetime.fromtimestamp(acquired, UTC),
                        "freshness_hours": interval,
                    }
                row["_field_metadata"] = field_metadata
    return rows


def upsert_feed_video_metadata(
    site: str,
    video_id: str,
    language: str,
    *,
    title: Optional[str],
    description: Optional[str],
    source: Optional[str],
    checked_at: Optional[datetime] = None,
    next_check_at: Optional[datetime] = None,
    observed_title: Optional[str] = None,
    observed_source: Optional[str] = None,
) -> None:
    """Store a successful snapshot exactly, including None and empty strings.

    checked_at is the last successful observation. A success resets failures.
    observed_title/source describe the listing at acquisition time, not the
    localized text. Include provider/language in observed_source when known.
    Refresh timing and merging partial acquisition results belong to callers.
    """
    _validate_language(language)
    checked = _timestamp(checked_at if checked_at is not None else datetime.now(UTC))
    next_check = _timestamp(next_check_at) if next_check_at is not None else None
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO feed_video_metadata (
                site, video_id, language, title, description, source, checked_at,
                next_check_at, observed_title, observed_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site, video_id, language) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                source = excluded.source,
                checked_at = excluded.checked_at,
                next_check_at = excluded.next_check_at,
                failure_count = 0,
                observed_title = excluded.observed_title,
                observed_source = excluded.observed_source
            """,
            [
                site,
                video_id,
                language,
                title,
                description,
                source,
                checked,
                next_check,
                observed_title,
                observed_source,
            ],
        )
        conn.commit()


def get_feed_video_metadata_to_refresh(
    language: str,
    *,
    site: str = "youtube",
    limit: int = 20,
    now: Optional[datetime] = None,
    observed_source: Optional[str] = None,
    prioritize_recent: bool = False,
) -> List[Dict[str, Any]]:
    """Select cached videos with missing, due, or changed-listing variants.

    A shared video is returned once, using its most recently inserted cache row.
    Title changes bypass the due date only with a known matching listing source
    and no failures; retry backoff always takes precedence after a failure.
    Unknown/mismatching sources rely on next_check_at. No work is claimed here.
    """
    _validate_language(language)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("Refresh limit must be a positive integer")
    current_time = _timestamp(now if now is not None else datetime.now(UTC))
    ordering = "COALESCE(m.next_check_at, ''), COALESCE(m.checked_at, ''), v.video_id"
    if prioritize_recent:
        ordering = f"(m.checked_at IS NOT NULL), COALESCE(v.published, 0) DESC, v.id DESC, {ordering}"
    with get_connection() as conn:
        cursor = conn.execute(
            f"""
            WITH latest AS (
                SELECT MAX(id) AS id FROM cached_videos
                WHERE site = ? GROUP BY video_id
            )
            SELECT v.site, v.video_id, ? AS language, v.title AS current_title,
                   v.published, v.id AS cache_id,
                   m.title, m.description, m.source, m.checked_at, m.next_check_at,
                   COALESCE(m.failure_count, 0) AS failure_count,
                   m.observed_title, m.observed_source
            FROM latest
            JOIN cached_videos v ON v.id = latest.id
            LEFT JOIN feed_video_metadata m
                ON m.site = v.site AND m.video_id = v.video_id AND m.language = ?
            WHERE m.video_id IS NULL OR m.next_check_at IS NULL OR m.next_check_at <= ?
                OR (m.failure_count = 0 AND m.observed_source = ?
                    AND m.observed_title IS NOT NULL AND m.observed_title IS NOT v.title)
            ORDER BY {ordering}
            LIMIT ?
            """,
            [site, language, language, current_time, observed_source, limit],
        )
        return [dict(row) for row in cursor.fetchall()]


def observe_feed_video_metadata(site: str, videos: List[Dict[str, Any]]) -> None:
    """Remember listing title/provider without fetching or replacing any text.

    Only a title change within the same known listing source advances a refresh.
    Provider switches establish a new observation baseline; failures retain their
    retry date. Rows without a successful acquisition remain missing variants.
    """
    observed_at = _timestamp(datetime.now(UTC))
    seen = set()
    with get_connection() as conn:
        for video in videos:
            video_id = video.get("video_id")
            if not video_id or video_id in seen:
                continue
            # Match upsert_cached_videos, which keeps the first occurrence.
            seen.add(video_id)
            title = video.get("title")
            source = video.get("metadata_source")
            if not isinstance(title, str) or not isinstance(source, str) or not source:
                continue
            for language in _LANGUAGES:
                conn.execute(
                    """
                    INSERT INTO feed_video_metadata (site, video_id, language, observed_title, observed_source)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(site, video_id, language) DO UPDATE SET
                        next_check_at = CASE
                            WHEN feed_video_metadata.failure_count = 0
                             AND feed_video_metadata.observed_source = excluded.observed_source
                             AND feed_video_metadata.observed_title IS NOT NULL
                             AND feed_video_metadata.observed_title IS NOT excluded.observed_title
                            THEN ? ELSE feed_video_metadata.next_check_at END,
                        observed_title = excluded.observed_title,
                        observed_source = excluded.observed_source
                    """,
                    [site, video_id, language, title, source, observed_at],
                )
        conn.commit()


def record_feed_video_metadata_failure(
    site: str,
    video_id: str,
    language: str,
    *,
    next_check_at: datetime,
    observed_title: Optional[str] = None,
    observed_source: Optional[str] = None,
) -> None:
    """Increment failures atomically without replacing successful text/provenance.

    The caller supplies the next retry time. First failures create a NULL-text
    row with no successful checked_at; they do not masquerade as translations.
    """
    _validate_language(language)
    next_check = _timestamp(next_check_at)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO feed_video_metadata (
                site, video_id, language, next_check_at, failure_count, observed_title, observed_source
            ) VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(site, video_id, language) DO UPDATE SET
                next_check_at = excluded.next_check_at,
                failure_count = feed_video_metadata.failure_count + 1
            """,
            [site, video_id, language, next_check, observed_title, observed_source],
        )
        conn.commit()


def _cleanup_orphaned_feed_video_metadata(conn: sqlite3.Connection) -> int:
    """Purge only after cache deletions, in the same transaction as the cleanup."""
    cursor = conn.execute("""
        DELETE FROM feed_video_metadata
        WHERE NOT EXISTS (
            SELECT 1 FROM cached_videos v
            WHERE v.site = feed_video_metadata.site AND v.video_id = feed_video_metadata.video_id
        )
    """)
    return cursor.rowcount


def cleanup_orphaned_feed_video_metadata() -> int:
    """Keep variants referenced by any channel on the same site."""
    with get_connection() as conn:
        deleted = _cleanup_orphaned_feed_video_metadata(conn)
        conn.commit()
        return deleted
