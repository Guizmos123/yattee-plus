"""Real HTTP route, shared acquisition and temporary SQLite integration."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI

import database
import settings
from database.repositories import metadata_acquisitions, metadata_sync
from database.repositories.metadata_acquisitions import MetadataStore
from innertube import _client
from innertube._metadata import MetadataAcquirer
from metadata_types import MetadataError, MetadataResult, MetadataText
from models import VideoResponse
from routers import videos


def response_payload(request, *, title="Acquired title", description="Acquired description"):
    if request.url.path.endswith("player"):
        return {"videoDetails": {"title": title, "shortDescription": description}, "description": "Merged text"}
    return {
        "contents": {
            "twoColumnWatchNextResults": {
                "results": {
                    "results": {
                        "contents": [
                            {"videoPrimaryInfoRenderer": {"title": {"simpleText": title}}} if title is not None else {},
                            {"videoSecondaryInfoRenderer": {"description": {"simpleText": description}}}
                            if description is not None
                            else {},
                        ]
                    }
                }
            }
        }
    }


def save(store, *, video_id="video", language="fr", title="Stored title", description="Stored description", at=None):
    at = at or datetime.now(UTC) - timedelta(seconds=1)
    source = "innertube:player" if language == "original" else "innertube:next"
    fields = {
        field: MetadataText(value, source, at)
        for field, value in (("title", title), ("description", description))
        if value is not None
    }
    lease = store.claim("youtube", video_id, language)
    store.finish(lease, MetadataResult(*lease.key, **fields, acquired_at=at))


@pytest.fixture
async def env(test_db, monkeypatch):
    store = MetadataStore(test_db)
    monkeypatch.setattr(settings, "_cached_settings", None)
    monkeypatch.setattr(videos, "validate_extractor_allowed", Mock())
    monkeypatch.setattr(videos, "get_current_user_from_request", Mock(return_value=None))
    monkeypatch.setattr(videos, "get_base_url", lambda _: "http://testserver")
    monkeypatch.setattr(videos.database, "get_site_by_extractor", lambda _: {"proxy_streaming": False})
    monkeypatch.setattr(videos.invidious_proxy, "is_enabled", lambda: False)
    monkeypatch.setattr(videos.innertube, "is_enabled", lambda: True)
    # Recommendation requests are preserved by user decision, outside META-04.
    # Keep them separate from the real, counted main-metadata transport below.
    recommendations = AsyncMock(return_value={"recommendedVideos": {}})
    monkeypatch.setattr(videos, "get_video_localized_metadata", recommendations)
    base = VideoResponse(
        videoId="video",
        title="Provider title",
        description="Provider description",
        descriptionHtml="<p>Provider</p>",
        author="Channel",
        authorId="UCchannel",
        lengthSeconds=120,
        extractionMethod="hybrid",
        formatStreams=[
            {
                "url": "https://example.invalid/video?token=retain",
                "itag": "18",
                "type": "video/mp4",
                "quality": "medium",
                "container": "mp4",
            }
        ],
        adaptiveFormats=[
            {
                "url": "https://example.invalid/audio?token=retain",
                "itag": "140",
                "type": "audio/mp4",
                "container": "m4a",
            }
        ],
        hlsUrl="https://example.invalid/hls",
        dashUrl="https://example.invalid/dash",
        captions=[{"label": "English", "languageCode": "en", "url": "https://example.invalid/captions"}],
        recommendedVideos=[
            {
                "videoId": "rec",
                "title": "Keep recommendation",
                "author": "Other",
                "authorId": "UCother",
                "lengthSeconds": 20,
            }
        ],
    )

    async def hybrid(video_id, *args, **kwargs):
        return base.model_copy(update={"videoId": video_id}, deep=True)

    monkeypatch.setattr(videos, "_get_video_hybrid", hybrid)
    requests = []
    handler = [lambda r: httpx.Response(200, json=response_payload(r))]

    async def respond(request):
        requests.append(request)
        reply = handler[0](request)
        return await reply if asyncio.iscoroutine(reply) else reply

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as network:
        monkeypatch.setattr(_client, "get_client", AsyncMock(return_value=network))
        monkeypatch.setattr(_client, "_load_youtube_cookies", AsyncMock(return_value={}))
        monkeypatch.setattr(_client, "asyncio", SimpleNamespace(sleep=AsyncMock()))
        app = FastAPI()
        app.include_router(videos.router, prefix="/api/v1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
            yield SimpleNamespace(
                store=store,
                client=client,
                base=base,
                requests=requests,
                handler=handler,
                recommendations=recommendations,
            )


async def get(env, video_id="video", **query):
    response = await env.client.get(f"/api/v1/videos/{video_id}", params=query)
    assert response.status_code == 200
    return response.json()


def assert_other_fields(env, response, **overrides):
    expected = env.base.model_dump(mode="json") | overrides
    for field in expected.keys() - {"title", "description", "descriptionHtml"}:
        assert response[field] == expected[field], field


@pytest.mark.parametrize("language", ["original", "fr", "en"])
async def test_present_variant_avoids_network_and_preserves_every_other_field(env, language):
    save(env.store, language=language)
    before = env.store.read("youtube", "video", language)
    data = await get(env, lang=language)
    assert data["title"] == "Stored title" and data["description"] == "Stored description"
    assert_other_fields(env, data)
    assert env.requests == [] and env.store.read("youtube", "video", language) == before


@pytest.mark.parametrize(
    "hours,age,expected", [(1, 2, "Provider title"), (24, 25, "Provider title"), (72, 48, "Stored title")]
)
async def test_stored_presence_does_not_introduce_swr_or_change_interval_policy(env, hours, age, expected):
    database.update_settings({"metadata_sync_interval_hours": hours})
    save(env.store, at=datetime.now(UTC) - timedelta(hours=age))
    data = await get(env, lang="fr")
    assert data["title"] == expected and env.requests == []


async def test_missing_variant_acquires_once_persists_real_dates_and_reuses(env):
    first = await get(env, lang="fr")
    stored = env.store.read("youtube", "video", "fr")
    assert first["title"] == "Acquired title" and first["description"] == "Acquired description"
    assert stored["title_source"] == "innertube:next" and stored["description_source"] == "innertube:next"
    assert stored["title_acquired_at"] and stored["title_acquired_at"] == stored["description_acquired_at"]
    assert stored["on_demand_entry"] == 1
    assert await get(env, lang="fr") == first
    assert env.store.read("youtube", "video", "fr") == stored
    assert len(env.requests) == env.store.attempts_used() == 1
    assert_other_fields(env, first)


async def test_original_fallback_is_not_mistaken_for_requested_french_presence(env):
    save(env.store, language="original")
    data = await get(env, lang="fr")
    assert data["title"] == "Acquired title" and len(env.requests) == 1


async def test_simultaneous_sheets_share_one_http_request_and_result(env):
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=response_payload(request))

    env.handler[0] = delayed
    first = asyncio.create_task(get(env, lang="fr"))
    await asyncio.wait_for(started.wait(), 1)
    second = asyncio.create_task(get(env, lang="fr"))
    await asyncio.sleep(0.05)
    release.set()
    left, right = await asyncio.gather(first, second)
    assert left == right and left["title"] == "Acquired title" and len(env.requests) == 1


@pytest.mark.parametrize("producer", ["job", "sheet"])
async def test_job_and_sheet_share_variant_lease(env, producer):
    database.upsert_cached_videos("channel", "youtube", [{"video_id": "video", "title": "Provider"}])
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=response_payload(request))

    env.handler[0] = delayed
    job = MetadataAcquirer(env.store, cache_ttl=0, sync_job=True)
    first = asyncio.create_task(job.acquire("youtube", "video", "fr") if producer == "job" else get(env, lang="fr"))
    await asyncio.wait_for(started.wait(), 1)
    second = asyncio.create_task(get(env, lang="fr") if producer == "job" else job.acquire("youtube", "video", "fr"))
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(first, second)
    sheet = results[1] if producer == "job" else results[0]
    assert sheet["title"] == "Acquired title" and len(env.requests) == 1


async def test_job_does_not_repeat_a_sheet_completed_after_batch_selection(env):
    database.upsert_cached_videos("channel", "youtube", [{"video_id": "video", "title": "Provider"}])
    stamp = datetime.now(UTC).timestamp()
    metadata_sync.seed_known_videos(env.store, stamp)
    entry = next(r for r in metadata_sync.select_batch(env.store, 24, stamp) if r["language"] == "fr")
    await get(env, lang="fr")
    acquired = await MetadataAcquirer(env.store, sync_job=True, cache_ttl=0).acquire(
        "youtube", "video", "fr", expected_generation=entry["generation"]
    )
    assert acquired.error == MetadataError.LEASE and len(env.requests) == 1


async def test_exhausted_budget_cannot_be_bypassed(env):
    database.update_settings({"metadata_sync_max_http_per_hour": 1})
    lease = env.store.claim("youtube", "budget", "en")
    env.store.begin_attempt(lease, "next", hourly_limit=1)
    env.store.finish(lease, None)
    env.recommendations.return_value = {
        "title": "Do not bypass the main-text budget", "description": "Unshared text", "recommendedVideos": {}
    }
    data = await get(env, lang="fr")
    assert data == env.base.model_dump(mode="json") and env.requests == []
    assert env.store.attempts_used() == 1
    env.recommendations.assert_awaited_once_with("video", "fr", include_recommendations=True)


async def test_429_pause_and_backoff_prevent_further_attempts(env):
    env.handler[0] = lambda _: httpx.Response(429, headers={"Retry-After": "120"})
    assert await get(env, lang="fr") == env.base.model_dump(mode="json")
    before = env.store.read("youtube", "video", "fr")
    env.handler[0] = lambda r: httpx.Response(200, json=response_payload(r))
    await get(env, lang="fr")
    await get(env, video_id="other", lang="en")
    assert len(env.requests) == 1 and env.store.attempts_used() == 1
    assert env.store.read("youtube", "video", "fr") == before


async def test_partial_response_preserves_previous_field_and_its_date(env):
    save(env.store, description=None)
    before = env.store.read("youtube", "video", "fr")
    env.handler[0] = lambda r: httpx.Response(200, json=response_payload(r, title=None, description="New description"))
    data = await get(env, lang="fr")
    stored = env.store.read("youtube", "video", "fr")
    assert data["title"] == "Stored title" and data["description"] == "New description"
    assert stored["title_acquired_at"] == before["title_acquired_at"]
    assert stored["description_acquired_at"] > before["title_acquired_at"]
    assert stored["retry_at"] > stored["description_acquired_at"]
    assert await get(env, lang="fr") == data and len(env.requests) == 1


async def test_failed_acquisition_keeps_previous_text_and_backoff(env):
    save(env.store, description=None)
    before = env.store.read("youtube", "video", "fr")
    env.handler[0] = lambda _: httpx.Response(503)
    data = await get(env, lang="fr")
    after = env.store.read("youtube", "video", "fr")
    assert data["title"] == "Stored title" and data["description"] == "Provider description"
    assert after["title_acquired_at"] == before["title_acquired_at"]
    await get(env, lang="fr")
    assert len(env.requests) == 3 and env.store.attempts_used() == 3
    assert env.store.read("youtube", "video", "fr") == after


async def test_non_feed_admission_is_bounded_without_removing_history(env, monkeypatch):
    monkeypatch.setattr(metadata_acquisitions, "MAX_ON_DEMAND_VARIANTS", 2)
    save(env.store, video_id="historical")
    historical = env.store.read("youtube", "historical", "fr")
    for video_id in ("one", "two", "three"):
        data = await get(env, video_id=video_id, lang="fr")
        assert data["title"] == ("Provider title" if video_id == "three" else "Acquired title")
    assert len(env.requests) == 2 and env.store.read("youtube", "three", "fr") is None
    with sqlite3.connect(env.store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM metadata_acquisitions WHERE on_demand_entry=1").fetchone()[0] == 2
    assert env.store.read("youtube", "historical", "fr") == historical
    # Existing Feed references use their established lifecycle, not extra slots.
    database.upsert_cached_videos("channel", "youtube", [{"video_id": "feed", "title": "Provider"}])
    assert (await get(env, video_id="feed", lang="fr"))["title"] == "Acquired title"
    assert env.store.read("youtube", "feed", "fr")["on_demand_entry"] == 0


async def test_no_lang_and_disabled_sync_do_not_acquire(env, monkeypatch):
    with monkeypatch.context() as isolated:
        isolated.setattr(videos, "_read_stored_metadata", Mock(side_effect=AssertionError("No-lang DB read")))
        assert await get(env) == env.base.model_dump(mode="json")
    database.update_settings({"metadata_sync_enabled": False})
    assert await get(env, lang="fr") == env.base.model_dump(mode="json")
    assert env.requests == [] and env.store.read("youtube", "video", "fr") is None


async def test_forced_invidious_keeps_provider_and_never_acquires(env, monkeypatch):
    monkeypatch.setattr(videos.invidious_proxy, "is_enabled", lambda: True)
    monkeypatch.setattr(videos.invidious_proxy, "get_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(videos.invidious_proxy, "get_video", AsyncMock(return_value={"videoId": "video"}))
    monkeypatch.setattr(videos, "invidious_to_video_response", lambda *a, **kw: env.base.model_copy(deep=True))
    for lang in ("original", "fr", "en"):
        data = await get(env, lang=lang, invidious="true")
        assert data == env.base.model_dump(mode="json") | {"extractionMethod": "invidious"}
    assert env.requests == []


async def test_original_uses_only_raw_player_text_without_claiming_verified_language(env):
    data = await get(env, lang="original")
    assert data["description"] == "Acquired description" and data["description"] != "Merged text"
    assert env.store.read("youtube", "video", "original")["description_source"] == "innertube:player"
    assert_other_fields(env, data)


async def test_empty_stored_strings_are_present_fields(env):
    save(env.store, title="", description="")
    data = await get(env, lang="fr")
    assert data["title"] == data["description"] == data["descriptionHtml"] == ""
    assert env.requests == []


async def test_preserved_recommendation_lookup_cannot_override_main_stored_fields(env):
    save(env.store)
    before = env.store.read("youtube", "video", "fr")
    env.recommendations.return_value = {
        "title": "Ignore this unshared main title",
        "description": "Ignore this unshared description",
        "recommendedVideos": {"rec": {"title": "Localized recommendation", "description": "Brief"}},
    }
    data = await get(env, lang="fr")
    assert data["title"] == "Stored title" and data["description"] == "Stored description"
    assert data["recommendedVideos"][0]["title"] == "Localized recommendation"
    assert data["recommendedVideos"][0]["videoId"] == "rec"
    env.recommendations.assert_awaited_once_with("video", "fr", include_recommendations=True)
    assert env.requests == [] and env.store.read("youtube", "video", "fr") == before


async def test_slow_producer_has_bounded_followers_without_duplicate_acquisition(env):
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=response_payload(request))

    env.handler[0] = delayed
    producer = asyncio.create_task(get(env, lang="fr"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        follower = await asyncio.wait_for(get(env, lang="fr"), 3)
        assert follower == env.base.model_dump(mode="json") and len(env.requests) == 1
    finally:
        release.set()
        await producer
    assert (await get(env, lang="fr"))["title"] == "Acquired title" and len(env.requests) == 1


def test_additive_migration_does_not_mark_or_remove_existing_metadata(test_db):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(config, "017")
    store = MetadataStore(test_db)
    save(store)
    before = store.read("youtube", "video", "fr")
    command.upgrade(config, "head")
    after = store.read("youtube", "video", "fr")
    assert after.pop("on_demand_entry") == 0 and after == before
