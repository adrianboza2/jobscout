"""User-preference exclusions, evaluated apart from professional-fit scoring.

The profile already has *scoring* inputs (skills, seniority, location) and hard
blockers derived from them. This module answers a different, narrower question:
does the posting match one of the user's **explicit exclusions**, independent of
how well the role fits professionally?

The only exclusions implemented here are:

* rotating-shift work (direct and close semantic variants, Spanish and English);
* SOC roles that are explicitly operational (monitoring, 24/7, shifts, on-call,
  continuous surveillance), while a generic mention of "SOC" stays eligible.

Design constraints:

* **Offline and deterministic.** No network, no LLM, no randomness. The same
  posting always yields the same assessment, which is what lets the dashboard and
  the CLI recompute it from stored evidence instead of persisting a second
  versioned judgment.
* **Separate from the score.** The model score and recommendation are never
  touched here, so historical evaluations stay intact.
* **Conservative.** A clear exclusion is ``FAIL``; anything ambiguous is
  ``FLAG`` for a human to review; absence of an exclusion is ``PASS``.

An optional semantic layer (for example an LLM comparing a posting against known
phrasings) is deliberately *not* wired in: it would add one model call per offer
and fan out the cost. If it is ever added it must reuse the already-captured text
and run only when explicitly requested.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import StrEnum

#: Stable schema tag for the serialized assessment.
PREFERENCE_SCHEMA = "jobscout.preference.v1"

#: How far, in characters, an operational cue may sit from a "SOC" mention for the
#: mention to be read as an operational SOC role. Wide enough to join a "SOC
#: Analyst" title with "24/7 monitoring" at the start of the description, narrow
#: enough to avoid joining unrelated clauses of a long posting.
SOC_CONTEXT_WINDOW = 240

#: How far, in characters, a qualifier ("puntual", "occasional") may sit from an
#: on-call mention to downgrade it from ambiguous to explicitly occasional.
ONCALL_QUALIFIER_WINDOW = 48


class PreferenceOutcome(StrEnum):
    """The three possible preference verdicts for one posting."""

    PASS = "pass"
    FAIL = "fail"
    FLAG = "flag"


@dataclass(frozen=True)
class PreferenceMatch:
    """One rule that matched, with the cue text that triggered it."""

    rule: str
    outcome: PreferenceOutcome
    label: str
    reason: str
    evidence: tuple[str, ...] = ()


@dataclass
class PreferenceAssessment:
    """The complete, explainable preference verdict for one posting."""

    outcome: PreferenceOutcome = PreferenceOutcome.PASS
    reason: str = ""
    matches: list[PreferenceMatch] = field(default_factory=list)

    @property
    def evidence(self) -> list[str]:
        """Distinct cue texts behind the verdict, in match order."""
        return list(
            dict.fromkeys(
                item for match in self.matches for item in match.evidence
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PREFERENCE_SCHEMA,
            "outcome": str(self.outcome),
            "reason": self.reason,
            "evidence": self.evidence,
            "rules": [match.rule for match in self.matches],
        }


# ---------------------------------------------------------------------------
# Text folding
# ---------------------------------------------------------------------------


def _fold(value: Any) -> str:
    """Lowercase, accent-stripped text that keeps punctuation (so 24/7 survives)."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("\u2019", "'")
    return re.sub(r"\s+", " ", text).lower().strip()


def _matches(pattern: str, text: str) -> list[str]:
    return [match.group(0).strip() for match in re.finditer(pattern, text, re.IGNORECASE)]


# ---------------------------------------------------------------------------
# Rotating shifts
# ---------------------------------------------------------------------------

# A clear rotating-shift description. These are the only shift phrasings that
# fail closed; every other shift mention is at most a FLAG.
_ROTATING_PATTERNS: tuple[str, ...] = (
    r"\brotating\s+shifts?\b",
    r"\brotating\s+(?:shift\s+)?(?:schedule|roster|rota|pattern)\b",
    r"\brotational\s+shifts?\b",
    r"\bshift\s+rotation\b",
    r"\bshift\s+rota\b",
    r"\bshifts?\s+on\s+rotat\w*\b",
    r"\brotating\s+on[- ]call\b",
    r"\bturnos?\s+rotativ\w*\b",
    r"\brotativ\w*\s+de\s+turnos?\b",
    r"\brotacion(?:es)?\s+de\s+(?:turnos?|guardias)\b",
    r"\bturnos?\s+rotatorios?\b",
    r"\bhorario\s+rotativ\w*\b",
    r"\btrabajo\s+rotativ\w*\b",
)

# 24/7 paired with an explicit shift word: continuous rotating coverage.
_ROTATING_247_PATTERNS: tuple[str, ...] = (
    r"\b(?:24\s*/\s*7|24\s*x\s*7|24-7)\s+(?:shift|shifts|turnos?|rota|rotation|rosters?)\b",
    r"\b(?:shift|shifts|turnos?)\s+(?:de\s+|covering\s+|que\s+cubren\s+)?(?:24\s*/\s*7|24\s*x\s*7|24-7)\b",
)

# Ambiguous shift-based work. Worth a human look, never an automatic rejection.
_GENERIC_SHIFT_PATTERNS: tuple[str, ...] = (
    r"\bshift\s+work\b",
    r"\bshift\s+pattern\b",
    r"\bshift[- ]based\b",
    r"\bwork\s+in\s+shifts\b",
    r"\b12[- ]hour\s+shifts?\b",
    r"\bnight\s+shifts?\b",
    r"\bweekend\s+shifts?\b",
    r"\btrabajo\s+por\s+turnos\b",
    r"\btrabajo\s+a\s+turnos\b",
)

_ROTATION_WORD_RE = re.compile(r"rotativ|rotator|rotacion")

#: A qualifier that proves a shift mention is not rotating (fixed shifts).
_FIXED_SHIFT_PATTERNS: tuple[str, ...] = (
    r"\bturno\s+(?:fijo|de\s+manana|de\s+tarde|de\s+noche)\b",
)

#: 24/7 mention not tied to a shift word: continuous coverage, flagged only.
_COVERAGE_247_PATTERNS: tuple[str, ...] = (
    r"\b(?:24\s*/\s*7|24\s*x\s*7|24-7)\b",
)

# ---------------------------------------------------------------------------
# On-call / guardias
# ---------------------------------------------------------------------------

_ONCALL_RE = r"\b(?:guardias?|on[- ]call)\b"

#: Qualifiers that make an on-call mention explicitly occasional, which the user
#: accepts, so it must not become a false FAIL (or even a FLAG).
_OCCASIONAL_PATTERNS: tuple[str, ...] = (
    r"\bpuntual(?:es)?\b",
    r"\bocasional(?:es)?\b",
    r"\besporadic\w*\b",
    r"\boccasional\w*\b",
    r"\bsporadic\w*\b",
    r"\bde\s+vez\s+en\s+cuando\b",
    r"\bfew\s+times\s+a\s+(?:year|month|quarter)\b",
)

# ---------------------------------------------------------------------------
# SOC
# ---------------------------------------------------------------------------

_SOC_RE = re.compile(
    r"(?<![a-z0-9])soc(?![a-z0-9])"
    r"|security\s+operations\s+cent(?:er|re)"
    r"|centro\s+de\s+operaciones\s+de\s+seguridad"
)

#: "SOC 2", "SOC II", "SOC 1/3" and similar are compliance frameworks, not a
#: security-operations role. They stay knowledge/generic and never fail.
_SOC_COMPLIANCE_RE = re.compile(r"^\s*(?:2|1|3|ii|iii|type)\b")

#: Cues that turn a SOC mention into an operational one, which the user excludes.
_SOC_OPERATIONAL_PATTERNS: tuple[str, ...] = (
    r"\bmonitor\w*\b",
    r"\bmonitoreo\b",
    r"\boperaci\w*\b",
    r"\boperativ\w*\b",
    r"\boperations?\b",
    r"\b(?:24\s*/\s*7|24\s*x\s*7|24-7)\b",
    r"\bturnos?\b",
    r"\bshifts?\b",
    r"\bguardias?\b",
    r"\bon[- ]call\b",
    r"\bvigilancia\b",
    r"\bsurveillance\b",
    r"\bincident\w*\b",
    r"\bincidente\w*\b",
    r"\balertas?\b",
    r"\balerts?\b",
    r"\btriaje\b",
    r"\btriage\b",
    r"\bthreat\s+(?:detection|monitoring|hunting)\b",
    r"\bdeteccion\s+de\s+amenazas\b",
)

#: A SOC role name without operational evidence is ambiguous, so it is flagged.
_SOC_ROLE_PATTERNS: tuple[str, ...] = (
    r"\b(?:analyst|analista|engineer|ingenier\w*|specialist|especialista)\b",
    r"\b(?:technician|tecnico|operator|operador|monitorista)\b",
    r"\b(?:manager|jefe|responsable|lead|leader|lider)\b",
    r"\b(?:tier|nivel|n[123])\b",
    r"\b(?:team|equipo)\b",
)

# ---------------------------------------------------------------------------
# Explicit non-exclusions (recorded as PASS evidence)
# ---------------------------------------------------------------------------

_FLEXIBLE_PATTERNS: tuple[str, ...] = (
    r"\bhorario\s+flexible\b",
    r"\bflexibilidad\s+horaria\b",
    r"\bflexible\s+(?:schedule|working\s+hours|hours)\b",
    r"\bflexible\s+time\b",
)


def _evidence(items: Iterable[str], *, limit: int = 6) -> tuple[str, ...]:
    deduped = dict.fromkeys(item.strip() for item in items if item.strip())
    return tuple(list(deduped)[:limit])


def _window(text: str, start: int, end: int, radius: int) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _clauses(text: str) -> list[tuple[int, int, str]]:
    """Split on sentence punctuation so a role name is read in its own clause.

    Operational cues use a character window instead (a "SOC Analyst" title and a
    "24/7 monitoring" description line are different clauses but still one role).
    Role names, however, must be adjacent to the mention, otherwise a job titled
    "Security Engineer" that merely mentions SOC as knowledge would be misread.
    """
    clauses: list[tuple[int, int, str]] = []
    start = 0
    for index, char in enumerate(text):
        if char in ".;\n":
            clause = text[start:index].strip()
            if clause:
                clauses.append((start, index, clause))
            start = index + 1
    tail = text[start:]
    if tail.strip():
        clauses.append((start, len(text), tail.strip()))
    return clauses


def _rotating_matches(text: str) -> list[PreferenceMatch]:
    found: list[str] = []
    for pattern in _ROTATING_PATTERNS:
        found.extend(_matches(pattern, text))
    return [
        PreferenceMatch(
            rule="shift.rotating",
            outcome=PreferenceOutcome.FAIL,
            label="rotating shifts",
            reason=(
                "Posting describes rotating shift work, which is an explicit "
                "preference exclusion."
            ),
            evidence=_evidence(found),
        )
    ] if found else []


def _rotating_247_matches(text: str) -> list[PreferenceMatch]:
    found: list[str] = []
    for pattern in _ROTATING_247_PATTERNS:
        found.extend(_matches(pattern, text))
    return [
        PreferenceMatch(
            rule="shift.247",
            outcome=PreferenceOutcome.FAIL,
            label="24/7 shift coverage",
            reason=(
                "Posting pairs 24/7 coverage with shift work, which is an "
                "explicit preference exclusion."
            ),
            evidence=_evidence(found),
        )
    ] if found else []


def _generic_shift_matches(text: str) -> list[PreferenceMatch]:
    matches: list[PreferenceMatch] = []
    generic: list[str] = []
    for pattern in _GENERIC_SHIFT_PATTERNS:
        generic.extend(_matches(pattern, text))

    # Spanish "turnos" is common and ambiguous: accept it as a FLAG, but skip the
    # mentions that already proved rotating (the FAIL rule owns those) and the
    # explicitly fixed ones ("turno fijo", "turno de mañana").
    for match in re.finditer(r"\bturnos?\b", text, re.IGNORECASE):
        context = _window(text, match.start(), match.end(), 24)
        if _ROTATION_WORD_RE.search(context):
            continue
        if any(re.search(pattern, context, re.IGNORECASE) for pattern in _FIXED_SHIFT_PATTERNS):
            continue
        generic.append(match.group(0).strip())

    if generic:
        matches.append(
            PreferenceMatch(
                rule="shift.generic",
                outcome=PreferenceOutcome.FLAG,
                label="shift-based work",
                reason=(
                    "Posting mentions shift-based work but does not state that "
                    "the shifts rotate, so it is flagged for review."
                ),
                evidence=_evidence(generic),
            )
        )
    return matches


def _coverage_247_matches(text: str) -> list[PreferenceMatch]:
    found: list[str] = []
    for pattern in _COVERAGE_247_PATTERNS:
        found.extend(_matches(pattern, text))
    if not found:
        return []
    return [
        PreferenceMatch(
            rule="coverage.247",
            outcome=PreferenceOutcome.FLAG,
            label="24/7 coverage",
            reason=(
                "Posting mentions 24/7 coverage; continuous coverage alone is not "
                "enough to call it rotating, so it is flagged for review."
            ),
            evidence=_evidence(found),
        )
    ]


def _oncall_matches(text: str) -> list[PreferenceMatch]:
    occasional: list[str] = []
    ambiguous: list[str] = []
    for match in re.finditer(_ONCALL_RE, text, re.IGNORECASE):
        context = _window(text, match.start(), match.end(), ONCALL_QUALIFIER_WINDOW)
        if any(re.search(pattern, context, re.IGNORECASE) for pattern in _OCCASIONAL_PATTERNS):
            occasional.append(match.group(0).strip())
        else:
            ambiguous.append(match.group(0).strip())

    matches: list[PreferenceMatch] = []
    if occasional:
        matches.append(
            PreferenceMatch(
                rule="oncall.occasional",
                outcome=PreferenceOutcome.PASS,
                label="occasional on-call",
                reason=(
                    "Posting states on-call duties are occasional, which the "
                    "preferences accept."
                ),
                evidence=_evidence(occasional),
            )
        )
    if ambiguous:
        matches.append(
            PreferenceMatch(
                rule="shift.oncall",
                outcome=PreferenceOutcome.FLAG,
                label="on-call duties",
                reason=(
                    "Posting mentions on-call or guardia duties without saying "
                    "they are occasional, so it is flagged for review."
                ),
                evidence=_evidence(ambiguous),
            )
        )
    return matches


def _soc_matches(text: str) -> list[PreferenceMatch]:
    """SOC rules: operational FAIL, role-name FLAG, generic/knowledge PASS.

    Proximity is what separates "SOC" as a knowledge requirement from an
    operational role: an operational cue must sit within
    :data:`SOC_CONTEXT_WINDOW` characters of the mention.
    """
    clauses = _clauses(text)
    operational: list[str] = []
    roles: list[str] = []
    for match in _SOC_RE.finditer(text):
        after = text[match.end() : match.end() + 16]
        if _SOC_COMPLIANCE_RE.match(after):
            continue  # "SOC 2", "SOC II": compliance framework, generic
        context = _window(text, match.start(), match.end(), SOC_CONTEXT_WINDOW)
        cues = [
            cue
            for pattern in _SOC_OPERATIONAL_PATTERNS
            for cue in _matches(pattern, context)
        ]
        if cues:
            operational.extend(cues)
            continue
        role_context = next(
            (clause for start, end, clause in clauses if start <= match.start() < end),
            "",
        )
        role = [
            cue
            for pattern in _SOC_ROLE_PATTERNS
            for cue in _matches(pattern, role_context)
        ]
        if role:
            roles.extend(role)

    matches: list[PreferenceMatch] = []
    if operational:
        matches.append(
            PreferenceMatch(
                rule="soc.operational",
                outcome=PreferenceOutcome.FAIL,
                label="operational SOC",
                reason=(
                    "Posting frames SOC as operational monitoring, operations, "
                    "shift, on-call, or continuous surveillance work, which is an "
                    "explicit preference exclusion."
                ),
                evidence=_evidence(operational),
            )
        )
    if roles:
        matches.append(
            PreferenceMatch(
                rule="soc.role",
                outcome=PreferenceOutcome.FLAG,
                label="SOC role",
                reason=(
                    "Posting names a SOC role but does not state monitoring, "
                    "operations, or shift work, so it is flagged for review."
                ),
                evidence=_evidence(roles),
            )
        )
    return matches


def _pass_matches(text: str) -> list[PreferenceMatch]:
    matches: list[PreferenceMatch] = []
    flexible: list[str] = []
    for pattern in _FLEXIBLE_PATTERNS:
        flexible.extend(_matches(pattern, text))
    if flexible:
        matches.append(
            PreferenceMatch(
                rule="schedule.flexible",
                outcome=PreferenceOutcome.PASS,
                label="flexible schedule",
                reason=(
                    "Posting offers a flexible schedule, which the preferences accept."
                ),
                evidence=_evidence(flexible),
            )
        )
    return matches


def assess_text(*parts: Any) -> PreferenceAssessment:
    """Assess pre-normalized text parts with the offline rule set.

    Parts are joined in the order given so title/description proximity is under
    the caller's control. The result is deterministic.
    """
    segments = [_fold(part) for part in parts if part]
    text = " . ".join(segment for segment in segments if segment)

    matches: list[PreferenceMatch] = []
    matches.extend(_rotating_matches(text))
    matches.extend(_rotating_247_matches(text))
    matches.extend(_soc_matches(text))
    matches.extend(_generic_shift_matches(text))
    matches.extend(_oncall_matches(text))
    matches.extend(_coverage_247_matches(text))
    matches.extend(_pass_matches(text))

    decisive = [match for match in matches if match.outcome is PreferenceOutcome.FAIL]
    if not decisive:
        decisive = [match for match in matches if match.outcome is PreferenceOutcome.FLAG]

    if decisive:
        outcome = decisive[0].outcome
        reasons: list[str] = []
        for match in decisive:
            if match.reason not in reasons:
                reasons.append(match.reason)
        reason = " ".join(reasons)
    else:
        outcome = PreferenceOutcome.PASS
        accepted = [match for match in matches if match.outcome is PreferenceOutcome.PASS]
        if accepted:
            reason = " ".join(dict.fromkeys(match.reason for match in accepted))
        else:
            reason = "No exclusion cue was found in the posting."

    # Keep the evidence list focused on the verdict: decisive matches first, then
    # the accepted PASS evidence that explains a clean result.
    ordered = list(decisive)
    if not decisive:
        ordered.extend(match for match in matches if match.outcome is PreferenceOutcome.PASS)

    return PreferenceAssessment(outcome=outcome, reason=reason, matches=ordered)


def assess_preference(job: Any) -> PreferenceAssessment:
    """Assess a stored job-like object without touching the database or network.

    Only the already-captured text is read. Title and description are adjacent so
    a "SOC Analyst" title can combine with "24/7 monitoring" at the start of the
    description, which is the case the exclusion targets.
    """
    if job is None:
        return assess_text()
    return assess_text(
        getattr(job, "title", None),
        getattr(job, "description", None),
        getattr(job, "employment_type", None),
        getattr(job, "seniority", None),
        getattr(job, "location", None),
        getattr(job, "company", None),
    )


__all__ = [
    "ONCALL_QUALIFIER_WINDOW",
    "PREFERENCE_SCHEMA",
    "SOC_CONTEXT_WINDOW",
    "PreferenceAssessment",
    "PreferenceMatch",
    "PreferenceOutcome",
    "assess_preference",
    "assess_text",
]
