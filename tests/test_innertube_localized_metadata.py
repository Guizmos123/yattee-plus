"""Offline unit tests for localized /next acquisition and cache isolation."""

import asyncio
import importlib.util
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

_ROOT = Path(__file__).resolve().parents[1]


def _module(name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _RequestError(Exception):
    pass


class _TimeoutException(_RequestError):
    pass


class _HTTPStatusError(Exception):
    pass


class _UnusedTTLCache:
    def __init__(self, *args, **kwargs):
        raise AssertionError("These unit tests inject cache storage instead of loading cachetools")


class TestVideoLocalizedMetadata(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with (_ROOT / "tests" / "fixtures" / "innertube_next_metadata.json").open(encoding="utf-8") as source:
            self.fixtures = json.load(source)

        self.settings = SimpleNamespace(innertube_enabled=True, cache_video_ttl=3600)
        # Load the real client, extractor and video module in isolation. HTTP,
        # credentials/settings and cache storage are test doubles; no installed
        # third-party packages, database or actual network are needed.
        dependencies = {
            "httpx": _module(
                "httpx",
                AsyncClient=Mock(side_effect=AssertionError("Unexpected HTTP client creation")),
                Response=object,
                RequestError=_RequestError,
                TimeoutException=_TimeoutException,
                HTTPStatusError=_HTTPStatusError,
            ),
            "cryptography": _module("cryptography", __path__=[]),
            "cryptography.fernet": _module("cryptography.fernet", InvalidToken=ValueError),
            "config": _module("config"),
            "database": _module("database", get_feed_video_metadata=Mock(return_value=[])),
            "egress": _module("egress"),
            "encryption": _module("encryption"),
            "settings": _module("settings", get_settings=Mock(return_value=self.settings)),
            "cachetools": _module("cachetools", TTLCache=_UnusedTTLCache),
            "converters": _module("converters", __path__=[]),
            "converters._helpers": _module(
                "converters._helpers", _xtags_from_url=Mock(side_effect=AssertionError("Unexpected stream processing"))
            ),
            "innertube": _module("innertube", __path__=[]),
        }
        imports = patch.dict(sys.modules, dependencies)
        imports.start()
        self.addCleanup(imports.stop)

        network = patch("socket.socket.connect", side_effect=AssertionError("Real network is forbidden"))
        network.start()
        self.addCleanup(network.stop)

        self.client = _load_module("innertube._client", "innertube/_client.py")
        _load_module("innertube._localization", "innertube/_localization.py")
        _load_module("innertube._converters", "innertube/_converters.py")
        self.version = AsyncMock(return_value="2.synthetic.dynamic")
        self.client._get_client_version = self.version
        self.client._load_youtube_cookies = AsyncMock(return_value={})

        self.requests = []
        self.shared_client = SimpleNamespace(
            is_closed=False,
            headers={"Accept-Language": "en-US,en;q=0.9", "Origin": "https://www.youtube.com"},
            post=AsyncMock(side_effect=self.respond),
            get=AsyncMock(side_effect=AssertionError("Client version must be mocked")),
        )
        self.headers_before = deepcopy(self.shared_client.headers)
        self.client._client = self.shared_client

        self.video = _load_module("innertube._video", "innertube/_video.py")
        self.video._localized_metadata_cache = {}
        self.player_cache = {"player_next:synthetic01": {"title": "Original", "formatStreams": [{"itag": "18"}]}}
        self.video._video_cache = self.player_cache

    def response(self, data):
        return SimpleNamespace(raise_for_status=Mock(), json=Mock(return_value=data))

    async def respond(self, url, *, json, headers):
        self.requests.append((url, deepcopy(json), deepcopy(headers)))
        language = json["context"]["client"]["hl"]
        fixture = "web_runs" if language == "fr" else "web_classic"
        return self.response(deepcopy(self.fixtures[fixture]))

    def metadata_response(self, title, description):
        return {
            "contents": {
                "twoColumnWatchNextResults": {
                    "results": {
                        "results": {
                            "contents": [
                                {"videoPrimaryInfoRenderer": {"title": {"simpleText": title}}},
                                {"videoSecondaryInfoRenderer": {"attributedDescription": {"content": description}}},
                            ]
                        }
                    }
                }
            }
        }

    async def test_french_context_uses_dynamic_web_version(self):
        result = await self.video.get_video_localized_metadata("synthetic01", "fr")
        url, body, _ = self.requests[0]
        self.assertIn("/next?", url)
        self.assertEqual(body["videoId"], "synthetic01")
        self.assertEqual(
            body["context"]["client"],
            {"clientName": "WEB", "clientVersion": "2.synthetic.dynamic", "hl": "fr", "gl": "US"},
        )
        self.assertEqual(result["title"], "J’ai essayé « ça » — résultat ! 🎬")
        self.version.assert_awaited_once_with()

    async def test_english_context(self):
        result = await self.video.get_video_localized_metadata("synthetic01", "en")
        self.assertEqual(self.requests[0][1]["context"]["client"]["hl"], "en")
        self.assertEqual(result["title"], "I tried it — here's what happened! 🎬")

    async def test_shared_http_headers_are_unchanged(self):
        await self.video.get_video_localized_metadata("synthetic01", "fr")
        await self.video.get_video_localized_metadata("synthetic01", "en")
        self.assertEqual(self.shared_client.headers, self.headers_before)
        for _, _, headers in self.requests:
            self.assertEqual(headers, {"Content-Type": "application/json"})

    async def test_successive_languages_have_independent_entries(self):
        french = await self.video.get_video_localized_metadata("synthetic01", "fr")
        english = await self.video.get_video_localized_metadata("synthetic01", "en")
        self.assertNotEqual(french, english)
        self.assertEqual(set(self.video._localized_metadata_cache), {("synthetic01", "fr"), ("synthetic01", "en")})
        self.assertEqual(await self.video.get_video_localized_metadata("synthetic01", "fr"), french)
        self.assertEqual(await self.video.get_video_localized_metadata("synthetic01", "en"), english)
        self.assertEqual(self.shared_client.post.await_count, 2)

    async def test_simultaneous_languages_keep_separate_contexts(self):
        both_started = asyncio.Event()
        bodies = []

        async def interleaved_post(url, *, json, headers):
            bodies.append(json)
            if len(bodies) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return await self.respond(url, json=json, headers=headers)

        self.shared_client.post.side_effect = interleaved_post
        french, english = await asyncio.gather(
            self.video.get_video_localized_metadata("synthetic01", "fr"),
            self.video.get_video_localized_metadata("synthetic01", "en"),
        )
        self.assertEqual(french["title"], "J’ai essayé « ça » — résultat ! 🎬")
        self.assertEqual(english["title"], "I tried it — here's what happened! 🎬")
        self.assertIsNot(bodies[0]["context"], bodies[1]["context"])
        self.assertEqual([body["context"]["client"]["hl"] for body in bodies], ["fr", "en"])
        self.assertEqual(self.shared_client.headers, self.headers_before)
        self.assertEqual(len(self.video._localized_metadata_cache), 2)

    async def test_same_language_reuses_cache_without_version_lookup(self):
        first = await self.video.get_video_localized_metadata("synthetic01", "fr")
        second = await self.video.get_video_localized_metadata("synthetic01", "fr")
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.shared_client.post.assert_awaited_once()
        self.version.assert_awaited_once()

    async def test_different_video_ids_do_not_share_entries(self):
        await self.video.get_video_localized_metadata("synthetic01", "fr")
        await self.video.get_video_localized_metadata("synthetic02", "fr")
        self.assertEqual(set(self.video._localized_metadata_cache), {("synthetic01", "fr"), ("synthetic02", "fr")})
        self.assertEqual(self.shared_client.post.await_count, 2)

    async def test_player_next_cache_and_contents_are_untouched(self):
        before = deepcopy(self.player_cache)
        await self.video.get_video_localized_metadata("synthetic01", "fr")
        await self.video.get_video_localized_metadata("synthetic01", "en")
        self.assertIs(self.video._video_cache, self.player_cache)
        self.assertEqual(self.player_cache, before)
        self.assertIsNot(self.video._localized_metadata_cache, self.player_cache)
        self.assertTrue(all("/next?" in url for url, _, _ in self.requests))

    async def test_mutating_miss_or_hit_result_cannot_change_cache(self):
        first = await self.video.get_video_localized_metadata("synthetic01", "fr")
        expected = first.copy()
        first["title"] = "Caller modification"
        second = await self.video.get_video_localized_metadata("synthetic01", "fr")
        self.assertEqual(second, expected)
        second["description"] = "Another modification"
        self.assertEqual(await self.video.get_video_localized_metadata("synthetic01", "fr"), expected)
        self.assertEqual(self.shared_client.post.await_count, 1)

    async def test_missing_and_empty_values_are_preserved_and_cached(self):
        for title, description in ((None, ""), ("", None), ("", ""), (None, "Description seule.")):
            with self.subTest(title=title, description=description):
                self.video._localized_metadata_cache.clear()
                self.shared_client.post.reset_mock()
                self.shared_client.post.side_effect = None
                self.shared_client.post.return_value = self.response(self.metadata_response(title, description))
                expected = {"title": title, "description": description}
                self.assertEqual(await self.video.get_video_localized_metadata("synthetic01", "fr"), expected)
                self.assertEqual(await self.video.get_video_localized_metadata("synthetic01", "fr"), expected)
                self.shared_client.post.assert_awaited_once()

    async def test_invalid_responses_do_not_poison_cache(self):
        for data in (None, [], "unexpected", {}, {"contents": None}, {"error": None}):
            with self.subTest(data=data):
                self.shared_client.post.side_effect = None
                self.shared_client.post.return_value = self.response(data)
                self.assertEqual(
                    await self.video.get_video_localized_metadata("synthetic01", "fr"),
                    {"title": None, "description": None},
                )
                self.assertEqual(self.video._localized_metadata_cache, {})
        self.shared_client.post.side_effect = self.respond
        recovered = await self.video.get_video_localized_metadata("synthetic01", "fr")
        self.assertEqual(recovered["title"], "J’ai essayé « ça » — résultat ! 🎬")

    async def test_network_error_falls_back_without_caching_and_allows_retry(self):
        self.shared_client.post.side_effect = _RequestError("Simulated network failure")
        with patch.object(self.client.asyncio, "sleep", new_callable=AsyncMock), self.assertLogs("innertube"):
            result = await self.video.get_video_localized_metadata("synthetic01", "fr")
        self.assertEqual(result, {"title": None, "description": None})
        self.assertEqual(self.shared_client.post.await_count, 3)
        self.assertEqual(self.video._localized_metadata_cache, {})
        self.shared_client.post.side_effect = self.respond
        self.assertIsNotNone((await self.video.get_video_localized_metadata("synthetic01", "fr"))["title"])

    async def test_inner_tube_error_does_not_affect_cached_other_language(self):
        english = await self.video.get_video_localized_metadata("synthetic01", "en")
        self.shared_client.post.side_effect = self.client.InnerTubeError("Simulated InnerTube failure")
        with self.assertLogs("innertube"):
            french = await self.video.get_video_localized_metadata("synthetic01", "fr")
        self.assertEqual(french, {"title": None, "description": None})
        self.assertEqual(await self.video.get_video_localized_metadata("synthetic01", "en"), english)
        self.assertEqual(set(self.video._localized_metadata_cache), {("synthetic01", "en")})

    async def test_invalid_json_falls_back_without_caching(self):
        response = self.response(None)
        response.json.side_effect = ValueError("Simulated invalid JSON")
        self.shared_client.post.side_effect = None
        self.shared_client.post.return_value = response
        with self.assertLogs("innertube"):
            result = await self.video.get_video_localized_metadata("synthetic01", "fr")
        self.assertEqual(result, {"title": None, "description": None})
        self.assertEqual(self.video._localized_metadata_cache, {})

    async def test_unsupported_languages_fail_before_any_request(self):
        for language in ("original", "FR", "fr-FR", "", None, [], 1):
            with self.subTest(language=language), self.assertRaises(ValueError):
                await self.video.get_video_localized_metadata("synthetic01", language)
        self.version.assert_not_awaited()
        self.shared_client.post.assert_not_awaited()

    async def test_disabled_inner_tube_does_not_request_client_version(self):
        self.settings.innertube_enabled = False
        self.assertEqual(
            await self.video.get_video_localized_metadata("synthetic01", "fr"),
            {"title": None, "description": None},
        )
        self.version.assert_not_awaited()
        self.shared_client.post.assert_not_awaited()

    async def test_cancellation_is_not_swallowed(self):
        self.shared_client.post.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.video.get_video_localized_metadata("synthetic01", "fr")
        self.assertEqual(self.video._localized_metadata_cache, {})

    def test_localized_cache_uses_configured_ttl_and_own_capacity(self):
        self.video.reset_localized_metadata_cache()
        with patch.object(self.video, "TTLCache", return_value={}) as constructor:
            cache = self.video._get_localized_metadata_cache()
            self.assertIs(self.video._get_localized_metadata_cache(), cache)
        constructor.assert_called_once_with(maxsize=200, ttl=3600)
        self.assertIs(self.video._video_cache, self.player_cache)

    async def test_reset_localized_cache_keeps_player_cache(self):
        await self.video.get_video_localized_metadata("synthetic01", "fr")
        before = deepcopy(self.player_cache)
        self.video.reset_localized_metadata_cache()
        self.assertIsNone(self.video._localized_metadata_cache)
        self.assertIs(self.video._video_cache, self.player_cache)
        self.assertEqual(self.player_cache, before)

    async def test_failed_version_lookup_is_safe(self):
        self.version.side_effect = self.client.InnerTubeError("Simulated version failure")
        with self.assertLogs("innertube"):
            result = await self.video.get_video_localized_metadata("synthetic01", "fr")
        self.assertEqual(result, {"title": None, "description": None})
        self.shared_client.post.assert_not_awaited()
        self.assertEqual(self.video._localized_metadata_cache, {})


if __name__ == "__main__":
    unittest.main()
