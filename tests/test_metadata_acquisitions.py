"""Process-level coordination and opt-in HTTP service tests, using temporary DBs."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic import command
from alembic.config import Config

import database
from database.repositories.metadata_acquisitions import CoordinationError, MetadataStore
from innertube import _client
from innertube._metadata import MetadataAcquirer
from metadata_types import MetadataError, MetadataResult, MetadataText, retry_deadline

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
KEY = ("youtube", "video", "fr")


@pytest.fixture
def store(test_db):
    return MetadataStore(test_db, busy_timeout=0.02)


def success(key=KEY, *, title="Titre", description="Description", at=NOW, cached=False):
    source = "innertube:player" if key[2] == "original" else "innertube:next"
    return MetadataResult(
        *key,
        title=MetadataText(title, source, at) if title is not None else None,
        description=MetadataText(description, source, at) if description is not None else None,
        from_cache=cached,
        error=MetadataError.PARTIAL if title is None or description is None else None,
    )


def run_contenders(path, mode):
    """Real independent Python processes; neither parent threads nor shared memory locks."""
    script = """
import json, sys
from database.repositories.metadata_acquisitions import MetadataStore, CoordinationError
s = MetadataStore(sys.argv[1], busy_timeout=1)
language = sys.argv[3] if sys.argv[2] == 'budget' else 'fr'
print('ready', flush=True)
sys.stdin.readline()
lease = s.claim('youtube', 'contended', language)
try:
    result = bool(lease)
    if lease and sys.argv[2] == 'budget':
        result = bool(s.begin_attempt(lease, 'next', hourly_limit=1))
except CoordinationError:
    result = False
print(json.dumps(result), flush=True)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-B", "-c", script, path, mode, language],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for language in ("fr", "en")
    ]
    try:
        for process in processes:
            assert process.stdout.readline().strip() == "ready"
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()


def test_two_processes_claim_one_variant(store, test_db):
    assert sorted(run_contenders(test_db, "lease")) == [False, True]
    # Winning process exited without release: its reservation survives its death.
    row = store.read("youtube", "contended", "fr")
    assert row["owner"] and row["generation"] == 1
    expired = datetime.fromtimestamp(row["expires_at"], UTC)
    replacement = store.claim("youtube", "contended", "fr", now=expired)
    assert replacement.generation == 2


def test_expiration_fences_crashed_owner_and_release(store):
    old = store.claim(*KEY, seconds=5, now=NOW)
    assert store.claim(*KEY, now=NOW) is None
    new = store.claim(*KEY, now=NOW + timedelta(seconds=5))
    assert new.generation == old.generation + 1 and new.owner != old.owner
    assert not store.finish(old, success(title="Late"), now=NOW + timedelta(seconds=6))
    with pytest.raises(CoordinationError, match="lease_unavailable"):
        store.begin_attempt(old, "next", now=NOW + timedelta(seconds=6))
    assert store.finish(new, success(title="Current"), now=NOW + timedelta(seconds=6))
    assert not store.finish(old, None, now=NOW + timedelta(seconds=7))
    assert store.read(*KEY)["title"] == "Current"
    assert store.claim(*KEY, now=NOW + timedelta(seconds=7)).generation == 3


def test_expired_owner_cannot_write_without_replacement(store):
    lease = store.claim(*KEY, seconds=1, now=NOW)
    assert not store.finish(lease, success(), now=NOW + timedelta(seconds=1))


def test_sqlite_busy_is_bounded_and_does_not_lose_data(store, test_db):
    lease = store.claim(*KEY, now=NOW)
    with sqlite3.connect(test_db) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(CoordinationError, match="sqlite_busy"):
            store.finish(lease, success(), now=NOW)
        assert time.monotonic() - started < 1
        with pytest.raises(CoordinationError, match="sqlite_busy"):
            store.claim("youtube", "another", "fr", now=NOW)
        blocker.rollback()
    assert store.finish(lease, success(), now=NOW)


def test_shared_hourly_budget_survives_process_exit_and_expires(store, test_db):
    assert sorted(run_contenders(test_db, "budget")) == [False, True]
    # A new store has no in-memory budget state and still sees the consumed slot.
    restarted = MetadataStore(test_db)
    lease = restarted.claim("youtube", "new-video", "original")
    assert restarted.attempts_used() == 1
    with pytest.raises(CoordinationError, match="budget_exhausted"):
        restarted.begin_attempt(lease, "player", hourly_limit=1)
    future = datetime.now(UTC) + timedelta(hours=1, seconds=1)
    lease = restarted.claim("youtube", "new-video", "original", now=future)
    assert restarted.begin_attempt(lease, "player", hourly_limit=1, now=future)
    assert restarted.attempts_used(now=future) == 1


@pytest.mark.parametrize(
    "value,seconds", [("120", 120), ("Mon, 19 Oct 2026 12:02:00 GMT", 120), (None, 60), ("invalid", 60), ("-1", 0)]
)
def test_retry_after_parsing(value, seconds):
    assert retry_deadline(value, NOW) == NOW + timedelta(seconds=seconds)


@pytest.mark.parametrize("retry_after", ["120", "Mon, 19 Oct 2026 12:02:00 GMT"])
def test_pause_shared_even_after_owner_expiration(store, test_db, retry_after):
    lease = store.claim(*KEY, seconds=1, now=NOW)
    attempt = store.begin_attempt(lease, "next", now=NOW)
    store.finish_attempt(attempt, status=429, retry_after=retry_after, now=NOW + timedelta(seconds=2))
    other = MetadataStore(test_db)
    new = other.claim("youtube", "different", "en", seconds=300, now=NOW + timedelta(seconds=2))
    with pytest.raises(CoordinationError, match="provider_paused"):
        other.begin_attempt(new, "next", now=NOW + timedelta(seconds=30))
    assert other.attempts_used(now=NOW + timedelta(seconds=30)) == 1
    assert other.begin_attempt(new, "next", now=NOW + timedelta(seconds=123))


def test_partial_cache_failure_and_old_result_preserve_field_dates(store):
    first = store.claim(*KEY, now=NOW)
    store.finish(first, success(), now=NOW)
    later = NOW + timedelta(seconds=10)
    lease = store.claim(*KEY, now=later)
    store.finish(lease, success(title=None, description="New description", at=later), now=later)
    row = store.read(*KEY)
    assert row["title"] == "Titre" and row["title_acquired_at"] == NOW.timestamp()
    assert row["description"] == "New description" and row["description_acquired_at"] == later.timestamp()
    for result in (
        success(title="Old", at=NOW),
        success(title="Cache", at=later, cached=True),
        MetadataResult(*KEY, error=MetadataError.TIMEOUT),
    ):
        lease = store.claim(*KEY, now=later)
        store.finish(lease, result, now=later)
        saved = store.read(*KEY)
        assert saved["title"] == "Titre" and saved["description"] == "New description"
        assert saved["title_acquired_at"] == row["title_acquired_at"]
        assert saved["description_acquired_at"] == row["description_acquired_at"]
    assert store.read(*KEY)["owner"] is None


def test_variants_isolated_and_description_only_is_usable(store):
    for language in ("original", "fr", "en"):
        key = (*KEY[:2], language)
        lease = store.claim(*key, now=NOW)
        store.finish(lease, success(key, title=None, description=language), now=NOW)
        fields = store.stored_fields(*key)
        assert "title" not in fields and fields["description"].value == language
        assert fields["description"].acquired_at == NOW


def test_migration_016_preserves_every_historical_row(test_db):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(config, "015")
    database.upsert_feed_video_metadata(
        *KEY, title="Historical", description="", source="innertube:next", checked_at=NOW
    )
    with sqlite3.connect(test_db) as conn:
        names = [
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] != "alembic_version"
        ]
        before = {n: conn.execute(f'SELECT * FROM "{n}"').fetchall() for n in names}
    command.upgrade(config, "016")
    with sqlite3.connect(test_db) as conn:
        assert {n: conn.execute(f'SELECT * FROM "{n}"').fetchall() for n in names} == before
    database.init_db()  # Repeatable startup, including subsequent additive settings migrations.
    fields = MetadataStore(test_db).stored_fields(*KEY)
    assert fields["title"].value == "Historical" and fields["description"].value == ""
    assert fields["title"].acquired_at is None
    assert database.get_feed_video_metadata([KEY[1]])[0]["title"] == "Historical"


@pytest.fixture
def http_service(store, monkeypatch):
    import settings

    monkeypatch.setattr(settings, "get_settings", lambda: SimpleNamespace(innertube_enabled=True))
    monkeypatch.setattr(_client, "_load_youtube_cookies", AsyncMock(return_value={}))
    monkeypatch.setattr(_client, "_get_client_version", AsyncMock(side_effect=AssertionError("Hidden version GET")))
    monkeypatch.setattr(_client.asyncio, "sleep", AsyncMock())
    clients = []

    def create(handler, **kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        monkeypatch.setattr(_client, "get_client", AsyncMock(return_value=client))
        return MetadataAcquirer(store, **kwargs)

    yield create
    for client in clients:
        asyncio.run(client.aclose())


def attempts(store):
    with sqlite3.connect(store.path) as conn:
        return conn.execute("SELECT http_status, error, retry_after FROM metadata_http_attempts ORDER BY id").fetchall()


@pytest.mark.parametrize("language", ["original", "fr", "en"])
async def test_service_provenance_partial_cache_no_renewal_and_no_http_transaction(http_service, store, language):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        # Another connection can take the writer lock DURING network I/O.
        with sqlite3.connect(store.path, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        if language == "original":
            return httpx.Response(200, json={"videoDetails": {"videoId": "video", "shortDescription": "Raw"}})
        # Use the regular parser with a minimal real next renderer below.
        return httpx.Response(
            200,
            json={
                "contents": {
                    "twoColumnWatchNextResults": {
                        "results": {
                            "results": {
                                "contents": [
                                    {"videoSecondaryInfoRenderer": {"description": {"simpleText": "Localized"}}}
                                ]
                            }
                        }
                    }
                }
            },
        )

    service = http_service(respond)
    result = await service.acquire("youtube", "video", language)
    assert result.outcome == "partial" and result.error == MetadataError.PARTIAL
    assert result.title is None
    assert result.description.source == ("innertube:player" if language == "original" else "innertube:next")
    assert result.description.acquired_at is not None and not result.from_cache
    assert result.acquired_at == result.description.acquired_at
    before = store.read("youtube", "video", language)
    cached = await service.acquire("youtube", "video", language)
    assert cached.from_cache and cached.description == result.description
    assert cached.acquired_at == result.acquired_at
    assert store.read("youtube", "video", language) == before
    assert len(requests) == store.attempts_used() == 1
    assert requests[0]["context"]["client"]["hl"] == ("en" if language == "original" else language)


@pytest.mark.parametrize(
    "failure,error",
    [
        (500, MetadataError.SERVER),
        (503, MetadataError.SERVER),
        ("timeout", MetadataError.TIMEOUT),
        ("network", MetadataError.NETWORK),
    ],
)
async def test_retries_each_counted_and_release_without_erasing(http_service, store, failure, error):
    lease = store.claim(*KEY)
    store.finish(lease, success(at=datetime.now(UTC)))
    before = store.read(*KEY)

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("simulated", request=request)
        if failure == "network":
            raise httpx.ConnectError("simulated", request=request)
        return httpx.Response(failure)

    result = await http_service(respond).acquire(*KEY)
    assert result.error == error
    assert result.acquired_at is not None
    assert store.attempts_used() == 3
    assert all(row[1] == error.value for row in attempts(store))
    after = store.read(*KEY)
    assert after["title"] == before["title"] and after["title_acquired_at"] == before["title_acquired_at"]
    assert after["description"] == before["description"] and after["owner"] is None


async def test_retry_success_counts_only_real_attempts(http_service, store):
    replies = iter(
        [
            httpx.Response(500),
            httpx.Response(200, json={"videoDetails": {"title": "Original candidate", "shortDescription": "Raw"}}),
        ]
    )
    result = await http_service(lambda _: next(replies)).acquire("youtube", "video", "original")
    assert result.outcome == "complete" and result.error is None
    assert [r[0] for r in attempts(store)] == [500, 200]


@pytest.mark.parametrize("status,error", [(429, MetadataError.RATE_LIMITED), (404, MetadataError.CLIENT)])
async def test_429_shared_pause_and_durable_4xx_no_retry(http_service, store, status, error):
    service = http_service(lambda _: httpx.Response(status, headers={"Retry-After": "120"}))
    result = await service.acquire(*KEY)
    assert result.error == error and result.http_status == status
    assert store.attempts_used() == 1
    if status == 429:
        assert result.retry_after == "120"
        blocked = await service.acquire("youtube", "different-video", "en")
        assert blocked.error == MetadataError.PAUSED and store.attempts_used() == 1
    assert store.read(*KEY)["owner"] is None


async def test_budget_blocks_retry_without_counting_it(http_service, store):
    result = await http_service(lambda _: httpx.Response(500), hourly_limit=1).acquire(*KEY)
    assert result.error == MetadataError.BUDGET and store.attempts_used() == 1
    assert store.read(*KEY)["owner"] is None


async def test_json_rate_limit_keeps_actual_http_status_and_pauses(http_service, store):
    service = http_service(
        lambda _: httpx.Response(
            200, json={"error": {"code": 429, "message": "Too many requests"}}, headers={"Retry-After": "90"}
        )
    )
    result = await service.acquire(*KEY)
    assert result.error == MetadataError.RATE_LIMITED and result.http_status == 200
    assert attempts(store) == [(200, "rate_limited", "90")]
    assert (await service.acquire("youtube", "other", "en")).error == MetadataError.PAUSED


async def test_redirects_not_followed_and_invalid_json_not_retried(http_service, store):
    for video_id, response in (
        ("redirect", httpx.Response(302, headers={"Location": "https://www.youtube.com/elsewhere"})),
        ("invalid", httpx.Response(200, content=b"not-json")),
    ):
        result = await http_service(lambda _, response=response: response).acquire("youtube", video_id, "fr")
        assert result.error == MetadataError.INVALID
    assert store.attempts_used() == 2


async def test_disabled_has_no_http_and_merged_original_not_adopted(http_service, store, monkeypatch):
    import settings

    service = http_service(
        lambda _: httpx.Response(
            200, json={"description": "Merged localized text", "videoDetails": {"title": "Candidate"}}
        )
    )
    result = await service.acquire("youtube", "video", "original")
    assert result.title.value == "Candidate" and result.description is None
    monkeypatch.setattr(settings, "get_settings", lambda: SimpleNamespace(innertube_enabled=False))
    disabled = await service.acquire("youtube", "video", "original")
    assert disabled.error == MetadataError.DISABLED and store.attempts_used() == 1


async def test_cancellation_releases_lease_and_retains_unfinished_attempt(http_service, store):
    async def respond(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await http_service(respond).acquire(*KEY)
    assert store.read(*KEY)["owner"] is None and store.attempts_used() == 1
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT finished_at FROM metadata_http_attempts").fetchone()[0] is None


async def test_whole_operation_deadline_releases_before_lease_expiry(http_service, store):
    async def respond(request):
        await asyncio.Event().wait()

    result = await http_service(respond, timeout_seconds=0.01).acquire(*KEY)
    assert result.error == MetadataError.TIMEOUT and result.acquired_at is not None
    assert attempts(store) == [(None, "timeout", None)]
    row = store.read(*KEY)
    assert row["owner"] is None and row["last_acquired_at"] == result.acquired_at.timestamp()


@pytest.mark.parametrize("payload", [{"error": "bad"}, {"error": {"code": "429"}}])
async def test_malformed_provider_error_is_recorded_not_retried(http_service, store, payload):
    result = await http_service(lambda _: httpx.Response(200, json=payload)).acquire(*KEY)
    assert result.error == MetadataError.INVALID and result.http_status == 200
    assert attempts(store) == [(200, "invalid_response", None)]


async def test_disable_between_service_and_client_is_typed(http_service, store, monkeypatch):
    import settings

    service = http_service(lambda _: pytest.fail("Disabled client must not send HTTP"))
    states = iter([True, False])
    monkeypatch.setattr(settings, "get_settings", lambda: SimpleNamespace(innertube_enabled=next(states)))
    result = await service.acquire(*KEY)
    assert result.error == MetadataError.DISABLED and result.acquired_at is None
    assert store.attempts_used() == 0 and store.read(*KEY)["owner"] is None
