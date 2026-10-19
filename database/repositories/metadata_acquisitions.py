"""Opt-in, process-safe metadata leases and accounting. No scheduling or HTTP.

Every write takes a short BEGIN IMMEDIATE transaction with a bounded busy wait.
Tokens must be checked again when starting each HTTP attempt and saving fields.
No connection escapes these methods, including during network acquisitions.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from database import connection
from metadata_types import MetadataError, MetadataResult, MetadataText, retry_deadline

MAX_ON_DEMAND_VARIANTS = 3000


class CoordinationError(Exception):
    def __init__(self, error: MetadataError):
        super().__init__(error.value)
        self.error = error


@dataclass(frozen=True)
class MetadataLease:
    site: str
    video_id: str
    language: str
    owner: str
    generation: int

    @property
    def key(self) -> tuple[str, str, str]:
        return self.site, self.video_id, self.language


def _epoch(now: datetime | None = None) -> float:
    value = now if now is not None else datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Acquisition dates must be timezone-aware")
    return value.timestamp()


class MetadataStore:
    def __init__(self, path: str | None = None, *, busy_timeout: float = 0.25):
        if not 0 <= busy_timeout <= 1:
            raise ValueError("busy_timeout must be between zero and one second")
        self.path = path
        self.busy_timeout = busy_timeout

    @contextmanager
    def _db(self, *, write=False):
        conn = sqlite3.connect(self.path or connection.get_db_path(), timeout=self.busy_timeout)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except sqlite3.OperationalError as error:
            # Extended SQLITE_BUSY/LOCKED codes retain their base code in the low byte.
            if getattr(error, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise CoordinationError(MetadataError.BUSY) from error
            raise
        finally:
            conn.close()  # Rolls back unfinished transactions, including rejected claims.

    @staticmethod
    def _owned(conn, lease: MetadataLease, now: float) -> bool:
        return (
            conn.execute(
                """SELECT 1 FROM metadata_acquisitions
               WHERE site=? AND video_id=? AND language=? AND owner=? AND generation=? AND expires_at>?""",
                (*lease.key, lease.owner, lease.generation, now),
            ).fetchone()
            is not None
        )

    def claim(
        self,
        site: str,
        video_id: str,
        language: str,
        *,
        seconds=60,
        now=None,
        on_demand=False,
        expected_generation=None,
    ) -> MetadataLease | None:
        if language not in ("original", "fr", "en") or not site or not video_id:
            raise ValueError("A site, video ID and supported language are required")
        if not 0 < seconds <= 3600:
            raise ValueError("Lease duration must be positive and at most one hour")
        key = site, video_id, language
        owner = uuid4().hex
        with self._db(write=True) as conn:
            stamp = _epoch(now)
            existing = conn.execute(
                "SELECT * FROM metadata_acquisitions WHERE site=? AND video_id=? AND language=?", key
            ).fetchone()
            if expected_generation is not None and (existing["generation"] if existing else 0) != expected_generation:
                return None  # Another caller acquired this formerly eligible job entry.
            extra_entry = False
            if on_demand:
                policy = conn.execute(
                    "SELECT metadata_sync_enabled, innertube_enabled FROM settings WHERE id=1"
                ).fetchone()
                if policy is None or not policy[0] or not policy[1]:
                    raise CoordinationError(MetadataError.DISABLED)
                legacy = conn.execute(
                    "SELECT * FROM feed_video_metadata WHERE site=? AND video_id=? AND language=?", key
                ).fetchone()
                source = "innertube:player" if language == "original" else "innertube:next"
                if all(
                    (existing is not None and existing[field] is not None and existing[f"{field}_source"] == source)
                    or (legacy is not None and legacy[field] is not None and legacy["source"] == source)
                    for field in ("title", "description")
                ):
                    return None  # Recheck presence inside the same transaction as the lease.
                if existing is not None and existing["retry_at"] is not None and existing["retry_at"] > stamp:
                    raise CoordinationError(MetadataError.BACKOFF)
                if legacy is not None and legacy["failure_count"] and legacy["next_check_at"]:
                    try:
                        retry = datetime.fromisoformat(legacy["next_check_at"])
                        if retry.tzinfo is not None and retry.timestamp() > stamp:
                            raise CoordinationError(MetadataError.BACKOFF)
                    except (ValueError, TypeError):
                        pass
                known = (
                    legacy is not None
                    or conn.execute(
                        "SELECT 1 FROM cached_videos WHERE site=? AND video_id=? LIMIT 1", key[:2]
                    ).fetchone()
                    is not None
                )
                extra_entry = existing is None and not known
                if (
                    extra_entry
                    and conn.execute("SELECT COUNT(*) FROM metadata_acquisitions WHERE on_demand_entry=1").fetchone()[0]
                    >= MAX_ON_DEMAND_VARIANTS
                ):
                    raise CoordinationError(MetadataError.CAPACITY)
            if extra_entry:
                conn.execute(
                    """INSERT INTO metadata_acquisitions(site, video_id, language, on_demand_entry)
                       VALUES (?, ?, ?, 1)""",
                    key,
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO metadata_acquisitions(site, video_id, language) VALUES (?, ?, ?)", key
                )
            updated = conn.execute(
                """UPDATE metadata_acquisitions SET owner=?, expires_at=?, generation=generation+1
                   WHERE site=? AND video_id=? AND language=? AND (owner IS NULL OR expires_at<=?)""",
                (owner, stamp + seconds, *key, stamp),
            )
            if not updated.rowcount:
                return None
            generation = conn.execute(
                "SELECT generation FROM metadata_acquisitions WHERE site=? AND video_id=? AND language=?", key
            ).fetchone()[0]
            return MetadataLease(*key, owner, generation)

    def renew(self, lease: MetadataLease, *, seconds=60, now=None) -> bool:
        """Extend only an unexpired generation, e.g. the shared job coordinator."""
        if not 0 < seconds <= 3600:
            raise ValueError("Invalid lease duration")
        with self._db(write=True) as conn:
            stamp = _epoch(now)
            if not self._owned(conn, lease, stamp):
                return False
            conn.execute(
                "UPDATE metadata_acquisitions SET expires_at=? WHERE site=? AND video_id=? AND language=?",
                (stamp + seconds, *lease.key),
            )
            return True

    def finish(self, lease: MetadataLease, result: MetadataResult | None, *, now=None, sync_job=False) -> bool:
        """Fence all writes; merge only obtained network fields, never age absent/cache fields.

        None releases after cancellation; a crashed process needs no release.
        Observation times order our observations, not YouTube title edit dates.
        """
        if result is not None and (result.site, result.video_id, result.language) != lease.key:
            raise ValueError("Result does not belong to this lease")
        with self._db(write=True) as conn:
            stamp = _epoch(now)
            if not self._owned(conn, lease, stamp):
                return False
            if result is not None:
                for field in ("title", "description"):
                    text = getattr(result, field)
                    if text is None or text.acquired_at is None or result.from_cache:
                        continue
                    acquired = _epoch(text.acquired_at)
                    if acquired > stamp:
                        raise ValueError("A field cannot have a future acquisition date")
                    conn.execute(
                        f"""UPDATE metadata_acquisitions
                            SET {field}=?, {field}_source=?, {field}_acquired_at=?
                            WHERE site=? AND video_id=? AND language=?
                              AND ({field}_acquired_at IS NULL OR {field}_acquired_at<?)""",
                        (text.value, text.source, acquired, *lease.key, acquired),
                    )
                conn.execute(
                    """UPDATE metadata_acquisitions SET last_outcome=?, last_error=?, last_http_status=?,
                       last_retry_after=?, last_finished_at=? WHERE site=? AND video_id=? AND language=?""",
                    (result.outcome, result.error, result.http_status, result.retry_after, stamp, *lease.key),
                )
                if not result.from_cache and result.acquired_at is not None:
                    acquired = _epoch(result.acquired_at)
                    if acquired > stamp:
                        raise ValueError("A result cannot have a future acquisition date")
                    conn.execute(
                        """UPDATE metadata_acquisitions SET last_acquired_at=?
                           WHERE site=? AND video_id=? AND language=?
                             AND (last_acquired_at IS NULL OR last_acquired_at<?)""",
                        (acquired, *lease.key, acquired),
                    )
            if result is not None and sync_job:
                from database.repositories.metadata_sync import record_result

                record_result(conn, lease, result, stamp)
            conn.execute(
                """UPDATE metadata_acquisitions SET owner=NULL, expires_at=NULL
                   WHERE site=? AND video_id=? AND language=?""",
                lease.key,
            )
            return True

    def read(self, site: str, video_id: str, language: str) -> dict | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM metadata_acquisitions WHERE site=? AND video_id=? AND language=?",
                (site, video_id, language),
            ).fetchone()
            return dict(row) if row is not None else None

    def stored_fields(self, site: str, video_id: str, language: str) -> dict[str, MetadataText]:
        """Read new observations, then legacy fallbacks without inventing network dates."""
        key = site, video_id, language
        with self._db() as conn:
            current = conn.execute(
                "SELECT * FROM metadata_acquisitions WHERE site=? AND video_id=? AND language=?", key
            ).fetchone()
            legacy = conn.execute(
                "SELECT * FROM feed_video_metadata WHERE site=? AND video_id=? AND language=?", key
            ).fetchone()
        fields = {}
        for field in ("title", "description"):
            if current is not None and current[field] is not None:
                fields[field] = MetadataText(
                    current[field],
                    current[f"{field}_source"],
                    datetime.fromtimestamp(current[f"{field}_acquired_at"], UTC),
                )
            elif legacy is not None and isinstance(legacy[field], str):
                # Existing original candidates must still have the admissible source.
                if language == "original" and legacy["source"] != "innertube:player":
                    continue
                fields[field] = MetadataText(legacy[field], legacy["source"] or "unknown", None)
        return fields

    def begin_attempt(
        self,
        lease: MetadataLease,
        endpoint: str,
        *,
        hourly_limit: int | None = None,
        now=None,
        advance_cursor=False,
        sync_job=False,
    ) -> int:
        """Atomically admit/count one dispatch, including retries across processes.

        A crash between this durable admission and socket dispatch conservatively
        consumes a slot. Exact-once external I/O cannot be guaranteed by SQLite.
        """
        if hourly_limit is not None and (type(hourly_limit) is not int or hourly_limit < 1):
            raise ValueError("Hourly limit must be a positive integer or None")
        with self._db(write=True) as conn:
            stamp = _epoch(now)
            if not self._owned(conn, lease, stamp):
                raise CoordinationError(MetadataError.LEASE)
            if sync_job:
                policy = conn.execute(
                    """SELECT metadata_sync_enabled, innertube_enabled, metadata_sync_max_http_per_hour
                       FROM settings WHERE id=1"""
                ).fetchone()
                if policy is None or not policy[0] or not policy[1]:
                    raise CoordinationError(MetadataError.DISABLED)
                hourly_limit = policy[2]
            pause = conn.execute(
                "SELECT paused_until FROM metadata_provider_pauses WHERE provider='innertube'"
            ).fetchone()
            if pause is not None and pause[0] > stamp:
                raise CoordinationError(MetadataError.PAUSED)
            used = conn.execute(
                "SELECT COUNT(*) FROM metadata_http_attempts WHERE started_at>?", (stamp - 3600,)
            ).fetchone()[0]
            if hourly_limit is not None and used >= hourly_limit:
                raise CoordinationError(MetadataError.BUDGET)
            cursor = conn.execute(
                """INSERT INTO metadata_http_attempts(site, video_id, language, generation, endpoint, started_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (*lease.key, lease.generation, endpoint, stamp),
            )
            if advance_cursor:
                conn.execute("UPDATE metadata_sync_state SET dispatch_cursor=dispatch_cursor+1 WHERE id=1")
            return cursor.lastrowid

    def finish_attempt(self, attempt_id: int, *, status=None, error=None, retry_after=None, now=None) -> None:
        stamp = _epoch(now)
        with self._db(write=True) as conn:
            updated = conn.execute(
                """UPDATE metadata_http_attempts SET finished_at=?, http_status=?, error=?, retry_after=?
                   WHERE id=? AND finished_at IS NULL""",
                (stamp, status, error, retry_after, attempt_id),
            )
            if updated.rowcount and (status == 429 or error == MetadataError.RATE_LIMITED):
                deadline = retry_deadline(retry_after, datetime.fromtimestamp(stamp, UTC)).timestamp()
                conn.execute(
                    """INSERT INTO metadata_provider_pauses(provider, paused_until) VALUES ('innertube', ?)
                       ON CONFLICT(provider) DO UPDATE SET paused_until=MAX(paused_until, excluded.paused_until)""",
                    (deadline,),
                )

    def attempts_used(self, *, now=None) -> int:
        with self._db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM metadata_http_attempts WHERE started_at>?", (_epoch(now) - 3600,)
            ).fetchone()[0]
