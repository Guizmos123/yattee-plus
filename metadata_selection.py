"""Select requested text without claiming that a provider verified its language.

Pure selection only: acquisition dates describe observations, not YouTube edit
dates. Older/undated variants remain fallbacks for missing fields, not overrides
of available provider text. No persistence or scheduling lives here.
"""

from datetime import UTC, datetime, timedelta
from typing import Optional

_MAX_OBSERVATION_AGE = timedelta(days=1)
_SOURCES = {"original": "innertube:player", "fr": "innertube:next", "en": "innertube:next"}


def _observation_time(value) -> Optional[datetime]:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def select_stored_metadata(
    provider: dict,
    language: Optional[str],
    variants: dict[str, dict],
    *,
    provider_language: Optional[str] = None,
    acquired_at=None,
    now: Optional[datetime] = None,
) -> dict[str, str]:
    """Return text overrides independently, preserving empty strings.

    A matching source identifies only an acquisition method/intention. In
    particular, /player is an original candidate, never proof of originality.
    A recent requested variant precedes a recent original candidate, then the
    provider, then old/undated candidates for absent provider fields only.

    When the provider was itself requested in this language, prefer its present
    fields unless comparable observation dates establish a newer recent variant
    of the same intention. An undated current localized list therefore keeps its
    own text; this does not assert that YouTube edited it more recently.
    """
    if language not in _SOURCES:
        return {}
    current_time = now if now is not None else datetime.now(UTC)
    acquired = _observation_time(acquired_at)
    candidates = []
    for candidate_language in dict.fromkeys((language, "original")):
        row = variants.get(candidate_language, {})
        if row.get("source") != _SOURCES[candidate_language] and not row.get("_field_metadata"):
            continue
        checked = _observation_time(row.get("checked_at"))
        due = _observation_time(row.get("next_check_at"))
        recent = checked is not None and timedelta(0) <= current_time - checked < _MAX_OBSERVATION_AGE
        if due is not None and due <= current_time:
            recent = False
        candidates.append((candidate_language, row, checked, recent))

    updates = {}
    for field in ("title", "description"):
        present = isinstance(provider.get(field), str)
        field_candidates = []
        for candidate_language, row, checked, recent in candidates:
            text = row.get(field)
            observation = row.get("_field_metadata", {}).get(field)
            if observation is not None:
                if observation.get("source") != _SOURCES[candidate_language]:
                    continue
                text = observation.get("value")
                checked = _observation_time(observation.get("acquired_at"))
                # Isolated META-03 rule, only for fields with real acquisition
                # facts. Historical rows keep their unchanged 24-hour policy.
                hours = observation.get("freshness_hours", 24)
                hours = hours if type(hours) is int and 1 <= hours <= 72 else 24
                recent = checked is not None and timedelta(0) <= current_time - checked < timedelta(hours=hours)
            elif row.get("source") != _SOURCES[candidate_language]:
                continue
            if not isinstance(text, str):
                continue
            field_candidates.append((candidate_language, text, checked, recent))
        ordered = sorted(field_candidates, key=lambda candidate: not candidate[3])
        for candidate_language, text, checked, recent in ordered:
            if present:
                if not recent:
                    continue
                if provider_language == language and (
                    candidate_language != language or acquired is None or checked < acquired
                ):
                    continue
            updates[field] = text
            break
    return updates
