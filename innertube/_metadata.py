"""Metadata-only acquisition service shared with the autonomous sync job.

Reuses HTTP authentication/egress/retries and existing /next extraction. It never
reads merged playback metadata, fetches streams, or falls back to other providers.
Only the HTTP client retries; a 429 stops immediately and pauses shared dispatch.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from cachetools import TTLCache

from database.repositories.metadata_acquisitions import CoordinationError, MetadataStore
from innertube import _client
from innertube._converters import _metadata_from_next
from metadata_types import MetadataError, MetadataResult, MetadataText, http_error


class _AttemptTrace:
    def __init__(self, store, lease, hourly_limit, before_attempt=None, sync_job=False, on_demand=False):
        self.store, self.lease, self.hourly_limit = store, lease, hourly_limit
        self.acquired_at = None
        self.status = None
        self.attempt_id = None
        self.before_attempt = before_attempt
        self.sync_job = sync_job
        self.on_demand = on_demand
        self.first_attempt = True

    def start(self, endpoint):
        if self.before_attempt is not None:
            self.hourly_limit = self.before_attempt()
        self.attempt_id = self.store.begin_attempt(
            self.lease,
            endpoint,
            hourly_limit=self.hourly_limit,
            advance_cursor=self.sync_job and self.first_attempt,
            sync_job=self.sync_job or self.on_demand,
        )
        self.first_attempt = False

    def finish(self, *, status=None, error=None, retry_after=None, provider_status=None):
        self.acquired_at = datetime.now(UTC)
        self.status = status
        self.store.finish_attempt(
            self.attempt_id,
            status=status,
            error=error or (http_error(provider_status or status) if (provider_status or status) else None),
            retry_after=retry_after,
            now=self.acquired_at,
        )
        self.attempt_id = None


class MetadataAcquirer:
    """One reusable instance per process; shared SQLite coordinates processes.

    hourly_limit=None prepares accounting without imposing a new production
    budget. Future workers must explicitly configure their shared ceiling.
    Memory hits retain original field dates and never write a fresh observation.
    Legacy caches are not adopted: they lack reliable per-field network dates.
    """

    def __init__(
        self,
        store: MetadataStore | None = None,
        *,
        hourly_limit=None,
        cache_ttl=3600,
        timeout_seconds=50,
        sync_job=False,
        before_attempt=None,
        on_demand=False,
    ):
        if not 0 < timeout_seconds <= 50:
            raise ValueError("Network deadline must be positive and shorter than the 60-second lease")
        self.store = store if store is not None else MetadataStore()
        self.hourly_limit = hourly_limit
        self.cache = TTLCache(maxsize=200, ttl=cache_ttl)
        self.timeout_seconds = timeout_seconds
        self.sync_job = sync_job
        self.before_attempt = before_attempt
        self.on_demand = on_demand

    async def acquire(self, site: str, video_id: str, language: str, *, expected_generation=None) -> MetadataResult:
        from settings import get_settings

        if language not in ("original", "fr", "en") or site != "youtube" or not video_id:
            raise ValueError("Only youtube video metadata in original/fr/en is supported")
        key = site, video_id, language
        missing = MetadataResult(*key)
        if not get_settings().innertube_enabled:
            return replace(missing, error=MetadataError.DISABLED)
        if key in self.cache:
            return replace(self.cache[key], from_cache=True)
        try:
            lease = self.store.claim(*key, on_demand=self.on_demand, expected_generation=expected_generation)
        except CoordinationError as error:
            return replace(missing, error=error.error)
        if lease is None:
            return replace(missing, error=MetadataError.LEASE)

        trace = _AttemptTrace(self.store, lease, self.hourly_limit, self.before_attempt, self.sync_job, self.on_demand)
        result = missing
        try:
            endpoint = "player" if language == "original" else "next"
            # Reuse an already refreshed version or the normal fallback version.
            # No hidden homepage GET outside the persistent attempt accounting.
            version = _client._client_version or _client.INNERTUBE_CLIENT_VERSION
            body = {
                "videoId": video_id,
                "context": _client._build_context(
                    language="en" if language == "original" else language, client_version=version
                ),
            }
            # This also bounds credential setup/retries; httpx's read timeout
            # alone does not bound the total duration of a streaming response.
            async with asyncio.timeout(self.timeout_seconds):
                data = await _client.innertube_post(endpoint, body, metadata_trace=trace)
            if language == "original":
                details = data.get("videoDetails") if isinstance(data, dict) else None
                if isinstance(details, dict) and details.get("videoId") in (None, video_id):
                    fields = {"title": details.get("title"), "description": details.get("shortDescription")}
                else:
                    fields = {}
            else:
                fields = _metadata_from_next(data)
            texts = {
                field: MetadataText(fields[field], f"innertube:{endpoint}", trace.acquired_at)
                for field in ("title", "description")
                if isinstance(fields.get(field), str)
            }
            error = None if len(texts) == 2 else MetadataError.PARTIAL if texts else MetadataError.INVALID
            result = replace(missing, **texts, error=error, http_status=trace.status, acquired_at=trace.acquired_at)
        except _client.InnerTubeError as error:
            kind = http_error(error.status_code) if error.status_code else None
            kind = kind or (MetadataError(error.error_kind) if error.error_kind else MetadataError.INVALID)
            result = replace(
                missing,
                error=kind,
                http_status=trace.status,
                retry_after=error.retry_after,
                acquired_at=trace.acquired_at,
            )
        except TimeoutError:
            if trace.attempt_id is not None:
                try:
                    trace.finish(error="timeout")
                except CoordinationError:
                    pass  # Fail closed; admission remains consumed and recoverable.
            result = replace(missing, error=MetadataError.TIMEOUT, acquired_at=trace.acquired_at)
        except CoordinationError as error:
            result = replace(missing, error=error.error, acquired_at=trace.acquired_at, http_status=trace.status)
        except (ValueError, TypeError, AttributeError):
            result = replace(
                missing, error=MetadataError.INVALID, http_status=trace.status, acquired_at=trace.acquired_at
            )
        except asyncio.CancelledError:
            # Release without erasing fields; an interrupted attempt remains in
            # the ledger, since its network outcome cannot safely be inferred.
            try:
                self.store.finish(lease, None)
            except CoordinationError:
                pass  # Bounded busy wait; the lease remains recoverable by expiry.
            raise

        try:
            if not self.store.finish(lease, result, sync_job=self.sync_job or self.on_demand):
                return replace(missing, error=MetadataError.LEASE)
        except CoordinationError as error:
            return replace(missing, error=error.error)
        if result.outcome != "failed":
            self.cache[key] = result
        return result
