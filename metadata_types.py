"""Internal acquisition facts; requested language is never verified language."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum


class MetadataError(StrEnum):
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    SERVER = "server"
    CLIENT = "client"
    NETWORK = "network"
    INVALID = "invalid_response"
    PARTIAL = "partial"
    DISABLED = "disabled"
    LEASE = "lease_unavailable"
    BUSY = "sqlite_busy"
    BUDGET = "budget_exhausted"
    PAUSED = "provider_paused"
    BACKOFF = "backoff"
    CAPACITY = "storage_capacity"


def http_error(status: int) -> MetadataError | None:
    if status == 429:
        return MetadataError.RATE_LIMITED
    if status == 408:
        return MetadataError.TIMEOUT
    if status >= 500:
        return MetadataError.SERVER
    if status >= 400:
        return MetadataError.CLIENT
    if status >= 300:
        return MetadataError.INVALID
    return None


def retry_deadline(value: str | None, now: datetime) -> datetime:
    """429 without a valid Retry-After pauses for one minute, never hot-loops."""
    try:
        seconds = int(value)  # type: ignore[arg-type]
        return now + timedelta(seconds=max(0, seconds))
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is not None:
                return max(now, parsed.astimezone(UTC))
        except (ValueError, TypeError, OverflowError):
            pass
    return now + timedelta(minutes=1)


@dataclass(frozen=True)
class MetadataText:
    value: str
    source: str
    # None is reserved for legacy data with no reliable network timestamp.
    acquired_at: datetime | None


@dataclass(frozen=True)
class MetadataResult:
    site: str
    video_id: str
    language: str
    title: MetadataText | None = None
    description: MetadataText | None = None
    from_cache: bool = False
    error: MetadataError | None = None
    http_status: int | None = None
    retry_after: str | None = None
    # Completion time of the last real network attempt, including failures.
    acquired_at: datetime | None = None

    @property
    def outcome(self) -> str:
        if self.title is not None and self.description is not None:
            return "complete"
        if self.title is not None or self.description is not None:
            return "partial"
        return "failed"
