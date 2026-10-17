"""Unit tests for current-video /next metadata, using synthetic JSON fixtures."""

import json
import runpy
import unittest
from copy import deepcopy
from pathlib import Path

# The converter only uses the standard library. Load it independently of the
# innertube package so these pure unit tests need no HTTP/database dependencies.
_ROOT = Path(__file__).resolve().parents[1]
_CONVERTERS = runpy.run_path(str(_ROOT / "innertube" / "_converters.py"))
_metadata_from_next = _CONVERTERS["_metadata_from_next"]


class TestMetadataFromNext(unittest.TestCase):
    def setUp(self):
        with (_ROOT / "tests" / "fixtures" / "innertube_next_metadata.json").open(encoding="utf-8") as source:
            self.fixtures = json.load(source)

    def web_contents(self, nxt):
        return nxt["contents"]["twoColumnWatchNextResults"]["results"]["results"]["contents"]

    def panel_items(self, nxt):
        return nxt["engagementPanels"][0]["engagementPanelSectionListRenderer"]["content"][
            "structuredDescriptionContentRenderer"
        ]["items"]

    def test_web_classic_prefers_attributed_description(self):
        self.assertEqual(
            _metadata_from_next(self.fixtures["web_classic"]),
            {
                "title": "I tried it — here's what happened! 🎬",
                "description": "First line.\nSecond line: café, déjà vu!\n",
            },
        )

    def test_classic_simple_text_description_fallback(self):
        nxt = self.fixtures["web_classic"]
        del self.web_contents(nxt)[1]["videoSecondaryInfoRenderer"]["attributedDescription"]
        self.assertEqual(_metadata_from_next(nxt)["description"], "Legacy description")

    def test_french_runs_preserve_unicode_punctuation_and_newlines(self):
        self.assertEqual(
            _metadata_from_next(self.fixtures["web_runs"]),
            {"title": "J’ai essayé « ça » — résultat ! 🎬", "description": "Été à Paris.\nMerci !\n"},
        )

    def test_engagement_panel_metadata(self):
        self.assertEqual(
            _metadata_from_next(self.fixtures["engagement_panel"]),
            {
                "title": "Un titre français — déjà !",
                "description": "Description complète.\nLiens : été, cœur et vidéo 🎥.\n",
            },
        )

    def test_engagement_panel_title_runs(self):
        nxt = self.fixtures["engagement_panel"]
        self.panel_items(nxt)[0]["videoDescriptionHeaderRenderer"]["title"] = {
            "runs": [{"text": "English "}, {"text": "panel title!"}]
        }
        self.assertEqual(_metadata_from_next(nxt)["title"], "English panel title!")

    def test_description_falls_back_to_panel_independently(self):
        nxt = self.fixtures["web_classic"]
        self.web_contents(nxt).pop()
        nxt.update(self.fixtures["engagement_panel"])
        self.assertEqual(
            _metadata_from_next(nxt),
            {
                "title": "I tried it — here's what happened! 🎬",
                "description": "Description complète.\nLiens : été, cœur et vidéo 🎥.\n",
            },
        )

    def test_title_falls_back_to_panel_independently(self):
        nxt = self.fixtures["web_classic"]
        self.web_contents(nxt).pop(0)
        nxt.update(self.fixtures["engagement_panel"])
        self.assertEqual(
            _metadata_from_next(nxt),
            {
                "title": "Un titre français — déjà !",
                "description": "First line.\nSecond line: café, déjà vu!\n",
            },
        )

    def test_web_metadata_precedes_panel_metadata(self):
        nxt = self.fixtures["web_classic"]
        expected = _metadata_from_next(nxt)
        nxt.update(self.fixtures["engagement_panel"])
        self.assertEqual(_metadata_from_next(nxt), expected)

    def test_empty_attributed_description_prevents_fallback(self):
        nxt = self.fixtures["web_classic"]
        self.web_contents(nxt)[1]["videoSecondaryInfoRenderer"]["attributedDescription"]["content"] = ""
        nxt.update(self.fixtures["engagement_panel"])
        self.assertEqual(_metadata_from_next(nxt)["description"], "")

    def test_empty_classic_description_prevents_panel_fallback(self):
        for empty in ({"simpleText": ""}, {"runs": []}, {"runs": [{"text": ""}]}):
            with self.subTest(empty=empty):
                nxt = deepcopy(self.fixtures["web_classic"])
                secondary = self.web_contents(nxt)[1]["videoSecondaryInfoRenderer"]
                del secondary["attributedDescription"]
                secondary["description"] = empty
                nxt.update(self.fixtures["engagement_panel"])
                self.assertEqual(_metadata_from_next(nxt)["description"], "")

    def test_empty_panel_description_is_preserved(self):
        nxt = self.fixtures["engagement_panel"]
        self.panel_items(nxt)[1]["expandableVideoDescriptionBodyRenderer"]["attributedDescriptionBodyText"] = {
            "content": ""
        }
        self.assertEqual(_metadata_from_next(nxt)["description"], "")

    def test_empty_title_is_distinct_from_missing_title(self):
        nxt = self.fixtures["web_classic"]
        self.web_contents(nxt)[0]["videoPrimaryInfoRenderer"]["title"] = {"simpleText": ""}
        nxt.update(self.fixtures["engagement_panel"])
        self.assertEqual(_metadata_from_next(nxt)["title"], "")

    def test_missing_fields(self):
        for nxt in ({}, {"contents": {}}, {"engagementPanels": []}):
            with self.subTest(nxt=nxt):
                self.assertEqual(_metadata_from_next(nxt), {"title": None, "description": None})

    def test_unexpected_json_structures(self):
        for index, nxt in enumerate(self.fixtures["unexpected"]):
            with self.subTest(index=index):
                self.assertEqual(_metadata_from_next(nxt), {"title": None, "description": None})

    def test_malformed_text_allows_panel_fallback(self):
        invalid_texts = [
            None,
            "unexpected",
            [],
            5,
            {},
            {"simpleText": None},
            {"simpleText": 7},
            {"runs": None},
            {"runs": {}},
            {"runs": [None]},
            {"runs": [{"text": 42}]},
            {"runs": [{"text": "Partial"}, {}]},
        ]
        for invalid in invalid_texts:
            with self.subTest(invalid=invalid):
                nxt = deepcopy(self.fixtures["web_classic"])
                self.web_contents(nxt)[0]["videoPrimaryInfoRenderer"]["title"] = invalid
                secondary = self.web_contents(nxt)[1]["videoSecondaryInfoRenderer"]
                secondary["attributedDescription"] = {"content": None}
                secondary["description"] = invalid
                nxt.update(self.fixtures["engagement_panel"])
                self.assertEqual(_metadata_from_next(nxt), _metadata_from_next(self.fixtures["engagement_panel"]))

    def test_malformed_attributed_description_allows_classic_fallback(self):
        for invalid in (None, [], "unexpected", {}, {"content": None}, {"content": 42}):
            with self.subTest(invalid=invalid):
                nxt = deepcopy(self.fixtures["web_classic"])
                self.web_contents(nxt)[1]["videoSecondaryInfoRenderer"]["attributedDescription"] = invalid
                self.assertEqual(_metadata_from_next(nxt)["description"], "Legacy description")

    def test_later_valid_metadata_survives_malformed_siblings(self):
        nxt = self.fixtures["web_classic"]
        self.web_contents(nxt)[:0] = [None, {"videoPrimaryInfoRenderer": {"title": None}}]
        self.assertEqual(_metadata_from_next(nxt)["title"], "I tried it — here's what happened! 🎬")
        panel_nxt = self.fixtures["engagement_panel"]
        panel_nxt["engagementPanels"][:0] = [None, {"engagementPanelSectionListRenderer": {"content": None}}]
        self.assertEqual(_metadata_from_next(panel_nxt)["title"], "Un titre français — déjà !")

    def test_recommendation_titles_are_never_selected(self):
        self.assertEqual(
            _metadata_from_next(self.fixtures["recommendations_only"]),
            {"title": None, "description": None},
        )

    def test_shorter_description_is_accepted(self):
        original = "An original description that is much longer than the localized description."
        nxt = self.fixtures["web_classic"]
        self.web_contents(nxt)[1]["videoSecondaryInfoRenderer"]["attributedDescription"]["content"] = "Bref."
        result = _metadata_from_next(nxt)
        self.assertLess(len(result["description"]), len(original))
        self.assertEqual(result["description"], "Bref.")

    def test_input_is_never_mutated(self):
        for name in ("web_classic", "web_runs", "engagement_panel", "recommendations_only"):
            with self.subTest(name=name):
                nxt = self.fixtures[name]
                before = deepcopy(nxt)
                result = _metadata_from_next(nxt)
                self.assertEqual(nxt, before)
                result["title"] = "Changed output"
                self.assertEqual(nxt, before)
                self.assertNotEqual(_metadata_from_next(nxt)["title"], "Changed output")

    def test_existing_player_converter_keeps_its_metadata_behavior(self):
        player = {
            "videoDetails": {
                "videoId": "synthetic01",
                "title": "Original title",
                "shortDescription": "Original description",
                "author": "Original channel",
            }
        }
        result = _CONVERTERS["innertube_player_to_invidious_video"](player, self.fixtures["web_classic"])
        self.assertEqual(result["title"], "Original title")
        self.assertEqual(result["description"], "First line.\nSecond line: café, déjà vu!\n")
        self.assertEqual(result["author"], "Original channel")


if __name__ == "__main__":
    unittest.main()
