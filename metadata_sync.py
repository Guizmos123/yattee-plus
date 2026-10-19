"""Autonomous metadata scheduling; discovery and on-demand paths stay separate."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime

import settings as settings_module
from database.repositories import metadata_sync as repository
from database.repositories.metadata_acquisitions import CoordinationError, MetadataStore
from innertube._metadata import MetadataAcquirer
from metadata_types import MetadataError

logger = logging.getLogger(__name__)
CONCURRENCY = 2
BATCH_SIZE = 30
CONFIG_POLL_SECONDS = 5
_task = None
_wake = None


def _policy(settings):
    return (
        settings.metadata_sync_enabled,
        settings.innertube_enabled,
        settings.metadata_sync_interval_hours,
        settings.metadata_sync_max_http_per_hour,
    )


class MetadataSync:
    def __init__(self, store=None):
        self.store = store or MetadataStore()
        self.leader = None

    def _before_attempt(self):
        # Reload persistent policy before EVERY retry, including remote edits.
        current = settings_module.load_settings()
        if not current.metadata_sync_enabled or not current.innertube_enabled:
            raise CoordinationError(MetadataError.DISABLED)
        row = self.store.read(*repository.LEADER_KEY)
        if (
            not row
            or row["owner"] != self.leader.owner
            or row["generation"] != self.leader.generation
            or row["expires_at"] <= datetime.now(UTC).timestamp()
        ):
            raise CoordinationError(MetadataError.LEASE)
        return current.metadata_sync_max_http_per_hour

    async def run_cycle(self, current):
        if not current.metadata_sync_enabled or not current.innertube_enabled:
            return
        status = repository.get_status(current, self.store)
        stamp = datetime.now(UTC).timestamp()
        maintenance = repository.maintenance_due(self.store, stamp)
        if status["state"] == "suspended" and not maintenance:
            return
        ready = (
            status["pending"]
            and status["next_resume"]
            and datetime.fromisoformat(status["next_resume"]).timestamp() <= stamp
        )
        if not ready and not maintenance:
            return
        self.leader = self.store.claim(*repository.LEADER_KEY)
        if self.leader is None:
            return
        try:
            stamp = datetime.now(UTC).timestamp()
            repository.prune_journal(self.store, stamp)
            if status["state"] == "suspended":
                return
            repository.seed_known_videos(self.store, stamp)
            entries = repository.select_batch(self.store, current.metadata_sync_interval_hours, stamp, BATCH_SIZE)
            if not entries:
                return
            repository.mark_run(self.store, stamp)
            # Due work must really reach the network. In particular a cached
            # partial must not postpone the still-missing field or hot-loop.
            acquirer = MetadataAcquirer(
                self.store,
                hourly_limit=current.metadata_sync_max_http_per_hour,
                cache_ttl=0,
                sync_job=True,
                before_attempt=self._before_attempt,
            )
            pending = iter(entries)

            async def worker():
                for entry in pending:
                    policy = settings_module.load_settings()
                    if _policy(policy) != _policy(current):
                        return
                    result = await acquirer.acquire(
                        entry["site"], entry["video_id"], entry["language"], expected_generation=entry["generation"]
                    )
                    if result.error in (
                        MetadataError.BUDGET,
                        MetadataError.PAUSED,
                        MetadataError.RATE_LIMITED,
                        MetadataError.DISABLED,
                        MetadataError.BUSY,
                        MetadataError.LEASE,
                    ):
                        return

            async def monitor():
                while True:
                    await asyncio.sleep(CONFIG_POLL_SECONDS)
                    policy = settings_module.load_settings()
                    # Cancel in-flight work on any relevant policy edit. All
                    # workers see it within 5 s; local API edits wake immediately.
                    if _policy(policy) != _policy(current):
                        return
                    if not self.store.renew(self.leader):
                        return

            workers = asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
            heartbeat = asyncio.create_task(monitor())
            changed = asyncio.create_task(_wake.wait()) if _wake is not None else None
            watched = [workers, heartbeat, *([changed] if changed is not None else [])]
            try:
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()  # Surface failures; finally cancels peers before lease release.
            finally:
                for task in watched:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*watched, return_exceptions=True)
        finally:
            try:
                self.store.finish(self.leader, None)
            except CoordinationError:
                logger.warning("Metadata coordinator release deferred to lease expiry")
            self.leader = None


async def _loop():
    sync = MetadataSync()
    last_policy = None
    while True:
        _wake.clear()
        try:
            # Process-local Settings caches cannot convey edits from other workers.
            current = settings_module.load_settings()
            if _policy(current) != last_policy:
                settings_module.invalidate_cache()
                last_policy = _policy(current)
            await sync.run_cycle(current)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Metadata sync cycle failed; retrying after configuration poll")
        with suppress(TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=CONFIG_POLL_SECONDS)


def notify_settings_changed():
    if _wake is not None:
        _wake.set()


def start_metadata_sync():
    global _task, _wake
    if _task is None or _task.done():
        _wake = asyncio.Event()
        _task = asyncio.create_task(_loop(), name="metadata-sync")


async def stop_metadata_sync():
    global _task, _wake
    if _task is not None:
        _task.cancel()
        with suppress(asyncio.CancelledError):
            await _task
        _task = None
        _wake = None
