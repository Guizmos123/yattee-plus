"""Bounded Feed enrichment and HTTP metadata selection, with no external I/O."""

import asyncio
import copy
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
import feed_fetcher
from database.repositories import feed_metadata
from routers import subscriptions
from settings import Settings

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
LISTING_SOURCE = "innertube:browse:en"


class Clock(datetime):
    value = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def isolated_enrichment(monkeypatch):
    Clock.value = NOW
    monkeypatch.setattr(feed_fetcher, "datetime", Clock)
    monkeypatch.setattr(feed_metadata, "datetime", Clock)
    monkeypatch.setattr(feed_fetcher, "_metadata_enrichment_running", False)
    monkeypatch.setattr(feed_fetcher, "_metadata_inflight", set())
    monkeypatch.setattr(feed_fetcher, "_METADATA_DELAY", 0)
    monkeypatch.setattr(feed_fetcher, "get_settings", lambda: Settings())
    monkeypatch.setattr(
        feed_fetcher, "get_video_original_metadata", AsyncMock(side_effect=AssertionError("Unmocked player"))
    )
    monkeypatch.setattr(
        feed_fetcher, "get_video_localized_metadata", AsyncMock(side_effect=AssertionError("Unmocked next"))
    )
    monkeypatch.setattr(feed_fetcher.avatar_cache, "get_cache", MagicMock())
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("External connections are forbidden")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_connect)

    def local_dns(host, *args, **kwargs):
        raise AssertionError(f"DNS is forbidden: {host}")

    monkeypatch.setattr(socket, "getaddrinfo", local_dns)


def cache_video(video_id="video", *, title="Historical title", published=None, site="youtube", observe=True):
    video = {
        "video_id": video_id,
        "title": title,
        "author": "Auteur inchangé",
        "author_id": "UC-author",
        "length_seconds": 123,
        "view_count": 456,
        "published": int(NOW.timestamp()) if published is None else published,
        "published_text": "1 day ago",
        "thumbnail_url": "https://i.ytimg.com/thumbnail.jpg",
        "thumbnails": [{"url": "https://i.ytimg.com/thumbnail.jpg", "quality": "high"}],
        "video_url": f"https://example.com/{video_id}",
        "metadata_source": LISTING_SOURCE,
    }
    database.upsert_watched_channels([{"channel_id": video_id, "site": site}])
    database.upsert_cached_videos(video_id, site, [video])
    if observe and site == "youtube":
        database.observe_feed_video_metadata(site, [video])
    return video


def save_variant(
    language="fr", *, video_id="video", title="Ancien titre", description="Ancienne description", **changes
):
    values = {
        "source": "innertube:player" if language == "original" else "innertube:next",
        "checked_at": NOW - timedelta(days=2),
        "next_check_at": NOW + timedelta(days=1),
        "observed_title": "Historical title",
        "observed_source": LISTING_SOURCE,
    }
    values.update(changes)
    database.upsert_feed_video_metadata(
        "youtube",
        video_id,
        language,
        title=title,
        description=description,
        **values,
    )


def variants(video_id="video"):
    return {row["language"]: row for row in database.get_feed_video_metadata([video_id])}


def api_app():
    app = FastAPI()
    app.include_router(subscriptions.router, prefix="/api/v1")
    return app


@pytest.fixture
def feed_client(test_db):
    with TestClient(api_app()) as client:
        yield client


def feed_body(video_ids=("video",), **changes):
    return {"channels": [{"channel_id": video_id, "site": "youtube"} for video_id in video_ids], **changes}


@pytest.mark.asyncio
async def test_budget_recency_concurrency_and_duplicate_passes(test_db, monkeypatch):
    for i in range(35):
        cache_video(f"video-{i:02}", published=int(NOW.timestamp()) - i * 3600)
    calls, active, peak = [], 0, 0

    async def acquire(video_id, language="original"):
        nonlocal active, peak
        calls.append((video_id, language))
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001)
            return {"title": language, "description": ""}
        finally:
            active -= 1

    monkeypatch.setattr(feed_fetcher, "get_video_original_metadata", acquire)
    monkeypatch.setattr(feed_fetcher, "get_video_localized_metadata", acquire)
    await asyncio.gather(feed_fetcher.enrich_feed_video_metadata(), feed_fetcher.enrich_feed_video_metadata())
    assert len(calls) == len(set(calls)) == 30
    assert peak == 2
    assert {video_id for video_id, _ in calls} == {f"video-{i:02}" for i in range(10)}
    assert {language for _, language in calls} == {"original", "fr", "en"}
    assert not feed_fetcher._metadata_inflight and not feed_fetcher._metadata_enrichment_running
    before = set(calls)
    await feed_fetcher.enrich_feed_video_metadata()
    assert len(calls) == 60
    assert not (before & set(calls[30:]))


@pytest.mark.asyncio
async def test_missing_language_precedes_expired_variant_and_refresh_is_periodic(test_db, monkeypatch):
    cache_video("older", published=int(NOW.timestamp()) - 86400)
    cache_video("recent")
    for video_id in ("older", "recent"):
        for language in ("original", "fr", "en"):
            save_variant(
                language, video_id=video_id, next_check_at=NOW if video_id == "recent" else NOW + timedelta(days=1)
            )
    with database.get_connection() as conn:
        conn.execute("DELETE FROM feed_video_metadata WHERE video_id = 'older' AND language = 'fr'")
        conn.commit()
    monkeypatch.setattr(feed_fetcher, "_METADATA_BUDGET", 1)
    localized = AsyncMock(return_value={"title": "Neuf", "description": None})
    monkeypatch.setattr(feed_fetcher, "get_video_localized_metadata", localized)
    await feed_fetcher.enrich_feed_video_metadata()
    localized.assert_awaited_once_with("older", "fr")
    assert variants("older")["fr"]["next_check_at"] == (NOW + timedelta(days=1)).isoformat(timespec="microseconds")
    assert len(database.get_feed_video_metadata_to_refresh("original", now=NOW)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, {"title": None, "description": None}, RuntimeError("network failure")])
async def test_failure_preserves_translation_and_increases_backoff(test_db, monkeypatch, result):
    cache_video()
    for language in ("original", "fr", "en"):
        save_variant(language, description="", next_check_at=NOW if language == "fr" else NOW + timedelta(days=1))
    before = variants()["fr"]
    lookup = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr(feed_fetcher, "get_video_localized_metadata", lookup)
    await feed_fetcher.enrich_feed_video_metadata()
    row = variants()["fr"]
    assert row["failure_count"] == 1
    assert row["title"] == before["title"] and row["description"] == ""
    assert row["checked_at"] == before["checked_at"]
    assert row["next_check_at"] == (NOW + timedelta(minutes=15)).isoformat(timespec="microseconds")
    Clock.value += timedelta(minutes=15)
    await feed_fetcher.enrich_feed_video_metadata()
    row = variants()["fr"]
    assert row["failure_count"] == 2
    assert row["next_check_at"] == (Clock.value + timedelta(minutes=30)).isoformat(timespec="microseconds")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "new_text",
    [
        {"title": "Nouveau", "description": None},
        {"title": None, "description": "court"},
        {"title": "", "description": ""},
    ],
)
async def test_partial_success_preserves_missing_fields_and_accepts_short_or_empty_text(test_db, monkeypatch, new_text):
    cache_video()
    for language in ("original", "fr", "en"):
        save_variant(language, next_check_at=NOW if language == "fr" else NOW + timedelta(days=1))
    monkeypatch.setattr(feed_fetcher, "get_video_localized_metadata", AsyncMock(return_value=new_text))
    await feed_fetcher.enrich_feed_video_metadata()
    row = variants()["fr"]
    assert row["title"] == (new_text["title"] if new_text["title"] is not None else "Ancien titre")
    assert row["description"] == (
        new_text["description"] if new_text["description"] is not None else "Ancienne description"
    )
    assert row["checked_at"] == NOW.isoformat(timespec="microseconds")
    assert row["source"] == "innertube:next"


@pytest.mark.asyncio
async def test_original_acquisition_does_not_certify_old_untrusted_fields(test_db, monkeypatch):
    cache_video()
    for language in ("original", "fr", "en"):
        save_variant(
            language, source="alternative", next_check_at=NOW if language == "original" else NOW + timedelta(days=1)
        )
    original = AsyncMock(return_value={"title": "Original identifié", "description": None})
    monkeypatch.setattr(feed_fetcher, "get_video_original_metadata", original)
    await feed_fetcher.enrich_feed_video_metadata()
    row = variants()["original"]
    assert row["title"] == "Original identifié" and row["description"] is None
    assert row["source"] == "innertube:player"


@pytest.mark.asyncio
async def test_acquisition_timeout_records_failure_and_releases_pair(test_db, monkeypatch):
    cache_video()
    for language in ("original", "fr", "en"):
        save_variant(language, next_check_at=NOW if language == "fr" else NOW + timedelta(days=1))

    async def hang(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(feed_fetcher, "get_video_localized_metadata", hang)
    monkeypatch.setattr(feed_fetcher, "_METADATA_TIMEOUT", 0.01)
    await feed_fetcher.enrich_feed_video_metadata()
    assert variants()["fr"]["failure_count"] == 1
    assert not feed_fetcher._metadata_inflight and not feed_fetcher._metadata_enrichment_running


@pytest.mark.asyncio
async def test_cycle_deadline_cancels_workers_and_allows_a_later_pass(test_db, monkeypatch):
    cache_video()

    async def hang(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(feed_fetcher, "get_video_original_metadata", hang)
    monkeypatch.setattr(feed_fetcher, "get_video_localized_metadata", hang)
    monkeypatch.setattr(feed_fetcher, "_METADATA_CYCLE_TIMEOUT", 0.01)
    await feed_fetcher.enrich_feed_video_metadata()
    assert not feed_fetcher._metadata_inflight and not feed_fetcher._metadata_enrichment_running
    assert all(row["checked_at"] is None for row in variants().values())
    monkeypatch.setattr(feed_fetcher, "_METADATA_CYCLE_TIMEOUT", 1)
    monkeypatch.setattr(feed_fetcher, "get_video_original_metadata", AsyncMock(return_value={"title": "Original"}))
    monkeypatch.setattr(feed_fetcher, "get_video_localized_metadata", AsyncMock(return_value={"title": "Localisé"}))
    await feed_fetcher.enrich_feed_video_metadata()
    assert all(row["checked_at"] is not None for row in variants().values())


@pytest.mark.asyncio
async def test_request_delay_is_applied_and_disabled_innertube_skips_work(test_db, monkeypatch):
    cache_video()
    monkeypatch.setattr(feed_fetcher, "_METADATA_DELAY", 1)
    monkeypatch.setattr(feed_fetcher, "get_video_original_metadata", AsyncMock(return_value={"title": "Original"}))
    monkeypatch.setattr(feed_fetcher, "get_video_localized_metadata", AsyncMock(return_value={"title": "Localisé"}))
    with patch.object(feed_fetcher.asyncio, "sleep", new_callable=AsyncMock) as sleep:
        await feed_fetcher.enrich_feed_video_metadata()
    assert sleep.await_count == 3
    assert all(call.args == (1,) for call in sleep.await_args_list)
    monkeypatch.setattr(feed_fetcher, "get_settings", lambda: Settings(innertube_enabled=False))
    with patch.object(database, "get_feed_video_metadata_to_refresh") as select:
        await feed_fetcher.enrich_feed_video_metadata()
    select.assert_not_called()


def test_listing_observations_detect_only_same_provider_title_changes(test_db):
    video = cache_video()
    for language in ("original", "fr", "en"):
        save_variant(language)
    switched = {**video, "metadata_source": "invidious:channel:instance", "title": "Another provider title"}
    database.observe_feed_video_metadata("youtube", [switched])
    assert database.get_feed_video_metadata_to_refresh("fr", now=NOW) == []
    assert variants()["fr"]["title"] == "Ancien titre"
    database.upsert_cached_videos("video", "youtube", [{**switched, "title": "Changed by same provider"}])
    database.observe_feed_video_metadata("youtube", [{**switched, "title": "Changed by same provider"}])
    assert len(database.get_feed_video_metadata_to_refresh("fr", now=NOW)) == 1
    assert variants()["fr"]["observed_title"] == "Changed by same provider"
    assert variants()["fr"]["source"] == "innertube:next"


def test_observations_preserve_retry_date_and_ignore_unknown_sources(test_db):
    video = cache_video()
    save_variant()
    database.record_feed_video_metadata_failure("youtube", "video", "fr", next_check_at=NOW + timedelta(hours=1))
    database.observe_feed_video_metadata("youtube", [{**video, "title": "Changed"}])
    assert database.get_feed_video_metadata_to_refresh("fr", now=NOW) == []
    before = variants()["fr"]
    unknown = {**video, "title": "Unknown origin"}
    unknown.pop("metadata_source")
    database.observe_feed_video_metadata("youtube", [unknown])
    assert variants()["fr"] == before


def test_observations_keep_first_duplicate_like_the_cached_video(test_db):
    first = cache_video()
    repeated = {**first, "title": "Duplicate title must not become the baseline"}
    before = copy.deepcopy([first, repeated])
    database.observe_feed_video_metadata("youtube", [first, repeated])
    assert variants()["fr"]["observed_title"] == "Historical title"
    assert [first, repeated] == before


@pytest.mark.asyncio
async def test_observation_failure_does_not_invalidate_channel_sync(test_db):
    video = cache_video()
    with (
        patch.object(feed_fetcher, "fetch_channel_feed", AsyncMock(return_value=([video], None, None))),
        patch.object(
            database, "observe_feed_video_metadata", side_effect=sqlite3.OperationalError("failed observation")
        ),
    ):
        await feed_fetcher.fetch_single_channel("video", "youtube", "")
    with database.get_connection() as conn:
        assert conn.execute("SELECT fetch_error FROM feed_fetch_status").fetchone()[0] is None
        assert conn.execute("SELECT title FROM cached_videos").fetchone()[0] == "Historical title"


@pytest.mark.asyncio
async def test_loop_enriches_after_sync_and_cleanup_and_survives_enrichment_failure(test_db, monkeypatch):
    events = []

    async def sync():
        events.append("sync")
        cache_video()
        database.update_fetch_status("video", "youtube", success=True)

    async def enrich():
        events.append("enrich")
        raise RuntimeError("failed enrichment")

    async def stop(delay):
        events.append("wait")
        assert delay == 21600
        raise asyncio.CancelledError

    monkeypatch.setattr(feed_fetcher, "fetch_all_channels", sync)
    monkeypatch.setattr(feed_fetcher, "enrich_feed_video_metadata", enrich)
    monkeypatch.setattr(feed_fetcher.asyncio, "sleep", stop)
    for name in ("cleanup_old_cached_videos", "cleanup_stale_watched_channels", "cleanup_orphaned_cached_videos"):
        original = getattr(database, name)

        def cleanup(*args, _name=name, _original=original, **kwargs):
            events.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(database, name, cleanup)
    with pytest.raises(asyncio.CancelledError):
        await feed_fetcher.feed_fetch_loop()
    assert events == [
        "sync",
        "cleanup_old_cached_videos",
        "cleanup_stale_watched_channels",
        "cleanup_orphaned_cached_videos",
        "enrich",
        "wait",
    ]
    with database.get_connection() as conn:
        assert conn.execute("SELECT fetch_error FROM feed_fetch_status").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM cached_videos").fetchone()[0] == 1


def test_normalizers_record_provider_without_changing_thumbnail_data():
    inv = feed_fetcher._process_invidious_video({"videoId": "video"}, "channel", "https://inv.example")
    tube = feed_fetcher._process_innertube_video({"videoId": "video"}, "channel")
    assert inv["metadata_source"] == "invidious:channel:https://inv.example"
    assert tube["metadata_source"] == LISTING_SOURCE


def test_feed_without_lang_is_exact_legacy_response_without_metadata_read(feed_client):
    cache_video()
    for language in ("original", "fr", "en"):
        save_variant(language, title="Must not appear")
    with patch.object(database, "get_feed_video_metadata", side_effect=AssertionError("Unexpected metadata read")):
        response = feed_client.post("/api/v1/feed", json=feed_body())
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "videos": [
            {
                "type": "video",
                "videoId": "video",
                "title": "Historical title",
                "author": "Auteur inchangé",
                "authorId": "UC-author",
                "lengthSeconds": 123,
                "published": int(NOW.timestamp()),
                "publishedText": "1 day ago",
                "viewCount": 456,
                "videoThumbnails": [{"url": "https://i.ytimg.com/thumbnail.jpg", "quality": "high"}],
                "extractor": "youtube",
                "videoUrl": "https://example.com/video",
            }
        ],
        "total": 1,
        "has_more": False,
        "ready_count": 1,
        "pending_count": 0,
        "error_count": 0,
        "eta_seconds": None,
    }
    feed_fetcher.get_video_original_metadata.assert_not_awaited()
    feed_fetcher.get_video_localized_metadata.assert_not_awaited()


@pytest.mark.parametrize("language", ["original", "fr", "en"])
def test_feed_language_reads_one_batch_and_preserves_other_fields_and_settings(feed_client, language):
    cache_video()
    cache_video("second", published=int(NOW.timestamp()) - 3600)
    for video_id in ("video", "second"):
        for lang in ("original", "fr", "en"):
            save_variant(lang, video_id=video_id, title=f"{lang} title", description=f"{lang} description")
    body = feed_body(("video", "second"), limit=1, offset=1)
    baseline = feed_client.post("/api/v1/feed", json=body).json()
    settings_before = database.get_settings_row()
    cached_before = database.get_feed_for_channels(body["channels"])
    metadata_before = database.get_feed_video_metadata(["video", "second"])
    with patch.object(database, "get_feed_video_metadata", wraps=database.get_feed_video_metadata) as read:
        response = feed_client.post(f"/api/v1/feed?lang={language}", json=body)
    assert response.status_code == 200
    read.assert_called_once_with(["second"], languages=[language, "original"])
    expected = copy.deepcopy(baseline)
    expected["videos"][0].update(title=f"{language} title", description=f"{language} description")
    assert response.json() == expected
    assert database.get_settings_row() == settings_before
    assert database.get_feed_for_channels(body["channels"]) == cached_before
    assert database.get_feed_video_metadata(["video", "second"]) == metadata_before
    feed_fetcher.get_video_original_metadata.assert_not_awaited()
    feed_fetcher.get_video_localized_metadata.assert_not_awaited()


def test_feed_reads_multiple_videos_in_one_metadata_batch(feed_client):
    cache_video()
    cache_video("second", published=int(NOW.timestamp()) - 3600)
    for video_id in ("video", "second"):
        save_variant(video_id=video_id)
    with patch.object(database, "get_feed_video_metadata", wraps=database.get_feed_video_metadata) as read:
        response = feed_client.post("/api/v1/feed?lang=fr", json=feed_body(("video", "second")))
    assert response.status_code == 200
    assert len(response.json()["videos"]) == 2
    read.assert_called_once_with(["video", "second"], languages=["fr", "original"])


def test_metadata_read_error_preserves_a_usable_feed(feed_client):
    cache_video()
    with patch.object(
        database, "get_feed_video_metadata", side_effect=sqlite3.OperationalError("metadata read failure")
    ):
        response = feed_client.post("/api/v1/feed?lang=fr", json=feed_body())
    assert response.status_code == 200
    row = response.json()["videos"][0]
    assert row["title"] == "Historical title"
    assert "description" not in row


@pytest.mark.parametrize(
    "title,description,expected_title,expected_description",
    [
        ("Titre français", None, "Titre français", "Original description"),
        (None, "court", "Original title", "court"),
        (None, None, "Original title", "Original description"),
        ("", "", "", ""),
        (None, "<b>Texte</b> & été\nSuite", "Original title", "<b>Texte</b> & été\nSuite"),
    ],
)
def test_feed_fallbacks_are_per_field_and_preserve_empty_strings(
    feed_client,
    title,
    description,
    expected_title,
    expected_description,
):
    cache_video()
    save_variant("original", title="Original title", description="Original description")
    save_variant("fr", title=title, description=description)
    row = feed_client.post("/api/v1/feed?lang=fr", json=feed_body()).json()["videos"][0]
    assert row["title"] == expected_title
    assert row["description"] == expected_description
    assert "descriptionHtml" not in row


@pytest.mark.parametrize("language", ["fr", "en", "original"])
def test_feed_unverified_original_falls_back_to_history_and_omits_missing_description(feed_client, language):
    cache_video()
    save_variant("original", source="invidious", title="Unverified", description="Unverified")
    row = feed_client.post(f"/api/v1/feed?lang={language}", json=feed_body()).json()["videos"][0]
    assert row["title"] == "Historical title"
    assert "description" not in row


@pytest.mark.parametrize("language", ["de", "FR", "", "null"])
def test_feed_invalid_language_returns_422_without_side_effects(feed_client, language):
    with patch.object(database, "upsert_watched_channels") as write:
        response = feed_client.post("/api/v1/feed", params={"lang": language}, json=feed_body())
    assert response.status_code == 422
    write.assert_not_called()


@pytest.mark.asyncio
async def test_simultaneous_fr_en_reads_stay_isolated(test_db):
    cache_video()
    save_variant("fr", title="Français", description="Description française")
    save_variant("en", title="English", description="English description")
    before = variants()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app()), base_url="http://test") as client:
        fr, en = await asyncio.gather(
            *[client.post(f"/api/v1/feed?lang={language}", json=feed_body()) for language in ("fr", "en")]
        )
    assert fr.status_code == en.status_code == 200
    assert fr.json()["videos"][0]["title"] == "Français"
    assert en.json()["videos"][0]["title"] == "English"
    assert variants() == before


def test_non_youtube_and_empty_feeds_do_not_read_or_acquire_youtube_metadata(feed_client):
    cache_video(site="other", observe=False)
    with patch.object(database, "get_feed_video_metadata", side_effect=AssertionError("Unexpected lookup")):
        response = feed_client.post(
            "/api/v1/feed?lang=fr", json={"channels": [{"channel_id": "video", "site": "other"}]}
        )
        empty = feed_client.post("/api/v1/feed?lang=fr", json={"channels": []})
    assert response.status_code == empty.status_code == 200
    assert response.json()["videos"][0]["title"] == "Historical title"
    assert empty.json()["videos"] == []
