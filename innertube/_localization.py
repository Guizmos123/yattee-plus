"""Request-scoped language context and read-only Feed text reuse for lists."""

import logging
import sqlite3
from typing import Optional

import database
from innertube._client import InnerTubeError, _build_context, _get_client_version
from settings import get_settings

logger = logging.getLogger("innertube")


async def _language_body(body: dict, language: Optional[str]) -> dict:
    if language is None:
        return body
    if language not in ("fr", "en"):
        raise ValueError("List metadata language must be 'fr' or 'en'")
    if not get_settings().innertube_enabled:
        raise InnerTubeError("InnerTube is disabled", is_retryable=False)
    return {
        **body,
        "context": _build_context(language=language, region="US", client_version=await _get_client_version()),
    }


def _apply_stored_list_metadata(items: list[dict], language: Optional[str]) -> list[dict]:
    """Overlay available requested-language fields; never register unknown IDs.

    This is called only on InnerTube lists. An absent SQLite field preserves the
    request's own text, including an explicit empty string. Historical requests
    and all other providers bypass this lookup.
    """
    if language not in ("fr", "en"):
        return items
    video_ids = [item["videoId"] for item in items if item.get("type", "video") == "video" and item.get("videoId")]
    if not video_ids:
        return items
    try:
        stored = {row["video_id"]: row for row in database.get_feed_video_metadata(video_ids, languages=[language])}
    except sqlite3.Error:
        logger.exception("[Lists] Stored metadata unavailable; retaining InnerTube text")
        return items
    result = []
    for item in items:
        row = stored.get(item.get("videoId"), {}) if item.get("type", "video") == "video" else {}
        updates = {field: row[field] for field in ("title", "description") if isinstance(row.get(field), str)}
        result.append({**item, **updates} if updates else item)
    return result
