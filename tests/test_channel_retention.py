"""Settings, migration and cleanup tests for inactive channel retention."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import database
import feed_fetcher
import settings
from routers.admin import settings as admin_settings

RETENTION = "feed_channel_retention_days"


@pytest.fixture(autouse=True)
def clear_runtime_settings():
    settings.invalidate_cache()
    yield
    settings.invalidate_cache()


@pytest.fixture
def settings_client(test_db):
    app = FastAPI()
    app.include_router(admin_settings.router)
    app.dependency_overrides[admin_settings.get_current_admin] = lambda: {"id": 1, "is_admin": True}
    with TestClient(app) as client:
        yield client


def test_default_retention_is_fourteen_days(test_db):
    assert settings.Settings().feed_channel_retention_days == 14
    assert settings.load_settings().feed_channel_retention_days == 14
    assert database.get_settings_row()[RETENTION] == 14


@pytest.mark.parametrize("value", [1, 14, 30])
def test_model_accepts_retention_boundaries(value):
    assert settings.Settings(**{RETENTION: value}).feed_channel_retention_days == value


@pytest.mark.parametrize("value", [0, 31, -1, -100, "invalid", 1.5, True, None])
def test_model_rejects_invalid_retention(value):
    with pytest.raises(ValidationError):
        settings.Settings(**{RETENTION: value})


@pytest.mark.parametrize("value", [1, 14, 30])
def test_api_saves_retention_without_changing_other_settings(settings_client, value):
    before = settings_client.get("/api/settings").json()
    response = settings_client.put("/api/settings", json={RETENTION: value})
    assert response.status_code == 200
    after = response.json()
    assert after[RETENTION] == value
    assert {key: val for key, val in after.items() if key != RETENTION} == {
        key: val for key, val in before.items() if key != RETENTION
    }
    settings.invalidate_cache()
    assert settings_client.get("/api/settings").json()[RETENTION] == value


@pytest.mark.parametrize("value", [0, 31, -1, "invalid", 1.5, True])
def test_api_rejects_invalid_retention_without_saving(settings_client, value):
    before = database.get_settings_row()
    response = settings_client.put("/api/settings", json={RETENTION: value})
    assert response.status_code == 422
    assert database.get_settings_row() == before


def test_api_rejects_null_retention_without_saving(settings_client):
    before = database.get_settings_row()
    response = settings_client.put("/api/settings", json={RETENTION: None})
    assert response.status_code == 400
    assert database.get_settings_row() == before


@pytest.mark.parametrize("value", [1, 14, 30])
def test_retention_survives_fresh_python_process(test_db, value):
    settings.save_settings(settings.load_settings().model_copy(update={RETENTION: value}))
    environment = {**os.environ, "DATA_DIR": str(Path(test_db).parent)}
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import settings; print(settings.get_settings().feed_channel_retention_days)"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(value)


def add_channel(channel_id, age_days):
    database.upsert_watched_channels([{"channel_id": channel_id, "site": "youtube", "channel_name": channel_id}])
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE watched_channels SET last_requested = datetime('now', ?) WHERE channel_id = ?",
            (f"-{age_days} days", channel_id),
        )
        connection.commit()
    database.upsert_cached_videos(channel_id, "youtube", [{"video_id": channel_id + "-video", "title": "Cached"}])
    database.update_fetch_status(channel_id, "youtube", success=True)


def test_existing_installation_migration_preserves_data_and_sets_default(test_db):
    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(alembic_config, "013")
    database.update_settings({"cache_video_ttl": 7200, "feed_video_max_age": 90})
    add_channel("existing-channel", 20)
    old_settings = database.get_settings_row()
    old_channels = database.get_all_watched_channels()
    assert RETENTION not in old_settings

    command.upgrade(alembic_config, "head")
    migrated = database.get_settings_row()
    assert migrated[RETENTION] == 14
    assert {key: migrated[key] for key in old_settings} == old_settings
    assert set(migrated) - set(old_settings) == {
        RETENTION,
        "metadata_sync_enabled",
        "metadata_sync_interval_hours",
        "metadata_sync_max_http_per_hour",
    }
    assert database.get_all_watched_channels() == old_channels
    with database.get_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cached_videos").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM feed_fetch_status").fetchone()[0] == 1
    assert settings.load_settings().feed_channel_retention_days == 14


@pytest.mark.parametrize("retention", [1, 14, 30])
def test_cleanup_respects_retention_for_already_inactive_channels(test_db, retention):
    add_channel("inactive-channel", 20)
    add_channel("active-channel", 0)
    settings.save_settings(settings.load_settings().model_copy(update={RETENTION: retention}))
    deleted = database.cleanup_stale_watched_channels(days=settings.get_settings().feed_channel_retention_days)
    assert deleted == (0 if retention == 30 else 1)
    remaining = {row["channel_id"] for row in database.get_all_watched_channels()}
    assert "active-channel" in remaining
    assert ("inactive-channel" in remaining) == (retention == 30)


def test_changing_retention_applies_to_existing_channels_and_associated_videos(test_db):
    add_channel("inactive-channel", 20)
    add_channel("active-channel", 0)
    settings.save_settings(settings.load_settings().model_copy(update={RETENTION: 30}))
    assert database.cleanup_stale_watched_channels(days=settings.get_settings().feed_channel_retention_days) == 0

    settings.save_settings(settings.get_settings().model_copy(update={RETENTION: 1}))
    assert database.cleanup_stale_watched_channels(days=settings.get_settings().feed_channel_retention_days) == 1
    # Keep the existing two-step cleanup: channel removal, then orphaned videos.
    with database.get_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cached_videos").fetchone()[0] == 2
    assert database.cleanup_orphaned_cached_videos() == 1
    with database.get_connection() as connection:
        assert connection.execute("SELECT channel_id FROM cached_videos").fetchone()[0] == "active-channel"
        # Existing fetch-status retention is deliberately unchanged.
        assert connection.execute("SELECT COUNT(*) FROM feed_fetch_status").fetchone()[0] == 2


def test_client_request_renews_channel_before_cleanup(test_db):
    add_channel("requested-again", 20)
    database.upsert_watched_channels([{"channel_id": "requested-again", "site": "youtube"}])
    assert database.cleanup_stale_watched_channels(days=1) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("retention", [1, 14, 30])
async def test_background_loop_passes_current_retention_to_cleanup(retention):
    current = settings.Settings(feed_channel_retention_days=retention)
    with (
        patch.object(feed_fetcher, "get_settings", return_value=current),
        patch.object(feed_fetcher, "fetch_all_channels", new_callable=AsyncMock),
        patch.object(feed_fetcher, "enrich_feed_video_metadata", new_callable=AsyncMock),
        patch.object(feed_fetcher.database, "cleanup_old_cached_videos") as old_videos,
        patch.object(feed_fetcher.database, "cleanup_stale_watched_channels") as stale_channels,
        patch.object(feed_fetcher.database, "cleanup_orphaned_cached_videos") as orphaned_videos,
        patch.object(feed_fetcher.asyncio, "sleep", new_callable=AsyncMock, side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await feed_fetcher.feed_fetch_loop()
    stale_channels.assert_called_once_with(days=retention)
    old_videos.assert_called_once_with(days=current.feed_video_max_age)
    orphaned_videos.assert_called_once_with()


@pytest.mark.asyncio
async def test_background_loop_reads_new_setting_on_next_cycle(test_db):
    observed = []

    async def next_cycle(_delay):
        if len(observed) == 1:
            settings.save_settings(settings.get_settings().model_copy(update={RETENTION: 1}))
        else:
            raise asyncio.CancelledError

    with (
        patch.object(feed_fetcher, "fetch_all_channels", new_callable=AsyncMock),
        patch.object(feed_fetcher, "enrich_feed_video_metadata", new_callable=AsyncMock),
        patch.object(feed_fetcher.database, "cleanup_old_cached_videos"),
        patch.object(
            feed_fetcher.database, "cleanup_stale_watched_channels", side_effect=lambda **kw: observed.append(kw)
        ),
        patch.object(feed_fetcher.database, "cleanup_orphaned_cached_videos"),
        patch.object(feed_fetcher.asyncio, "sleep", side_effect=next_cycle),
        pytest.raises(asyncio.CancelledError),
    ):
        await feed_fetcher.feed_fetch_loop()
    assert observed == [{"days": 14}, {"days": 1}]
