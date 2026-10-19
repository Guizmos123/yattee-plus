"""Targeted language contexts, list conversion, pagination and HTTP regressions."""

import asyncio
import copy
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
import innertube
import innertube._browse as browse
import innertube._client as client_module
import innertube._localization as localization
import innertube._search as search_module
from innertube._converters import (
    _localize_list_video,
    _localized_published,
    _localized_view_count,
    _recommended_videos_from_next,
    video_renderer_to_invidious,
)
from routers import channels, search
from settings import Settings

CHANNEL = "UC" + "a" * 22
ROUTES = [
    ("/api/v1/search", "search"),
    (f"/api/v1/channels/{CHANNEL}/videos", "get_channel_videos"),
    (f"/api/v1/channels/{CHANNEL}/shorts", "get_channel_shorts"),
    (f"/api/v1/channels/{CHANNEL}/search", "search_channel"),
]


@pytest.fixture(autouse=True)
def no_network_and_fresh_tokens(monkeypatch):
    monkeypatch.setattr(client_module, "get_client", AsyncMock(side_effect=AssertionError("Network forbidden")))
    monkeypatch.setattr(localization, "_get_client_version", AsyncMock(return_value="dynamic-test-version"))
    monkeypatch.setattr(localization, "get_settings", lambda: Settings(innertube_enabled=True))
    monkeypatch.setattr(search_module, "_continuation_cache", {})
    monkeypatch.setattr(browse, "_handle_cache", {})


@pytest.fixture
def api(test_db, monkeypatch):
    app = FastAPI()
    app.include_router(search.router, prefix="/api/v1")
    app.include_router(channels.router, prefix="/api/v1")
    config = Settings(invidious_proxy_channels=True, invidious_proxy_channel_tabs=True)
    monkeypatch.setattr(channels, "get_settings", lambda: config)
    monkeypatch.setattr(search, "get_settings", lambda: config)
    with TestClient(app) as client:
        yield client


def params(language=None, **extra):
    return {"q": "same query", **({"lang": language} if language else {}), **extra}


def video_item(video_id="known", **fields):
    return {
        "type": "video",
        "videoId": video_id,
        "title": "YouTube title",
        "description": "YouTube snippet",
        "author": "Author",
        "authorId": CHANNEL,
        "lengthSeconds": 61,
        "viewCount": 123,
        "published": 1790000000,
        "publishedText": "1 day ago",
        "videoThumbnails": [],
        **fields,
    }


def raw_video(video_id="known", **fields):
    return {
        "videoRenderer": {
            "videoId": video_id,
            "title": {"runs": [{"text": "Titre — été 🎬"}]},
            "ownerText": {
                "runs": [{"text": "Author", "navigationEndpoint": {"browseEndpoint": {"browseId": CHANNEL}}}]
            },
            "viewCountText": {"simpleText": "1,2 M de vues"},
            "publishedTimeText": {"simpleText": "il y a 2 jours"},
            "lengthText": {"simpleText": "1:01"},
            "detailedMetadataSnippets": [{"snippetText": {"runs": [{"text": "Extrait français\n& <texte>"}]}}],
            **fields,
        }
    }


def token_item(token):
    return {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": token}}}}


def initial_search(items, token=None):
    contents = [{"itemSectionRenderer": {"contents": items}}]
    if token:
        contents.append(token_item(token))
    return {
        "contents": {
            "twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {"contents": contents}}}
        }
    }


def continuation(items, token=None, key="onResponseReceivedCommands", rich=False):
    contents = [{"richItemRenderer": {"content": item}} for item in items] if rich else items[:]
    if token:
        contents.append(token_item(token))
    return {key: [{"appendContinuationItemsAction": {"continuationItems": contents}}]}


def initial_browse(items, token=None):
    contents = [{"richItemRenderer": {"content": item}} for item in items]
    if token:
        contents.append(token_item(token))
    return {
        "contents": {
            "twoColumnBrowseResultsRenderer": {
                "tabs": [
                    {"tabRenderer": {"content": {"richGridRenderer": {"contents": contents}}}},
                ]
            }
        }
    }


def channel_search_initial(items, token=None):
    contents = [{"itemSectionRenderer": {"contents": items}}]
    if token:
        contents.append(token_item(token))
    return {
        "contents": {
            "twoColumnBrowseResultsRenderer": {
                "tabs": [
                    {"tabRenderer": {"selected": True, "content": {"sectionListRenderer": {"contents": contents}}}},
                ]
            }
        }
    }


def response_items(response, method):
    return response.json() if method == "search" else response.json()["videos"]


def store(language="fr", **fields):
    database.upsert_feed_video_metadata(
        "youtube",
        "known",
        language,
        title=fields.get("title"),
        description=fields.get("description"),
        source=fields.get("source", "innertube:player" if language == "original" else "innertube:next"),
        checked_at=fields.get("checked_at"),
    )


@pytest.mark.parametrize("language", [None])
@pytest.mark.parametrize("path,method", ROUTES)
def test_historical_keeps_exact_response_and_calls(api, monkeypatch, path, method, language):
    items = [video_item()]
    mock = AsyncMock(return_value=items if method == "search" else {"videos": items, "continuation": "token"})
    monkeypatch.setattr(innertube, method, mock)
    with patch.object(database, "get_feed_video_metadata", side_effect=AssertionError("Historical SQLite lookup")):
        baseline = api.get(path, params=params()).json()
        mock.reset_mock()
        response = api.get(path, params=params(language))
    assert response.status_code == 200 and response.json() == baseline
    assert "language" not in mock.await_args.kwargs


@pytest.mark.parametrize("path,method", ROUTES)
def test_original_uses_local_candidate_without_additional_acquisition(api, monkeypatch, path, method):
    items = [video_item(), video_item("unknown")]
    provider = AsyncMock(return_value=items if method == "search" else {"videos": items, "continuation": "token"})
    monkeypatch.setattr(innertube, method, provider)
    store("original", title="Candidate original", description="Description candidate")
    baseline = api.get(path, params=params()).json()
    provider.reset_mock()
    response = api.get(path, params=params("original"))
    expected = copy.deepcopy(baseline)
    rows = expected if method == "search" else expected["videos"]
    rows[0].update(title="Candidate original", description="Description candidate")
    assert response.json() == expected
    assert provider.await_count == 1
    assert "language" not in provider.await_args.kwargs


@pytest.mark.parametrize("language", ["fr", "en"])
@pytest.mark.parametrize("path,method", ROUTES)
def test_old_storage_does_not_override_localized_provider(api, monkeypatch, path, method, language):
    store(
        language,
        title="Old stored title",
        description="Old stored description",
        checked_at=datetime.now(UTC) - timedelta(days=2),
    )
    items = [video_item()]
    monkeypatch.setattr(innertube, method, AsyncMock(return_value=items if method == "search" else {"videos": items}))
    row = response_items(api.get(path, params=params(language)), method)[0]
    assert row["title"] == "YouTube title" and row["description"] == "YouTube snippet"


@pytest.mark.parametrize("path,method", ROUTES)
def test_missing_title_uses_local_candidate_independently_of_current_description(api, monkeypatch, path, method):
    store(title="Stored FR", description="Old description")
    items = [video_item(title=None, description="Current description")]
    monkeypatch.setattr(innertube, method, AsyncMock(return_value=items if method == "search" else {"videos": items}))
    row = response_items(api.get(path, params=params("fr")), method)[0]
    assert row["title"] == "Stored FR" and row["description"] == "Current description"


@pytest.mark.parametrize("language", ["fr", "en"])
@pytest.mark.parametrize("path,method", ROUTES)
def test_localized_provider_precedes_storage_without_changing_order_or_other_fields(
    api, monkeypatch, path, method, language
):
    items = [video_item(), video_item("unknown"), video_item("third", description=None)]
    before = copy.deepcopy(items)
    store(language, title="", description="court")
    metadata_before = database.get_feed_video_metadata(["known", "unknown", "third"])
    mocked = AsyncMock(return_value=items if method == "search" else {"videos": items, "continuation": "token"})
    monkeypatch.setattr(innertube, method, mocked)
    baseline = api.get(path, params=params()).json()
    with patch.object(database, "get_feed_video_metadata", wraps=database.get_feed_video_metadata) as read:
        response = api.get(path, params=params(language))
    assert response.status_code == 200
    mocked.assert_awaited()
    assert mocked.await_args.kwargs["language"] == language
    read.assert_called_once_with(["known", "unknown", "third"], languages=[language, "original"])
    expected = copy.deepcopy(baseline)
    assert response.json() == expected
    assert items == before
    assert database.get_feed_video_metadata(["known", "unknown", "third"]) == metadata_before


@pytest.mark.parametrize("path,method", ROUTES)
def test_sqlite_null_preserves_innertube_text_and_other_language_is_not_used(api, monkeypatch, path, method):
    store("fr", title=None, description=None)
    store("en", title="English must not replace FR", description="English")
    store("original", title="Original must not replace current FR", description="Original")
    items = [video_item(description="")]
    monkeypatch.setattr(
        innertube,
        method,
        AsyncMock(return_value=items if method == "search" else {"videos": items, "continuation": "token"}),
    )
    result = response_items(api.get(path, params=params("fr")), method)[0]
    assert result["title"] == "YouTube title" and result["description"] == ""


@pytest.mark.parametrize("path,method", ROUTES)
def test_bad_language_is_422_before_provider_calls(api, monkeypatch, path, method):
    mock = AsyncMock(side_effect=AssertionError("Unexpected provider call"))
    monkeypatch.setattr(innertube, method, mock)
    assert api.get(path, params=params("de")).status_code == 422
    mock.assert_not_awaited()


@pytest.mark.parametrize("path,method", ROUTES)
def test_sqlite_failure_retains_available_text(api, monkeypatch, path, method):
    items = [video_item()]
    monkeypatch.setattr(innertube, method, AsyncMock(return_value=items if method == "search" else {"videos": items}))
    with patch.object(database, "get_feed_video_metadata", side_effect=sqlite3.OperationalError("locked")):
        result = api.get(path, params=params("fr"))
    assert result.status_code == 200
    assert response_items(result, method)[0]["title"] == "YouTube title"


@pytest.mark.parametrize("language", ["original", "fr", "en"])
@pytest.mark.parametrize("provider", ["invidious", "ytdlp"])
@pytest.mark.parametrize("path,method", ROUTES)
def test_fallback_provider_uses_local_variants(api, monkeypatch, path, method, provider, language):
    monkeypatch.setattr(innertube, method, AsyncMock(side_effect=innertube.InnerTubeError("unavailable")))
    monkeypatch.setattr(channels.invidious_proxy, "is_enabled", lambda: provider == "invidious")
    if provider == "invidious":
        inv_method = {
            "search": "search",
            "get_channel_videos": "get_channel_videos",
            "get_channel_shorts": "get_channel_shorts",
            "search_channel": "search_channel",
        }[method]
        items = [video_item("before"), video_item(), video_item("after")]
        output = items if method == "search" else {"videos": items, "continuation": "token"}
        mock = AsyncMock(return_value=output)
        monkeypatch.setattr(channels.invidious_proxy, inv_method, mock)
    else:
        route_module = search if method == "search" else channels
        yt_method = {
            "search": "search_videos",
            "get_channel_videos": "get_channel_videos",
            "get_channel_shorts": "get_channel_tab",
            "search_channel": "ytdlp_search_channel",
        }[method]
        mock = AsyncMock(
            return_value=[
                {"id": video_id, "title": "Fallback", "duration": 61} for video_id in ("before", "known", "after")
            ]
        )
        monkeypatch.setattr(route_module, yt_method, mock)
    baseline = api.get(path, params=params())
    store(language, title="Local title", description="Local description")
    mock.reset_mock()
    localized = api.get(path, params=params(language))
    assert localized.status_code == baseline.status_code == 200
    expected = baseline.json()
    rows = expected if method == "search" else expected["videos"]
    rows[1].update(title="Local title", description="Local description")
    assert localized.json() == expected
    assert "language" not in mock.await_args.kwargs
    with patch.object(database, "get_feed_video_metadata", return_value=[]):
        assert api.get(path, params=params(language)).json() == baseline.json()


def test_mixed_search_only_overlays_videos(api, monkeypatch):
    items = [
        video_item(),
        {"type": "channel", "author": "Channel unchanged", "authorId": CHANNEL},
        {
            "type": "playlist",
            "title": "Playlist unchanged",
            "playlistId": "PLtest",
            "author": "Author",
            "videoCount": 1,
        },
    ]
    monkeypatch.setattr(innertube, "search", AsyncMock(return_value=items))
    store(title="FR video", description="FR description")
    baseline = api.get("/api/v1/search", params=params(type="all")).json()
    localized = api.get("/api/v1/search", params=params("fr", type="all")).json()
    assert localized[0]["title"] == "YouTube title"
    assert localized[1:] == baseline[1:]


@pytest.mark.parametrize("path,method", ROUTES)
def test_route_to_context_converter_and_sqlite_integration(api, monkeypatch, path, method):
    recorded = []

    async def post(endpoint, body):
        recorded.append((endpoint, copy.deepcopy(body)))
        if endpoint == "navigation/resolve_url":
            return {"endpoint": {"browseEndpoint": {"browseId": CHANNEL, "params": "search-params"}}}
        if method == "search":
            return initial_search([raw_video(), raw_video("unknown")], token="next")
        if method == "search_channel":
            return channel_search_initial([raw_video(), raw_video("unknown")], token="next")
        return initial_browse([raw_video(), raw_video("unknown")], token="next")

    monkeypatch.setattr(search_module, "innertube_post", post)
    monkeypatch.setattr(browse, "innertube_post", post)
    store(title="Titre SQLite", description=None)
    result = api.get(path, params=params("fr"))
    assert result.status_code == 200
    rows = response_items(result, method)
    assert [row["videoId"] for row in rows] == ["known", "unknown"]
    assert rows[0]["title"] == "Titre — été 🎬"
    assert rows[1]["title"] == "Titre — été 🎬"
    for row in rows:
        assert row["description"] == "Extrait français\n& <texte>"
        assert row["viewCount"] == 1_200_000
        assert row["published"] is not None and row["publishedText"] == "il y a 2 jours"
        assert row["author"] == "Author" and row["lengthSeconds"] == 61
    assert all(
        body["context"]["client"]["hl"] == "fr" and body["context"]["client"]["gl"] == "US" for _, body in recorded
    )


@pytest.mark.asyncio
async def test_language_context_is_fresh_and_does_not_mutate_input():
    original = {"continuation": "token"}
    before = copy.deepcopy(original)
    fr, en = await asyncio.gather(
        localization._language_body(original, "fr"), localization._language_body(original, "en")
    )
    assert fr["context"]["client"]["hl"] == "fr" and en["context"]["client"]["hl"] == "en"
    assert fr["context"]["client"]["clientVersion"] == "dynamic-test-version"
    assert fr["context"]["client"]["gl"] == en["context"]["client"]["gl"] == "US"
    assert original == before
    assert await localization._language_body(original, None) is original


@pytest.mark.asyncio
async def test_disabled_innertube_does_not_fetch_client_version(monkeypatch):
    monkeypatch.setattr(localization, "get_settings", lambda: Settings(innertube_enabled=False))
    with pytest.raises(innertube.InnerTubeError, match="disabled"):
        await localization._language_body({"query": "same"}, "fr")
    localization._get_client_version.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["videos", "shorts", "search"])
def test_non_youtube_channels_ignore_language(api, monkeypatch, endpoint):
    monkeypatch.setattr(
        database, "get_subscription_by_channel_id", lambda _: {"channel_url": "https://example.com/channel"}
    )
    monkeypatch.setattr(channels.invidious_proxy, "is_enabled", lambda: False)
    youtube = AsyncMock(side_effect=innertube.InnerTubeError("unavailable"))
    monkeypatch.setattr(innertube, "get_channel_shorts", youtube)
    name = {"videos": "extract_channel_url", "shorts": "get_channel_tab", "search": "ytdlp_search_channel"}[endpoint]
    data = [{"id": "external", "title": "External title", "duration": 61}]
    provider = AsyncMock(return_value={"entries": data} if endpoint == "videos" else data)
    monkeypatch.setattr(channels, name, provider)
    path = f"/api/v1/channels/external/{endpoint}"
    with patch.object(database, "get_feed_video_metadata", side_effect=AssertionError("Non-YouTube lookup")):
        baseline = api.get(path, params=params())
        localized = api.get(path, params=params("fr"))
    assert baseline.status_code == localized.status_code == 200
    assert baseline.json() == localized.json()
    assert "language" not in provider.await_args.kwargs
    if endpoint == "shorts":
        assert "language" not in youtube.await_args.kwargs


@pytest.mark.asyncio
async def test_search_continuations_are_isolated_and_keep_filters(monkeypatch):
    calls = []

    async def post(endpoint, body):
        calls.append(copy.deepcopy(body))
        language = body.get("context", {}).get("client", {}).get("hl", "historical")
        if "continuation" in body:
            assert body["continuation"] == f"{language}-next"
            return continuation([{"itemSectionRenderer": {"contents": [raw_video(f"{language}-page2")]}}])
        return initial_search([raw_video(f"{language}-page1")], token=f"{language}-next")

    monkeypatch.setattr(search_module, "innertube_post", post)
    for language in ("fr", "en", None):
        options = {"language": language} if language else {}
        await search_module.search("same", search_type="video", sort="date", date="week", duration="short", **options)
    for language in ("en", "fr", None):
        options = {"language": language} if language else {}
        result = await search_module.search(
            "same", search_type="video", sort="date", date="week", duration="short", page=2, **options
        )
        assert result[0]["videoId"] == f"{language or 'historical'}-page2"
    assert len(calls) == 6
    assert len(search_module._continuation_cache) == 3
    assert all("params" in body for body in calls[:3])
    assert all(body["context"]["client"]["gl"] == "US" for body in calls if "context" in body)


@pytest.mark.asyncio
async def test_search_walk_and_stale_token_retry_keep_language(monkeypatch):
    calls = []

    async def post(endpoint, body):
        calls.append(body)
        assert body["context"]["client"]["hl"] == "fr"
        if body.get("continuation") == "stale":
            raise innertube.InnerTubeError("stale", status_code=400)
        if "query" in body:
            return initial_search([raw_video("one")], token="fresh")
        return continuation([raw_video("two")], token="third")

    monkeypatch.setattr(search_module, "innertube_post", post)
    prefix = ("same", "all", None, None, None, "fr", "US")
    search_module._cache_set((*prefix, 1), "stale")
    rows = await search_module.search("same", page=2, language="fr")
    assert rows[0]["videoId"] == "two" and len(calls) == 3
    rows = await search_module.search("cold query", page=2, language="fr")
    assert rows[0]["videoId"] == "two"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["fr", "en"])
@pytest.mark.parametrize("method", ["get_channel_videos", "get_channel_shorts"])
async def test_channel_initial_handle_and_continuation_context(monkeypatch, language, method):
    calls = []

    async def post(endpoint, body):
        calls.append((endpoint, body))
        assert body["context"]["client"]["hl"] == language
        assert body["context"]["client"]["gl"] == "US"
        if endpoint == "navigation/resolve_url":
            return {"endpoint": {"browseEndpoint": {"browseId": CHANNEL}}}
        if "continuation" in body:
            return continuation([raw_video("page2")], key="onResponseReceivedActions", rich=True)
        return initial_browse([raw_video()], token="next")

    monkeypatch.setattr(browse, "innertube_post", post)
    function = getattr(browse, method)
    first = await function("@handle", language=language)
    second = await function("@handle", first["continuation"], language=language)
    assert second["videos"][0]["videoId"] == "page2"
    assert len(calls) == 3 and calls[1][1]["browseId"] == CHANNEL


@pytest.mark.asyncio
@pytest.mark.parametrize("language", [None, "fr", "en"])
async def test_channel_search_pagination_keeps_context_and_legacy_behavior(monkeypatch, language):
    calls = []

    async def post(endpoint, body):
        calls.append(body)
        if language:
            assert body["context"]["client"]["hl"] == language
        else:
            assert "context" not in body
        if endpoint == "navigation/resolve_url":
            return {"endpoint": {"browseEndpoint": {"browseId": CHANNEL, "params": "params"}}}
        if "continuation" in body:
            return continuation([raw_video("page2")], key="onResponseReceivedActions")
        return channel_search_initial([raw_video("page1")], token="next")

    monkeypatch.setattr(search_module, "innertube_post", post)
    result = await search_module.search_channel(
        CHANNEL, "query", page=2, **({"language": language} if language else {})
    )
    assert result["videos"][0]["videoId"] == ("page2" if language else "page1")
    assert len(calls) == (3 if language else 2)


@pytest.mark.asyncio
async def test_channel_search_selected_tab_and_followup_continuation_keep_language(monkeypatch):
    calls = []

    async def post(endpoint, body):
        calls.append(body)
        assert body["context"]["client"]["hl"] == "fr"
        if endpoint == "navigation/resolve_url":
            return {"endpoint": {"browseEndpoint": {"browseId": CHANNEL, "params": "initial"}}}
        if body.get("params") == "initial":
            return {
                "contents": {
                    "twoColumnBrowseResultsRenderer": {
                        "tabs": [
                            {
                                "expandableTabRenderer": {
                                    "selected": True,
                                    "endpoint": {"browseEndpoint": {"params": "tab-params"}},
                                }
                            }
                        ]
                    }
                }
            }
        if "continuation" in body:
            return continuation([raw_video("page2")], key="onResponseReceivedActions")
        assert body["query"] == "query" and body["params"] == "tab-params"
        return continuation([raw_video("page1")], token="next", key="onResponseReceivedActions")

    monkeypatch.setattr(search_module, "innertube_post", post)
    rows = await search_module.search_channel(CHANNEL, "query", page=2, language="fr")
    assert rows["videos"][0]["videoId"] == "page2" and len(calls) == 4


def test_short_lockup_keeps_localized_view_count_and_absent_description():
    raw = {
        "shortsLockupViewModel": {
            "onTap": {"innertubeCommand": {"reelWatchEndpoint": {"videoId": "short-id"}}},
            "overlayMetadata": {"primaryText": {"content": "Un Short"}, "secondaryText": {"content": "1,2 M de vues"}},
        }
    }
    rows, _ = browse._extract_items_from_tab(initial_browse([raw]), localized=True)
    assert rows[0]["isShort"] is True
    assert rows[0]["viewCount"] == 1200000
    assert rows[0]["description"] is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("270\u202f091\u202f348 vues", 270091348),
        ("270,091,348 views", 270091348),
        ("1,2 M de vues", 1200000),
        ("1.2M views", 1200000),
        ("974 k vues", 974000),
        ("1,5 Md de vues", 1500000000),
        ("2 millions de vues", 2000000),
        ("No views", 0),
        ("Aucune vue", 0),
        ("Author name", None),
    ],
)
def test_localized_counts(text, expected):
    assert _localized_view_count(text) == expected


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("il y a 2 jours", 172800),
        ("il y a 3 mois", 7776000),
        ("Diffusé il y a un an", 31536000),
        ("2 days ago", 172800),
        ("Premiered 1 week ago", 604800),
        ("il y a une heure", 3600),
    ],
)
def test_localized_dates(monkeypatch, text, seconds):
    monkeypatch.setattr("innertube._converters.time.time", lambda: 1800000000)
    assert _localized_published(text) == 1800000000 - seconds


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({}, None),
        ({"descriptionSnippet": {"simpleText": ""}}, ""),
        ({"descriptionSnippet": {"runs": []}}, ""),
        ({"detailedMetadataSnippets": [{"snippetText": {"runs": [{"text": "A\n"}, {"text": "B"}]}}]}, "A\nB"),
    ],
)
def test_description_presence_and_nonmutation(fields, expected):
    item = raw_video()
    item["videoRenderer"].pop("detailedMetadataSnippets")
    item["videoRenderer"].update(fields)
    before = copy.deepcopy(item)
    legacy = video_renderer_to_invidious(item["videoRenderer"])
    assert legacy["description"] == ""
    result = _localize_list_video(item, legacy, localized=True)
    assert result["description"] == expected
    assert _localize_list_video(item, legacy) is legacy
    assert legacy["description"] == "" and item == before


def test_lockup_french_metrics_do_not_become_author():
    raw = {
        "lockupViewModel": {
            "contentId": "id",
            "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
            "metadata": {
                "lockupMetadataViewModel": {
                    "metadata": {
                        "contentMetadataViewModel": {
                            "metadataRows": [
                                {"metadataParts": [{"text": {"content": "1,2 M de vues"}}]},
                                {"metadataParts": [{"text": {"content": "il y a 2 jours"}}]},
                            ]
                        }
                    }
                }
            },
        }
    }
    legacy = {"type": "video", "title": "Title", "author": "1,2 M de vues", "authorId": "", "published": None}
    result = _localize_list_video(raw, legacy, localized=True)
    assert result["viewCount"] == 1200000 and result["published"] is not None
    assert result["author"] == "" and legacy["author"] == "1,2 M de vues"


def test_player_recommendations_keep_legacy_conversion():
    raw = raw_video()
    raw["compactVideoRenderer"] = raw.pop("videoRenderer")
    nxt = {"contents": {"twoColumnWatchNextResults": {"secondaryResults": {"secondaryResults": {"results": [raw]}}}}}
    assert _recommended_videos_from_next(nxt)[0]["description"] == ""
