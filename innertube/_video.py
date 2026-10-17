"""InnerTube player + next endpoints - video metadata and stream shapes.

Fetches video details from YouTube's InnerTube /player and /next endpoints
in parallel, then merges the result into an Invidious-shaped dict. Stream
URLs from the WEB client are ciphered, so the caller is expected to join
deciphered URLs from yt-dlp by itag via `merge_stream_urls`.
"""

import asyncio
import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache

from converters._helpers import _xtags_from_url
from innertube._client import InnerTubeError, _build_context, _get_client_version, innertube_post
from innertube._converters import (
    _metadata_from_next,
    _parse_storyboard_spec,
    _recommended_metadata_from_next,
    innertube_player_to_invidious_video,
)

logger = logging.getLogger("innertube")

_video_cache: Optional[TTLCache] = None
_localized_metadata_cache: Optional[TTLCache] = None


def _get_cache() -> TTLCache:
    global _video_cache
    if _video_cache is None:
        from settings import get_settings

        _video_cache = TTLCache(maxsize=100, ttl=get_settings().cache_video_ttl)
    return _video_cache


def reset_cache() -> None:
    global _video_cache
    _video_cache = None


def _get_localized_metadata_cache() -> TTLCache:
    global _localized_metadata_cache
    if _localized_metadata_cache is None:
        from settings import get_settings

        _localized_metadata_cache = TTLCache(maxsize=200, ttl=get_settings().cache_video_ttl)
    return _localized_metadata_cache


def reset_localized_metadata_cache() -> None:
    """Reset localized text entries independently of the player/next cache."""
    global _localized_metadata_cache
    _localized_metadata_cache = None


async def get_video_localized_metadata(
    video_id: str, language: str, *, include_recommendations: bool = False
) -> Dict[str, Any]:
    """Request /next text in French or English without touching playback data.

    YouTube can return original text when no localization is available; hl is
    a preference, not proof of the returned language. Missing/malformed data or
    InnerTube failures return None fields without caching the failed lookup.
    Explicit empty strings are preserved and cached. Every return is a fresh
    dict so callers cannot modify a cached entry. Recommendation text from the
    same response is retained in the existing language cache, and optionally
    returned by videoId; requesting it never triggers another acquisition.
    """
    if language not in ("fr", "en"):
        raise ValueError("Localized metadata language must be 'fr' or 'en'")

    from settings import get_settings

    def selected(metadata: dict) -> Dict[str, Any]:
        result: Dict[str, Any] = {"title": metadata.get("title"), "description": metadata.get("description")}
        if include_recommendations:
            result["recommendedVideos"] = deepcopy(metadata.get("recommendedVideos", {}))
        return result

    missing = selected({})
    if not get_settings().innertube_enabled:
        return missing

    cache = _get_localized_metadata_cache()
    cache_key = (video_id, language)
    cached = cache.get(cache_key)
    if cached is not None:
        return selected(cached)

    try:
        version = await _get_client_version()
        body = {
            "videoId": video_id,
            "context": _build_context(language=language, client_version=version),
        }
        data = await innertube_post("next", body)
        metadata: Dict[str, Any] = _metadata_from_next(data)
        metadata["recommendedVideos"] = _recommended_metadata_from_next(data)
    except (InnerTubeError, AttributeError, TypeError, ValueError) as error:
        logger.warning("[InnerTube] Localized metadata failed for %s (%s): %s", video_id, language, error)
        return missing

    if metadata["title"] is not None or metadata["description"] is not None or metadata["recommendedVideos"]:
        cache[cache_key] = deepcopy(metadata)
    return selected(metadata)


async def get_video_original_metadata(video_id: str) -> Dict[str, Optional[str]]:
    """Read identifiable original text from /player videoDetails only.

    This separate metadata lookup is used only for an explicit original-text
    request. It never reuses the merged player/next description as an original
    and does not alter either metadata cache or playback extraction.
    """
    from settings import get_settings

    missing: Dict[str, Optional[str]] = {"title": None, "description": None}
    if not get_settings().innertube_enabled:
        return missing

    try:
        data = await innertube_post("player", {"videoId": video_id})
        details = data.get("videoDetails") if isinstance(data, dict) else None
        if not isinstance(details, dict):
            return missing
        if details.get("videoId") not in (None, video_id):
            return missing
        title = details.get("title")
        description = details.get("shortDescription")
        return {
            "title": title if isinstance(title, str) else None,
            "description": description if isinstance(description, str) else None,
        }
    except (InnerTubeError, AttributeError, TypeError, ValueError) as error:
        logger.warning("[InnerTube] Original metadata failed for %s: %s", video_id, error)
        return missing


async def get_video_player_next(video_id: str, use_cache: bool = True) -> Dict[str, Any]:
    """Fetch /player and /next in parallel and return an Invidious-shaped dict.

    Raises InnerTubeError when the video is unplayable (LOGIN_REQUIRED, ERROR,
    UNPLAYABLE) so the caller can fall back to yt-dlp / Invidious.

    The returned dict has the same field names as `invidious_to_video_response`
    consumes, but its formatStreams/adaptiveFormats URLs are empty — stream URLs
    must be merged in separately via `merge_stream_urls`.
    """
    cache = _get_cache()
    cache_key = f"player_next:{video_id}"
    if use_cache and cache_key in cache:
        return cache[cache_key]

    player_body = {
        "videoId": video_id,
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
    }
    next_body = {"videoId": video_id}

    player_task = innertube_post("player", player_body)
    next_task = innertube_post("next", next_body)

    results = await asyncio.gather(player_task, next_task, return_exceptions=True)
    player_data, next_data = results

    if isinstance(player_data, Exception):
        raise player_data

    # /next is optional enrichment — don't fail if it breaks
    if isinstance(next_data, Exception):
        logger.warning(f"[InnerTube] /next failed for {video_id}: {next_data}")
        next_data = {}

    # NOTE: we don't raise on UNPLAYABLE/LOGIN_REQUIRED — YouTube frequently
    # returns those for WEB-client /player calls from data-centre IPs even
    # though videoDetails, /next metadata and captions are still populated.
    # The router merges yt-dlp's deciphered URLs by itag, and if the merge
    # yields no streams it falls back to the yt-dlp-only path.
    playability = player_data.get("playabilityStatus", {}) or {}
    status = playability.get("status")
    if status and status != "OK":
        logger.info(f"[InnerTube] /player playabilityStatus={status} for {video_id}")

    if not player_data.get("videoDetails") and not next_data:
        raise InnerTubeError(
            f"No video data: playabilityStatus={status}",
            is_retryable=False,
        )

    result = innertube_player_to_invidious_video(player_data, next_data)

    # The WEB client often omits playerStoryboardSpecRenderer. When the primary
    # /player response has no storyboards, do a supplemental /player call with
    # the TVHTML5 client context which reliably returns the spec, and parse it
    # into the Invidious-shaped list. yt-dlp and Invidious remain as further
    # fallbacks in the router if this also yields nothing.
    if not result.get("storyboards"):
        try:
            spec = await _fetch_storyboard_spec_tvhtml5(video_id)
            if spec:
                result["storyboards"] = _parse_storyboard_spec(spec, video_id)
        except InnerTubeError as e:
            logger.info(f"[InnerTube] TVHTML5 storyboard fetch failed for {video_id}: {e}")

    cache[cache_key] = result
    return result


async def _fetch_storyboard_spec_tvhtml5(video_id: str) -> str:
    """Fetch the raw storyboard spec string via the TVHTML5 client.

    The WEB client omits playerStoryboardSpecRenderer for many videos; TVHTML5
    consistently returns it. We call /player with an overridden context and
    extract the spec string. Caller is responsible for parsing it.
    """
    body = {
        "videoId": video_id,
        "context": {
            "client": {
                "clientName": "TVHTML5",
                "clientVersion": "7.20240304.10.00",
                "hl": "en",
                "gl": "US",
            }
        },
    }
    data = await innertube_post("player", body, use_cookies=False)
    renderer = data.get("storyboards", {}).get("playerStoryboardSpecRenderer", {}) or {}
    return renderer.get("spec") or ""


def _ytdlp_format_key(fmt: Dict[str, Any]) -> Optional[Tuple[str, Tuple[Tuple[str, str], ...]]]:
    """Return the (itag, xtags) join key for a yt-dlp format, or None if unusable.

    yt-dlp names YouTube formats by itag, suffixed when one itag has several
    variants: `140-3` (audio track index on multi-language videos), `251-drc`
    (dynamic range compressed), `hls-480` (HLS ladder). The suffix alone does
    not say which InnerTube variant it is, but the deciphered googlevideo URL
    carries the plain `xtags=acont=...:lang=...` marker, which is exactly what
    InnerTube's protobuf `xtags` decodes to. Formats without an `xtags` param
    (video-only, single-audio-track videos) key on the bare itag.
    """
    url = fmt.get("url")
    if not url:
        return None
    fid = str(fmt.get("format_id") or "")
    itag = fid.split("-", 1)[0]
    if not itag.isdigit():
        return None
    return itag, tuple(sorted(_xtags_from_url(url).items()))


def merge_stream_urls(
    it_video: Dict[str, Any], ytdlp_formats: Optional[List[Dict[str, Any]]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Join InnerTube format shapes with yt-dlp's deciphered URLs by itag.

    InnerTube provides rich metadata (itag, mimeType, bitrate, audioTrack, ...)
    but its WEB client URLs are ciphered. yt-dlp returns deciphered URLs keyed
    by format_id, which is the itag for YouTube, plus the per-track `xtags`
    on the URL for videos with several audio tracks (see `_ytdlp_format_key`).
    We look up by (itag, xtags) and drop formats we cannot serve. InnerTube
    variants yt-dlp does not expose (DRC, `vb=1`) therefore fall away instead
    of duplicating the same URL under several entries.

    Returns (formatStreams, adaptiveFormats) as lists of Invidious-shaped dicts
    with the internal `_xtags` helper key removed.
    """
    ytdlp_by_key: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], Dict[str, Any]] = {}
    for fmt in ytdlp_formats or []:
        key = _ytdlp_format_key(fmt)
        if key is not None:
            ytdlp_by_key.setdefault(key, fmt)

    def _join(fmt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        itag = str(fmt.get("itag") or "")
        xtags = fmt.get("_xtags") or {}
        yt = ytdlp_by_key.get((itag, tuple(sorted(xtags.items()))))
        if not yt:
            return None
        merged = {k: v for k, v in fmt.items() if k != "_xtags"}
        merged["url"] = yt["url"]
        return merged

    merged_format_streams: List[Dict[str, Any]] = []
    merged_adaptive: List[Dict[str, Any]] = []
    dropped = 0

    for fmt in it_video.get("formatStreams", []):
        merged = _join(fmt)
        if merged is None:
            dropped += 1
            continue
        merged_format_streams.append(merged)

    for fmt in it_video.get("adaptiveFormats", []):
        merged = _join(fmt)
        if merged is None:
            dropped += 1
            continue
        merged_adaptive.append(merged)

    if dropped:
        logger.debug(f"[InnerTube] merge_stream_urls: dropped {dropped} itag(s) missing from yt-dlp")

    return merged_format_streams, merged_adaptive
