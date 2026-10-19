"""ASGI contract tests using the real FastAPI router and VideoResponse model."""

import asyncio
import importlib.util
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

_HAS_APP_DEPENDENCIES = all(
    importlib.util.find_spec(name) is not None
    for name in ("fastapi", "pydantic", "httpx", "cachetools", "cryptography", "jwt", "bcrypt", "alembic", "sqlalchemy")
)


@unittest.skipUnless(_HAS_APP_DEPENDENCIES, "Application dependencies are unavailable; no network installation allowed")
class TestVideoLanguageHTTP(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from fastapi import FastAPI

        from models import VideoResponse
        from routers import videos

        self.route = videos
        self.base = VideoResponse(
            videoId="synthetic01",
            title="Historical title",
            description="Historical description, much longer.",
            descriptionHtml="<p>Historical description</p>",
            author="Channel name",
            authorId="UCsynthetic",
            lengthSeconds=120,
            extractionMethod="hybrid",
            adaptiveFormats=[
                {
                    "url": "https://example.invalid/audio",
                    "itag": "140",
                    "type": "audio/mp4",
                    "container": "m4a",
                    "audioTrack": {"id": "en.4", "displayName": "English (original)", "isDefault": True},
                }
            ],
            captions=[{"label": "English", "languageCode": "en", "url": "https://example.invalid/captions"}],
        )

        def patched(target, **kwargs):
            handle = patch(target, **kwargs)
            result = handle.start()
            self.addCleanup(handle.stop)
            return result

        patched("routers.videos.validate_extractor_allowed", return_value=None)
        self.stored = patched("routers.videos._read_stored_metadata", return_value={})
        patched("routers.videos.get_base_url", return_value="http://testserver")
        patched("routers.videos.get_settings", return_value=SimpleNamespace(invidious_proxy_videos=True))
        patched("routers.videos.get_current_user_from_request", return_value=None)
        patched("routers.videos.database.get_site_by_extractor", return_value={"proxy_streaming": False})
        patched("routers.videos.avatar_cache.get_cache", return_value=SimpleNamespace(schedule_background_fetch=Mock()))
        self.invidious_enabled = patched("routers.videos.invidious_proxy.is_enabled", return_value=False)
        self.player_next = patched(
            "routers.videos.innertube.get_video_player_next",
            new_callable=AsyncMock,
            side_effect=AssertionError("Unexpected acquisition during HTTP contract tests"),
        )
        patched("routers.videos.innertube.is_enabled", return_value=True)
        self.hybrid = patched("routers.videos._get_video_hybrid", new_callable=AsyncMock, return_value=self.base)
        self.original = AsyncMock(
            return_value={"title": "Original title", "description": "Original description"},
        )

        async def localized(video_id, language):
            return {"title": f"Title {language}", "description": f"Description {language}"}

        self.localized = AsyncMock(side_effect=localized)
        self.recommendations = patched(
            "routers.videos.get_video_localized_metadata", new_callable=AsyncMock,
            return_value={"recommendedVideos": {}},
        )

        async def acquire(video_id, language, provider):
            return await self.original(video_id) if language == "original" else await self.localized(video_id, language)

        patched("routers.videos._acquire_video_metadata", new_callable=AsyncMock, side_effect=acquire)
        app = FastAPI()
        app.include_router(videos.router, prefix="/api/v1")
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        self.addAsyncCleanup(self.http.aclose)

    async def request(self, **params):
        return await self.http.get("/api/v1/videos/synthetic01", params=params)

    async def test_missing_lang_preserves_historical_json_and_performs_no_metadata_lookup(self):
        result = await self.request()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), self.base.model_dump(mode="json"))
        self.original.assert_not_awaited()
        self.localized.assert_not_awaited()

    async def test_explicit_original_is_distinct_from_missing_lang(self):
        historical = await self.request()
        original = await self.request(lang="original")
        self.assertEqual(original.status_code, 200)
        self.assertEqual(historical.json()["title"], "Historical title")
        self.assertEqual(original.json()["title"], "Original title")
        self.original.assert_awaited_once_with("synthetic01")

    async def test_french_english_and_existing_parameters(self):
        for language in ("fr", "en"):
            with self.subTest(language=language):
                result = await self.request(lang=language, proxy="false", proxy_mode="download", invidious="false")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["title"], f"Title {language}")
        self.assertEqual(self.localized.await_count, 2)

    async def test_invalid_language_returns_422_before_extraction(self):
        for language in ("de", "FR", "", "originale", "fr-FR"):
            with self.subTest(language=language):
                result = await self.request(lang=language)
                self.assertEqual(result.status_code, 422)
                self.assertIn(["query", "lang"], [error["loc"] for error in result.json()["detail"]])
        self.hybrid.assert_not_awaited()
        self.original.assert_not_awaited()
        self.localized.assert_not_awaited()

    async def test_empty_description_and_missing_title_are_applied_independently(self):
        self.localized.side_effect = None
        self.localized.return_value = {"title": None, "description": ""}
        result = await self.request(lang="fr")
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["title"], "Historical title")
        self.assertEqual(data["description"], "")
        self.assertEqual(data["descriptionHtml"], "")

    async def test_html_and_schema_are_coherent_without_mutating_response(self):
        before = self.base.model_dump(mode="json")
        self.localized.side_effect = None
        self.localized.return_value = {"title": "Titre", "description": "<b>Texte</b> &\nsuite"}
        result = await self.request(lang="fr")
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["descriptionHtml"], "&lt;b&gt;Texte&lt;/b&gt; &amp;<br>suite")
        self.assertEqual(set(data), set(before))
        for field in set(before) - {"title", "description", "descriptionHtml"}:
            self.assertEqual(data[field], before[field])
        self.assertEqual(self.base.model_dump(mode="json"), before)

    async def test_forced_invidious_uses_local_candidates_and_never_calls_inner_tube(self):
        self.invidious_enabled.return_value = True
        data = self.base.model_dump(mode="json")
        with (
            patch.object(self.route.invidious_proxy, "get_video", new_callable=AsyncMock, return_value=data),
            patch.object(self.route.invidious_proxy, "get_base_url", return_value="https://invidious.invalid"),
            patch.object(self.route, "invidious_to_video_response", return_value=self.base),
        ):
            for language in ("original", "fr", "en"):
                with self.subTest(language=language):
                    self.stored.return_value = {
                        ("synthetic01", language): {
                            "source": "innertube:player" if language == "original" else "innertube:next",
                            "checked_at": datetime.now(UTC).isoformat(),
                            "title": f"Local {language}",
                            "description": "<texte>\nsuite",
                        }
                    }
                    result = await self.request(lang=language, invidious="true")
                    self.assertEqual(result.status_code, 200)
                    expected = {
                        **data,
                        "extractionMethod": "invidious",
                        "title": f"Local {language}",
                        "description": "<texte>\nsuite",
                        "descriptionHtml": "&lt;texte&gt;<br>suite",
                    }
                    self.assertEqual(result.json(), expected)
        self.hybrid.assert_not_awaited()
        self.original.assert_not_awaited()
        self.localized.assert_not_awaited()
        self.player_next.assert_not_awaited()

    async def test_acquisition_failure_preserves_usable_video(self):
        self.localized.side_effect = self.route.InnerTubeError("Simulated metadata error")
        with self.assertLogs("routers.videos"):
            result = await self.request(lang="fr")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), self.base.model_dump(mode="json"))

    async def test_acquisition_failure_uses_local_fields_without_mutating_streams(self):
        self.stored.return_value = {
            ("synthetic01", "fr"): {
                "source": "innertube:next",
                "checked_at": datetime.now(UTC).isoformat(),
                "title": None,
                "description": "Local description",
            }
        }
        self.localized.side_effect = self.route.InnerTubeError("unavailable")
        with self.assertLogs("routers.videos"):
            result = await self.request(lang="fr")
        expected = {
            **self.base.model_dump(mode="json"),
            "description": "Local description",
            "descriptionHtml": "Local description",
        }
        self.assertEqual(result.json(), expected)

    async def test_forced_invidious_without_local_fields_keeps_complete_provider_response(self):
        self.invidious_enabled.return_value = True
        data = self.base.model_dump(mode="json")
        with (
            patch.object(self.route.invidious_proxy, "get_video", new_callable=AsyncMock, return_value=data),
            patch.object(self.route.invidious_proxy, "get_base_url", return_value="https://invidious.invalid"),
            patch.object(self.route, "invidious_to_video_response", return_value=self.base),
        ):
            result = await self.request(lang="original", invidious="true")
        self.assertEqual(result.json(), {**data, "extractionMethod": "invidious"})
        self.original.assert_not_awaited()
        self.localized.assert_not_awaited()

    async def test_simultaneous_languages_are_isolated(self):
        started = asyncio.Event()
        languages = []

        async def localized(video_id, language):
            languages.append(language)
            if len(languages) == 2:
                started.set()
            await asyncio.wait_for(started.wait(), timeout=1)
            return {"title": f"Title {language}", "description": f"Description {language}"}

        self.localized.side_effect = localized
        french, english = await asyncio.gather(self.request(lang="fr"), self.request(lang="en"))
        self.assertEqual(french.status_code, 200)
        self.assertEqual(english.status_code, 200)
        self.assertEqual(french.json()["title"], "Title fr")
        self.assertEqual(english.json()["title"], "Title en")


if __name__ == "__main__":
    unittest.main()
