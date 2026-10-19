"""Video endpoints."""

import asyncio
import logging
import sqlite3
from html import escape
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request

import avatar_cache
import database
import innertube
import invidious_proxy
from basic_auth import get_current_user_from_request
from converters import (
    invidious_to_video_list_item,
    invidious_to_video_response,
    ytdlp_to_video_list_item,
    ytdlp_to_video_response,
)
from innertube import InnerTubeError
from innertube._localization import _read_stored_metadata
from innertube._video import get_video_localized_metadata
from metadata_selection import select_stored_metadata
from models import ChannelExtractResponse, Thumbnail, VideoResponse
from settings import get_settings
from utils import get_base_url
from ytdlp_wrapper import (
    YtDlpError,
    extract_channel_url,
    extract_url,
    get_channel_avatar,
    get_video_info,
    is_safe_url,
    is_valid_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["videos"])


def validate_extractor_allowed(extractor: str):
    """Check if extractor is allowed based on settings and enabled sites.

    Raises HTTPException 403 if the extractor is not allowed.
    """
    app_settings = get_settings()
    if app_settings.allow_all_sites_for_extraction:
        return  # All sites allowed

    # Check if site is enabled in database
    site = database.get_site_by_extractor(extractor)
    if not site or not site.get("enabled", False):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Extraction from '{extractor}' is not allowed. Enable this site in admin settings "
                "or enable 'Allow all sites for extraction'."
            ),
        )


@router.get("/videos/{video_id}", response_model=VideoResponse)
async def get_video(
    video_id: str,
    request: Request,
    proxy: Optional[bool] = Query(None, description="Override site proxy_streaming setting (true=proxy, false=direct)"),
    proxy_mode: Optional[str] = Query(
        None,
        description=(
            "How to proxy stream URLs when proxying is on. "
            "'relay' (default): byte-relay through /proxy/relay (fast, supports Range, no disk write). "
            "'download': /proxy/fast/ — downloads via yt-dlp, caches on disk (right shape for downloads). "
            "'off': bypass proxying entirely. "
            "Ignored when the per-site proxy_streaming flag (or ?proxy=) is off."
        ),
        pattern="^(relay|download|off)$",
    ),
    invidious: Optional[bool] = Query(
        None,
        description=(
            "Force a specific extraction path. Unset (default): try Invidious first when enabled, "
            "fall back to InnerTube/yt-dlp. true: Invidious only (fails if disabled/unavailable). "
            "false: InnerTube/yt-dlp only (no Invidious fallback)."
        ),
    ),
    lang: Optional[Literal["original", "fr", "en"]] = Query(
        None,
        description=(
            "Video title/description preference. Omitted: preserve historical metadata. "
            "original: prefer /player videoDetails candidates; fr/en: request /next text in that language context. "
            "Unavailable fields keep historical values, whose language may be unknown. "
            "With invidious=true, only locally stored candidates are applied; no InnerTube calls."
        ),
    ),
):
    """Get video details including streams (Invidious-compatible).

    Response includes `extractionMethod` indicating which path served the video:
    "invidious", "hybrid" (InnerTube + yt-dlp), or "ytdlp".
    """
    validate_extractor_allowed("youtube")

    base_url = get_base_url(request)
    s = get_settings()

    user = get_current_user_from_request(request)
    user_id = user.get("id") if user else None

    site_config = database.get_site_by_extractor("youtube")
    site_proxy_streaming = site_config.get("proxy_streaming", True) if site_config else True
    proxy_streams = proxy if proxy is not None else site_proxy_streaming

    # Resolve proxy_mode. When proxying is off, mode is irrelevant. When on,
    # default to "relay" (fast path). "download" preserves the legacy
    # /proxy/fast/ shape for the iOS download flow.
    if not proxy_streams:
        effective_proxy_mode = "off"
    elif proxy_mode in ("relay", "download"):
        effective_proxy_mode = proxy_mode
    else:
        effective_proxy_mode = "relay"

    invidious_usable = invidious_proxy.is_enabled() and s.invidious_proxy_videos

    def _looks_like_live(data: dict) -> bool:
        """Detect a live broadcast from the extracted Invidious response.

        Invidious's own ``liveNow`` flag has been observed to be wrong for
        streams that just went live, so the URL params are treated as
        authoritative — `source=yt_live_broadcast` / `&live=1` / `?live=1`
        baked into any extracted format URL means it's a live broadcast.
        """
        if data.get("liveNow") or data.get("isUpcoming"):
            return True

        def _live_url(u):
            return bool(u) and (
                "source=yt_live_broadcast" in u
                or "&live=1" in u
                or "?live=1" in u
            )

        for f in (data.get("adaptiveFormats") or []):
            if _live_url(f.get("url") or ""):
                return True
        for f in (data.get("formatStreams") or []):
            if _live_url(f.get("url") or ""):
                return True
        return False

    async def _via_invidious() -> VideoResponse:
        data = await invidious_proxy.get_video(video_id)
        if not data:
            raise invidious_proxy.InvidiousProxyError(
                f"Video {video_id} not found on Invidious", is_retryable=False
            )
        # Live broadcasts: skip Invidious and let yt-dlp extract instead.
        # Invidious + companion's POT for live segments is fragile (we've
        # seen consistent 403s on /videoplayback even when IPs match);
        # yt-dlp (with the bgutil POT provider, when yt_pot_enabled is on)
        # produces fresher tokens. Falling through to the hybrid path picks
        # up yt-dlp.
        if _looks_like_live(data):
            logger.info(
                f"[Videos] Live broadcast detected for {video_id}; "
                "skipping Invidious in favour of yt-dlp"
            )
            raise invidious_proxy.InvidiousProxyError(
                f"Live broadcast {video_id} — deferring to yt-dlp",
                is_retryable=False,
            )
        response = invidious_to_video_response(
            data,
            base_url,
            proxy_streams=proxy_streams,
            proxy_mode=effective_proxy_mode,
            invidious_base_url=invidious_proxy.get_base_url(),
            user_id=user_id,
        )
        response.extractionMethod = "invidious"
        channel_id = data.get("authorId")
        if channel_id:
            avatar_cache.get_cache().schedule_background_fetch(channel_id)
        return await _select_video_metadata(response, video_id, lang, invidious_only=invidious is True)

    async def _via_hybrid() -> VideoResponse:
        response = await _get_video_hybrid(
            video_id, base_url, proxy_streams=proxy_streams,
            proxy_mode=effective_proxy_mode, user_id=user_id,
        )
        if response is None:
            raise HTTPException(status_code=404, detail="Video not found")
        return await _select_video_metadata(response, video_id, lang, invidious_only=invidious is True)

    # invidious=true: force Invidious, no fallback
    if invidious is True:
        if not invidious_usable:
            raise HTTPException(
                status_code=503,
                detail="Invidious is disabled or not configured for video requests",
            )
        try:
            return await _via_invidious()
        except invidious_proxy.InvidiousProxyError as e:
            raise HTTPException(status_code=502, detail=f"Invidious error: {e}")

    # invidious=false: force hybrid, no Invidious fallback
    if invidious is False:
        try:
            return await _via_hybrid()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except YtDlpError as e:
            raise HTTPException(status_code=404, detail=f"Video not found: {e}")
        except (KeyError, TypeError) as e:
            logger.error(f"[Videos] Unexpected error for {video_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    # Default: Invidious first when usable, hybrid as fallback
    if invidious_usable:
        try:
            return await _via_invidious()
        except invidious_proxy.InvidiousProxyError as e:
            logger.info(f"[Videos] Invidious failed for {video_id}: {e}; falling back to hybrid")

    try:
        return await _via_hybrid()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except YtDlpError as e:
        raise HTTPException(status_code=404, detail=f"Video not found: {e}")
    except (KeyError, TypeError) as e:
        logger.error(f"[Videos] Unexpected error for {video_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _select_video_metadata(
    response: VideoResponse,
    video_id: str,
    lang: Optional[Literal["original", "fr", "en"]],
    *,
    invidious_only: bool = False,
) -> VideoResponse:
    """Apply explicitly requested text to a copy after provider extraction.

    Omitted lang preserves the complete historical response. Forced Invidious
    applies local candidates only, without changing the extraction provider.
    Unavailable fields retain historical text without claiming it is original.
    Playback and authors are never changed here. Hybrid recommendations retain
    their separate historical localization, outside shared main-text acquisition.
    """
    if lang is None:
        return response

    stored = _read_stored_metadata([video_id], lang)
    provider = {"title": response.title, "description": response.description}
    variants = {language: stored.get((video_id, language), {}) for language in (lang, "original")}
    metadata = select_stored_metadata(provider, lang, variants)
    # An absent requested variant differs from a provider/original fallback.
    # Presence is independent of display freshness: only the job refreshes stale
    # but present text, using exactly the unchanged LANG-02/META-03 selection.
    present = select_stored_metadata({}, lang, {lang: variants.get(lang, {})})
    try:
        acquired = None
        if len(present) < 2 and not invidious_only and innertube.is_enabled():
            acquired = await _acquire_video_metadata(video_id, lang, provider)
        if isinstance(acquired, dict):
            metadata.update(
                {field: acquired[field] for field in ("title", "description") if isinstance(acquired.get(field), str)}
            )
    except (InnerTubeError, sqlite3.Error, AttributeError, TypeError, ValueError) as error:
        logger.warning("[Videos] Metadata preference unavailable for %s (%s): %s", video_id, lang, error)
    updates = {}
    if isinstance(metadata.get("title"), str):
        updates["title"] = metadata["title"]
    if isinstance(metadata.get("description"), str):
        description = metadata["description"]
        updates["description"] = description
        updates["descriptionHtml"] = escape(description).replace("\n", "<br>")
    # Preserve the existing recommendation policy, explicitly excluded from
    # META-04. Its legacy /next/cache remains separate; never use its main texts
    # to bypass the shared budget, leases, persistence or display selection.
    if (
        lang in ("fr", "en")
        and not invidious_only
        and innertube.is_enabled()
        and getattr(response, "extractionMethod", None) == "hybrid"
        and response.recommendedVideos
    ):
        try:
            localized = await get_video_localized_metadata(video_id, lang, include_recommendations=True)
            recommendations = localized.get("recommendedVideos") if isinstance(localized, dict) else None
            if isinstance(recommendations, dict) and recommendations:
                selected = []
                for video in response.recommendedVideos:
                    texts = recommendations.get(video.videoId)
                    text_updates = (
                        {field: texts[field] for field in ("title", "description") if isinstance(texts.get(field), str)}
                        if isinstance(texts, dict)
                        else {}
                    )
                    selected.append(video.model_copy(update=text_updates) if text_updates else video)
                updates["recommendedVideos"] = selected
        except (InnerTubeError, AttributeError, TypeError, ValueError) as error:
            logger.warning("[Videos] Recommendation preference unavailable for %s (%s): %s", video_id, lang, error)
    return response.model_copy(update=updates) if updates else response


async def _acquire_video_metadata(video_id: str, language: str, provider: dict) -> dict:
    """Use shared acquisition, then apply the normal selector to persisted facts.

    A follower waits at most two seconds for an existing acquisition, without
    reclaiming or retrying it. Slow/failed producers retain the provider fallback.
    Lazy imports keep the historical no-lang and forced-provider paths isolated.
    """
    from database.repositories.metadata_acquisitions import CoordinationError, MetadataStore
    from innertube._metadata import MetadataAcquirer
    from metadata_types import MetadataError

    store = MetadataStore()
    result = await MetadataAcquirer(store, on_demand=True, cache_ttl=0).acquire("youtube", video_id, language)
    if result.error == MetadataError.LEASE:
        deadline = asyncio.get_running_loop().time() + 2
        try:
            while asyncio.get_running_loop().time() < deadline:
                entry = store.read("youtube", video_id, language)
                if not entry or entry["owner"] is None:
                    break
                await asyncio.sleep(0.05)
        except CoordinationError:
            pass
    stored = _read_stored_metadata([video_id], language)
    return select_stored_metadata(
        provider, language, {candidate: stored.get((video_id, candidate), {}) for candidate in (language, "original")}
    )


async def _get_video_hybrid(
    video_id: str,
    base_url: str,
    *,
    proxy_streams: bool,
    proxy_mode: str,
    user_id: Optional[int],
) -> Optional[VideoResponse]:
    """Run InnerTube (/player + /next) and yt-dlp in parallel, merge the result.

    - If InnerTube reports the video is live or fails, use yt-dlp alone.
    - If yt-dlp fails while InnerTube succeeded, raise YtDlpError so the
      caller can attempt the Invidious fallback.
    """
    s = get_settings()

    it_video: Optional[dict] = None
    it_error: Optional[BaseException] = None

    if innertube.is_enabled():
        it_result, ytdlp_result = await asyncio.gather(
            innertube.get_video_player_next(video_id),
            get_video_info(video_id),
            return_exceptions=True,
        )
        if isinstance(it_result, BaseException):
            it_error = it_result
            if not isinstance(it_result, InnerTubeError):
                logger.warning(f"[Videos] InnerTube unexpected error for {video_id}: {it_result}")
            else:
                logger.info(f"[Videos] InnerTube failed for {video_id}: {it_result}")
        else:
            it_video = it_result
        if isinstance(ytdlp_result, BaseException):
            raise ytdlp_result
        info = ytdlp_result
    else:
        info = await get_video_info(video_id)

    # Live video or InnerTube unavailable → yt-dlp alone
    if it_video is None or it_video.get("liveNow", False):
        if it_error is None and it_video is None:
            logger.debug(f"[Videos] InnerTube disabled; using yt-dlp for {video_id}")
        response = ytdlp_to_video_response(
            info, base_url, proxy_streams=proxy_streams, proxy_mode=proxy_mode, user_id=user_id
        )
        response.extractionMethod = "ytdlp"
        channel_id = info.get("channel_id")
        if channel_id:
            avatar_cache.get_cache().schedule_background_fetch(channel_id)
        if s.invidious_author_thumbnails and channel_id and not response.authorThumbnails:
            thumbnails = await invidious_proxy.get_channel_thumbnails(channel_id)
            if thumbnails:
                response.authorThumbnails = thumbnails
            else:
                avatar_url = await get_channel_avatar(channel_id)
                if avatar_url:
                    response.authorThumbnails = [
                        Thumbnail(quality="default", url=avatar_url, width=176, height=176)
                    ]
        return response

    # Both succeeded — merge stream URLs by itag
    format_streams, adaptive_formats = innertube.merge_stream_urls(it_video, info.get("formats"))

    if not adaptive_formats and not format_streams:
        # InnerTube /player had no usable streamingData (commonly UNPLAYABLE
        # from data-centre IPs). Use yt-dlp's streams + base metadata and
        # enrich with InnerTube /next fields that yt-dlp doesn't provide.
        logger.info(
            f"[Videos] InnerTube /player had no matching itags for {video_id}; enriching yt-dlp response from /next"
        )
        response = ytdlp_to_video_response(
            info, base_url, proxy_streams=proxy_streams, proxy_mode=proxy_mode, user_id=user_id
        )
        _enrich_with_innertube_next(response, it_video)
        response.extractionMethod = "hybrid"
        channel_id = info.get("channel_id") or it_video.get("authorId")
        if channel_id:
            avatar_cache.get_cache().schedule_background_fetch(channel_id)
        return response

    merged = {**it_video, "formatStreams": format_streams, "adaptiveFormats": adaptive_formats}
    response = invidious_to_video_response(
        merged, base_url, proxy_streams=proxy_streams, proxy_mode=proxy_mode,
        invidious_base_url="", user_id=user_id,
    )
    response.extractionMethod = "hybrid"

    channel_id = it_video.get("authorId") or info.get("channel_id")
    if channel_id:
        avatar_cache.get_cache().schedule_background_fetch(channel_id)

    # Fall back to Invidious/scrape for author thumbnails only if still missing
    if s.invidious_author_thumbnails and channel_id and not response.authorThumbnails:
        thumbnails = await invidious_proxy.get_channel_thumbnails(channel_id)
        if thumbnails:
            response.authorThumbnails = thumbnails
        else:
            avatar_url = await get_channel_avatar(channel_id)
            if avatar_url:
                response.authorThumbnails = [
                    Thumbnail(quality="default", url=avatar_url, width=176, height=176)
                ]

    return response


def _enrich_with_innertube_next(response: VideoResponse, it_video: dict) -> None:
    """Overlay author thumbnails, recommended videos, and full description
    from an InnerTube /next-derived dict onto a yt-dlp-built response.

    Used when /player streamingData is missing (UNPLAYABLE / data-centre IP)
    but /next still has rich metadata we want to surface.
    """
    it_author_thumbs = it_video.get("authorThumbnails") or []
    if it_author_thumbs and not response.authorThumbnails:
        response.authorThumbnails = [
            Thumbnail(
                quality=t.get("quality", "default"),
                url=t.get("url", ""),
                width=t.get("width"),
                height=t.get("height"),
            )
            for t in it_author_thumbs
        ]

    it_desc = it_video.get("description")
    if it_desc and len(it_desc) > len(response.description or ""):
        response.description = it_desc

    it_recommended = it_video.get("recommendedVideos") or []
    if it_recommended and not response.recommendedVideos:
        converted = []
        for rec in it_recommended:
            try:
                converted.append(invidious_to_video_list_item(rec))
            except (KeyError, TypeError, ValueError):
                continue
        if converted:
            response.recommendedVideos = converted

    if response.isShort is None and it_video.get("isShort") is not None:
        response.isShort = it_video["isShort"]


@router.get("/extract", response_model=VideoResponse)
async def extract_video_url(
    request: Request, url: str = Query(..., description="URL to extract video from (any site yt-dlp supports)")
):
    """Extract video details from any URL that yt-dlp supports.

    This endpoint accepts arbitrary URLs (Vimeo, Twitter, TikTok, etc.) and
    returns video information in the same format as /videos/{id}.

    The response includes:
    - `extractor`: Site identifier (e.g., "vimeo", "twitter")
    - `originalUrl`: The URL for re-extraction when stream URLs expire

    Note: Stream URLs from most sites expire after a few hours.

    Args:
        url: Full URL to extract (e.g., https://vimeo.com/12345)
    """
    # Validate URL format
    if not is_valid_url(url):
        raise HTTPException(status_code=400, detail="Invalid URL format")

    # SSRF prevention - block requests to private/internal networks
    if not is_safe_url(url):
        raise HTTPException(status_code=403, detail="URL targets restricted network resources")

    base_url = get_base_url(request)

    # Get user_id for token generation if basic auth is enabled
    user = get_current_user_from_request(request)
    user_id = user.get("id") if user else None

    try:
        info = await extract_url(url)

        # Check site configuration for proxy_streaming setting
        extractor = info.get("extractor", "")

        # Validate that this extractor is allowed
        validate_extractor_allowed(extractor)

        site_config = database.get_site_by_extractor(extractor)
        proxy_streams = site_config.get("proxy_streaming", True) if site_config else True
        # `extract_url` is the share-extension / external-URL flow; downloads
        # of arbitrary sites benefit from the on-disk caching of /proxy/fast/,
        # so default to "download" mode here rather than "relay".
        ext_proxy_mode = "download" if proxy_streams else "off"

        response = ytdlp_to_video_response(
            info, base_url, proxy_streams=proxy_streams, proxy_mode=ext_proxy_mode,
            original_url=url, user_id=user_id,
        )
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except YtDlpError as e:
        raise HTTPException(status_code=422, detail=f"Could not extract video: {e}")
    except (KeyError, TypeError) as e:
        logger.error(f"[Videos] Unexpected error extracting {url}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/extract/channel", response_model=ChannelExtractResponse)
async def extract_channel(
    url: str = Query(..., description="Channel/user URL to extract videos from (any site yt-dlp supports)"),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
):
    """Extract videos from a channel/user page on any site that yt-dlp supports.

    This endpoint accepts channel/user URLs (Vimeo, Dailymotion, TikTok, etc.)
    and returns a list of videos from that channel.

    The response includes:
    - `author`: Channel/user name
    - `authorId`: Channel/user identifier
    - `authorUrl`: The channel URL (for re-extraction)
    - `extractor`: Site name (e.g., "Vimeo", "Dailymotion")
    - `videos`: List of videos with metadata
    - `continuation`: Next page number as string, or null if no more pages

    Note: Not all sites support channel extraction. If extraction fails,
    a 422 error is returned with details.

    Args:
        url: Full channel/user URL (e.g., https://vimeo.com/username)
        page: Page number (default: 1)
    """
    # Validate URL format
    if not is_valid_url(url):
        raise HTTPException(status_code=400, detail="Invalid URL format")

    # SSRF prevention - block requests to private/internal networks
    if not is_safe_url(url):
        raise HTTPException(status_code=403, detail="URL targets restricted network resources")

    try:
        info = await extract_channel_url(url, page=page)

        # Validate that this extractor is allowed
        extractor = info.get("extractor", "generic")
        validate_extractor_allowed(extractor)

        # Convert entries to VideoListItem
        videos = []
        for entry in info.get("entries", []):
            try:
                videos.append(ytdlp_to_video_list_item(entry))
            except (KeyError, TypeError, ValueError):
                continue  # Skip entries that fail to convert

        # Determine if there are more pages (if we got a full page, there might be more)
        per_page = 30
        continuation = str(page + 1) if len(videos) >= per_page else None

        return ChannelExtractResponse(
            author=info.get("channel", ""),
            authorId=info.get("channel_id", ""),
            authorUrl=info.get("channel_url", url),
            extractor=info.get("extractor", "generic"),
            videos=videos,
            continuation=continuation,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except YtDlpError as e:
        raise HTTPException(status_code=422, detail=f"Could not extract channel: {e}")
    except (KeyError, TypeError) as e:
        logger.error(f"[Videos] Unexpected error extracting channel {url}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
