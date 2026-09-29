"""Deterministic heuristic scorer.

This is the offline default. It never calls a network, never uses randomness,
and always returns the same output for the same (job, profile) pair, which makes
it usable as a test oracle and as a fallback when no LLM endpoint is configured.

Score shape (all persisted by :mod:`jobscout.db`): overall score, per-dimension
scores with evidence, hard blockers, strengths, gaps, unknowns, confidence, and
a recommendation.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from ..models import (
    ContentStatus,
    DimensionScore,
    Evaluation,
    Recommendation,
    ScorerName,
)
from ..normalize import normalize_company, normalize_text, slugify
from ..timeutil import age_days, utcnow
from .currency import salary_currencies
from .fingerprint import effective_input_fingerprint
from .profile import Profile

SCORER_NAME = ScorerName.HEURISTIC
APPLY_THRESHOLD = 75.0
CONSIDER_THRESHOLD = 55.0
MAX_UNKNOWNS_FOR_CONFIDENT_ADVICE = 3

#: Producer identity for a heuristic evaluation. Kept stable so persisted rows
#: and the fallback marker (``heuristic-v1+fallback``) stay comparable.
HEURISTIC_MODEL = "heuristic-v1"
#: Version of the deterministic scoring policy. Bump it whenever a scoring rule
#: changes (thresholds, dimensions, semantics) so the effective-input fingerprint
#: invalidates evaluations produced by the previous policy.
HEURISTIC_POLICY_VERSION = "heuristic-policy-v1"
#: Recency score for a posting known to be older than the profile limit (or the
#: 60-day floor when the limit is disabled).
STALE_RECENCY_SCORE = 25.0
#: Recency score for an unknown posting date. Neutral on purpose: a missing date
#: proves neither freshness nor staleness, so it is never scored as stale.
UNKNOWN_DATE_RECENCY_SCORE = 50.0

#: Salary period cues. Only explicit data is honored: an unlabeled number is
#: never assumed to be a comparable amount. The currency policy itself (supported
#: vs unsupported, ``$``/``R$`` handling) lives in :mod:`jobscout.scoring.currency`
#: so the heuristic, the HTTP LLM, and OpenCode share one rule.
_MONTH_PERIOD_RE = re.compile(
    r"(?i)(?:per\s+month|/\s*month|monthly|a\s+month|por\s+mes|/mes|mensual)"
)
_YEAR_PERIOD_RE = re.compile(
    r"(?i)(?:per\s+(?:year|annum)|/\s*(?:year|yr)|annual|annually|yearly|"
    r"por\s+a[nñ]o|anual)"
)
_AMOUNT_RE = re.compile(r"(\d[\d,\.]*)\s*([kKmM])?")

_ONSITE_PATTERNS = (
    r"\bon[-\s]?site\b",
    r"\bin[-\s]?office\b",
    r"\bpresencial\b",
    r"\b100%\s*office\b",
    r"\b5\s*days?\s*(a|per)\s*week\s*in\s*office\b",
)
_REMOTE_PATTERNS = (r"\bremote\b", r"\bhybrid\b", r"\bremoto\b", r"\bteletrabajo\b", r"\bhome office\b")
_OTHER_LANGUAGES = (
    "english",
    "spanish",
    "german",
    "french",
    "portuguese",
    "italian",
    "dutch",
    "mandarin",
    "chinese",
    "japanese",
    "russian",
    "polish",
)
_LANGUAGE_REQUIREMENT_PATTERNS = (
    r"\bfluen\w*\b",
    r"\bnative\b",
    r"\brequired\b",
    r"\bmandatory\b",
    r"\bbilingual\b",
    r"\b(c1|c2|b2)\b",
    r"\badvanced\b",
)
_SENIORITY_ORDER = ("internship", "junior", "mid", "senior", "lead", "manager", "director")


class ScorableJob(Protocol):  # pragma: no cover - structural typing helper
    title: str
    company: str
    location: str | None
    description: str | None
    seniority: str | None
    salary: str | None
    posted_at: str | None
    content_status: ContentStatus
    content_hash: str
    fingerprint: str
    language: str | None


def _skill_present(skill: str, haystack_slug: str, haystack_raw: str) -> bool:
    needle = slugify(skill)
    if len(needle) < 2:
        raw = normalize_text(skill).lower()
        return bool(raw) and raw in haystack_raw
    pattern = r"(?<![a-z0-9])" + re.escape(needle).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
    return re.search(pattern, haystack_slug) is not None


def _score_ratio(matched: list[str], total: list[str], *, empty_score: float) -> float:
    if not total:
        return empty_score
    return round(100.0 * len(matched) / len(total), 2)


def _seniority_fit(profile: Profile, job_seniority: str | None) -> tuple[float, str, str | None]:
    """Return ``(score, rationale, unknown_or_gap)``."""
    job_level = (job_seniority or "unknown").lower()
    profile_level = profile.seniority
    if job_level not in _SENIORITY_ORDER or profile_level not in _SENIORITY_ORDER:
        return 50.0, "Seniority could not be compared (unknown level on one side).", "job seniority is unknown"
    distance = abs(_SENIORITY_ORDER.index(job_level) - _SENIORITY_ORDER.index(profile_level))
    if distance == 0:
        return 100.0, f"Seniority matches the profile level ({profile_level}).", None
    if distance == 1:
        return 65.0, f"Seniority is one step away from the profile level ({profile_level}).", None
    return (
        10.0,
        f"Seniority '{job_level}' is {distance} steps from the profile level '{profile_level}'.",
        None,
    )


def _location_fit(
    profile: Profile, job_location: str | None, haystack_raw: str
) -> tuple[float, str, list[str], list[str]]:
    """Return ``(score, rationale, evidence_or_gaps, unknowns)``."""
    unknowns: list[str] = []
    gaps: list[str] = []
    mentions_remote = any(re.search(pattern, haystack_raw) for pattern in _REMOTE_PATTERNS)

    if profile.remote_preference == "remote_only":
        if mentions_remote:
            return 100.0, "Posting mentions remote work and the profile requires remote.", ["remote"], []
        return 45.0, "Profile requires remote work; the posting does not mention remote.", [], [
            "posting does not state whether remote work is allowed"
        ]

    if not job_location:
        unknowns.append("job location is missing")
        return 50.0, "No location data; assumed workable.", [], unknowns

    normalized_job_location = slugify(job_location)
    for allowed in profile.locations:
        normalized_allowed = slugify(allowed)
        if not normalized_allowed:
            continue
        if normalized_allowed in normalized_job_location or normalized_job_location in normalized_allowed:
            return 100.0, f"Location matches profile preference '{allowed}'.", [allowed], []
    if mentions_remote and profile.remote_preference in {"remote_preferred", "any"}:
        return 90.0, "Posting offers remote work, which the profile accepts.", ["remote"], []
    gaps.append(f"location outside the profile list: {job_location}")
    return 25.0, f"Location '{job_location}' is outside the profile's preferred locations.", [], []


def _language_fit(
    profile: Profile, haystack_slug: str, haystack_raw: str
) -> tuple[float | None, str, list[str], list[str], list[str]]:
    if not profile.languages:
        return None, "", [], [], []
    profile_languages = {slugify(name): name for name in profile.language_names if name}
    strengths: list[str] = []
    gaps: list[str] = []
    unknowns: list[str] = []

    for slug, name in profile_languages.items():
        if slug and re.search(r"(?<![a-z])" + re.escape(slug) + r"(?![a-z])", haystack_slug):
            strengths.append(f"posting mentions {name}")
            return 100.0, f"Posting references {name}, which the profile lists.", strengths, [], []
    for language in _OTHER_LANGUAGES:
        if re.search(r"(?<![a-z])" + language + r"(?![a-z])", haystack_slug):
            if any(re.search(pattern, haystack_raw) for pattern in _LANGUAGE_REQUIREMENT_PATTERNS):
                gaps.append(f"posting may require {language}, which is not in the profile")
                return 25.0, f"Posting appears to require {language}.", [], gaps, []
    unknowns.append("no language requirement stated")
    return 50.0, "No language requirement stated in the posting.", [], [], unknowns


def _recency_fit(
    posted_at: str | None, *, now: Any, max_age_days: int | None
) -> tuple[float, str, list[str]]:
    """Score freshness, honoring the profile's ``max_posting_age_days``.

    Conservative semantics for the two edge cases:

    * unknown date -> neutral (never scored as stale; a missing date proves
      nothing);
    * a known date older than the limit -> the stale penalty, regardless of the
      graduated tiers, so ``max_posting_age_days`` is actually effective;
    * ``max_age_days is None`` disables the limit and keeps only the graduated
      freshness tiers.
    """
    days = age_days(posted_at, now=now)
    if days is None:
        return (
            UNKNOWN_DATE_RECENCY_SCORE,
            "Posting date is unknown.",
            ["posting date is unknown"],
        )
    if max_age_days is not None and days > max_age_days:
        return (
            STALE_RECENCY_SCORE,
            f"Posted {days:.1f} days ago, older than the profile's {max_age_days}-day limit.",
            ["posting is stale (older than the profile's max_posting_age_days)"],
        )
    if days <= 7:
        return 100.0, f"Posted {days:.1f} day(s) ago.", []
    if days <= 14:
        return 85.0, f"Posted {days:.1f} days ago.", []
    if days <= 30:
        return 70.0, f"Posted {days:.1f} days ago.", []
    if days <= 60:
        return 50.0, f"Posted {days:.1f} days ago.", []
    return (
        STALE_RECENCY_SCORE,
        f"Posted {days:.1f} days ago; likely stale.",
        ["posting may be stale (older than 60 days)"],
    )


def _salary_amounts(salary_text: str) -> list[float]:
    """Extract the numeric amounts a posting states, with explicit K/M units.

    Only the digits (and an optional explicit ``K``/``M`` magnitude suffix) are
    read; nothing is inferred from a missing unit.
    """
    values: list[float] = []
    for match in _AMOUNT_RE.finditer(salary_text):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:  # pragma: no cover - the regex only matches digits
            continue
        magnitude = (match.group(2) or "").lower()
        if magnitude == "k":
            value *= 1_000
        elif magnitude == "m":
            value *= 1_000_000
        if value > 0:
            values.append(value)
    return values


def _salary_currencies(salary_text: str) -> tuple[set[str], set[str]]:
    """Return the explicitly signalled ``(supported, unsupported)`` currencies.

    Thin alias for :func:`jobscout.scoring.currency.salary_currencies`, the single
    policy shared with the HTTP LLM and OpenCode scorers.
    """
    return salary_currencies(salary_text)


def _salary_period(salary_text: str) -> str | None:
    """Return ``"month"``/``"year"`` only when the period is explicitly stated."""
    if _MONTH_PERIOD_RE.search(salary_text):
        return "month"
    if _YEAR_PERIOD_RE.search(salary_text):
        return "year"
    return None


def _compensation_fit(
    profile: Profile, salary_text: str | None
) -> tuple[float | None, str, list[str], list[str], list[str]]:
    """Compare an advertised salary to the profile floor, same currency only.

    Returns ``(score, rationale, gaps, unknowns, blockers)``. Never compares
    across currencies: a posting in a different supported currency, in an
    ambiguous/unknown currency, or without an explicit period is treated as an
    unknown (neutral) compensation signal with an explicit reason, not as a
    comparable amount. A posting whose only explicit currency is outside the
    supported set (USD/EUR) is a hard blocker: it is a known, non-comparable
    amount. Only an explicitly annual amount is normalized to monthly (the
    profile floor's unit) using the known 12-month year.
    """
    if profile.min_salary is None and not salary_text:
        return None, "", [], [], []
    if not salary_text:
        return 50.0, "No salary information in the posting.", [], ["salary is not disclosed"], []

    # Detect the currency before reading any amount, so an explicit unsupported
    # currency hard-rejects even when the number is unparseable. This is the same
    # rule the HTTP LLM and OpenCode scorers enforce before their model calls.
    supported, unsupported = _salary_currencies(salary_text)
    if unsupported and not supported:
        codes = ", ".join(sorted(unsupported))
        return (
            None,
            f"Salary is quoted in {codes}, which is not an accepted currency.",
            [],
            [
                f"salary is quoted in {codes}, which is not comparable to the "
                f"{profile.salary_currency} profile minimum"
            ],
            [
                f"salary is quoted in an unsupported currency ({codes}); only "
                "USD and EUR are accepted"
            ],
        )
    if profile.min_salary is None:
        return None, "", [], [], []

    amounts = _salary_amounts(salary_text)
    if not amounts:
        return 50.0, "Salary text could not be interpreted.", [], [
            f"salary text not comparable to {profile.salary_currency} {profile.min_salary:g}"
        ], []

    if len(supported) > 1 or (supported and unsupported):
        return 50.0, "Salary currency is unknown or ambiguous.", [], [
            "salary currency is unknown or ambiguous; it is not treated as a "
            f"comparable {profile.salary_currency} amount"
        ], []

    currency = next(iter(supported), None)
    if currency is None:
        return 50.0, "Salary currency is unknown or ambiguous.", [], [
            "salary currency is unknown or ambiguous; it is not treated as a "
            f"comparable {profile.salary_currency} amount"
        ], []
    if currency != profile.salary_currency:
        return 50.0, f"Salary is quoted in {currency}, not {profile.salary_currency}.", [], [
            f"salary is in {currency} but the profile minimum is "
            f"{profile.salary_currency}; no FX conversion is applied"
        ], []

    period = _salary_period(salary_text)
    if period is None:
        return 50.0, "Salary period is not stated.", [], [
            "salary period is not stated; it is not compared to the monthly "
            f"{profile.salary_currency} profile minimum"
        ], []

    monthly = [value / 12.0 if period == "year" else value for value in amounts]
    best = max(monthly)
    if best >= profile.min_salary:
        return 100.0, f"Advertised monthly pay ({best:g} {currency}) meets the profile minimum.", [], [], []
    return (
        20.0,
        f"Advertised monthly pay ({best:g} {currency}) is below the profile "
        f"minimum ({profile.min_salary:g} {currency}).",
        [],
        [],
        [],
    )


def score_job(
    job: ScorableJob,
    profile: Profile,
    *,
    now: Any = None,
    job_id: int | None = None,
    profile_id: int | None = None,
) -> Evaluation:
    """Score one job with the deterministic heuristic scorer."""
    reference = now or utcnow()
    title = normalize_text(getattr(job, "title", ""))
    company = normalize_text(getattr(job, "company", ""))
    location = getattr(job, "location", None)
    description = getattr(job, "description", None) or ""
    combined_raw = " ".join(part for part in (title, company, location or "", description) if part)
    combined_slug = slugify(combined_raw)

    blockers: list[str] = []
    strengths: list[str] = []
    gaps: list[str] = []
    unknowns: list[str] = []
    dimensions: list[DimensionScore] = []

    # -- hard blockers ---------------------------------------------------
    normalized_company = normalize_company(company)
    for excluded in profile.excluded_companies:
        if excluded and normalize_company(excluded) == normalized_company:
            blockers.append(f"company is on the exclusion list: {excluded}")

    for dealbreaker in profile.dealbreakers:
        needle = slugify(dealbreaker)
        if len(needle) < 2:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(needle).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pattern, combined_slug):
            blockers.append(f"dealbreaker phrase present: {dealbreaker}")

    if profile.remote_preference == "remote_only":
        if any(re.search(pattern, combined_raw.lower()) for pattern in _ONSITE_PATTERNS) and not any(
            re.search(pattern, combined_raw.lower()) for pattern in _REMOTE_PATTERNS
        ):
            blockers.append("profile requires remote work but the posting is on-site only")

    # -- dimensions ------------------------------------------------------
    must_matched = [skill for skill in profile.must_have_skills if _skill_present(skill, combined_slug, combined_raw.lower())]
    must_missing = [skill for skill in profile.must_have_skills if skill not in must_matched]
    if profile.must_have_skills:
        must_score = _score_ratio(must_matched, profile.must_have_skills, empty_score=50.0)
        dimensions.append(
            DimensionScore(
                name="must_have_skills",
                score=must_score,
                weight=profile.weights.get("must_have_skills", 1.0),
                rationale=f"Matched {len(must_matched)}/{len(profile.must_have_skills)} must-have skills.",
                evidence=must_matched,
            )
        )
        strengths.extend(f"must-have skill present: {skill}" for skill in must_matched)
        gaps.extend(f"must-have skill not found: {skill}" for skill in must_missing)
        if profile.require_all_must_have and must_missing:
            blockers.append("missing required skills: " + ", ".join(must_missing))

    nice_matched = [
        skill for skill in profile.nice_to_have_skills if _skill_present(skill, combined_slug, combined_raw.lower())
    ]
    if profile.nice_to_have_skills:
        dimensions.append(
            DimensionScore(
                name="skills",
                score=_score_ratio(nice_matched, profile.nice_to_have_skills, empty_score=50.0),
                weight=profile.weights.get("skills", 1.0),
                rationale=f"Matched {len(nice_matched)}/{len(profile.nice_to_have_skills)} nice-to-have skills.",
                evidence=nice_matched,
            )
        )
        strengths.extend(f"nice-to-have skill present: {skill}" for skill in nice_matched)

    seniority_score, seniority_rationale, seniority_unknown = _seniority_fit(
        profile, getattr(job, "seniority", None)
    )
    dimensions.append(
        DimensionScore(
            name="seniority",
            score=seniority_score,
            weight=profile.weights.get("seniority", 1.0),
            rationale=seniority_rationale,
            evidence=[],
        )
    )
    if seniority_unknown:
        unknowns.append(seniority_unknown)
    if seniority_score < 40:
        gaps.append(seniority_rationale)

    location_score, location_rationale, location_evidence, location_unknowns = _location_fit(
        profile, location, combined_raw.lower()
    )
    dimensions.append(
        DimensionScore(
            name="location",
            score=location_score,
            weight=profile.weights.get("location", 1.0),
            rationale=location_rationale,
            evidence=location_evidence,
        )
    )
    unknowns.extend(location_unknowns)
    if location_score <= 30:
        gaps.append(location_rationale)

    language_score, language_rationale, language_strengths, language_gaps, language_unknowns = _language_fit(
        profile, combined_slug, combined_raw.lower()
    )
    if language_score is not None:
        dimensions.append(
            DimensionScore(
                name="language",
                score=language_score,
                weight=profile.weights.get("language", 1.0),
                rationale=language_rationale,
                evidence=language_strengths,
            )
        )
        strengths.extend(language_strengths)
        gaps.extend(language_gaps)
        unknowns.extend(language_unknowns)

    recency_score, recency_rationale, recency_unknowns = _recency_fit(
        getattr(job, "posted_at", None),
        now=reference,
        max_age_days=profile.max_posting_age_days,
    )
    dimensions.append(
        DimensionScore(
            name="recency",
            score=recency_score,
            weight=profile.weights.get("recency", 1.0),
            rationale=recency_rationale,
            evidence=[],
        )
    )
    unknowns.extend(recency_unknowns)

    compensation_score, compensation_rationale, compensation_gaps, compensation_unknowns, compensation_blockers = _compensation_fit(
        profile, getattr(job, "salary", None)
    )
    blockers.extend(compensation_blockers)
    if compensation_score is not None:
        dimensions.append(
            DimensionScore(
                name="compensation",
                score=compensation_score,
                weight=profile.weights.get("compensation", 0.05),
                rationale=compensation_rationale,
                evidence=[],
            )
        )
        gaps.extend(compensation_gaps)
        unknowns.extend(compensation_unknowns)
        if compensation_score <= 20:
            gaps.append(compensation_rationale)

    # -- content completeness -------------------------------------------
    content_status = getattr(job, "content_status", ContentStatus.UNKNOWN)
    if isinstance(content_status, ContentStatus):
        content_status = str(content_status)
    if not description:
        unknowns.append("job description is not available")
    if content_status != ContentStatus.OK.value:
        unknowns.append(f"captured content is incomplete (content_status={content_status})")

    # -- overall ---------------------------------------------------------
    weight_total = sum(dimension.weight for dimension in dimensions if dimension.weight > 0)
    if weight_total <= 0:
        overall = 0.0
    else:
        overall = round(
            sum(dimension.score * dimension.weight for dimension in dimensions if dimension.weight > 0)
            / weight_total,
            2,
        )

    # -- confidence ------------------------------------------------------
    confidence = 0.85
    if not description:
        confidence -= 0.25
    if not location:
        confidence -= 0.10
    if not getattr(job, "posted_at", None):
        confidence -= 0.10
    if seniority_unknown:
        confidence -= 0.05
    confidence -= 0.05 * max(0, len(set(unknowns)) - 2)
    confidence = round(min(0.95, max(0.05, confidence)), 2)

    # -- recommendation ---------------------------------------------------
    if blockers:
        recommendation = Recommendation.SKIP
    elif overall >= APPLY_THRESHOLD and confidence >= 0.5:
        recommendation = Recommendation.APPLY
    elif overall >= CONSIDER_THRESHOLD:
        recommendation = Recommendation.CONSIDER
    else:
        recommendation = Recommendation.SKIP
    if not blockers and len(set(unknowns)) > MAX_UNKNOWNS_FOR_CONFIDENT_ADVICE and confidence < 0.5:
        recommendation = Recommendation.NEED_MORE_INFO

    rationale = _build_rationale(
        job_title=title,
        company=company,
        overall=overall,
        blockers=blockers,
        must_matched=must_matched,
        must_total=len(profile.must_have_skills),
        confidence=confidence,
        unknowns=sorted(set(unknowns)),
    )

    return Evaluation(
        job_id=int(job_id or 0),
        scorer=SCORER_NAME,
        overall_score=None if blockers and not dimensions else overall,
        recommendation=recommendation,
        confidence=confidence,
        dimensions=dimensions,
        blockers=_dedupe_preserve_order(blockers),
        strengths=_dedupe_preserve_order(strengths),
        gaps=_dedupe_preserve_order(gaps),
        unknowns=sorted(set(unknowns)),
        rationale=rationale,
        model=HEURISTIC_MODEL,
        prompt_version=HEURISTIC_POLICY_VERSION,
        profile_id=profile_id,
        input_fingerprint=input_fingerprint(job, profile),
    )


def input_fingerprint(job: Any, profile: Profile) -> str:
    """Effective-input fingerprint for the deterministic heuristic scorer."""
    return effective_input_fingerprint(
        scorer=SCORER_NAME,
        model=HEURISTIC_MODEL,
        prompt_version=HEURISTIC_POLICY_VERSION,
        profile=profile,
        job=job,
    )


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _build_rationale(
    *,
    job_title: str,
    company: str,
    overall: float,
    blockers: list[str],
    must_matched: list[str],
    must_total: int,
    confidence: float,
    unknowns: list[str],
) -> str:
    parts = [f"Deterministic heuristic score for '{job_title or 'unknown role'}' at {company or 'unknown company'}."]
    if blockers:
        parts.append(f"Hard blockers found ({len(blockers)}): " + "; ".join(blockers) + ".")
    if must_total:
        parts.append(f"Must-have skills matched: {len(must_matched)}/{must_total}.")
    parts.append(f"Weighted overall score {overall:.2f}/100 with confidence {confidence:.2f}.")
    if unknowns:
        parts.append("Unknowns: " + "; ".join(unknowns) + ".")
    return " ".join(parts)


__all__ = [
    "APPLY_THRESHOLD",
    "CONSIDER_THRESHOLD",
    "HEURISTIC_MODEL",
    "HEURISTIC_POLICY_VERSION",
    "SCORER_NAME",
    "STALE_RECENCY_SCORE",
    "ScorableJob",
    "UNKNOWN_DATE_RECENCY_SCORE",
    "input_fingerprint",
    "score_job",
]
