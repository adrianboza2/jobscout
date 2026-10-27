"""Title-based seniority pre-filter.

The rule is a pure function computed at read time, never stored in SQLite: the
owner withdrew the CP-10 ban on a title heuristic on 2026-10-27, but keeping the
decision out of the schema means the rule can change without a migration.
"""

from __future__ import annotations

import re

#: Whole-word senior markers.
SENIOR_MARKERS: tuple[str, ...] = (
    "senior",
    "sr",
    "lead",
    "principal",
    "architect",
    "arquitecto",
    "arquitecta",
    "manager",
    "head",
    "director",
    "staff",
    "jefe",
)

#: Any of these cancels the filter, so junior-titled roles with a senior word
#: (for example "Junior Lead Developer") are never hidden.
JUNIOR_MARKERS: tuple[str, ...] = (
    "junior",
    "jr",
    "trainee",
    "beca",
    "becario",
    "becaria",
    "practicas",
    "intern",
    "internship",
)

# ``\b`` keeps ``sr`` out of ``SRE`` and ``lead`` out of ``leader``, while still
# matching ``Sr.`` because the dot is not a word character. ``prácticas`` is
# accepted with or without its accent.
_SENIOR_RE = re.compile(
    r"\b(" + "|".join(SENIOR_MARKERS) + r")\b",
    re.IGNORECASE,
)
_JUNIOR_PATTERN = "|".join(
    marker.replace("practicas", "pr[áa]cticas") for marker in JUNIOR_MARKERS
)
_JUNIOR_RE = re.compile(r"\b(" + _JUNIOR_PATTERN + r")\b", re.IGNORECASE)


def seniority_filter_reason(title: str | None) -> str | None:
    """Return ``"title: <marker>"`` when *title* is clearly senior, else ``None``."""
    if not title:
        return None
    if _JUNIOR_RE.search(title):
        return None
    match = _SENIOR_RE.search(title)
    if match is None:
        return None
    # Lowercased because the reason reports the marker as written in the list,
    # not the casing used by the posting.
    return f"title: {match.group(1).lower()}"
