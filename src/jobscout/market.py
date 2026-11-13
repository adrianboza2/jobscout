"""Market aggregates for the dashboard's "Mercado" tab.

Three read-only views over every stored offer, closed ones included:

* companies that publish non-senior cloud/DevOps/security roles again and again;
* technologies asked for by the good offers (latest successful evaluation at or
  above :data:`MARKET_MIN_SCORE`) and whether each one is already in the
  candidate profile;
* the estimated and stated salary by employer tier and seniority level.

Only aggregates reach the HTML payload: no description, no per-offer row. The
module reads SQLite and nothing else; it never touches the network.

Seniority reuses the same read-time title rule as the offers tab
(:func:`jobscout.seniority.seniority_filter_reason`) plus the stored
``jobs.seniority`` column, so "non-senior" means the same thing in both places.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any

from .seniority import seniority_filter_reason

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .scoring.profile import Profile

#: An offer counts as "good" for the technology table when its latest
#: successful evaluation reaches this score.
MARKET_MIN_SCORE = 60.0

#: Rows shown in the repeated-companies block.
COMPANY_LIMIT = 25

#: A salary cell with fewer estimates than this shows no median.
MIN_MEDIAN_OFFERS = 3

#: Stored ``jobs.seniority`` values that make an offer senior even when its
#: title has no senior marker.
SENIOR_LEVELS = frozenset({"senior", "lead", "manager", "director"})

#: Display label for offers whose latest evaluation has no ``employer_tier``.
UNCLASSIFIED_TIER = "sin clasificar"

#: Fixed technology vocabulary: canonical name plus whole-word aliases. Kept in
#: code on purpose, so the vocabulary is reviewable and needs no migration.
TECHNOLOGIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AWS", ("AWS", "Amazon Web Services")),
    ("Azure", ("Azure", "Microsoft Azure")),
    ("GCP", ("GCP", "Google Cloud", "Google Cloud Platform")),
    ("VMware", ("VMware",)),
    ("OpenShift", ("OpenShift",)),
    ("Kubernetes", ("Kubernetes", "K8s")),
    ("Docker", ("Docker",)),
    ("Helm", ("Helm chart", "Helm charts", "Helm")),
    ("Terraform", ("Terraform",)),
    ("Ansible", ("Ansible",)),
    ("Jenkins", ("Jenkins",)),
    ("GitHub Actions", ("GitHub Actions",)),
    ("GitLab CI", ("GitLab CI", "GitLab CI/CD")),
    ("ArgoCD", ("ArgoCD", "Argo CD")),
    ("GitLab", ("GitLab",)),
    ("Git", ("Git",)),
    ("Linux", ("Linux",)),
    ("Bash", ("Bash",)),
    ("PowerShell", ("PowerShell",)),
    ("Python", ("Python",)),
    ("Golang", ("Golang",)),
    ("Java", ("Java",)),
    ("Node.js", ("Node.js", "NodeJS")),
    (".NET", (".NET", "dotnet")),
    ("REST API", ("REST API",)),
    ("GraphQL", ("GraphQL",)),
    ("PostgreSQL", ("PostgreSQL", "Postgres")),
    ("MySQL", ("MySQL",)),
    ("MongoDB", ("MongoDB",)),
    ("Redis", ("Redis",)),
    ("Kafka", ("Kafka", "Apache Kafka")),
    ("Elasticsearch", ("Elasticsearch", "Elastic Search")),
    ("Prometheus", ("Prometheus",)),
    ("Grafana", ("Grafana",)),
    ("Datadog", ("Datadog", "Data Dog")),
    ("Splunk", ("Splunk",)),
    ("Wazuh", ("Wazuh",)),
    ("SIEM", ("SIEM",)),
    ("EDR", ("EDR",)),
    ("IDS/IPS", ("IDS/IPS", "IDS", "IPS")),
    ("Wireshark", ("Wireshark",)),
    ("Burp Suite", ("Burp Suite", "Burp")),
    ("OWASP", ("OWASP",)),
    ("Vault", ("HashiCorp Vault",)),
    ("Keycloak", ("Keycloak",)),
    ("CyberArk", ("CyberArk",)),
    ("Active Directory", ("Active Directory", "AD DS")),
    ("SAML", ("SAML",)),
    ("OAuth", ("OAuth", "OAuth2")),
    ("OIDC", ("OIDC", "OpenID Connect")),
    ("Zero Trust", ("Zero Trust",)),
    ("ISO 27001", ("ISO 27001", "ISO/IEC 27001")),
    ("GDPR", ("GDPR", "RGPD")),
    ("IAM", ("IAM",)),
    ("DevSecOps", ("DevSecOps",)),
    ("CI/CD", ("CI/CD", "CI CD")),
    ("Nmap", ("Nmap",)),
    ("SonarQube", ("SonarQube",)),
    ("Snyk", ("Snyk",)),
    ("Nessus", ("Nessus",)),
)


#: Aliases that must keep their case: a lowercase "helm" is the steering wheel,
#: not the chart tool.
_CASE_SENSITIVE_ALIASES = frozenset({"Helm"})

#: Aliases that start with a symbol but must still start a word, so ``.NET``
#: never matches inside "acme.net" or "ASP.NET".
_LEADING_BOUNDARY_ALIASES = frozenset({".NET"})


def _compile_alias(alias: str) -> tuple[re.Pattern[str], bool]:
    """Whole-word matcher plus whether it must be applied case-sensitively."""
    case_sensitive = alias in _CASE_SENSITIVE_ALIASES
    text = alias if case_sensitive else alias.lower()
    left = alias in _LEADING_BOUNDARY_ALIASES or text[:1].isalnum()
    right = alias in _LEADING_BOUNDARY_ALIASES or text[-1:].isalnum()
    prefix = r"(?<![0-9A-Za-z])" if left else ""
    suffix = r"(?![0-9A-Za-z])" if right else ""
    return re.compile(prefix + re.escape(text) + suffix), case_sensitive


#: One compiled matcher per alias, built once at import time.
_TECH_PATTERNS: tuple[
    tuple[str, tuple[tuple[re.Pattern[str], bool], ...]], ...
] = tuple(
    (name, tuple(_compile_alias(alias) for alias in aliases))
    for name, aliases in TECHNOLOGIES
)


def _matches(
    patterns: tuple[tuple[re.Pattern[str], bool], ...], raw: str, lowered: str
) -> bool:
    """Match against the original or the lowercased text, per alias."""
    return any(
        pattern.search(raw if case_sensitive else lowered)
        for pattern, case_sensitive in patterns
    )


def is_senior_offer(title: str | None, seniority: str | None) -> bool:
    """Whether an offer is senior by its title or by the stored seniority."""
    if seniority_filter_reason(title) is not None:
        return True
    return (seniority or "").strip().lower() in SENIOR_LEVELS


def _company_key(company: str | None) -> str:
    """Lowercased, whitespace-collapsed company name used to group offers."""
    return " ".join(str(company or "").split()).lower()


def _display_date(value: Any) -> str | None:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else (text or None)


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return int(ordered[middle])
    return int(round((ordered[middle - 1] + ordered[middle]) / 2))


def _company_rows(conn: sqlite3.Connection, *, limit: int = COMPANY_LIMIT) -> list[dict[str, Any]]:
    """Companies with two or more non-senior offers, best first."""
    rows = conn.execute(
        """
        SELECT j.company AS company,
               j.title AS title,
               j.seniority AS seniority,
               j.is_active AS is_active,
               j.last_seen_at AS last_seen_at,
               e.overall_score AS score
        FROM jobs j
        LEFT JOIN latest_evaluations e ON e.job_id = j.id
        """
    ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        if is_senior_offer(row["title"], row["seniority"]):
            continue
        key = _company_key(row["company"])
        if not key:
            continue
        group = groups.setdefault(
            key,
            {"names": {}, "offers": 0, "active": 0, "last_seen": None, "best": None},
        )
        group["offers"] += 1
        if int(row["is_active"] or 0):
            group["active"] += 1
        seen = str(row["last_seen_at"] or "")
        if seen and (group["last_seen"] is None or seen > group["last_seen"]):
            group["last_seen"] = seen
        score = row["score"]
        if score is not None:
            value = float(score)
            if group["best"] is None or value > group["best"]:
                group["best"] = value
        display = " ".join(str(row["company"] or "").split())
        group["names"][display] = group["names"].get(display, 0) + 1

    items: list[dict[str, Any]] = []
    for group in groups.values():
        if group["offers"] < 2:
            continue
        # The most frequent spelling wins; a tie goes to the alphabetically first,
        # so the label is stable across builds.
        display = max(group["names"].items(), key=lambda item: (item[1], item[0]))[0]
        best = group["best"]
        items.append(
            {
                "company": display,
                "offers": group["offers"],
                "active": group["active"],
                "last_seen": _display_date(group["last_seen"]),
                "score": round(best, 2) if best is not None else None,
            }
        )

    # Stable sorts, least significant key first.
    items.sort(key=lambda item: item["company"].lower())
    items.sort(key=lambda item: item["last_seen"] or "", reverse=True)
    items.sort(key=lambda item: item["offers"], reverse=True)
    return items[:limit]


def _profile_technologies(profile: "Profile | None") -> set[str] | None:
    """Technology names mentioned by the profile's skills, or ``None``."""
    if profile is None:
        return None
    skills: list[str] = []
    for attribute in ("must_have_skills", "nice_to_have_skills"):
        skills.extend(str(skill) for skill in (getattr(profile, attribute, None) or []))
    text = "\n".join(skills)
    lowered = text.lower()
    return {
        name
        for name, patterns in _TECH_PATTERNS
        if _matches(patterns, text, lowered)
    }


def _technology_rows(
    conn: sqlite3.Connection,
) -> tuple[int, list[dict[str, Any]]]:
    """Counts per technology over the offers whose latest score is 60+.

    The returned total is the number of offers actually analyzed (60+ with some
    text), so the percentage and the "N offers" line share one denominator.
    """
    rows = conn.execute(
        """
        SELECT j.title AS title, j.description AS description
        FROM jobs j
        JOIN latest_evaluations e ON e.job_id = j.id
        WHERE e.overall_score IS NOT NULL AND e.overall_score >= ?
        """,
        (MARKET_MIN_SCORE,),
    ).fetchall()

    counts = {name: 0 for name, _ in _TECH_PATTERNS}
    analyzed = 0
    for row in rows:
        raw = " ".join(
            str(part) for part in (row["title"], row["description"]) if part
        )
        if not raw.strip():
            # An offer with no title or description cannot mention a
            # technology, so it never enters the denominator either.
            continue
        analyzed += 1
        lowered = raw.lower()
        for name, patterns in _TECH_PATTERNS:
            # One offer counts once per technology, however many aliases it hits.
            if _matches(patterns, raw, lowered):
                counts[name] += 1

    total = analyzed
    items = [
        {
            "name": name,
            "offers": count,
            "pct": round(100 * count / total) if total else 0,
        }
        for name, count in counts.items()
        if count
    ]
    items.sort(key=lambda item: (-item["offers"], item["name"].lower()))
    return total, items


def _salary_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Offer count, estimated median and stated-salary count by tier and level.

    Only offers with a successful evaluation can land in this table; an offer
    with no evaluation is counted separately as still unscored, so
    ``sin clasificar`` means "evaluated, but the evaluation carried no tier".
    """
    rows = conn.execute(
        """
        SELECT j.title AS title,
               j.seniority AS seniority,
               j.salary AS salary,
               e.employer_tier AS employer_tier,
               e.salary_estimate_mid AS salary_estimate_mid
        FROM jobs j
        JOIN latest_evaluations e ON e.job_id = j.id
        """
    ).fetchall()

    groups: dict[tuple[str | None, str], dict[str, Any]] = {}
    for row in rows:
        level = "senior" if is_senior_offer(row["title"], row["seniority"]) else "junior"
        raw_tier = row["employer_tier"]
        tier = " ".join(str(raw_tier).split()) if raw_tier else None
        group = groups.setdefault(
            (tier, level), {"offers": 0, "mids": [], "stated": 0}
        )
        group["offers"] += 1
        mid = row["salary_estimate_mid"]
        if mid is not None:
            group["mids"].append(int(mid))
        if " ".join(str(row["salary"] or "").split()):
            group["stated"] += 1

    items: list[dict[str, Any]] = []
    for (tier, level), group in groups.items():
        offers = group["offers"]
        # The gate is the number of estimates, not offers: three offers with two
        # estimates still cannot support a median, so the report shows "—".
        median = (
            _median(group["mids"])
            if len(group["mids"]) >= MIN_MEDIAN_OFFERS
            else None
        )
        items.append(
            {
                "tier": tier if tier is not None else UNCLASSIFIED_TIER,
                "level": level,
                "offers": offers,
                "median": median,
                "stated": group["stated"],
            }
        )

    # Unclassified tiers go last; within a tier, junior before senior.
    items.sort(
        key=lambda item: (
            item["tier"] == UNCLASSIFIED_TIER,
            item["tier"].lower(),
            item["level"] != "junior",
        )
    )
    return items


def _unscored_offers(conn: sqlite3.Connection) -> int:
    """Offers with no successful evaluation, excluded from the salary table."""
    row = conn.execute(
        """
        SELECT COUNT(*) FROM jobs j
        WHERE NOT EXISTS (
            SELECT 1 FROM latest_evaluations e WHERE e.job_id = j.id
        )
        """
    ).fetchone()
    return int(row[0]) if row is not None else 0


def build_market(
    conn: sqlite3.Connection, profile: "Profile | None" = None
) -> dict[str, Any]:
    """Build the market block of the dashboard payload.

    *profile* is the same object the scorer loads from the configured profile
    file; ``None`` means "no profile available", and the technology table then
    reports ``in_profile`` as ``None`` instead of guessing.
    """
    in_profile = _profile_technologies(profile)
    technology_total, technology_rows = _technology_rows(conn)
    for item in technology_rows:
        item["in_profile"] = (
            None if in_profile is None else item["name"] in in_profile
        )
    return {
        "min_score": int(MARKET_MIN_SCORE),
        "companies": _company_rows(conn),
        "technologies": {
            "scored_offers": technology_total,
            "rows": technology_rows,
        },
        "salaries": _salary_rows(conn),
        "unscored_offers": _unscored_offers(conn),
        "profile_available": in_profile is not None,
    }


__all__ = [
    "COMPANY_LIMIT",
    "MARKET_MIN_SCORE",
    "MIN_MEDIAN_OFFERS",
    "TECHNOLOGIES",
    "UNCLASSIFIED_TIER",
    "build_market",
    "is_senior_offer",
]
