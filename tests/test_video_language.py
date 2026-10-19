"""Offline route-function tests for explicit video metadata preferences."""

import asyncio
import sys
import unittest
from copy import copy, deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests import test_innertube_localized_metadata as acquisition_tests


class _VideoResponse:
    """Model double implementing the Pydantic operations used by the route."""

    def __init__(self, **fields):
        self.__dict__.update(deepcopy(fields))

    def model_copy(self, *, update):
        result = copy(self)
        result.__dict__.update(update)
        return result

    def model_dump(self):
        return deepcopy(self.__dict__)


class _HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class _InvidiousError(Exception):
    def __init__(self, message, **kwargs):
        super().__init__(message)
        self.__dict__.update(kwargs)


class _Router:
    def __init__(self, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return lambda function: function


class TestVideoLanguage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.acquisition = acquisition_tests.TestVideoLocalizedMetadata(methodName="test_english_context")
        # Register synchronous sandbox cleanup on this running async test case.
        self.acquisition.addCleanup = self.addCleanup
        self.acquisition.setUp()
        self.video = self.acquisition.video
        self.acquisition.settings.allow_all_sites_for_extraction = True
        self.acquisition.settings.invidious_proxy_videos = True
        self.acquisition.settings.invidious_author_thumbnails = False
        self.base = {
            "videoId": "synthetic01",
            "title": "Historical title",
            "description": "Historical description that is longer than the localized version. " * 3,
            "descriptionHtml": "<p>Historical formatted description</p>",
            "author": "Original channel name",
            "authorId": "UCsynthetic",
            "authorUrl": "/channel/UCsynthetic",
            "authorThumbnails": [],
            "lengthSeconds": 123,
            "viewCount": 42,
            "liveNow": False,
            "isUpcoming": False,
            "isShort": False,
            "formatStreams": [{"itag": "18", "url": "https://example.invalid/video"}],
            "adaptiveFormats": [{
                "itag": "140", "url": "https://example.invalid/audio",
                "audioTrack": {"id": "en.4", "displayName": "English (original)", "isDefault": True},
            }],
            "captions": [{"languageCode": "en", "url": "https://example.invalid/captions"}],
            "recommendedVideos": [{"videoId": "recommended", "title": "Keep recommendation"}],
            "hlsUrl": "https://example.invalid/hls",
            "dashUrl": "https://example.invalid/dash",
            "storyboards": [],
        }
        self.it_video = {**deepcopy(self.base), "title": "Historical InnerTube title"}
        self.ytdlp_info = {**deepcopy(self.base), "title": "Historical yt-dlp title", "channel_id": "UCsynthetic"}
        self.invidious_data = {**deepcopy(self.base), "title": "Historical Invidious title"}
        self.original_player = {
            "videoDetails": {
                "videoId": "synthetic01", "title": "Titre original",
                "shortDescription": "Description originale.\nSuite.", "author": "Never replace the channel",
            }
        }
        self.player_requests = []

        async def respond(url, *, json, headers):
            if "/player?" in url:
                self.player_requests.append(deepcopy(json))
                return self.acquisition.response(deepcopy(self.original_player))
            return await self.acquisition.respond(url, json=json, headers=headers)

        self.respond = respond
        self.acquisition.shared_client.post.side_effect = respond
        package = sys.modules["innertube"]
        package.InnerTubeError = self.acquisition.client.InnerTubeError
        package.is_enabled = Mock(return_value=True)
        package.get_video_player_next = AsyncMock(return_value=self.it_video)
        package.merge_stream_urls = Mock(return_value=(self.base["formatStreams"], self.base["adaptiveFormats"]))
        converters = sys.modules["converters"]
        converters.invidious_to_video_response = lambda data, *args, **kwargs: _VideoResponse(**data)
        converters.ytdlp_to_video_response = lambda data, *args, **kwargs: _VideoResponse(**data)
        converters.invidious_to_video_list_item = lambda data: deepcopy(data)
        converters.ytdlp_to_video_list_item = lambda data: deepcopy(data)
        self.invidious = acquisition_tests._module(
            "invidious_proxy", InvidiousProxyError=_InvidiousError,
            is_enabled=Mock(return_value=False), get_video=AsyncMock(return_value=self.invidious_data),
            get_base_url=Mock(return_value="https://invidious.invalid"),
        )
        ytdlp = acquisition_tests._module(
            "ytdlp_wrapper", YtDlpError=type("YtDlpError", (Exception,), {}),
            get_video_info=AsyncMock(return_value=self.ytdlp_info),
            extract_channel_url=AsyncMock(), extract_url=AsyncMock(), get_channel_avatar=AsyncMock(),
            is_safe_url=Mock(), is_valid_url=Mock(),
        )
        modules = {
            "fastapi": acquisition_tests._module(
                "fastapi", APIRouter=_Router, HTTPException=_HTTPException,
                Query=lambda default=None, **kwargs: default, Request=object,
            ),
            "avatar_cache": acquisition_tests._module(
                "avatar_cache", get_cache=Mock(return_value=SimpleNamespace(schedule_background_fetch=Mock()))
            ),
            "basic_auth": acquisition_tests._module(
                "basic_auth", get_current_user_from_request=Mock(return_value=None)
            ),
            "invidious_proxy": self.invidious,
            "models": acquisition_tests._module(
                "models", VideoResponse=_VideoResponse, Thumbnail=_VideoResponse, ChannelExtractResponse=_VideoResponse
            ),
            "utils": acquisition_tests._module("utils", get_base_url=Mock(return_value="http://testserver")),
            "ytdlp_wrapper": ytdlp,
            "routers": acquisition_tests._module("routers", __path__=[]),
        }
        imports = patch.dict(sys.modules, modules)
        imports.start()
        self.addCleanup(imports.stop)
        sys.modules["database"].get_site_by_extractor = Mock(return_value={"proxy_streaming": False})
        self.route = acquisition_tests._load_module("routers.videos", "routers/videos.py")

        # These isolated route-contract tests keep their existing provider
        # fixtures. Real shared service/SQLite integration is tested separately.
        async def acquire(video_id, language, provider):
            if language == "original":
                return await self.video.get_video_original_metadata(video_id)
            return await self.video.get_video_localized_metadata(video_id, language)

        self.route._acquire_video_metadata = AsyncMock(side_effect=acquire)

    async def request(self, **query):
        return await self.route.get_video("synthetic01", SimpleNamespace(), **query)

    def assert_only_text_changed(self, before, after):
        text_fields = {"title", "description", "descriptionHtml"}
        self.assertEqual(
            {key: value for key, value in before.model_dump().items() if key not in text_fields},
            {key: value for key, value in after.model_dump().items() if key not in text_fields},
        )

    async def test_omitted_lang_preserves_hybrid_response_and_does_no_extra_lookup(self):
        result = await self.request()
        self.assertEqual(result.title, "Historical InnerTube title")
        self.assertEqual(result.description, self.base["description"])
        self.assertEqual(result.descriptionHtml, self.base["descriptionHtml"])
        self.assertEqual(result.extractionMethod, "hybrid")
        self.route.innertube.get_video_player_next.assert_awaited_once_with("synthetic01")
        self.acquisition.shared_client.post.assert_not_awaited()
        self.acquisition.version.assert_not_awaited()

    async def test_omitted_lang_preserves_invidious_priority(self):
        self.invidious.is_enabled.return_value = True
        result = await self.request()
        self.assertEqual(result.title, "Historical Invidious title")
        self.assertEqual(result.descriptionHtml, self.base["descriptionHtml"])
        self.assertEqual(result.extractionMethod, "invidious")
        self.route.innertube.get_video_player_next.assert_not_awaited()
        self.acquisition.shared_client.post.assert_not_awaited()

    async def test_explicit_original_uses_player_details_and_not_merged_description(self):
        before = await self.request()
        result = await self.request(lang="original")
        self.assertEqual(result.title, "Titre original")
        self.assertEqual(result.description, "Description originale.\nSuite.")
        self.assertEqual(result.descriptionHtml, "Description originale.<br>Suite.")
        self.assertEqual(len(self.player_requests), 1)
        self.assertEqual(self.player_requests[0]["videoId"], "synthetic01")
        self.assertEqual(self.acquisition.requests, [])
        self.assert_only_text_changed(before, result)

    async def test_explicit_original_with_invidious_extraction_keeps_its_streams(self):
        self.invidious.is_enabled.return_value = True
        before = await self.request()
        result = await self.request(lang="original")
        self.assertEqual(result.title, "Titre original")
        self.assertEqual(result.extractionMethod, "invidious")
        self.assert_only_text_changed(before, result)
        self.route.innertube.get_video_player_next.assert_not_awaited()

    async def test_original_empty_and_missing_fields_are_independent(self):
        for title, description in ((None, ""), ("", None), ("", "")):
            with self.subTest(title=title, description=description):
                self.original_player["videoDetails"].update(title=title, shortDescription=description)
                result = await self.request(lang="original")
                self.assertEqual(result.title, "Historical InnerTube title" if title is None else title)
                self.assertEqual(result.description, self.base["description"] if description is None else description)
                self.assertEqual(result.descriptionHtml, self.base["descriptionHtml"] if description is None else "")

    async def test_original_does_not_treat_microformat_or_alternative_text_as_original(self):
        self.original_player = {"microformat": {"playerMicroformatRenderer": {"title": {"simpleText": "Translated"}}}}
        result = await self.request(lang="original")
        self.assertEqual(result.title, "Historical InnerTube title")
        self.assertEqual(result.description, self.base["description"])

    async def test_original_rejects_different_video_details(self):
        self.original_player["videoDetails"]["videoId"] = "another_video"
        result = await self.request(lang="original")
        self.assertEqual(result.title, "Historical InnerTube title")

    async def test_french_and_english_successive_requests_use_separate_cache_entries(self):
        french = await self.request(lang="fr")
        english = await self.request(lang="en")
        self.assertEqual(french.title, "J’ai essayé « ça » — résultat ! 🎬")
        self.assertEqual(english.title, "I tried it — here's what happened! 🎬")
        self.assertEqual((await self.request(lang="fr")).model_dump(), french.model_dump())
        self.assertEqual((await self.request(lang="en")).model_dump(), english.model_dump())
        self.assertEqual(self.acquisition.shared_client.post.await_count, 2)

    async def test_localized_fields_are_applied_independently_including_empty_strings(self):
        for title, description in (("Titre seul", None), (None, "Bref."), ("", ""), (None, None)):
            with self.subTest(title=title, description=description):
                self.video._localized_metadata_cache.clear()
                self.acquisition.fixtures["web_runs"] = self.acquisition.metadata_response(title, description)
                result = await self.request(lang="fr")
                self.assertEqual(result.title, "Historical InnerTube title" if title is None else title)
                self.assertEqual(result.description, self.base["description"] if description is None else description)
                expected_html = self.base["descriptionHtml"] if description is None else description
                self.assertEqual(result.descriptionHtml, expected_html)

    async def test_shorter_translation_replaces_longer_description(self):
        result = await self.request(lang="fr")
        self.assertLess(len(result.description), len(self.base["description"]))
        self.assertEqual(result.description, "Été à Paris.\nMerci !\n")

    async def test_description_html_escapes_text_and_preserves_line_breaks(self):
        description = '<script>alert("x")</script> & été\n<a>link</a>\n'
        self.acquisition.fixtures["web_runs"] = self.acquisition.metadata_response("Titre", description)
        result = await self.request(lang="fr")
        self.assertEqual(result.description, description)
        self.assertEqual(
            result.descriptionHtml,
            '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; été<br>&lt;a&gt;link&lt;/a&gt;<br>',
        )

    async def test_original_description_html_is_also_escaped(self):
        self.original_player["videoDetails"]["shortDescription"] = "<b>original</b> &\nfin"
        result = await self.request(lang="original")
        self.assertEqual(result.descriptionHtml, "&lt;b&gt;original&lt;/b&gt; &amp;<br>fin")

    async def test_localization_applies_after_yt_dlp_fallback_without_changing_other_fields(self):
        self.route.innertube.get_video_player_next.side_effect = self.route.InnerTubeError("Simulated player failure")
        before = await self.request()
        result = await self.request(lang="fr")
        self.assertEqual(result.extractionMethod, "ytdlp")
        self.assertEqual(result.title, "J’ai essayé « ça » — résultat ! 🎬")
        self.assert_only_text_changed(before, result)

    async def test_localization_applies_to_live_yt_dlp_response(self):
        self.it_video["liveNow"] = True
        before = await self.request()
        result = await self.request(lang="en")
        self.assertEqual(result.extractionMethod, "ytdlp")
        self.assertEqual(result.title, "I tried it — here's what happened! 🎬")
        self.assert_only_text_changed(before, result)

    async def test_localization_applies_when_stream_merge_uses_yt_dlp_response(self):
        self.route.innertube.merge_stream_urls.return_value = ([], [])
        before = await self.request()
        result = await self.request(lang="fr")
        self.assertEqual(result.extractionMethod, "hybrid")
        self.assertEqual(result.title, "J’ai essayé « ça » — résultat ! 🎬")
        self.assert_only_text_changed(before, result)

    async def test_localization_applies_after_invidious_without_switching_provider(self):
        self.invidious.is_enabled.return_value = True
        before = await self.request()
        result = await self.request(lang="en")
        self.assertEqual(result.extractionMethod, "invidious")
        self.assertEqual(result.title, "I tried it — here's what happened! 🎬")
        self.assert_only_text_changed(before, result)
        self.route.innertube.get_video_player_next.assert_not_awaited()

    async def test_invidious_failure_keeps_historical_hybrid_fallback(self):
        self.invidious.is_enabled.return_value = True
        self.invidious.get_video.side_effect = _InvidiousError("Simulated provider failure")
        result = await self.request(lang="fr")
        self.assertEqual(result.extractionMethod, "hybrid")
        self.assertEqual(result.title, "J’ai essayé « ça » — résultat ! 🎬")

    async def test_forced_hybrid_does_not_try_invidious(self):
        self.invidious.is_enabled.return_value = True
        result = await self.request(lang="fr", invidious=False)
        self.assertEqual(result.extractionMethod, "hybrid")
        self.invidious.get_video.assert_not_awaited()

    async def test_forced_invidious_never_calls_inner_tube_for_any_language(self):
        self.invidious.is_enabled.return_value = True
        for language in (None, "original", "fr", "en"):
            with self.subTest(language=language):
                result = await self.request(lang=language, invidious=True)
                self.assertEqual(result.title, "Historical Invidious title")
                self.assertEqual(result.descriptionHtml, self.base["descriptionHtml"])
        self.route.innertube.get_video_player_next.assert_not_awaited()
        self.acquisition.shared_client.post.assert_not_awaited()
        self.acquisition.version.assert_not_awaited()

    async def test_acquisition_failure_preserves_historical_response(self):
        before = await self.request()
        self.acquisition.shared_client.post.side_effect = self.route.InnerTubeError("Simulated metadata failure")
        for language in ("original", "fr", "en"):
            with self.subTest(language=language), self.assertLogs("innertube"):
                result = await self.request(lang=language)
            self.assertEqual(result.model_dump(), before.model_dump())

    async def test_selector_handles_failed_or_invalid_metadata_helper(self):
        baseline = _VideoResponse(**self.base)
        for value in (None, [], {"title": 42, "description": []}):
            with self.subTest(value=value), patch.object(
                self.route, "_acquire_video_metadata", new_callable=AsyncMock, return_value=value
            ):
                self.assertIs(await self.route._select_video_metadata(baseline, "synthetic01", "fr"), baseline)
        with patch.object(
            self.route, "_acquire_video_metadata", new_callable=AsyncMock,
            side_effect=self.route.InnerTubeError("Simulated helper failure"),
        ), self.assertLogs("routers.videos"):
            self.assertIs(await self.route._select_video_metadata(baseline, "synthetic01", "fr"), baseline)

    async def test_disabled_inner_tube_preserves_historical_yt_dlp_response(self):
        self.route.innertube.is_enabled.return_value = False
        for language in (None, "original", "fr", "en"):
            with self.subTest(language=language):
                result = await self.request(lang=language)
                self.assertEqual(result.title, "Historical yt-dlp title")
                self.assertEqual(result.descriptionHtml, self.base["descriptionHtml"])
        self.acquisition.shared_client.post.assert_not_awaited()

    async def test_missing_lang_returns_same_response_without_copying_or_metadata_lookup(self):
        response = _VideoResponse(**self.base)
        self.assertIs(await self.route._select_video_metadata(response, "synthetic01", None), response)
        self.acquisition.shared_client.post.assert_not_awaited()

    async def test_input_and_cached_objects_are_not_mutated(self):
        snapshots = deepcopy((self.it_video, self.ytdlp_info, self.invidious_data, self.acquisition.player_cache))
        base_response = _VideoResponse(**self.base)
        selected = await self.route._select_video_metadata(base_response, "synthetic01", "fr")
        self.assertIsNot(base_response, selected)
        self.assertEqual(base_response.model_dump(), self.base)
        cached = deepcopy(self.video._localized_metadata_cache)
        selected.title = "Caller modification"
        self.assertEqual(self.video._localized_metadata_cache, cached)
        result = await self.request(lang="fr")
        self.assertEqual(result.title, "J’ai essayé « ça » — résultat ! 🎬")
        self.assertEqual(
            (self.it_video, self.ytdlp_info, self.invidious_data, self.acquisition.player_cache), snapshots
        )

    async def test_simultaneous_french_english_requests_keep_text_and_streams_isolated(self):
        both_started = asyncio.Event()
        bodies = []

        async def interleaved_post(url, *, json, headers):
            bodies.append(json)
            if len(bodies) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return await self.respond(url, json=json, headers=headers)

        self.acquisition.shared_client.post.side_effect = interleaved_post
        french, english = await asyncio.gather(self.request(lang="fr"), self.request(lang="en"))
        self.assertEqual(french.title, "J’ai essayé « ça » — résultat ! 🎬")
        self.assertEqual(english.title, "I tried it — here's what happened! 🎬")
        self.assert_only_text_changed(french, english)
        self.assertIsNot(bodies[0]["context"], bodies[1]["context"])

    async def test_video_extraction_error_does_not_trigger_metadata_lookup(self):
        self.route.get_video_info.side_effect = self.route.YtDlpError("Unavailable video")
        with self.assertRaises(_HTTPException) as error:
            await self.request(lang="fr")
        self.assertEqual(error.exception.status_code, 404)
        self.acquisition.shared_client.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
