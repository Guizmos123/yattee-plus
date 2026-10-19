"""Autonomous metadata job, fairness, persistence and admin integration."""

import asyncio
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
import metadata_sync
import settings
from database.repositories import metadata_sync as repository
from database.repositories.metadata_acquisitions import MetadataStore
from innertube import _client
from metadata_selection import select_stored_metadata
from metadata_types import MetadataError, MetadataResult, MetadataText
from routers.admin.deps import get_current_admin
from routers.admin.settings import router

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def cache(video="video", channel="channel"):
    # The production upsert replaces a channel snapshot, so keep its other
    # videos when building multi-video fixtures.
    with database.get_connection() as conn:
        videos = [
            dict(row)
            for row in conn.execute(
                "SELECT video_id, title FROM cached_videos WHERE channel_id=? AND site='youtube'", (channel,)
            )
        ]
    database.upsert_cached_videos(channel, "youtube", [*videos, {"video_id": video, "title": "Provider"}])


def result(key, at=NOW, *, title="Title", description="Description", error=None):
    source = "innertube:player" if key[2] == "original" else "innertube:next"
    fields = {
        name: MetadataText(value, source, at)
        for name, value in (("title", title), ("description", description))
        if value is not None
    }
    return MetadataResult(*key, **fields, acquired_at=at, error=error)


def save(store, video, language="fr", at=NOW, **kwargs):
    key = "youtube", video, language
    lease = store.claim(*key, now=at)
    assert store.finish(lease, result(key, at, **kwargs), now=at, sync_job=True)


@pytest.fixture
def store(test_db):
    return MetadataStore(test_db)


@pytest.mark.parametrize("hours", [1, 24, 72])
def test_interval_deadlines_survive_restart_and_do_not_requeue_fresh(store, test_db, hours):
    cache()
    repository.seed_known_videos(store, NOW.timestamp())
    for language in ("original", "fr", "en"):
        save(store, "video", language)
    restarted = MetadataStore(test_db)
    before = (NOW + timedelta(hours=hours, seconds=-1)).timestamp()
    assert repository.select_batch(restarted, hours, before) == []
    due = repository.select_batch(restarted, hours, (NOW + timedelta(hours=hours)).timestamp())
    assert len(due) == 3 and all(r["category"] == "renew" for r in due)


def test_fair_50_40_10_and_borrowing_under_continuous_new_arrivals(store):
    for index in range(20):
        for category in ("missing", "renew", "retry"):
            video = f"{category}-{index}"
            cache(video, f"channel-{index % 3}")
            if category == "renew":
                save(store, video, at=NOW - timedelta(days=2))
            elif category == "retry":
                save(store, video, at=NOW - timedelta(days=2), title=None, description=None, error=MetadataError.SERVER)
    stamp = NOW.timestamp()
    repository.seed_known_videos(store, stamp)
    for cycle in range(3):
        cache(f"new-{cycle}")
        repository.seed_known_videos(store, stamp)
        batch = repository.select_batch(store, 24, stamp)
        assert {c: sum(r["category"] == c for r in batch) for c in ("missing", "renew", "retry")} == {
            "missing": 15,
            "renew": 12,
            "retry": 3,
        }
        # Finish one old retry/renewal per cycle despite continuous new entries.
        for row in batch:
            if row["category"] in ("retry", "renew"):
                save(store, row["video_id"], row["language"])
                break
    # When there is only missing work the entire lot is usable.
    with sqlite3.connect(store.path) as conn:
        conn.execute("DELETE FROM cached_videos WHERE video_id NOT LIKE 'new-%'")
    borrowed = repository.select_batch(store, 24, stamp)
    assert len(borrowed) == 9 and all(r["category"] == "missing" for r in borrowed)


def test_missing_fifo_prevents_new_entries_jumping_old_and_rotates_channels(store):
    for channel in ("a", "b"):
        for video in ("1", "2"):
            cache(f"{channel}-{video}", channel)
    repository.seed_known_videos(store, NOW.timestamp())
    cache("new", "a")
    repository.seed_known_videos(store, NOW.timestamp() + 1)
    batch = repository.select_batch(store, 24, NOW.timestamp() + 1)
    assert all(row["video_id"] != "new" for row in batch[:12])
    assert len({r["channel_id"] for r in batch[:2]}) == 2
    assert {r["language"] for r in batch[:6]} == {"original", "fr", "en"}


def test_fair_cursor_survives_one_attempt_hourly_budget_and_restart(store):
    for video in ("old-a", "old-b"):
        cache(video)
        for language in ("original", "fr", "en"):
            save(store, video, language, NOW - timedelta(days=2))
    cache("retry")
    save(store, "retry", at=NOW - timedelta(days=2), title=None, description=None, error=MetadataError.SERVER)
    selected = []
    for hour in range(10):
        stamp = NOW + timedelta(hours=hour)
        cache(f"continuous-{hour}")
        restarted = MetadataStore(store.path)
        repository.seed_known_videos(restarted, stamp.timestamp())
        entry = repository.select_batch(restarted, 24, stamp.timestamp())[0]
        selected.append(entry["category"])
        key = entry["site"], entry["video_id"], entry["language"]
        lease = restarted.claim(*key, now=stamp)
        restarted.begin_attempt(lease, "next", hourly_limit=1, advance_cursor=True, now=stamp)
        restarted.finish(lease, result(key, stamp), now=stamp, sync_job=True)
    assert {c: selected.count(c) for c in ("missing", "renew", "retry")} == {"missing": 5, "renew": 4, "retry": 1}


@pytest.mark.parametrize("body_error", [False, True])
async def test_server_lifespan_starts_and_awaits_metadata_job(monkeypatch, body_error):
    import server

    events = []
    for target in (
        "database.init_db",
        "env_provisioning.apply_env_provisioning",
        "cookie_health.start_task",
        "cookie_health.stop_task",
        "proxy.cleanup_old_files_sync",
        "proxy.start_cleanup_task",
        "feed_fetcher.start_feed_fetcher",
        "feed_fetcher.stop_feed_fetcher",
        "avatar_cache.start_avatar_cleanup_task",
        "avatar_cache.stop_avatar_cleanup_task",
    ):
        module, name = target.split(".")
        monkeypatch.setattr(getattr(server, module), name, lambda: None)
    monkeypatch.setattr(server.pot_provider.manager, "apply_settings", AsyncMock())
    monkeypatch.setattr(server.pot_provider.manager, "stop", AsyncMock())
    monkeypatch.setattr(server, "get_settings", lambda: settings.Settings())
    monkeypatch.setattr(metadata_sync, "start_metadata_sync", lambda: events.append("start"))

    async def stop():
        events.append("stop")

    monkeypatch.setattr(metadata_sync, "stop_metadata_sync", stop)
    try:
        async with server.lifespan(server.app):
            assert events == ["start"]
            if body_error:
                raise RuntimeError("application exit")
    except RuntimeError:
        assert body_error
    assert events == ["start", "stop"]


def test_partial_fields_keep_independent_deadlines_and_backoff(store):
    cache()
    repository.seed_known_videos(store, NOW.timestamp())
    save(store, "video", at=NOW - timedelta(days=3))
    save(store, "video", at=NOW, title=None, description="New", error=MetadataError.PARTIAL)
    row = next(r for r in repository.queue_snapshot(store, 72, NOW.timestamp()) if r["language"] == "fr")
    assert row["due"] == NOW.timestamp() and row["eligible_at"] == NOW.timestamp() + 900
    assert row["category"] == "retry"
    assert not any(r["language"] == "fr" for r in repository.select_batch(store, 72, NOW.timestamp()))
    assert any(r["language"] == "fr" for r in repository.select_batch(store, 72, NOW.timestamp() + 900))
    fields = store.stored_fields("youtube", "video", "fr")
    assert fields["title"].acquired_at == NOW - timedelta(days=3)
    assert fields["description"].acquired_at == NOW
    variants = {r["language"]: r for r in database.get_feed_video_metadata(["video"])}
    selected = select_stored_metadata({"title": "Provider", "description": "Current"}, "fr", variants, now=NOW)
    assert "title" not in selected and selected["description"] == "New"


def test_admission_uses_current_persistent_policy_in_same_transaction(store):
    database.update_settings({"metadata_sync_max_http_per_hour": 1})
    lease = store.claim("youtube", "video", "fr", now=NOW)
    store.begin_attempt(lease, "next", hourly_limit=150, sync_job=True, advance_cursor=True, now=NOW)
    from database.repositories.metadata_acquisitions import CoordinationError

    with pytest.raises(CoordinationError, match="budget_exhausted"):
        store.begin_attempt(lease, "next", hourly_limit=150, sync_job=True, now=NOW)
    database.update_settings({"metadata_sync_enabled": False})
    with pytest.raises(CoordinationError, match="disabled"):
        store.begin_attempt(lease, "next", hourly_limit=150, sync_job=True, now=NOW)
    assert store.attempts_used(now=NOW) == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT dispatch_cursor FROM metadata_sync_state").fetchone()[0] == 1


def test_status_budget_resume_accounts_for_lowering_the_limit(store):
    cache()
    for seconds in (300, 200, 100):
        stamp = NOW - timedelta(seconds=seconds)
        lease = store.claim("youtube", "video", "fr", now=stamp)
        store.begin_attempt(lease, "next", now=stamp)
        store.finish(lease, None, now=stamp)
    status = repository.get_status(settings.Settings(metadata_sync_max_http_per_hour=1), store, now=NOW.timestamp())
    assert status["next_resume"] == (NOW + timedelta(seconds=3500)).isoformat()


def test_backoff_persists_errors_and_late_owner_cannot_change_counters(store):
    cache()
    save(store, "video")
    later = NOW + timedelta(seconds=1)
    save(store, "video", at=later, title=None, description=None, error=MetadataError.TIMEOUT)
    row = store.read("youtube", "video", "fr")
    assert row["title"] == "Title" and row["failure_count"] == 1
    assert row["retry_at"] == later.timestamp() + 900
    old = store.claim("youtube", "video", "fr", now=later, seconds=1)
    replacement = store.claim("youtube", "video", "fr", now=later + timedelta(seconds=1))
    assert not store.finish(old, result(old.key), now=later + timedelta(seconds=1), sync_job=True)
    assert store.read(*replacement.key)["failure_count"] == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT successes, failures FROM metadata_sync_state").fetchone() == (1, 1)


def test_new_field_freshness_72_is_isolated_from_legacy_and_not_faked(store):
    cache()
    at = NOW - timedelta(hours=48)
    save(store, "video", at=at)
    database.update_settings({"metadata_sync_interval_hours": 72})
    row = database.get_feed_video_metadata(["video"], languages=["fr"])[0]
    assert "checked_at" not in row  # No fabricated legacy control date.
    assert select_stored_metadata({"title": "Provider"}, "fr", {"fr": row}, now=NOW)["title"] == "Title"
    assert "title" not in select_stored_metadata({"title": "Provider"}, "fr", {"fr": row}, now=at + timedelta(hours=72))
    legacy = {"title": "Legacy", "source": "innertube:next", "checked_at": at}
    assert select_stored_metadata({"title": "Provider"}, "fr", {"fr": legacy}, now=NOW) == {}
    assert select_stored_metadata({"title": "Provider"}, None, {"fr": row}, now=NOW) == {}
    assert store.read("youtube", "video", "fr")["title_acquired_at"] == at.timestamp()


def test_newer_legacy_observation_is_not_overridden(store):
    cache()
    save(store, "video", at=NOW - timedelta(hours=2))
    database.upsert_feed_video_metadata(
        "youtube", "video", "fr", title="Later", description=None, source="innertube:next", checked_at=NOW
    )
    row = database.get_feed_video_metadata(["video"], languages=["fr"])[0]
    assert select_stored_metadata({"title": "Provider"}, "fr", {"fr": row}, now=NOW)["title"] == "Later"


def test_legacy_transition_keeps_24h_and_earlier_due_without_inventing_acquisition_dates(store):
    cache()
    checked = NOW - timedelta(hours=25)
    database.upsert_feed_video_metadata(
        "youtube",
        "video",
        "fr",
        title="Legacy",
        description="Legacy desc",
        source="innertube:next",
        checked_at=checked,
        next_check_at=checked + timedelta(hours=24),
    )
    batch = repository.select_batch(store, 72, NOW.timestamp())
    assert any(r["language"] == "fr" and r["category"] == "renew" for r in batch)
    database.upsert_feed_video_metadata(
        "youtube",
        "video",
        "en",
        title="Changed",
        description="Description",
        source="innertube:next",
        checked_at=NOW - timedelta(hours=1),
        next_check_at=NOW,
    )
    assert any(r["language"] == "en" for r in repository.select_batch(store, 72, NOW.timestamp() + 0.001))
    assert store.stored_fields("youtube", "video", "fr")["title"].acquired_at is None


def test_journal_retention_is_batched_and_never_deletes_video_fields(store):
    cache()
    save(store, "video")
    with sqlite3.connect(store.path) as conn:
        conn.executemany(
            """INSERT INTO metadata_http_attempts
            (site, video_id, language, generation, endpoint, started_at) VALUES ('youtube','v','fr',1,'next',?)""",
            [(NOW.timestamp() - 8 * 86400,)] * 1001 + [(NOW.timestamp() - 60,)] * 2,
        )
    repository.prune_journal(store, NOW.timestamp())
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM metadata_http_attempts").fetchone()[0] == 3
    repository.prune_journal(store, NOW.timestamp())
    assert store.attempts_used(now=NOW) == 2
    assert store.stored_fields("youtube", "video", "fr")["title"].value == "Title"


def test_migration_from_016_preserves_texts_and_recovers_failed_work(store):
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(cfg, "016")
    database.upsert_feed_video_metadata(
        "youtube", "video", "fr", title="Historical", description="", source="innertube:next", checked_at=NOW
    )
    lease = store.claim("youtube", "video", "en", now=NOW)
    store.finish(lease, MetadataResult(*lease.key, error=MetadataError.SERVER), now=NOW)
    command.upgrade(cfg, "head")
    row = store.read("youtube", "video", "en")
    assert row["failure_count"] == 1 and row["retry_at"] == NOW.timestamp() + 900
    assert store.stored_fields("youtube", "video", "fr")["title"].value == "Historical"
    assert settings.load_settings().metadata_sync_interval_hours == 24


def payload(request):
    if request.url.path.endswith("player"):
        return {"videoDetails": {"title": "Original candidate", "shortDescription": "Raw"}}
    return {
        "contents": {
            "twoColumnWatchNextResults": {
                "results": {
                    "results": {
                        "contents": [
                            {"videoPrimaryInfoRenderer": {"title": {"simpleText": "Localized"}}},
                            {"videoSecondaryInfoRenderer": {"description": {"simpleText": "Description"}}},
                        ]
                    }
                }
            }
        }
    }


@pytest.fixture
def service_env(store, monkeypatch):
    current = [settings.Settings()]
    monkeypatch.setattr(settings, "load_settings", lambda: current[0])
    monkeypatch.setattr(settings, "get_settings", lambda: current[0])
    monkeypatch.setattr(_client, "_load_youtube_cookies", AsyncMock(return_value={}))
    monkeypatch.setattr(_client, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    monkeypatch.setattr(metadata_sync, "_wake", None)
    clients = []

    def prepare(handler=None):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler or (lambda r: httpx.Response(200, json=payload(r))))
        )
        clients.append(client)
        monkeypatch.setattr(_client, "get_client", AsyncMock(return_value=client))
        return metadata_sync.MetadataSync(store)

    yield current, prepare
    for client in clients:
        asyncio.run(client.aclose())


async def test_autonomous_job_never_scans_feed_and_disabled_does_no_work(store, service_env, monkeypatch):
    current, prepare = service_env
    cache()
    monkeypatch.setattr("feed_fetcher.fetch_all_channels", AsyncMock(side_effect=AssertionError("Feed called")))
    sync = prepare()
    current[0] = settings.Settings(metadata_sync_enabled=False)
    await sync.run_cycle(current[0])
    assert store.read("youtube", "video", "fr") is None and store.attempts_used() == 0
    current[0] = settings.Settings()
    await sync.run_cycle(current[0])
    assert store.attempts_used() == 3
    assert all(store.read("youtube", "video", lang)["title"] for lang in ("original", "fr", "en"))
    generation = store.read(*repository.LEADER_KEY)["generation"]
    # A fresh worker/cycle must not repeat these successful observations.
    await prepare().run_cycle(current[0])
    assert store.attempts_used() == 3
    assert store.read(*repository.LEADER_KEY)["generation"] == generation


async def test_budget_150_counts_retries_and_is_shared_across_cycles(store, service_env):
    current, prepare = service_env
    for index in range(60):
        cache(str(index))
    sync = prepare(lambda _: httpx.Response(500))
    await sync.run_cycle(current[0])
    await sync.run_cycle(current[0])
    await prepare().run_cycle(current[0])
    assert store.attempts_used() == 150
    status = repository.get_status(current[0], store)
    assert status["state"] == "suspended" and status["http_used"] == 150 and status["next_resume"]
    assert status["failures"] == 50


async def test_429_pause_and_recovery_without_reset_or_extra_retry(store, service_env):
    current, prepare = service_env
    cache()
    sync = prepare(lambda _: httpx.Response(429, headers={"Retry-After": "120"}))
    await sync.run_cycle(current[0])
    used = store.attempts_used()
    assert 1 <= used <= 2  # Already in flight concurrency is bounded by two.
    status = repository.get_status(current[0], store)
    assert status["state"] == "suspended" and status["provider_paused_until"]
    await sync.run_cycle(current[0])
    assert store.attempts_used() == used
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE metadata_provider_pauses SET paused_until=0")
    await prepare().run_cycle(current[0])
    assert store.attempts_used() > used  # Other variants can advance; failed one still backs off.


async def test_two_workers_use_one_coordinator_and_fence_renewal(store, service_env):
    current, prepare = service_env
    cache()
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=payload(request))

    first = prepare(respond)
    running = asyncio.create_task(first.run_cycle(current[0]))
    await asyncio.wait_for(entered.wait(), timeout=2)
    other = metadata_sync.MetadataSync(MetadataStore(store.path))
    await other.run_cycle(current[0])
    assert repository.get_status(current[0], store)["state"] == "running"
    assert store.renew(first.leader)
    release.set()
    await running
    assert store.attempts_used() == 3
    assert not store.renew(type("Expired", (), {"key": repository.LEADER_KEY, "owner": "old", "generation": 0})())


async def test_disable_while_inflight_cancels_requests_and_releases_both_leases(store, service_env, monkeypatch):
    current, prepare = service_env
    cache()
    entered = asyncio.Event()

    async def respond(request):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(metadata_sync, "_wake", asyncio.Event())
    sync = prepare(respond)
    task = asyncio.create_task(sync.run_cycle(current[0]))
    await asyncio.wait_for(entered.wait(), 2)
    current[0] = settings.Settings(metadata_sync_enabled=False)
    metadata_sync.notify_settings_changed()
    await asyncio.wait_for(task, 2)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM metadata_acquisitions WHERE owner IS NOT NULL").fetchone()[0] == 0


async def test_settings_edit_wakes_sleep_and_shutdown_is_awaited(store, service_env, monkeypatch):
    current, prepare = service_env
    current[0] = settings.Settings(metadata_sync_enabled=False)
    called = asyncio.Event()
    real_cycle = metadata_sync.MetadataSync.run_cycle

    async def cycle(self, policy):
        if policy.metadata_sync_enabled:
            called.set()
        await real_cycle(self, policy)

    monkeypatch.setattr(metadata_sync.MetadataSync, "run_cycle", cycle)
    prepare()
    metadata_sync.start_metadata_sync()
    first = metadata_sync._task
    try:
        await asyncio.sleep(0.01)
        assert not called.is_set()
        current[0] = settings.Settings(metadata_sync_interval_hours=72)
        metadata_sync.notify_settings_changed()
        await asyncio.wait_for(called.wait(), 1)
        metadata_sync.start_metadata_sync()
        assert metadata_sync._task is first
    finally:
        await metadata_sync.stop_metadata_sync()
    assert first.done() and metadata_sync._task is None


def test_admin_settings_status_auth_validation_and_reload(store, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_admin] = lambda: {"is_admin": True}
    monkeypatch.setattr(settings, "_cached_settings", None)
    with TestClient(app) as client:
        assert client.get("/api/settings").json()["metadata_sync_max_http_per_hour"] == 150
        for hours in (1, 24, 72):
            response = client.put(
                "/api/settings",
                json={
                    "metadata_sync_interval_hours": hours,
                    "metadata_sync_enabled": False,
                    "metadata_sync_max_http_per_hour": 200,
                },
            )
            assert response.status_code == 200
            settings.invalidate_cache()
            assert client.get("/api/settings").json()["metadata_sync_interval_hours"] == hours
        for key, invalid in (
            ("metadata_sync_interval_hours", 0),
            ("metadata_sync_interval_hours", 73),
            ("metadata_sync_interval_hours", 1.5),
            ("metadata_sync_interval_hours", "24"),
            ("metadata_sync_max_http_per_hour", True),
            ("metadata_sync_max_http_per_hour", 0),
            ("metadata_sync_max_http_per_hour", 10001),
            ("metadata_sync_enabled", 1),
        ):
            assert client.put("/api/settings", json={key: invalid}).status_code == 422
        cache()
        status = client.get("/api/metadata-sync/status").json()
        assert not status["enabled"] and status["pending"] == 3 and status["oldest_due"]
        assert set(status) == {
            "enabled",
            "state",
            "last_run",
            "last_success",
            "successes",
            "failures",
            "partials",
            "pending",
            "oldest_due",
            "http_used",
            "http_limit",
            "provider_paused_until",
            "next_resume",
        }
    app.dependency_overrides.clear()
    with TestClient(app) as client:
        assert client.get("/api/metadata-sync/status").status_code in (401, 403)


def test_two_real_job_processes_share_coordination_and_persistent_deadlines(store):
    cache()
    script = """
import asyncio, sys, httpx
from unittest.mock import AsyncMock
from database.repositories.metadata_acquisitions import MetadataStore
from innertube import _client
import metadata_sync, settings
async def respond(request):
    await asyncio.sleep(0.15)
    if request.url.path.endswith('player'):
        data={'videoDetails': {'title':'Title', 'shortDescription':'Description'}}
    else:
        data={'contents':{'twoColumnWatchNextResults':{'results':{'results':{'contents':[
            {'videoPrimaryInfoRenderer':{'title':{'simpleText':'Title'}}},
            {'videoSecondaryInfoRenderer':{'description':{'simpleText':'Description'}}}]}}}}}
    return httpx.Response(200,json=data)
async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        _client.get_client=AsyncMock(return_value=client)
        _client._load_youtube_cookies=AsyncMock(return_value={})
        await metadata_sync.MetadataSync(MetadataStore(sys.argv[1])).run_cycle(settings.load_settings())
print('ready',flush=True)
sys.stdin.readline()
asyncio.run(main())
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-B", "-c", script, store.path],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DATA_DIR": str(Path(store.path).parent)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        for process in processes:
            _, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
        assert store.attempts_used() == 3
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
