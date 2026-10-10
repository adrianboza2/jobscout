"""Candidate profile loading and evidence extraction.

The profile is a local, versioned artifact (YAML or JSON). Loading it validates
the shape early, gives every field a documented default, and derives a stable
fingerprint so evaluations can be traced back to the exact profile version that
produced them.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import ProfileEvidence, ProfileSnapshot
from ..timeutil import utcnow_iso

PROFILE_VERSION = "v1"

SENIORITY_LEVELS = ("internship", "junior", "mid", "senior", "lead", "manager", "director", "unknown")
REMOTE_PREFERENCES = ("remote_only", "remote_preferred", "onsite_ok", "any")
#: Currencies the profile's own compensation floor may use. Offers are compared
#: only within one currency: ``$`` means USD, there is no FX conversion, and a
#: floor in any other currency is a profile error rather than a silently applied
#: number.
SUPPORTED_CURRENCIES = ("USD", "EUR")
DEFAULT_WEIGHTS = {
    "skills": 0.35,
    "must_have_skills": 0.15,
    "seniority": 0.15,
    "location": 0.15,
    "language": 0.10,
    "recency": 0.10,
}

#: Keys the parser consumes. Everything else in the source file is an unknown
#: key: ignored by scoring but preserved verbatim in the snapshot and included in
#: the fingerprint (see :meth:`Profile.to_dict`).
KNOWN_FIELDS = frozenset(
    {
        "profile",
        "name",
        "summary",
        "target_roles",
        "seniority",
        "years_experience",
        "locations",
        "remote_preference",
        "languages",
        "min_salary",
        "salary_currency",
        "must_have_skills",
        "nice_to_have_skills",
        "dealbreakers",
        "excluded_companies",
        "require_all_must_have",
        "max_posting_age_days",
        "weights",
    }
)


class ProfileError(ValueError):
    """Raised when a profile file is missing or malformed."""


@dataclass
class Profile:
    """Structured candidate profile used for scoring."""

    name: str = "default"
    summary: str = ""
    target_roles: list[str] = field(default_factory=list)
    seniority: str = "unknown"
    years_experience: float | None = None
    locations: list[str] = field(default_factory=list)
    remote_preference: str = "any"
    languages: list[dict[str, str]] = field(default_factory=list)
    min_salary: float | None = None
    salary_currency: str = "USD"
    must_have_skills: list[str] = field(default_factory=list)
    nice_to_have_skills: list[str] = field(default_factory=list)
    dealbreakers: list[str] = field(default_factory=list)
    excluded_companies: list[str] = field(default_factory=list)
    require_all_must_have: bool = False
    max_posting_age_days: int | None = 30
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    raw: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None

    # -- helpers ----------------------------------------------------------
    @property
    def language_names(self) -> list[str]:
        return [str(item.get("name", "")).strip() for item in self.languages if item.get("name")]

    def to_dict(self) -> dict[str, Any]:
        """Canonical profile view: normalized known fields plus unknown keys.

        Known fields keep their canonicalized values; every key the parser did
        not consume round-trips verbatim. The same mapping feeds
        :meth:`fingerprint`, so adding or editing an unknown key changes the
        profile version even though scoring ignores it — which is what
        ``profile.example.yaml`` promises.
        """
        payload: dict[str, Any] = {
            "name": self.name,
            "summary": self.summary,
            "target_roles": list(self.target_roles),
            "seniority": self.seniority,
            "years_experience": self.years_experience,
            "locations": list(self.locations),
            "remote_preference": self.remote_preference,
            "languages": [dict(item) for item in self.languages],
            "min_salary": self.min_salary,
            "salary_currency": self.salary_currency,
            "must_have_skills": list(self.must_have_skills),
            "nice_to_have_skills": list(self.nice_to_have_skills),
            "dealbreakers": list(self.dealbreakers),
            "excluded_companies": list(self.excluded_companies),
            "require_all_must_have": self.require_all_must_have,
            "max_posting_age_days": self.max_posting_age_days,
            "weights": dict(self.weights),
        }
        for key, value in self.raw.items():
            if key not in KNOWN_FIELDS:
                payload[key] = value
        return payload

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, default=str)
        return f"{PROFILE_VERSION}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProfileError(f"profile not found: {path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        try:
            import yaml  # imported lazily so JSON profiles need no dependency
        except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
            raise ProfileError(
                "PyYAML is required for YAML profiles; install it or use a JSON profile"
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ProfileError(f"profile {path} is not valid YAML: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProfileError(f"profile {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileError(f"profile {path} must contain a mapping at the top level")
    # Allow a nested "profile:" key for tidiness.
    nested = data.get("profile")
    if isinstance(nested, dict):
        merged = {key: value for key, value in data.items() if key != "profile"}
        merged.update(nested)
        return merged
    return data


def _require_str_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key) or []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        raise ProfileError(f"profile field '{key}' must be a list of strings")
    result: list[str] = []
    for item in value:
        if isinstance(item, dict):
            candidate = item.get("name") or item.get("label") or item.get("value")
            if candidate is None:
                raise ProfileError(f"profile field '{key}' contains a mapping without a name")
            result.append(str(candidate).strip())
        elif isinstance(item, (str, int, float)):
            text = str(item).strip()
            if text:
                result.append(text)
        else:
            raise ProfileError(f"profile field '{key}' contains an unsupported value")
    return result


def _opt_float(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    # A real boolean is a YAML/JSON ``true``/``false``, never a number. Without
    # this guard ``float(True)`` would silently read ``true`` as ``1``.
    if isinstance(value, bool):
        raise ProfileError(f"profile field '{key}' must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"profile field '{key}' must be a number") from exc


def _opt_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ProfileError(f"profile field '{key}' must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"profile field '{key}' must be an integer") from exc


def _opt_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    """Strict boolean: only a real YAML/JSON boolean is accepted.

    A quoted ``"false"`` is a string, not a boolean, and must never be coerced
    to ``True`` by :func:`bool` (which treats any non-empty string as truthy).
    """
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if isinstance(value, bool):
        return value
    raise ProfileError(f"profile field '{key}' must be a boolean (true/false)")


def _parse_currency(data: dict[str, Any]) -> str:
    raw = data.get("salary_currency", "USD")
    if raw is None or raw == "":
        return "USD"
    if not isinstance(raw, str):
        raise ProfileError("profile field 'salary_currency' must be a currency code")
    code = raw.strip().upper()
    if code not in SUPPORTED_CURRENCIES:
        raise ProfileError(
            "profile field 'salary_currency' must be one of "
            f"{', '.join(SUPPORTED_CURRENCIES)} (got {raw!r})"
        )
    return code


def _parse_languages(data: dict[str, Any]) -> list[dict[str, str]]:
    value = data.get("languages") or []
    if isinstance(value, str):
        return [{"name": value.strip(), "level": "unknown"}]
    if not isinstance(value, list):
        raise ProfileError("profile field 'languages' must be a list")
    languages: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            languages.append({"name": item.strip(), "level": "unknown"})
        elif isinstance(item, dict) and item.get("name"):
            languages.append(
                {"name": str(item["name"]).strip(), "level": str(item.get("level", "unknown"))}
            )
        else:
            raise ProfileError("each entry in 'languages' needs a name")
    return languages


def _parse_weights(data: dict[str, Any]) -> dict[str, float]:
    raw = data.get("weights") or {}
    if not isinstance(raw, dict):
        raise ProfileError("profile field 'weights' must be a mapping")
    weights = dict(DEFAULT_WEIGHTS)
    for key, value in raw.items():
        if isinstance(value, bool):
            raise ProfileError(f"weight '{key}' must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"weight '{key}' must be a number") from exc
        if not math.isfinite(number):
            raise ProfileError(f"weight '{key}' must be a finite number")
        if number < 0:
            raise ProfileError(f"weight '{key}' must be >= 0")
        weights[str(key)] = number
    if sum(weights.values()) <= 0:
        raise ProfileError("profile weights must sum to a positive value")
    return weights


def profile_from_dict(data: dict[str, Any], *, source_path: str | None = None) -> Profile:
    """Validate and build a :class:`Profile` from a mapping."""
    seniority = str(data.get("seniority", "unknown") or "unknown").strip().lower()
    if seniority not in SENIORITY_LEVELS:
        raise ProfileError(
            f"profile field 'seniority' must be one of {', '.join(SENIORITY_LEVELS)}"
        )
    remote_preference = str(data.get("remote_preference", "any") or "any").strip().lower()
    if remote_preference not in REMOTE_PREFERENCES:
        raise ProfileError(
            f"profile field 'remote_preference' must be one of {', '.join(REMOTE_PREFERENCES)}"
        )

    max_posting_age_days = (
        _opt_int(data, "max_posting_age_days") if "max_posting_age_days" in data else 30
    )
    if max_posting_age_days is not None and max_posting_age_days < 0:
        raise ProfileError("profile field 'max_posting_age_days' must be >= 0 or null")

    profile = Profile(
        name=str(data.get("name", "default") or "default").strip(),
        summary=str(data.get("summary", "") or ""),
        target_roles=_require_str_list(data, "target_roles"),
        seniority=seniority,
        years_experience=_opt_float(data, "years_experience"),
        locations=_require_str_list(data, "locations"),
        remote_preference=remote_preference,
        languages=_parse_languages(data),
        min_salary=_opt_float(data, "min_salary"),
        salary_currency=_parse_currency(data),
        must_have_skills=_require_str_list(data, "must_have_skills"),
        nice_to_have_skills=_require_str_list(data, "nice_to_have_skills"),
        dealbreakers=_require_str_list(data, "dealbreakers"),
        excluded_companies=_require_str_list(data, "excluded_companies"),
        require_all_must_have=_opt_bool(data, "require_all_must_have", False),
        max_posting_age_days=max_posting_age_days,
        weights=_parse_weights(data),
        raw=dict(data),
        source_path=source_path,
    )
    if profile.require_all_must_have and not profile.must_have_skills:
        raise ProfileError("'require_all_must_have' requires at least one 'must_have_skills'")
    return profile


def load_profile(path: str | Path) -> Profile:
    """Load, validate, and return a profile from a YAML or JSON file."""
    profile_path = Path(path)
    data = _load_raw(profile_path)
    return profile_from_dict(data, source_path=str(profile_path))


def extract_evidence(profile: Profile) -> list[ProfileEvidence]:
    """Flatten the profile into structured, individually weighted evidence rows."""
    evidence: list[ProfileEvidence] = []

    def add(kind: str, label: str, value: Any, weight: float = 1.0, source_text: str | None = None) -> None:
        evidence.append(
            ProfileEvidence(
                kind=kind,
                label=label,
                value=value,
                weight=weight,
                source_text=source_text,
            )
        )

    for skill in profile.must_have_skills:
        add("skill_must", skill, skill, weight=profile.weights.get("must_have_skills", 1.0))
    for skill in profile.nice_to_have_skills:
        add("skill_nice", skill, skill, weight=profile.weights.get("skills", 1.0))
    add(
        "seniority",
        profile.seniority,
        {"level": profile.seniority, "years_experience": profile.years_experience},
        weight=profile.weights.get("seniority", 1.0),
    )
    for location in profile.locations:
        add("location", location, location, weight=profile.weights.get("location", 1.0))
    add(
        "remote_preference",
        profile.remote_preference,
        profile.remote_preference,
        weight=profile.weights.get("location", 1.0),
    )
    for language in profile.languages:
        add(
            "language",
            str(language.get("name", "")),
            language,
            weight=profile.weights.get("language", 1.0),
        )
    for role in profile.target_roles:
        add("target_role", role, role)
    if profile.min_salary is not None:
        add("salary_min", profile.salary_currency, profile.min_salary)
    for dealbreaker in profile.dealbreakers:
        add("dealbreaker", dealbreaker, dealbreaker)
    for company in profile.excluded_companies:
        add("exclusion", company, company)
    return evidence


def build_snapshot(profile: Profile) -> ProfileSnapshot:
    """Create a persistable snapshot with evidence rows attached."""
    return ProfileSnapshot(
        name=profile.name,
        fingerprint=profile.fingerprint(),
        raw=profile.to_dict(),
        evidence=extract_evidence(profile),
        source_path=profile.source_path,
        loaded_at=utcnow_iso(),
    )


def profile_summary_for_prompt(profile: Profile) -> dict[str, Any]:
    """Compact, JSON-serializable profile view handed to an LLM scorer."""
    return {
        "name": profile.name,
        "summary": profile.summary,
        "target_roles": profile.target_roles,
        "seniority": profile.seniority,
        "years_experience": profile.years_experience,
        "locations": profile.locations,
        "remote_preference": profile.remote_preference,
        "languages": profile.languages,
        "min_salary": profile.min_salary,
        "salary_currency": profile.salary_currency,
        "must_have_skills": profile.must_have_skills,
        "nice_to_have_skills": profile.nice_to_have_skills,
        "dealbreakers": profile.dealbreakers,
        "excluded_companies": profile.excluded_companies,
    }


__all__ = [
    "DEFAULT_WEIGHTS",
    "KNOWN_FIELDS",
    "PROFILE_VERSION",
    "Profile",
    "ProfileError",
    "REMOTE_PREFERENCES",
    "SENIORITY_LEVELS",
    "SUPPORTED_CURRENCIES",
    "build_snapshot",
    "extract_evidence",
    "load_profile",
    "profile_from_dict",
    "profile_summary_for_prompt",
]
