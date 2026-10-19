"""Request-scoped language context and read-only Feed text reuse for lists."""

import logging
import sqlite3
from typing import Optional

import database
from innertube._client import InnerTubeError, _build_context, _get_client_version
from metadata_selection import select_stored_metadata
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


def _read_stored_metadata(video_ids: list[str], language: Optional[str]) -> dict:
    """Read candidates once per batch, without registering or acquiring videos."""
    if language is None or not video_ids:
        return {}
    try:
        return {
            (row["video_id"], row["language"]): row
            for row in database.get_feed_video_metadata(video_ids, languages=[language, "original"])
        }
    except sqlite3.Error:
        logger.exception("[Metadata] Stored text unavailable; retaining provider text")
        return {}


def _apply_stored_list_metadata(
    items: list[dict], language: Optional[str], *, provider_language: Optional[str] = None, id_field: str = "videoId"
) -> list[dict]:
    """Select local candidates on any provider; a localized provider keeps its text.

    Original remains a distinct local preference, without per-video acquisition.
    Provider language denotes request intent only, never verified text language.
    """
    if language is None:
        return items
    video_ids = [item[id_field] for item in items if item.get("type", "video") == "video" and item.get(id_field)]
    if not video_ids:
        return items
    stored = _read_stored_metadata(video_ids, language)
    result = []
    for item in items:
        variants = (
            {candidate: stored.get((item.get(id_field), candidate), {}) for candidate in (language, "original")}
            if item.get("type", "video") == "video"
            else {}
        )
        updates = select_stored_metadata(item, language, variants, provider_language=provider_language)
        result.append({**item, **updates} if updates else item)
    return result
