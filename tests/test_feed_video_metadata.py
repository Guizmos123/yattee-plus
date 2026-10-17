"""SQLite-only tests for persistent multilingual Feed metadata."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

import database

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
LISTING_SOURCE = "innertube:browse:en"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("Network access is forbidden in SQLite metadata tests")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)


def cache_video(channel="channel-a", site="youtube", video_id="video", title="Listed title"):
    database.upsert_cached_videos(channel, site, [{"video_id": video_id, "title": title}])


def save_metadata(video_id="video", language="fr", site="youtube", **changes):
    values = {
        "title": "Titre français — été 🎬",
        "description": "Première ligne\nDeuxième ligne : <texte> & ponctuation !",
        "source": "innertube:next",
        "checked_at": NOW,
        "next_check_at": LATER,
        "observed_title": "Listed title",
        "observed_source": LISTING_SOURCE,
    }
    values.update(changes)
    database.upsert_feed_video_metadata(site, video_id, language, **values)


def read_metadata(video_id="video", language="fr", site="youtube"):
    rows = database.get_feed_video_metadata([video_id], site=site, languages=[language])
    return rows[0] if rows else None


def snapshot_existing_tables():
    with database.get_connection() as conn:
        names = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            if row[0] != "alembic_version"
        ]
        return {name: [tuple(row) for row in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in names}


def test_migration_from_014_is_additive_and_preserves_existing_rows(test_db):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(config, "014")
    database.create_user("existing-user", "existing-password-hash")
    database.update_settings({"feed_channel_retention_days": 30})
    database.upsert_watched_channels([{"channel_id": "channel-a", "site": "youtube"}])
    cache_video()
    database.update_fetch_status("channel-a", "youtube", success=True)
    before = snapshot_existing_tables()

    command.upgrade(config, "015")
    after = snapshot_existing_tables()
    assert after.pop("feed_video_metadata") == []
    assert after == before
    with database.get_connection() as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "015"
        assert conn.execute("PRAGMA foreign_key_list(feed_video_metadata)").fetchall() == []
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(cached_videos)")}
        assert "idx_cached_videos_site_video" in indexes


def test_initialization_is_repeatable_and_preserves_variants(test_db):
    save_metadata()
    before = read_metadata()
    database.init_db()
    assert read_metadata() == before


def test_metadata_survives_a_new_python_process(test_db):
    save_metadata(description="")
    environment = {**os.environ, "DATA_DIR": str(Path(test_db).parent)}
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import json, database; print(json.dumps(database.get_feed_video_metadata(['video'])))",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == database.get_feed_video_metadata(["video"])


def test_cache_replacement_preserves_all_variants_and_does_not_duplicate_them(test_db):
    cache_video()
    for language in ("original", "fr", "en"):
        save_metadata(language=language)
    before = database.get_feed_video_metadata(["video"])
    for title in ("Renamed", "Renamed again"):
        cache_video(title=title)
        assert database.get_feed_video_metadata(["video"]) == before
    assert database.get_feed_for_channels([{"channel_id": "channel-a", "site": "youtube"}])[0]["title"] == title


def test_upsert_is_isolated_by_site_video_and_language(test_db):
    for site, video_id, language in (
        ("youtube", "video", "original"),
        ("youtube", "video", "fr"),
        ("youtube", "video", "en"),
        ("other", "video", "fr"),
        ("youtube", "second", "fr"),
    ):
        save_metadata(video_id, language, site, title=f"{site}:{video_id}:{language}")
    others = [read_metadata(language="en"), read_metadata(language="original"), read_metadata(site="other")]
    save_metadata(title="Updated", description="Short")
    assert read_metadata()["title"] == "Updated"
    assert read_metadata()["description"] == "Short"
    assert [read_metadata(language="en"), read_metadata(language="original"), read_metadata(site="other")] == others
    assert len(database.get_feed_video_metadata(["video"])) == 3
    assert read_metadata("second")["title"] == "youtube:second:fr"


@pytest.mark.parametrize("title,description", [(None, None), ("", ""), (None, ""), ("", None)])
def test_null_and_empty_text_remain_distinct_on_insert_and_update(test_db, title, description):
    save_metadata(title=title, description=description)
    assert (read_metadata()["title"], read_metadata()["description"]) == (title, description)
    save_metadata(title="Intermediate", description="Intermediate")
    save_metadata(title=title, description=description)
    assert (read_metadata()["title"], read_metadata()["description"]) == (title, description)


def test_unicode_newlines_and_punctuation_are_preserved(test_db):
    save_metadata()
    row = read_metadata()
    assert row["title"] == "Titre français — été 🎬"
    assert row["description"] == "Première ligne\nDeuxième ligne : <texte> & ponctuation !"


@pytest.mark.parametrize("language", ["de", "FR", "", None])
def test_invalid_languages_are_rejected_by_repository_and_sqlite(test_db, language):
    with pytest.raises(ValueError):
        save_metadata(language=language)
    with pytest.raises(ValueError):
        database.get_feed_video_metadata(["video"], languages=[language])
    with pytest.raises(ValueError):
        database.get_feed_video_metadata_to_refresh(language)
    with pytest.raises(ValueError):
        database.record_feed_video_metadata_failure("youtube", "video", language, next_check_at=LATER)
    with database.get_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO feed_video_metadata(site, video_id, language) VALUES ('youtube', 'video', ?)", [language]
        )


def test_sqlite_enforces_unique_variant_and_nonnegative_failure_count(test_db):
    save_metadata()
    with database.get_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO feed_video_metadata(site, video_id, language) VALUES ('youtube', 'video', 'fr')")
    for invalid in (-1, 0.5, "invalid", None):
        with database.get_connection() as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE feed_video_metadata SET failure_count = ?", [invalid])


def test_batch_read_filters_languages_deduplicates_ids_and_handles_large_pages(test_db):
    save_metadata(language="en")
    save_metadata(language="fr")
    save_metadata(video_id="outside")
    ids = ["video", *[f"missing-{i}" for i in range(1000)], "video"]
    assert database.get_feed_video_metadata(ids, languages=["fr", "fr"]) == [read_metadata()]
    assert database.get_feed_video_metadata([]) == []
    assert database.get_feed_video_metadata(["video"], languages=[]) == []


def test_refresh_selection_includes_missing_and_due_but_not_fresh_or_uncached(test_db):
    for video_id in ("missing", "due", "fresh", "unscheduled"):
        cache_video(channel=video_id, video_id=video_id)
    save_metadata("due", next_check_at=NOW)
    save_metadata("fresh")
    save_metadata("unscheduled", next_check_at=None)
    save_metadata("uncached", next_check_at=NOW - timedelta(days=1))
    due = database.get_feed_video_metadata_to_refresh("fr", now=NOW)
    assert {row["video_id"] for row in due} == {"missing", "due", "unscheduled"}
    missing = next(row for row in due if row["video_id"] == "missing")
    assert missing["language"] == "fr"
    assert missing["title"] is None and missing["checked_at"] is None
    assert missing["failure_count"] == 0


def test_refresh_selection_is_bounded_and_isolated_by_site_and_language(test_db):
    for video_id in ("a", "b", "c"):
        cache_video(channel=video_id, video_id=video_id)
        save_metadata(video_id, language="fr")
    cache_video(site="other", video_id="foreign")
    assert database.get_feed_video_metadata_to_refresh("fr", now=NOW) == []
    assert len(database.get_feed_video_metadata_to_refresh("en", now=NOW, limit=2)) == 2
    foreign = database.get_feed_video_metadata_to_refresh("en", site="other", now=NOW)
    assert [row["video_id"] for row in foreign] == ["foreign"]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_refresh_selection_rejects_unbounded_or_invalid_limits(test_db, limit):
    with pytest.raises(ValueError):
        database.get_feed_video_metadata_to_refresh("fr", limit=limit)


def test_shared_video_is_selected_once_using_latest_listing_observation(test_db):
    cache_video(channel="first", title="First title")
    cache_video(channel="second", title="Latest title")
    rows = database.get_feed_video_metadata_to_refresh("fr", now=NOW)
    assert len(rows) == 1
    assert rows[0]["current_title"] == "Latest title"


def test_only_comparable_title_change_advances_refresh(test_db):
    cache_video(title="Changed title")
    save_metadata()
    assert database.get_feed_video_metadata_to_refresh("fr", now=NOW) == []
    assert database.get_feed_video_metadata_to_refresh("fr", now=NOW, observed_source="invidious:en") == []
    rows = database.get_feed_video_metadata_to_refresh("fr", now=NOW, observed_source=LISTING_SOURCE)
    assert len(rows) == 1
    assert rows[0]["observed_title"] == "Listed title"
    assert rows[0]["current_title"] == "Changed title"


def test_unknown_observed_title_does_not_claim_a_title_change(test_db):
    cache_video(title="")
    save_metadata(observed_title=None)
    assert database.get_feed_video_metadata_to_refresh("fr", now=NOW, observed_source=LISTING_SOURCE) == []
    save_metadata(observed_title="Listed title")
    assert len(database.get_feed_video_metadata_to_refresh("fr", now=NOW, observed_source=LISTING_SOURCE)) == 1


def test_failure_preserves_successful_metadata_and_enforces_retry_date(test_db):
    cache_video(title="Changed title")
    save_metadata(description="")
    save_metadata(language="en")
    before = read_metadata()
    english = read_metadata(language="en")
    for count in (1, 2):
        database.record_feed_video_metadata_failure(
            "youtube", "video", "fr", next_check_at=LATER, observed_title="New listing", observed_source="other"
        )
        after = read_metadata()
        assert after["failure_count"] == count
        assert {k: v for k, v in after.items() if k != "failure_count"} == {
            k: v for k, v in before.items() if k != "failure_count"
        }
    assert read_metadata(language="en") == english
    assert database.get_feed_video_metadata_to_refresh("fr", now=NOW, observed_source=LISTING_SOURCE) == []
    assert len(database.get_feed_video_metadata_to_refresh("fr", now=LATER, observed_source=LISTING_SOURCE)) == 1
    save_metadata(title="Recovered")
    assert read_metadata()["failure_count"] == 0
    assert read_metadata()["title"] == "Recovered"


def test_first_failure_creates_retry_state_without_fabricated_text(test_db):
    cache_video()
    database.record_feed_video_metadata_failure(
        "youtube", "video", "fr", next_check_at=LATER, observed_title="Listed title", observed_source=LISTING_SOURCE
    )
    row = read_metadata()
    assert (row["title"], row["description"], row["source"], row["checked_at"]) == (None, None, None, None)
    assert row["failure_count"] == 1
    assert database.get_feed_video_metadata_to_refresh("fr", now=NOW) == []
    assert len(database.get_feed_video_metadata_to_refresh("fr", now=LATER)) == 1


def test_timestamps_are_normalized_to_utc_for_due_comparisons(test_db):
    cache_video()
    offset_time = NOW.astimezone(timezone(timedelta(hours=2)))
    save_metadata(checked_at=offset_time, next_check_at=offset_time)
    assert read_metadata()["checked_at"] == NOW.isoformat(timespec="microseconds")
    assert database.get_feed_video_metadata_to_refresh("fr", now=NOW - timedelta(microseconds=1)) == []
    assert len(database.get_feed_video_metadata_to_refresh("fr", now=NOW)) == 1


def test_naive_timestamps_are_rejected_without_writing(test_db):
    with pytest.raises(ValueError):
        save_metadata(checked_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        save_metadata(next_check_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        database.record_feed_video_metadata_failure("youtube", "video", "fr", next_check_at=NOW.replace(tzinfo=None))
    assert read_metadata() is None


def test_purge_waits_for_cleanup_and_preserves_shared_video_after_replacement(test_db):
    cache_video(channel="first")
    cache_video(channel="second")
    for language in ("original", "fr", "en"):
        save_metadata(language=language)
    database.upsert_cached_videos("first", "youtube", [])
    assert database.cleanup_orphaned_feed_video_metadata() == 0
    assert len(database.get_feed_video_metadata(["video"])) == 3
    database.upsert_cached_videos("second", "youtube", [])
    # Replacement does not purge between individual channel synchronizations.
    assert len(database.get_feed_video_metadata(["video"])) == 3
    assert database.cleanup_old_cached_videos() == 0
    assert database.get_feed_video_metadata(["video"]) == []


def test_purge_uses_site_and_keeps_variants_referenced_by_any_channel(test_db):
    cache_video(site="youtube")
    save_metadata(site="youtube")
    save_metadata(site="other")
    save_metadata(video_id="unreferenced")
    assert database.cleanup_orphaned_feed_video_metadata() == 2
    assert read_metadata() is not None
    assert read_metadata(site="other") is None
    assert database.cleanup_orphaned_feed_video_metadata() == 0


@pytest.mark.parametrize("shared", [False, True])
def test_age_cleanup_preserves_variant_only_if_another_cached_copy_remains(test_db, shared):
    cache_video(channel="old")
    save_metadata()
    with database.get_connection() as conn:
        conn.execute("UPDATE cached_videos SET fetched_at = datetime('now', '-60 days')")
        conn.commit()
    if shared:
        cache_video(channel="recent")
    assert database.cleanup_old_cached_videos(days=30) == 1
    assert (read_metadata() is not None) == shared


@pytest.mark.parametrize("shared", [False, True])
def test_orphan_cleanup_preserves_variant_only_if_watched_channel_still_references_it(test_db, shared):
    cache_video(channel="removed")
    save_metadata()
    if shared:
        database.upsert_watched_channels([{"channel_id": "watched", "site": "youtube"}])
        cache_video(channel="watched")
    assert database.cleanup_orphaned_cached_videos() == 1
    assert (read_metadata() is not None) == shared


def test_metadata_cleanup_rolls_back_together_with_video_cleanup(test_db):
    cache_video()
    save_metadata()
    with database.get_connection() as conn:
        conn.execute("""
            CREATE TRIGGER prevent_metadata_delete BEFORE DELETE ON feed_video_metadata
            BEGIN SELECT RAISE(ABORT, 'simulated cleanup failure'); END
        """)
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated cleanup failure"):
        database.cleanup_orphaned_cached_videos()
    assert read_metadata() is not None
    assert len(database.get_feed_for_channels([{"channel_id": "channel-a", "site": "youtube"}])) == 1
