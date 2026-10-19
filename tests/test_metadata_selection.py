"""Selection uses observation provenance, not assumed YouTube edit dates."""

from datetime import UTC, datetime, timedelta

import pytest

from metadata_selection import select_stored_metadata

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def variant(language="fr", *, checked_at=NOW, **fields):
    return {
        "source": "innertube:player" if language == "original" else "innertube:next",
        "checked_at": checked_at,
        "title": "Stored title",
        "description": "Stored description",
        **fields,
    }


def select(provider, language="fr", variants=None, **options):
    return select_stored_metadata(
        provider, language, {"fr": variant()} if variants is None else variants, now=NOW, **options
    )


def test_omitted_language_does_not_select_even_with_available_variants():
    assert select({"title": "Historical"}, language=None) == {}


@pytest.mark.parametrize("language", ["original", "fr", "en"])
def test_requested_candidate_is_distinct_from_history_and_is_only_a_candidate(language):
    assert select({}, language, {language: variant(language)}) == {
        "title": "Stored title",
        "description": "Stored description",
    }


@pytest.mark.parametrize(
    "checked", [None, "invalid", "2026-10-19T12:00:00", NOW - timedelta(days=2), NOW + timedelta(seconds=1)]
)
def test_old_undated_malformed_naive_and_future_observations_only_fill_absent_fields(checked):
    assert select({"title": "Provider"}, variants={"fr": variant(checked_at=checked)}) == {
        "description": "Stored description"
    }


def test_due_variant_does_not_replace_available_provider_field():
    assert (
        select({"title": "Provider", "description": ""}, variants={"fr": variant(next_check_at=NOW.isoformat())}) == {}
    )


@pytest.mark.parametrize("source", [None, "invidious", "innertube:next"])
def test_original_requires_player_provenance_without_certifying_its_language(source):
    assert select({}, "original", {"original": variant("original", source=source)}) == {}


def test_requested_then_original_then_provider_then_old_missing_field_fallback():
    variants = {
        "fr": variant(title=None, description=""),
        "original": variant("original", title="Candidate original"),
    }
    assert select({"title": "Provider"}, variants=variants) == {"title": "Candidate original", "description": ""}
    variants["fr"] = variant(checked_at=NOW - timedelta(days=2))
    assert select({}, variants=variants)["title"] == "Candidate original"


@pytest.mark.parametrize("language", ["fr", "en"])
def test_localized_provider_keeps_present_fields_when_dates_are_not_comparable(language):
    assert (
        select(
            {"title": "Provider", "description": ""},
            language,
            {language: variant(language)},
            provider_language=language,
        )
        == {}
    )


def test_comparable_acquisitions_allow_only_newer_matching_intent_override():
    provider = {"title": "Listing", "description": ""}
    assert select(provider, provider_language="fr", acquired_at=NOW + timedelta(seconds=1)) == {}
    assert select(provider, provider_language="fr", acquired_at=NOW - timedelta(seconds=1)) == {
        "title": "Stored title",
        "description": "Stored description",
    }
    # A newer English-intent listing does not establish freshness of French text.
    assert select(provider, provider_language="en", acquired_at=NOW + timedelta(seconds=1))["title"] == "Stored title"


def test_unknown_source_and_another_language_are_not_used():
    assert select({}, variants={"fr": variant(source="unknown"), "en": variant("en")}) == {}
