"""Offline coverage of recommendation text from the existing localized /next."""

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from innertube._converters import _recommended_metadata_from_next
from tests import test_innertube_localized_metadata as acquisition_tests
from tests import test_video_language_http as http_tests


def next_response(items):
    return {"contents": {"twoColumnWatchNextResults": {"secondaryResults": {"secondaryResults": {"results": items}}}}}


def compact(video_id, **fields):
    return {"compactVideoRenderer": {"videoId": video_id, **fields}}


def lockup(video_id, **fields):
    return {
        "lockupViewModel": {
            "contentId": video_id,
            "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
            "metadata": {"lockupMetadataViewModel": fields},
        }
    }


def test_sidebar_shapes_and_unicode_do_not_mutate_input():
    data = next_response(
        [
            compact(
                "a",
                title={"runs": [{"text": "Été — "}, {"text": "Paris 🎬"}]},
                descriptionSnippet={"runs": [{"text": "Bref.\n"}, {"text": "Merci !"}]},
            ),
            lockup("b", title={"content": "English title"}),
            {
                "itemSectionRenderer": {
                    "contents": [
                        compact("c", title={"simpleText": "Wrapped"}, description={"simpleText": ""}),
                        lockup("d", title={"content": "Titre français"}, description={"content": "Texte"}),
                    ]
                }
            },
        ]
    )
    before = deepcopy(data)
    assert _recommended_metadata_from_next(data) == {
        "a": {"title": "Été — Paris 🎬", "description": "Bref.\nMerci !"},
        "b": {"title": "English title", "description": None},
        "c": {"title": "Wrapped", "description": ""},
        "d": {"title": "Titre français", "description": "Texte"},
    }
    assert data == before


@pytest.mark.parametrize("renderer", [compact, lockup])
@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, {"title": None, "description": None}),
        ({"title": {"content": ""}, "description": {"runs": []}}, {"title": "", "description": ""}),
        ({"title": {"runs": [None]}, "description": []}, {"title": None, "description": None}),
        (
            {"detailedMetadataSnippets": [{"snippetText": {"simpleText": "Short"}}]},
            {"title": None, "description": "Short"},
        ),
    ],
)
def test_absent_empty_and_malformed_text(renderer, fields, expected):
    assert _recommended_metadata_from_next(next_response([renderer("a", **fields)])) == {"a": expected}


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        "bad",
        {},
        {"contents": None},
        next_response(None),
        next_response([None, [], {"itemSectionRenderer": None}]),
    ],
)
def test_unexpected_structures(data):
    assert _recommended_metadata_from_next(data) == {}


def test_non_video_cards_are_ignored_and_duplicate_text_is_merged_independently():
    playlist = lockup("playlist", title={"content": "Not a video"})
    playlist["lockupViewModel"]["contentType"] = "LOCKUP_CONTENT_TYPE_PLAYLIST"
    data = next_response(
        [
            playlist,
            compact(None, title="Missing ID"),
            compact("a", title={"simpleText": ""}),
            compact("a", title={"simpleText": "Do not replace explicit empty"}, description="Description"),
            {"videoPrimaryInfoRenderer": {"title": {"simpleText": "Main video"}}},
        ]
    )
    assert _recommended_metadata_from_next(data) == {"a": {"title": "", "description": "Description"}}


class TestRecommendationAcquisition(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.case = acquisition_tests.TestVideoLocalizedMetadata(methodName="test_english_context")
        self.case.addCleanup = self.addCleanup
        self.case.setUp()
        self.video = self.case.video
        for key, language in (("web_runs", "fr"), ("web_classic", "en")):
            self.case.fixtures[key]["contents"]["twoColumnWatchNextResults"].update(
                next_response([compact("a", title=f"Title {language}", descriptionSnippet="")])["contents"][
                    "twoColumnWatchNextResults"
                ]
            )

    async def test_existing_lookup_warms_recommendations_without_another_request(self):
        before = deepcopy((self.case.fixtures, self.case.player_cache))
        main = await self.video.get_video_localized_metadata("synthetic01", "fr")
        self.assertEqual(set(main), {"title", "description"})
        details = await self.video.get_video_localized_metadata("synthetic01", "fr", include_recommendations=True)
        self.assertEqual(details["recommendedVideos"], {"a": {"title": "Title fr", "description": ""}})
        details["recommendedVideos"]["a"]["title"] = "Caller mutation"
        hit = await self.video.get_video_localized_metadata("synthetic01", "fr", include_recommendations=True)
        self.assertEqual(hit["recommendedVideos"]["a"]["title"], "Title fr")
        self.assertEqual((self.case.fixtures, self.case.player_cache), before)
        self.case.shared_client.post.assert_awaited_once()

    async def test_concurrent_and_successive_languages_remain_isolated(self):
        french, english = await asyncio.gather(
            *(
                self.video.get_video_localized_metadata("synthetic01", language, include_recommendations=True)
                for language in ("fr", "en")
            )
        )
        for language, result in (("fr", french), ("en", english)):
            self.assertEqual(result["recommendedVideos"]["a"]["title"], f"Title {language}")
            self.assertEqual(
                await self.video.get_video_localized_metadata("synthetic01", language, include_recommendations=True),
                result,
            )
        self.assertEqual(self.case.shared_client.post.await_count, 2)
        self.assertEqual(self.case.shared_client.headers, self.case.headers_before)

    async def test_recommendations_without_main_text_are_available_and_cached(self):
        self.case.fixtures["web_runs"] = next_response([compact("a", title="Titre associé")])
        for _ in range(2):
            result = await self.video.get_video_localized_metadata("synthetic01", "fr", include_recommendations=True)
            self.assertIsNone(result["title"])
            self.assertIsNone(result["description"])
            self.assertEqual(result["recommendedVideos"]["a"]["title"], "Titre associé")
        self.case.shared_client.post.assert_awaited_once()

    async def test_failed_lookup_has_no_recommendation_override(self):
        self.case.shared_client.post.side_effect = self.case.client.InnerTubeError("Simulated failure")
        with self.assertLogs("innertube"):
            result = await self.video.get_video_localized_metadata("synthetic01", "fr", include_recommendations=True)
        self.assertEqual(result, {"title": None, "description": None, "recommendedVideos": {}})
        self.assertEqual(self.video._localized_metadata_cache, {})


class TestRecommendationHTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from models import VideoListItem

        self.case = http_tests.TestVideoLanguageHTTP(methodName="test_french_english_and_existing_parameters")
        self.case.addCleanup = self.addCleanup
        self.case.addAsyncCleanup = self.addAsyncCleanup
        await self.case.asyncSetUp()
        network = patch("socket.socket.connect", side_effect=AssertionError("Real network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.case.base.recommendedVideos = [
            VideoListItem(
                videoId=video_id,
                title=f"Historical {video_id}",
                description="Long historical description",
                author="Channel name",
                authorId="UCauthor",
                lengthSeconds=123,
                viewCount=456,
                videoThumbnails=[
                    {"quality": "default", "url": "https://example.invalid/thumb", "width": 120, "height": 90}
                ],
            )
            for video_id in ("a", "b", "c")
        ]
        self.before = self.case.base.model_dump(mode="json")
        self.case.localized.side_effect = None
        self.case.localized.return_value = {
            "title": None,
            "description": None,
            "recommendedVideos": {
                "b": {"title": None, "description": ""},
                "foreign": {"title": "Never insert", "description": "Never insert"},
                "a": {"title": "Titre français", "description": "Bref.\n🎬"},
            },
        }

    async def test_matching_preserves_order_properties_and_cached_models(self):
        result = await self.case.request(lang="fr")
        self.assertEqual(result.status_code, 200)
        data = result.json()
        items = data["recommendedVideos"]
        self.assertEqual([item["videoId"] for item in items], ["a", "b", "c"])
        self.assertEqual((items[0]["title"], items[0]["description"]), ("Titre français", "Bref.\n🎬"))
        self.assertEqual((items[1]["title"], items[1]["description"]), ("Historical b", ""))
        self.assertEqual(items[2], self.before["recommendedVideos"][2])
        for old, new in zip(self.before["recommendedVideos"], items):
            for field in set(old) - {"title", "description"}:
                self.assertEqual(new[field], old[field])
        self.assertEqual(
            {k: v for k, v in data.items() if k != "recommendedVideos"},
            {k: v for k, v in self.before.items() if k != "recommendedVideos"},
        )
        self.assertEqual(self.case.base.model_dump(mode="json"), self.before)
        self.case.localized.assert_awaited_once_with("synthetic01", "fr", include_recommendations=True)
        self.case.player_next.assert_not_awaited()

    async def test_original_and_omitted_language_preserve_recommendations(self):
        for params in ({}, {"lang": "original"}):
            result = await self.case.request(**params)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["recommendedVideos"], self.before["recommendedVideos"])
        self.case.localized.assert_not_awaited()

    async def test_alternative_provider_recommendations_are_unchanged(self):
        for source in ("invidious", "ytdlp"):
            self.case.base.extractionMethod = source
            result = await self.case.request(lang="fr")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["recommendedVideos"], self.before["recommendedVideos"])
        self.assertEqual(self.case.localized.await_count, 2)
        self.case.localized.assert_awaited_with("synthetic01", "fr")

    async def test_missing_or_failed_localized_next_preserves_recommendations(self):
        for metadata in ({"title": None, "description": None}, {"recommendedVideos": {}}):
            self.case.localized.return_value = metadata
            result = await self.case.request(lang="fr")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), self.before)
        self.case.localized.side_effect = self.case.route.InnerTubeError("Simulated next failure")
        with self.assertLogs("routers.videos"):
            result = await self.case.request(lang="en")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), self.before)

    async def test_concurrent_languages_do_not_mix_recommendation_text(self):
        async def localized(video_id, language, *, include_recommendations):
            await asyncio.sleep(0)
            return {"recommendedVideos": {"a": {"title": f"Title {language}", "description": None}}}

        self.case.localized.side_effect = localized
        french, english = await asyncio.gather(self.case.request(lang="fr"), self.case.request(lang="en"))
        for language, response in (("fr", french), ("en", english)):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["recommendedVideos"][0]["title"], f"Title {language}")
        self.assertEqual(self.case.base.model_dump(mode="json"), self.before)

    async def test_route_reuses_the_single_localized_next_and_its_language_cache(self):
        from innertube import _video

        async def next_data(endpoint, body):
            self.assertEqual(endpoint, "next")
            context = body["context"]["client"]
            self.assertEqual(context["gl"], "US")
            return next_response([compact("a", title=f"Title {context['hl']}", descriptionSnippet="Brief.")])

        self.case.localized.side_effect = _video.get_video_localized_metadata
        with (
            patch("settings.get_settings", return_value=SimpleNamespace(innertube_enabled=True)),
            patch.object(_video, "_get_localized_metadata_cache", return_value={}),
            patch.object(_video, "_get_client_version", new_callable=AsyncMock, return_value="2.synthetic"),
            patch.object(_video, "innertube_post", new_callable=AsyncMock, side_effect=next_data) as post,
        ):
            for language in ("fr", "en", "fr", "en"):
                result = await self.case.request(lang=language)
                self.assertEqual(result.status_code, 200)
                items = result.json()["recommendedVideos"]
                self.assertEqual(items[0]["title"], f"Title {language}")
                self.assertEqual(items[0]["description"], "Brief.")
                self.assertEqual(items[1:], self.before["recommendedVideos"][1:])
            self.assertEqual(post.await_count, 2)
        self.case.player_next.assert_not_awaited()
        self.assertEqual(self.case.base.model_dump(mode="json"), self.before)
