"""Curated, bounded keyword families for manual LinkedIn searches.

Every query jobscout runs is explicit. There is no query generation from a model
and no expansion past the lists below, so adding coverage is a deliberate,
reviewable change instead of an uncontrolled growth of live requests. The
catalog is the single source of truth for *which searches are worth running*;
the collector still requires ``--confirm-network`` per invocation, and observed
LinkedIn result position remains provenance, never a quality signal.

Families are grouped:

* ``baseline`` — the queries the first working version already relied on.
* ``security`` — Cloud Security, DevSecOps, Azure security, and
  IAM/security-infrastructure families added after too few security roles were
  found.

Adding or changing a query:

1. add it to the closest family (or a new family) as a literal string;
2. keep a family at or below :data:`HARD_MAX_QUERIES_PER_FAMILY`;
3. run ``jobscout keyword-plan`` and the focused keyword tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: Absolute upper bound on the queries one family may declare. A family is a
#: reviewable unit; a runaway list would silently multiply live requests.
HARD_MAX_QUERIES_PER_FAMILY = 3

#: Family groups. Kept as plain strings so the catalog stays trivially
#: serializable into the ``keyword-plan`` payload.
GROUP_BASELINE = "baseline"
GROUP_SECURITY = "security"


class UnknownKeywordFamily(ValueError):
    """Raised when a requested family id is not in the catalog."""


@dataclass(frozen=True)
class KeywordFamily:
    """One reviewable group of related searches."""

    id: str
    label: str
    focus: str
    group: str
    queries: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "focus": self.focus,
            "group": self.group,
            "queries": list(self.queries),
        }


#: The explicit catalog. Order is the presentation order of ``keyword-plan``.
KEYWORD_FAMILIES: tuple[KeywordFamily, ...] = (
    KeywordFamily(
        id="junior-cloud",
        label="Junior Cloud Engineer",
        focus="Entry-level cloud engineer roles already known to be productive.",
        group=GROUP_BASELINE,
        queries=("Junior Cloud Engineer",),
    ),
    KeywordFamily(
        id="junior-devops",
        label="Junior DevOps Engineer",
        focus="Entry-level DevOps roles already known to be productive.",
        group=GROUP_BASELINE,
        queries=("Junior DevOps Engineer",),
    ),
    KeywordFamily(
        id="platform-engineer",
        label="Platform Engineer",
        focus="Platform engineering roles adjacent to DevOps.",
        group=GROUP_BASELINE,
        queries=("Platform Engineer",),
    ),
    KeywordFamily(
        id="cloud-security",
        label="Cloud Security Engineer",
        focus="Cloud security roles at mid and junior level.",
        group=GROUP_SECURITY,
        queries=("Cloud Security Engineer", "Junior Cloud Security Engineer"),
    ),
    KeywordFamily(
        id="devsecops",
        label="DevSecOps Engineer",
        focus="DevSecOps roles at mid and junior level.",
        group=GROUP_SECURITY,
        queries=("DevSecOps Engineer", "Junior DevSecOps Engineer"),
    ),
    KeywordFamily(
        id="azure-cloud-security",
        label="Azure Cloud Security",
        focus="Azure-specific security roles and variants.",
        group=GROUP_SECURITY,
        queries=("Azure Cloud Security", "Azure Security Engineer"),
    ),
    KeywordFamily(
        id="azure-devsecops",
        label="Azure DevSecOps",
        focus="Azure-specific DevSecOps and pipeline-security variants.",
        group=GROUP_SECURITY,
        queries=("Azure DevSecOps",),
    ),
    KeywordFamily(
        id="iam-security-infra",
        label="IAM and security infrastructure",
        focus="Identity/access management and security-infrastructure roles.",
        group=GROUP_SECURITY,
        queries=(
            "IAM Engineer",
            "Identity and Access Management Engineer",
            "Security Infrastructure Engineer",
        ),
    ),
)


def _validate_catalog() -> None:
    """Fail loudly on a malformed literal catalog (import-time self-check)."""
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()
    for family in KEYWORD_FAMILIES:
        if not family.id or not family.id.strip():
            raise ValueError("every keyword family needs a non-empty id")
        if family.id in seen_ids:
            raise ValueError(f"duplicate keyword family id {family.id!r}")
        seen_ids.add(family.id)
        if not family.queries:
            raise ValueError(f"keyword family {family.id!r} declares no query")
        if len(family.queries) > HARD_MAX_QUERIES_PER_FAMILY:
            raise ValueError(
                f"keyword family {family.id!r} declares {len(family.queries)} queries; "
                f"the hard bound is {HARD_MAX_QUERIES_PER_FAMILY}"
            )
        for query in family.queries:
            if not query.strip():
                raise ValueError(f"keyword family {family.id!r} has an empty query")
            if query in seen_queries:
                raise ValueError(f"duplicate query {query!r} across keyword families")
            seen_queries.add(query)


_validate_catalog()

_BY_ID: dict[str, KeywordFamily] = {family.id: family for family in KEYWORD_FAMILIES}


def family_ids() -> tuple[str, ...]:
    """Every family id, in catalog order."""
    return tuple(family.id for family in KEYWORD_FAMILIES)


def get_family(family_id: str) -> KeywordFamily:
    """Return one family, or raise :class:`UnknownKeywordFamily`."""
    key = str(family_id or "").strip()
    family = _BY_ID.get(key)
    if family is None:
        raise UnknownKeywordFamily(
            f"unknown keyword family {family_id!r}; known ids: {', '.join(family_ids())}"
        )
    return family


def resolve_families(family_ids_requested: Iterable[str] | None = None) -> tuple[KeywordFamily, ...]:
    """Resolve requested ids (or the whole catalog) to families.

    ``None`` and an empty selection both mean "every family". The result follows
    catalog order and each family appears at most once, so a repeated or
    reordered ``--family`` selection still plans the same bounded list.
    """
    requested = {str(item).strip() for item in (family_ids_requested or []) if str(item).strip()}
    if not requested:
        return KEYWORD_FAMILIES
    # Validate before selecting so an unknown id never returns a partial plan.
    for family_id in requested:
        get_family(family_id)
    return tuple(family for family in KEYWORD_FAMILIES if family.id in requested)


def search_command(
    query: str,
    *,
    location: str | None = None,
    remote: bool = False,
) -> str:
    """The exact preview command for one query, ready to paste.

    The command is a ``--dry-run`` preview. Turning it into a real run means
    replacing ``--dry-run`` with ``--confirm-network``, which is always a
    deliberate human action.
    """
    parts = ["python", "-m", "jobscout", "search-linkedin", "--keywords", f'"{query}"']
    if location:
        parts.extend(["--location", f'"{location}"'])
    if remote:
        parts.append("--remote")
    parts.append("--dry-run")
    return " ".join(parts)


def planned_queries(
    family_ids_requested: Sequence[str] | None = None,
    *,
    location: str | None = None,
    remote: bool = False,
) -> list[dict[str, Any]]:
    """Flatten the selected families into bounded, runnable query entries."""
    planned: list[dict[str, Any]] = []
    for family in resolve_families(family_ids_requested):
        for query in family.queries:
            planned.append(
                {
                    "family": family.id,
                    "label": family.label,
                    "group": family.group,
                    "query": query,
                    "command": search_command(query, location=location, remote=remote),
                }
            )
    return planned


__all__ = [
    "GROUP_BASELINE",
    "GROUP_SECURITY",
    "HARD_MAX_QUERIES_PER_FAMILY",
    "KEYWORD_FAMILIES",
    "KeywordFamily",
    "UnknownKeywordFamily",
    "family_ids",
    "get_family",
    "planned_queries",
    "resolve_families",
    "search_command",
]
