"""Convert InnerTube response structures to Invidious-compatible dicts.

These converters produce dicts in the same shape as the Invidious API,
so the existing `invidious_to_video_list_item()` etc. converters can be reused.
"""

import base64
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("innertube")


def _extract_text(obj: Any) -> str:
    """Extract plain text from InnerTube text objects."""
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    if "simpleText" in obj:
        return obj["simpleText"]
    if "runs" in obj:
        return "".join(run.get("text", "") for run in obj["runs"])
    return ""


def _optional_list_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    for key in ("simpleText", "content"):
        if isinstance(value.get(key), str):
            return value[key]
    runs = value.get("runs")
    if isinstance(runs, list) and all(isinstance(run, dict) and isinstance(run.get("text"), str) for run in runs):
        return "".join(run["text"] for run in runs)
    return None


def _list_description(renderer: dict) -> Optional[str]:
    for key in ("descriptionSnippet", "description"):
        text = _optional_list_text(renderer.get(key))
        if text is not None:
            return text
    snippets = renderer.get("detailedMetadataSnippets")
    if isinstance(snippets, list):
        texts = [_optional_list_text(row.get("snippetText")) for row in snippets if isinstance(row, dict)]
        texts = [text for text in texts if text is not None]
        if texts:
            return "\n".join(texts)
    return None


def _localized_view_count(text: str) -> Optional[int]:
    normalized = " ".join(text.lower().split())
    if normalized in ("no views", "aucune vue", "aucune vues"):
        return 0
    match = re.search(
        r"(\d[\d\s.,]*?)\s*(milliers?|millions?|milliards?|md|[kmb])?\s*(?:de\s+)?(?:views?|vues?)\b",
        normalized,
    )
    if not match:
        return None
    number, suffix = match.groups()
    number = number.replace(" ", "")
    if suffix:
        if "," in number and "." in number:
            decimal, grouping = (",", ".") if number.rfind(",") > number.rfind(".") else (".", ",")
            number = number.replace(grouping, "").replace(decimal, ".")
        else:
            number = number.replace(",", ".")
    else:
        number = number.replace(",", "").replace(".", "")
    multiplier = 1
    if suffix:
        multiplier = 1000 if suffix == "k" or suffix.startswith("millier") else 1_000_000
        if suffix in ("b", "md") or suffix.startswith("milliard"):
            multiplier = 1_000_000_000
    try:
        return int(float(number) * multiplier)
    except (ValueError, OverflowError):
        return None


def _localized_published(text: str) -> Optional[int]:
    normalized = " ".join(text.lower().split())
    french = re.search(
        r"il y a (\d+|un|une)\s+(secondes?|minutes?|heures?|jours?|semaines?|mois|ans?|années?)\b", normalized
    )
    english = re.search(r"(\d+|an?|one)\s+(seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\b", normalized)
    match = french or english
    if not match:
        return None
    number, unit = match.groups()
    units = {
        "seconde": 1,
        "second": 1,
        "minute": 60,
        "heure": 3600,
        "hour": 3600,
        "jour": 86400,
        "day": 86400,
        "semaine": 604800,
        "week": 604800,
        "mois": 2592000,
        "month": 2592000,
        "an": 31536000,
        "année": 31536000,
        "year": 31536000,
    }
    multiplier = units.get(unit, units.get(unit.rstrip("s")))
    if multiplier is None:
        return None
    return int(time.time()) - (int(number) if number.isdigit() else 1) * multiplier


def _localize_list_video(raw_item: dict, converted: dict, *, localized: bool = False) -> dict:
    """Correct FR/EN list fields without changing historical/shared converters."""
    if not localized or converted.get("type", "video") != "video":
        return converted
    wrapper = raw_item.get("richItemRenderer", raw_item)
    wrapper = wrapper.get("content", wrapper)
    renderer = next(
        (
            wrapper[key]
            for key in (
                "videoRenderer",
                "gridVideoRenderer",
                "lockupViewModel",
                "reelItemRenderer",
                "shortsLockupViewModel",
            )
            if isinstance(wrapper.get(key), dict)
        ),
        {},
    )
    result = {**converted, "description": _list_description(renderer)}
    metadata = renderer.get("metadata", {}).get("lockupMetadataViewModel", {})
    rows = metadata.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
    texts = [part.get("text", {}).get("content", "") for row in rows for part in row.get("metadataParts", [])]
    texts.extend(
        text
        for text in (
            _optional_list_text(renderer.get("viewCountText")),
            _optional_list_text(renderer.get("publishedTimeText")),
            converted.get("viewCountText"),
            converted.get("publishedText"),
        )
        if isinstance(text, str)
    )
    for text in texts:
        if not isinstance(text, str):
            continue
        count = _localized_view_count(text)
        published = _localized_published(text)
        if count is not None:
            result.update(viewCount=count, viewCountText=text)
        if published is not None:
            result.update(published=published, publishedText=text)
        # A metrics-only first lockup row is not a channel name.
        if (count is not None or published is not None) and not result.get("authorId") and result.get("author") == text:
            result["author"] = ""
    return result


def _extract_thumbnails(obj: Any) -> List[Dict[str, Any]]:
    """Extract thumbnails from InnerTube thumbnail container."""
    if not obj:
        return []
    thumbs = obj.get("thumbnails", [])
    return [
        {
            "quality": _thumbnail_quality(t.get("width", 0)),
            "url": t.get("url", ""),
            "width": t.get("width"),
            "height": t.get("height"),
        }
        for t in thumbs
    ]


def _thumbnail_quality(width: int) -> str:
    """Map thumbnail width to quality label."""
    if width >= 1280:
        return "maxres"
    if width >= 640:
        return "sddefault"
    if width >= 480:
        return "high"
    if width >= 320:
        return "medium"
    return "default"


_STANDARD_THUMBNAILS = [
    ("default.jpg", "default", 120, 90),
    ("mqdefault.jpg", "medium", 320, 180),
    ("hqdefault.jpg", "high", 480, 360),
    ("sddefault.jpg", "sddefault", 640, 480),
    ("maxresdefault.jpg", "maxres", 1280, 720),
]


def _standard_video_thumbnails(video_id: str) -> List[Dict[str, Any]]:
    """Generate standard YouTube thumbnail entries for a video ID."""
    if not video_id:
        return []
    return [
        {"quality": quality, "url": f"https://i.ytimg.com/vi/{video_id}/{filename}", "width": width, "height": height}
        for filename, quality, width, height in _STANDARD_THUMBNAILS
    ]


def _parse_count_text(text: str) -> Optional[int]:
    """Parse a count string like '1.2K views', '3M subscribers' to an integer."""
    if not text:
        return None
    text = text.strip().upper().replace(",", "")
    # Remove trailing words like "views", "subscribers"
    parts = text.split()
    if not parts:
        return None
    num_part = parts[0]

    multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    for suffix, mult in multipliers.items():
        if num_part.endswith(suffix):
            try:
                return int(float(num_part[:-1]) * mult)
            except ValueError:
                return None
    try:
        return int(num_part)
    except ValueError:
        return None


def _parse_duration_text(text: str) -> int:
    """Parse duration text like '12:34' or '1:23:45' to seconds."""
    if not text:
        return 0
    parts = text.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 1:
            return int(parts[0])
    except ValueError:
        pass
    return 0


def video_renderer_to_invidious(renderer: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an InnerTube videoRenderer to Invidious video dict format.

    The output dict can be passed to `invidious_to_video_list_item()`.
    """
    video_id = renderer.get("videoId", "")
    title = _extract_text(renderer.get("title"))

    # Author info
    channel = renderer.get("ownerText") or renderer.get("longBylineText") or renderer.get("shortBylineText", {})
    author = _extract_text(channel)
    author_id = ""
    author_url = ""
    if "runs" in channel:
        for run in channel["runs"]:
            nav = run.get("navigationEndpoint", {})
            browse = nav.get("browseEndpoint", {})
            if browse.get("browseId"):
                author_id = browse["browseId"]
                author_url = f"/channel/{author_id}"
                break

    # Thumbnails – use well-known YouTube CDN URLs for full quality range
    thumbnails = _standard_video_thumbnails(video_id)

    # Duration
    length_text = _extract_text(renderer.get("lengthText"))
    length_seconds = _parse_duration_text(length_text)

    # For accessibility-based duration (more reliable)
    if not length_seconds:
        accessibility = renderer.get("lengthText", {}).get("accessibility", {}).get("accessibilityData", {})
        duration_label = accessibility.get("label", "")
        # Parse "12 minutes, 34 seconds" format
        length_seconds = _parse_accessibility_duration(duration_label)

    # View count
    view_count_text = _extract_text(renderer.get("viewCountText"))
    view_count = _parse_count_text(view_count_text)

    # Published time
    published_text = _extract_text(renderer.get("publishedTimeText"))

    # Live status
    badges = renderer.get("badges", [])
    is_live = False
    for badge in badges:
        badge_renderer = badge.get("metadataBadgeRenderer", {})
        if badge_renderer.get("style") in ("BADGE_STYLE_TYPE_LIVE_NOW", "BADGE_STYLE_TYPE_LIVE_NOW_DEFAULT"):
            is_live = True
            break

    # Also check thumbnailOverlays for live status
    for overlay in renderer.get("thumbnailOverlays", []):
        status = overlay.get("thumbnailOverlayTimeStatusRenderer", {})
        if status.get("style") == "LIVE":
            is_live = True
            break

    # Description snippet
    description = _extract_text(renderer.get("descriptionSnippet") or renderer.get("detailedMetadataSnippets", [{}]))

    return {
        "type": "video",
        "videoId": video_id,
        "title": title,
        "description": description,
        "author": author,
        "authorId": author_id,
        "authorUrl": author_url,
        "videoThumbnails": thumbnails,
        "lengthSeconds": length_seconds,
        "viewCount": view_count,
        "viewCountText": view_count_text,
        "published": None,
        "publishedText": published_text,
        "liveNow": is_live,
        "isUpcoming": False,
        "isShort": False,
    }


def _parse_accessibility_duration(label: str) -> int:
    """Parse accessibility duration label like '12 minutes, 34 seconds'."""
    if not label:
        return 0
    total = 0
    parts = label.lower().replace(",", "").split()
    i = 0
    while i < len(parts) - 1:
        try:
            num = int(parts[i])
            unit = parts[i + 1]
            if "hour" in unit:
                total += num * 3600
            elif "minute" in unit:
                total += num * 60
            elif "second" in unit:
                total += num
            i += 2
        except (ValueError, IndexError):
            i += 1
    return total


def playlist_video_renderer_to_invidious(renderer: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an InnerTube playlistVideoRenderer to Invidious video dict format.

    Used for entries inside a playlistVideoListRenderer (playlist browse response).
    """
    video_id = renderer.get("videoId", "")
    title = _extract_text(renderer.get("title"))

    # Author – from shortBylineText runs
    byline = renderer.get("shortBylineText") or renderer.get("longBylineText") or {}
    author = _extract_text(byline)
    author_id = ""
    author_url = ""
    if isinstance(byline, dict):
        for run in byline.get("runs", []):
            browse = run.get("navigationEndpoint", {}).get("browseEndpoint", {})
            if browse.get("browseId"):
                author_id = browse["browseId"]
                author_url = f"/channel/{author_id}"
                break

    # Thumbnails – prefer well-known CDN URLs for consistent full range
    thumbnails = _standard_video_thumbnails(video_id)
    if not thumbnails:
        thumbnails = _extract_thumbnails(renderer.get("thumbnail"))

    # Duration – playlistVideoRenderer usually has numeric lengthSeconds string
    length_seconds = 0
    raw_length = renderer.get("lengthSeconds")
    if raw_length is not None:
        try:
            length_seconds = int(raw_length)
        except (TypeError, ValueError):
            length_seconds = 0
    if not length_seconds:
        length_seconds = _parse_duration_text(_extract_text(renderer.get("lengthText")))
    if not length_seconds:
        accessibility = renderer.get("lengthText", {}).get("accessibility", {}).get("accessibilityData", {})
        length_seconds = _parse_accessibility_duration(accessibility.get("label", ""))

    # Live status – thumbnailOverlays with LIVE style
    is_live = False
    for overlay in renderer.get("thumbnailOverlays", []):
        status = overlay.get("thumbnailOverlayTimeStatusRenderer", {})
        if status.get("style") == "LIVE":
            is_live = True
            break

    return {
        "type": "video",
        "videoId": video_id,
        "title": title,
        "description": "",
        "author": author,
        "authorId": author_id,
        "authorUrl": author_url,
        "videoThumbnails": thumbnails,
        "lengthSeconds": length_seconds,
        "viewCount": 0,
        "viewCountText": "",
        "published": None,
        "publishedText": None,
        "liveNow": is_live,
        "isUpcoming": False,
    }


def playlist_renderer_to_invidious(renderer: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an InnerTube playlistRenderer to Invidious playlist dict format."""
    playlist_id = renderer.get("playlistId", "")
    title = _extract_text(renderer.get("title"))

    # Author
    channel = renderer.get("longBylineText") or renderer.get("shortBylineText", {})
    author = _extract_text(channel)
    author_id = ""
    if "runs" in channel:
        for run in channel["runs"]:
            browse = run.get("navigationEndpoint", {}).get("browseEndpoint", {})
            if browse.get("browseId"):
                author_id = browse["browseId"]
                break

    # Video count
    video_count_text = _extract_text(renderer.get("videoCount") or renderer.get("videoCountText"))
    video_count = 0
    if video_count_text:
        try:
            video_count = int(video_count_text.replace(",", "").split()[0])
        except (ValueError, IndexError):
            pass

    # Thumbnail
    thumbnails = _extract_thumbnails(renderer.get("thumbnail") or renderer.get("thumbnails"))
    playlist_thumbnail = thumbnails[0]["url"] if thumbnails else None

    return {
        "type": "playlist",
        "playlistId": playlist_id,
        "title": title,
        "author": author,
        "authorId": author_id,
        "videoCount": video_count,
        "playlistThumbnail": playlist_thumbnail,
        "videos": [],
    }


def lockup_view_model_to_invidious(renderer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert an InnerTube lockupViewModel to Invidious format.

    YouTube uses lockupViewModel for playlist (and sometimes channel) results
    in search instead of the older playlistRenderer/channelRenderer.
    """
    content_type = renderer.get("contentType", "")
    content_id = renderer.get("contentId", "")

    if not content_id:
        return None

    # Extract title
    title = (
        renderer.get("metadata", {})
        .get("lockupMetadataViewModel", {})
        .get("title", {})
        .get("content", "")
    )

    # Extract metadata rows for author info
    metadata_rows = (
        renderer.get("metadata", {})
        .get("lockupMetadataViewModel", {})
        .get("metadata", {})
        .get("contentMetadataViewModel", {})
        .get("metadataRows", [])
    )

    author = ""
    author_id = ""
    for row in metadata_rows:
        for part in row.get("metadataParts", []):
            text_obj = part.get("text", {})
            for cmd_run in text_obj.get("commandRuns", []):
                browse = (
                    cmd_run.get("onTap", {})
                    .get("innertubeCommand", {})
                    .get("browseEndpoint", {})
                )
                if browse.get("browseId") and not author_id:
                    author = text_obj.get("content", "")
                    author_id = browse["browseId"]
                    break
            if author_id:
                break
        if author_id:
            break

    if content_type == "LOCKUP_CONTENT_TYPE_PLAYLIST":
        # Extract video count from thumbnail badge
        video_count = 0
        badges = (
            renderer.get("contentImage", {})
            .get("collectionThumbnailViewModel", {})
            .get("primaryThumbnail", {})
            .get("thumbnailViewModel", {})
            .get("overlays", [])
        )
        for overlay in badges:
            badge_vm = overlay.get("thumbnailOverlayBadgeViewModel", {})
            for badge in badge_vm.get("thumbnailBadges", []):
                badge_text = badge.get("thumbnailBadgeViewModel", {}).get("text", "")
                if badge_text:
                    try:
                        video_count = int(badge_text.replace(",", "").split()[0])
                    except (ValueError, IndexError):
                        pass

        # Extract thumbnail
        thumb_sources = (
            renderer.get("contentImage", {})
            .get("collectionThumbnailViewModel", {})
            .get("primaryThumbnail", {})
            .get("thumbnailViewModel", {})
            .get("image", {})
            .get("sources", [])
        )
        playlist_thumbnail = thumb_sources[0].get("url", "") if thumb_sources else None

        return {
            "type": "playlist",
            "playlistId": content_id,
            "title": title,
            "author": author,
            "authorId": author_id,
            "videoCount": video_count,
            "playlistThumbnail": playlist_thumbnail,
            "videos": [],
        }

    if content_type == "LOCKUP_CONTENT_TYPE_CHANNEL":
        return {
            "type": "channel",
            "authorId": content_id,
            "author": title,
            "description": "",
            "subCount": None,
            "subCountText": "",
            "videoCount": None,
            "authorThumbnails": [],
            "authorVerified": False,
        }

    return None


def channel_renderer_to_invidious(renderer: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an InnerTube channelRenderer to Invidious channel dict format."""
    channel_id = renderer.get("channelId", "")
    title = _extract_text(renderer.get("title"))
    description = _extract_text(renderer.get("descriptionSnippet"))

    # Subscriber count
    sub_count_text = _extract_text(renderer.get("subscriberCountText"))
    sub_count = _parse_count_text(sub_count_text)

    # Video count
    video_count_text = _extract_text(renderer.get("videoCountText"))
    video_count = _parse_count_text(video_count_text)

    # Thumbnails
    thumbnails = _extract_thumbnails(renderer.get("thumbnail"))

    # Verified badge
    verified = False
    for badge in renderer.get("ownerBadges", []):
        if badge.get("metadataBadgeRenderer", {}).get("style") == "BADGE_STYLE_TYPE_VERIFIED":
            verified = True
            break

    return {
        "type": "channel",
        "authorId": channel_id,
        "author": title,
        "description": description,
        "subCount": sub_count,
        "subCountText": sub_count_text,
        "videoCount": video_count,
        "authorThumbnails": thumbnails,
        "authorVerified": verified,
    }


def rich_item_to_invidious(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert an InnerTube richItemRenderer to Invidious format.

    richItemRenderer wraps the actual item used in rich grids. YouTube channel
    video tabs historically used videoRenderer here, but current WEB responses
    use lockupViewModel. Video-typed lockups go through the dedicated video
    converter; playlist/channel-typed lockups fall through to the general
    lockup handler so rich grids that mix content types still produce items.
    """
    content = item.get("content", {})
    if "videoRenderer" in content:
        return video_renderer_to_invidious(content["videoRenderer"])
    if "reelItemRenderer" in content:
        return _reel_item_to_invidious(content["reelItemRenderer"])
    if "shortsLockupViewModel" in content:
        return _shorts_lockup_to_invidious(content["shortsLockupViewModel"])
    if "lockupViewModel" in content:
        lvm = content["lockupViewModel"]
        if lvm.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
            return _lockup_video_view_model_to_invidious(lvm)
        return lockup_view_model_to_invidious(lvm)
    return None


def _shorts_lockup_to_invidious(renderer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a shortsLockupViewModel (shorts shelf entry) to Invidious shape.

    Shape varies — videoId can live in onTap.innertubeCommand.reelWatchEndpoint
    or as a suffix on entityId. Return None if neither yields an id.
    """
    video_id = ""
    reel = (
        renderer.get("onTap", {})
        .get("innertubeCommand", {})
        .get("reelWatchEndpoint", {})
    )
    if reel.get("videoId"):
        video_id = reel["videoId"]
    if not video_id:
        entity_id = renderer.get("entityId", "")
        if entity_id and "-" in entity_id:
            video_id = entity_id.rsplit("-", 1)[-1]
    if not video_id:
        return None

    overlay = renderer.get("overlayMetadata", {}) or {}
    title = overlay.get("primaryText", {}).get("content", "")
    view_count_text = overlay.get("secondaryText", {}).get("content", "")

    return {
        "type": "video",
        "videoId": video_id,
        "title": title,
        "description": "",
        "author": "",
        "authorId": "",
        "authorUrl": "",
        "videoThumbnails": _standard_video_thumbnails(video_id),
        "lengthSeconds": 0,
        "viewCount": _parse_count_text(view_count_text),
        "viewCountText": view_count_text,
        "published": None,
        "publishedText": None,
        "liveNow": False,
        "isUpcoming": False,
        "isShort": True,
    }


def _reel_item_to_invidious(renderer: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a reelItemRenderer (shorts) to Invidious video format."""
    video_id = renderer.get("videoId", "")
    title = _extract_text(renderer.get("headline"))
    thumbnails = _standard_video_thumbnails(video_id)
    view_count_text = _extract_text(renderer.get("viewCountText"))

    # Reels often omit lengthText, but when present we prefer the real value.
    length_seconds = _parse_duration_text(_extract_text(renderer.get("lengthText")))
    if not length_seconds:
        accessibility = renderer.get("accessibility", {}).get("accessibilityData", {})
        length_seconds = _parse_accessibility_duration(accessibility.get("label", ""))

    return {
        "type": "video",
        "videoId": video_id,
        "title": title,
        "description": "",
        "author": "",
        "authorId": "",
        "authorUrl": "",
        "videoThumbnails": thumbnails,
        "lengthSeconds": length_seconds,
        "viewCount": _parse_count_text(view_count_text),
        "viewCountText": view_count_text,
        "published": None,
        "publishedText": None,
        "liveNow": False,
        "isUpcoming": False,
        "isShort": True,
    }


def grid_video_to_invidious(renderer: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a gridVideoRenderer to Invidious video format.

    Used in channel video tabs.
    """
    video_id = renderer.get("videoId", "")
    title = _extract_text(renderer.get("title"))
    thumbnails = _standard_video_thumbnails(video_id)
    published_text = _extract_text(renderer.get("publishedTimeText"))
    view_count_text = _extract_text(renderer.get("viewCountText"))

    channel = renderer.get("ownerText") or renderer.get("longBylineText") or renderer.get("shortBylineText", {})
    author = _extract_text(channel)
    author_id = ""
    author_url = ""
    if "runs" in channel:
        for run in channel["runs"]:
            browse = run.get("navigationEndpoint", {}).get("browseEndpoint", {})
            if browse.get("browseId"):
                author_id = browse["browseId"]
                author_url = f"/channel/{author_id}"
                break

    # Get duration from thumbnail overlay
    length_seconds = 0
    for overlay in renderer.get("thumbnailOverlays", []):
        time_status = overlay.get("thumbnailOverlayTimeStatusRenderer", {})
        length_seconds = _parse_duration_text(_extract_text(time_status.get("text")))
        if length_seconds:
            break

    return {
        "type": "video",
        "videoId": video_id,
        "title": title,
        "description": "",
        "author": author,
        "authorId": author_id,
        "authorUrl": author_url,
        "videoThumbnails": thumbnails,
        "lengthSeconds": length_seconds,
        "viewCount": _parse_count_text(view_count_text),
        "viewCountText": view_count_text,
        "published": None,
        "publishedText": published_text,
        "liveNow": False,
        "isUpcoming": False,
        "isShort": False,
    }


def grid_playlist_to_invidious(renderer: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a gridPlaylistRenderer to Invidious playlist format."""
    playlist_id = renderer.get("playlistId", "")
    title = _extract_text(renderer.get("title"))
    thumbnails = _extract_thumbnails(renderer.get("thumbnail"))

    video_count_text = _extract_text(renderer.get("videoCountText") or renderer.get("videoCountShortText"))
    video_count = 0
    if video_count_text:
        try:
            video_count = int(video_count_text.replace(",", "").split()[0])
        except (ValueError, IndexError):
            pass

    return {
        "type": "playlist",
        "playlistId": playlist_id,
        "title": title,
        "author": "",
        "authorId": "",
        "videoCount": video_count,
        "playlistThumbnail": thumbnails[0]["url"] if thumbnails else None,
        "videos": [],
    }


# =============================================================================
# /player + /next converter (video metadata)
# =============================================================================


_MIME_RE = re.compile(r'^([^/]+)/([^;]+)(?:;\s*codecs="([^"]+)")?')


def _parse_mime_type(mime_type: str) -> Dict[str, str]:
    """Split a mimeType like 'video/mp4; codecs="avc1.64001f"' into parts."""
    if not mime_type:
        return {"type": "", "container": "", "encoding": ""}
    match = _MIME_RE.match(mime_type)
    if not match:
        return {"type": mime_type, "container": "", "encoding": ""}
    _top, container, encoding = match.group(1), match.group(2), match.group(3) or ""
    return {"type": mime_type, "container": container, "encoding": encoding}


def _decode_xtags(raw: Optional[str]) -> Dict[str, str]:
    """Decode InnerTube's base64url protobuf `xtags` into key/value pairs.

    The WEB /player response encodes per-track audio metadata as a protobuf
    message (repeated `{string key = 1; string value = 2;}`), e.g. decoding
    `ChEKBWFjb250EghvcmlnaW5hbAoNCgRsYW5nEgVlbi1VUw` yields
    `{"acont": "original", "lang": "en-US"}`. The same pairs appear in plain
    `acont=original:lang=en-US` form on the deciphered googlevideo URL, which
    is what lets `merge_stream_urls` pair multi-audio-track formats.

    Returns an empty dict for a missing or undecodable value.
    """
    if not raw:
        return {}
    try:
        padded = raw.replace("-", "+").replace("_", "/")
        data = base64.b64decode(padded + "=" * (-len(padded) % 4))
    except (ValueError, TypeError):
        return {}

    def _varint(buf: bytes, pos: int) -> tuple:
        result = shift = 0
        while True:
            byte = buf[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return result, pos

    out: Dict[str, str] = {}
    try:
        i = 0
        while i < len(data):
            tag, i = _varint(data, i)
            if tag != 0x0A:  # field 1, length-delimited
                return {}
            length, i = _varint(data, i)
            entry = data[i : i + length]
            i += length
            j = 0
            pair: Dict[int, str] = {}
            while j < len(entry):
                inner_tag, j = _varint(entry, j)
                inner_len, j = _varint(entry, j)
                pair[inner_tag >> 3] = entry[j : j + inner_len].decode("utf-8")
                j += inner_len
            if 1 in pair:
                out[pair[1]] = pair.get(2, "")
    except (IndexError, UnicodeDecodeError):
        return {}
    return out


def _format_to_invidious(fmt: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a single InnerTube streamingData format to Invidious shape.

    URL is intentionally left empty — WEB client URLs are ciphered, so the
    caller must join yt-dlp's deciphered URL by itag. The decoded `xtags`
    (audio language / DRC / ...) travel along under `_xtags` so the join can
    tell multi-audio-track variants of the same itag apart; the router strips
    that key before the response is built.
    """
    mime_type = fmt.get("mimeType", "")
    parts = _parse_mime_type(mime_type)
    height = fmt.get("height")
    width = fmt.get("width")
    bitrate = fmt.get("bitrate")
    is_audio = parts["type"].startswith("audio/") if parts["type"] else False

    result: Dict[str, Any] = {
        "url": "",
        "itag": str(fmt.get("itag") or ""),
        "type": parts["type"],
        "container": parts["container"],
        "resolution": f"{height}p" if height else None,
        "width": width if not is_audio else None,
        "height": height if not is_audio else None,
        "encoding": parts["encoding"] or None,
        "fps": fmt.get("fps"),
        "bitrate": str(bitrate) if bitrate is not None else None,
        "clen": str(fmt.get("contentLength")) if fmt.get("contentLength") else None,
        "quality": fmt.get("qualityLabel") or fmt.get("quality", "") or (f"{height}p" if height else ""),
        "size": str(fmt.get("contentLength")) if fmt.get("contentLength") else None,
        "audioQuality": fmt.get("audioQuality") if is_audio else None,
        "_xtags": _decode_xtags(fmt.get("xtags")),
    }

    audio_track = fmt.get("audioTrack")
    if audio_track:
        result["audioTrack"] = {
            "id": audio_track.get("id"),
            "displayName": audio_track.get("displayName"),
            "isDefault": audio_track.get("audioIsDefault", False),
        }

    return result


def _parse_storyboard_spec(spec: str, video_id: str) -> List[Dict[str, Any]]:
    """Parse a YouTube storyboard spec string into Invidious-shaped storyboards.

    Spec format: `BASE_URL|LEVEL1|LEVEL2|...` where each LEVEL is:
    `w#h#count#cols#rows#interval#name#sig`
    """
    if not spec:
        return []
    parts = spec.split("|")
    if len(parts) < 2:
        return []
    base_url = parts[0]
    storyboards = []
    for level_idx, level_str in enumerate(parts[1:]):
        fields = level_str.split("#")
        if len(fields) < 8:
            continue
        try:
            w = int(fields[0])
            h = int(fields[1])
            total_count = int(fields[2])
            cols = int(fields[3])
            rows = int(fields[4])
            interval = int(fields[5])
            name = fields[6]
            sig = fields[7]
        except (ValueError, IndexError):
            continue

        # base_url has $L placeholder for level; $N for sheet number
        template = base_url.replace("$L", str(level_idx)).replace("$N", name)
        if "?" in template:
            template = f"{template}&sigh={sig}"
        else:
            template = f"{template}?sigh={sig}"

        storyboards.append({
            # `url` would be treated by Yattee clients as a proxy endpoint
            # (appending `&storyboard=N`); a direct ytimg URL ignores that
            # and always returns sheet 0. Leave unset so the client uses
            # `templateUrl` with $M substitution.
            "url": None,
            "templateUrl": template,
            "width": w,
            "height": h,
            "count": total_count,
            "interval": interval,
            "storyboardWidth": cols,
            "storyboardHeight": rows,
            "storyboardCount": max(1, (total_count + cols * rows - 1) // (cols * rows)) if cols and rows else 0,
        })
    _ = video_id  # reserved for future use
    return storyboards


def _captions_from_player(player: Dict[str, Any]) -> List[Dict[str, Any]]:
    tracks = (
        player.get("captions", {})
        .get("playerCaptionsTracklistRenderer", {})
        .get("captionTracks", [])
    )
    captions = []
    for track in tracks:
        label = _extract_text(track.get("name")) or ""
        lang = track.get("languageCode", "") or ""
        is_auto = track.get("kind") == "asr" or "(auto" in label.lower()
        captions.append({
            "label": label,
            "languageCode": lang,
            "url": track.get("baseUrl", ""),
            "auto_generated": is_auto,
        })
    return captions


def _published_timestamp(microformat: Dict[str, Any]) -> Optional[int]:
    date_str = microformat.get("publishDate") or microformat.get("uploadDate")
    if not date_str:
        return None
    try:
        if "T" in date_str:
            return int(datetime.fromisoformat(date_str.replace("Z", "+00:00")).timestamp())
        return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
    except (ValueError, TypeError):
        return None


def _author_thumbnails_from_next(nxt: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract channel avatar thumbnails from /next videoOwnerRenderer."""
    contents = (
        nxt.get("contents", {})
        .get("twoColumnWatchNextResults", {})
        .get("results", {})
        .get("results", {})
        .get("contents", [])
    )
    for item in contents:
        owner = (
            item.get("videoSecondaryInfoRenderer", {})
            .get("owner", {})
            .get("videoOwnerRenderer", {})
        )
        thumbs = owner.get("thumbnail", {}).get("thumbnails", [])
        if thumbs:
            return [
                {
                    "quality": _thumbnail_quality(t.get("width", 0)),
                    "url": t.get("url", ""),
                    "width": t.get("width"),
                    "height": t.get("height"),
                }
                for t in thumbs
            ]
    return []


def _metadata_from_next(nxt: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Extract the current video's title and description from a /next response.

    Prefer WEB primary/secondary info, then structured description panels,
    independently for each field. None means missing or malformed; an explicit
    empty string is preserved and prevents fallback. Only known metadata paths
    are visited, never sidebar recommendations. The input is not modified.
    """

    def at_path(value: Any, *keys: str) -> Any:
        for key in keys:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value

    def text(value: Any) -> Optional[str]:
        if not isinstance(value, dict):
            return None
        simple = value.get("simpleText")
        if isinstance(simple, str):
            return simple
        runs = value.get("runs")
        if not isinstance(runs, list):
            return None
        if not all(isinstance(run, dict) and isinstance(run.get("text"), str) for run in runs):
            return None
        return "".join(run["text"] for run in runs)

    def attributed_text(value: Any) -> Optional[str]:
        content = at_path(value, "content")
        return content if isinstance(content, str) else None

    metadata: Dict[str, Optional[str]] = {"title": None, "description": None}
    contents = at_path(nxt, "contents", "twoColumnWatchNextResults", "results", "results", "contents")
    for item in contents if isinstance(contents, list) else []:
        if metadata["title"] is None:
            metadata["title"] = text(at_path(item, "videoPrimaryInfoRenderer", "title"))
        if metadata["description"] is None:
            secondary = at_path(item, "videoSecondaryInfoRenderer")
            description = attributed_text(at_path(secondary, "attributedDescription"))
            if description is None:
                description = text(at_path(secondary, "description"))
            metadata["description"] = description

    panels = at_path(nxt, "engagementPanels")
    for panel in panels if isinstance(panels, list) else []:
        items = at_path(
            panel, "engagementPanelSectionListRenderer", "content", "structuredDescriptionContentRenderer", "items"
        )
        for item in items if isinstance(items, list) else []:
            if metadata["title"] is None:
                metadata["title"] = text(at_path(item, "videoDescriptionHeaderRenderer", "title"))
            if metadata["description"] is None:
                metadata["description"] = attributed_text(
                    at_path(item, "expandableVideoDescriptionBodyRenderer", "attributedDescriptionBodyText")
                )

    return metadata


def _full_description_from_next(nxt: Dict[str, Any]) -> str:
    """Extract full description text from /next videoSecondaryInfoRenderer."""
    contents = (
        nxt.get("contents", {})
        .get("twoColumnWatchNextResults", {})
        .get("results", {})
        .get("results", {})
        .get("contents", [])
    )
    for item in contents:
        sec = item.get("videoSecondaryInfoRenderer", {})
        attr = sec.get("attributedDescription")
        if attr and attr.get("content"):
            return attr["content"]
        desc = sec.get("description")
        if desc:
            return _extract_text(desc)
    return ""


def _like_count_from_next(nxt: Dict[str, Any]) -> Optional[int]:
    """Extract like count from /next videoPrimaryInfoRenderer / menu buttons."""
    contents = (
        nxt.get("contents", {})
        .get("twoColumnWatchNextResults", {})
        .get("results", {})
        .get("results", {})
        .get("contents", [])
    )
    for item in contents:
        primary = item.get("videoPrimaryInfoRenderer", {})
        actions = primary.get("videoActions", {}).get("menuRenderer", {})
        for button in actions.get("topLevelButtons", []):
            seg = button.get("segmentedLikeDislikeButtonViewModel", {})
            like = seg.get("likeButtonViewModel", {}).get("likeButtonViewModel", {})
            like_count = like.get("toggleButtonViewModel", {}).get("toggleButtonViewModel", {}).get(
                "defaultButtonViewModel", {}
            ).get("buttonViewModel", {}).get("title")
            if isinstance(like_count, str):
                parsed = _parse_count_text(like_count)
                if parsed:
                    return parsed
            # Older shape: topLevelButtons[].toggleButtonRenderer.defaultText
            toggle = button.get("toggleButtonRenderer", {})
            if toggle:
                count = _extract_text(toggle.get("defaultText"))
                parsed = _parse_count_text(count)
                if parsed:
                    return parsed
    return None


def _recommended_metadata_from_next(nxt: Any) -> Dict[str, Dict[str, Optional[str]]]:
    """Read only recommendation text, without defaulting absent fields to empty.

    Follow the same sidebar shapes as _recommended_videos_from_next. Never
    interpret channel/playlist lockups or unrelated text as video metadata.
    """
    secondary = nxt
    for key in ("contents", "twoColumnWatchNextResults", "secondaryResults", "secondaryResults", "results"):
        secondary = secondary.get(key) if isinstance(secondary, dict) else None
    pending = list(secondary) if isinstance(secondary, list) else []
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for item in pending:
        if not isinstance(item, dict):
            continue
        section = item.get("itemSectionRenderer")
        if isinstance(section, dict) and isinstance(section.get("contents"), list):
            pending.extend(section["contents"])

        renderer = item.get("compactVideoRenderer")
        if isinstance(renderer, dict):
            video_id = renderer.get("videoId")
            title = _optional_list_text(renderer.get("title"))
            description = _list_description(renderer)
        else:
            renderer = item.get("lockupViewModel")
            if not isinstance(renderer, dict):
                continue
            if renderer.get("contentType") not in (None, "", "LOCKUP_CONTENT_TYPE_VIDEO"):
                continue
            video_id = renderer.get("contentId")
            metadata = renderer.get("metadata")
            metadata = metadata.get("lockupMetadataViewModel") if isinstance(metadata, dict) else None
            metadata = metadata if isinstance(metadata, dict) else {}
            title = _optional_list_text(metadata.get("title"))
            description = _list_description(renderer)
            if description is None:
                description = _list_description(metadata)

        if not isinstance(video_id, str) or not video_id:
            continue
        texts = out.setdefault(video_id, {"title": None, "description": None})
        for field, value in (("title", title), ("description", description)):
            if texts[field] is None:
                texts[field] = value
    return out


def _recommended_videos_from_next(nxt: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract recommended videos from /next secondaryResults.

    Modern WEB responses place each recommendation in:
    - lockupViewModel directly in results[*] (current)
    - itemSectionRenderer.contents[*].lockupViewModel
    - compactVideoRenderer (legacy path)
    """
    secondary = (
        nxt.get("contents", {})
        .get("twoColumnWatchNextResults", {})
        .get("secondaryResults", {})
        .get("secondaryResults", {})
        .get("results", [])
    )
    out: List[Dict[str, Any]] = []
    for item in secondary:
        if "compactVideoRenderer" in item:
            out.append(_compact_video_to_invidious(item["compactVideoRenderer"]))
            continue
        if "lockupViewModel" in item:
            converted = _lockup_video_view_model_to_invidious(item["lockupViewModel"])
            if converted:
                out.append(converted)
            continue
        section = item.get("itemSectionRenderer", {})
        for content in section.get("contents", []):
            if "compactVideoRenderer" in content:
                out.append(_compact_video_to_invidious(content["compactVideoRenderer"]))
            elif "lockupViewModel" in content:
                converted = _lockup_video_view_model_to_invidious(content["lockupViewModel"])
                if converted:
                    out.append(converted)
    return out


def _lockup_video_view_model_to_invidious(renderer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert a video-typed lockupViewModel (sidebar recommendation) to Invidious shape.

    Only handles video content; channel/playlist lockups are skipped. Unlike
    `lockup_view_model_to_invidious` (which returns playlist/channel shapes),
    this targets LOCKUP_CONTENT_TYPE_VIDEO used for recommended videos.
    """
    content_id = renderer.get("contentId", "")
    if not content_id:
        return None

    content_type = renderer.get("contentType", "")
    if content_type and content_type != "LOCKUP_CONTENT_TYPE_VIDEO":
        return None

    metadata_vm = renderer.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = metadata_vm.get("title", {}).get("content", "")

    # Author info lives in metadataRows. Two shapes observed:
    #   1) Part text has a commandRuns with browseEndpoint (linked channel)
    #   2) First row is just plain text with the channel name (no link)
    author = ""
    author_id = ""
    rows = (
        metadata_vm.get("metadata", {})
        .get("contentMetadataViewModel", {})
        .get("metadataRows", [])
    )
    for row in rows:
        for part in row.get("metadataParts", []):
            text_obj = part.get("text", {})
            for cmd_run in text_obj.get("commandRuns", []):
                browse = (
                    cmd_run.get("onTap", {})
                    .get("innertubeCommand", {})
                    .get("browseEndpoint", {})
                )
                if browse.get("browseId") and not author_id:
                    author = text_obj.get("content", "")
                    author_id = browse["browseId"]
                    break
            if author_id:
                break
        if author_id:
            break

    # Fallback: first metadata row with a single plain-text part is usually the author
    if not author and rows:
        first_parts = rows[0].get("metadataParts", [])
        if len(first_parts) == 1:
            candidate = first_parts[0].get("text", {}).get("content", "")
            if candidate and "view" not in candidate.lower() and "ago" not in candidate.lower():
                author = candidate

    # View count / published text live in rows too (second metadata row typically)
    view_count_text = ""
    published_text = ""
    for row in rows:
        for part in row.get("metadataParts", []):
            text = part.get("text", {}).get("content", "")
            if "view" in text.lower():
                view_count_text = text
            elif "ago" in text.lower():
                published_text = text

    # Duration comes from the thumbnail overlay. Two shapes observed:
    #   1) thumbnailOverlayBadgeViewModel.thumbnailBadges[].thumbnailBadgeViewModel.text
    #      (sidebar / recommended-video lockups)
    #   2) thumbnailBottomOverlayViewModel.badges[].thumbnailBadgeViewModel.text
    #      (channel "Videos" tab lockups)
    length_seconds = 0
    overlays = (
        renderer.get("contentImage", {})
        .get("thumbnailViewModel", {})
        .get("overlays", [])
    )
    for overlay in overlays:
        badge_sources = []
        direct_badge = overlay.get("thumbnailOverlayBadgeViewModel")
        if direct_badge:
            badge_sources.append(direct_badge)

        # Current channel-video lockups wrap duration badges in
        # thumbnailBottomOverlayViewModel.badges[].thumbnailBadgeViewModel.
        bottom_overlay = overlay.get("thumbnailBottomOverlayViewModel", {})
        for badge in bottom_overlay.get("badges", []):
            nested_badge = badge.get("thumbnailBadgeViewModel")
            if nested_badge:
                badge_sources.append(nested_badge)

        for badge in badge_sources:
            # Older thumbnailOverlayBadgeViewModel shapes store a list under
            # thumbnailBadges; newer nested thumbnailBadgeViewModel stores text directly.
            badge_texts = [badge.get("text", "")]
            badge_texts.extend(
                b.get("thumbnailBadgeViewModel", {}).get("text", "")
                for b in badge.get("thumbnailBadges", [])
            )
            for text in badge_texts:
                if text and ":" in text:
                    length_seconds = _parse_duration_text(text)
                    break
            if length_seconds:
                break
        if length_seconds:
            break

    return {
        "type": "video",
        "videoId": content_id,
        "title": title,
        "description": "",
        "author": author,
        "authorId": author_id,
        "authorUrl": f"/channel/{author_id}" if author_id else "",
        "videoThumbnails": _standard_video_thumbnails(content_id),
        "lengthSeconds": length_seconds,
        "viewCount": _parse_count_text(view_count_text),
        "viewCountText": view_count_text,
        "published": None,
        "publishedText": published_text or None,
        "liveNow": False,
        "isUpcoming": False,
        "isShort": False,
    }


def _compact_video_to_invidious(renderer: Dict[str, Any]) -> Dict[str, Any]:
    """Convert compactVideoRenderer (sidebar recommendations) to Invidious shape."""
    video_id = renderer.get("videoId", "")
    title = _extract_text(renderer.get("title"))
    byline = renderer.get("longBylineText") or renderer.get("shortBylineText") or {}
    author = _extract_text(byline)
    author_id = ""
    if isinstance(byline, dict):
        for run in byline.get("runs", []):
            browse = run.get("navigationEndpoint", {}).get("browseEndpoint", {})
            if browse.get("browseId"):
                author_id = browse["browseId"]
                break

    length_text = _extract_text(renderer.get("lengthText"))
    length_seconds = _parse_duration_text(length_text)

    view_count_text = _extract_text(renderer.get("viewCountText"))
    view_count = _parse_count_text(view_count_text)

    return {
        "type": "video",
        "videoId": video_id,
        "title": title,
        "description": _extract_text(renderer.get("descriptionSnippet")),
        "author": author,
        "authorId": author_id,
        "authorUrl": f"/channel/{author_id}" if author_id else "",
        "videoThumbnails": _standard_video_thumbnails(video_id),
        "lengthSeconds": length_seconds,
        "viewCount": view_count,
        "viewCountText": view_count_text,
        "published": None,
        "publishedText": _extract_text(renderer.get("publishedTimeText")),
        "liveNow": False,
        "isUpcoming": False,
    }


def _detect_is_short(player: Dict[str, Any], nxt: Dict[str, Any]) -> Optional[bool]:
    """Determine if a video is a YouTube Short from already-fetched /player data.

    Returns True/False when the reliable signal is present, None when unknown.
    No extra network calls — only inspects fields in the dicts we already have.

    Signal: `microformat.playerMicroformatRenderer.isShortsEligible`. This is
    the field iv-org/invidious settled on in PR #5609 after maintainers
    rejected length-based heuristics (livestreams also report length=0).
    """
    microformat = player.get("microformat", {}).get("playerMicroformatRenderer", {}) or {}
    if "isShortsEligible" in microformat:
        return bool(microformat["isShortsEligible"])
    return None


def innertube_player_to_invidious_video(
    player: Dict[str, Any], nxt: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Convert InnerTube /player + /next responses to Invidious-shaped dict.

    Stream URLs in formatStreams/adaptiveFormats are empty strings because
    WEB client URLs are ciphered. Call `merge_stream_urls` with yt-dlp's
    format list to fill them in.
    """
    nxt = nxt or {}
    details = player.get("videoDetails", {}) or {}
    streaming = player.get("streamingData", {}) or {}
    microformat = player.get("microformat", {}).get("playerMicroformatRenderer", {}) or {}

    video_id = details.get("videoId", "")

    format_streams = [_format_to_invidious(f) for f in streaming.get("formats", [])]
    adaptive_formats = [_format_to_invidious(f) for f in streaming.get("adaptiveFormats", [])]

    length_seconds = 0
    raw_length = details.get("lengthSeconds")
    if raw_length is not None:
        try:
            length_seconds = int(raw_length)
        except (ValueError, TypeError):
            length_seconds = 0

    view_count = None
    raw_views = details.get("viewCount")
    if raw_views is not None:
        try:
            view_count = int(raw_views)
        except (ValueError, TypeError):
            view_count = None

    storyboards = _parse_storyboard_spec(
        player.get("storyboards", {}).get("playerStoryboardSpecRenderer", {}).get("spec", ""),
        video_id,
    )

    captions = _captions_from_player(player)

    author_thumbnails = _author_thumbnails_from_next(nxt)
    full_description = _full_description_from_next(nxt)
    like_count = _like_count_from_next(nxt)
    recommended = _recommended_videos_from_next(nxt)

    channel_id = details.get("channelId", "")

    return {
        "videoId": video_id,
        "title": details.get("title", ""),
        "description": full_description or details.get("shortDescription", ""),
        "author": details.get("author", ""),
        "authorId": channel_id,
        "authorUrl": f"/channel/{channel_id}" if channel_id else None,
        "authorThumbnails": author_thumbnails,
        "lengthSeconds": length_seconds,
        "published": _published_timestamp(microformat),
        "publishedText": None,
        "viewCount": view_count,
        "likeCount": like_count,
        "videoThumbnails": _standard_video_thumbnails(video_id),
        "liveNow": bool(details.get("isLive", False)),
        "isUpcoming": bool(details.get("isUpcoming", False)),
        "isShort": _detect_is_short(player, nxt),
        "hlsUrl": streaming.get("hlsManifestUrl"),
        "dashUrl": streaming.get("dashManifestUrl"),
        "formatStreams": format_streams,
        "adaptiveFormats": adaptive_formats,
        "captions": captions,
        "storyboards": storyboards,
        "recommendedVideos": recommended,
        "keywords": details.get("keywords", []),
    }
