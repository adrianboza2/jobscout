"""Robust validation of LLM scoring output.

An LLM response is untrusted input: it may be wrapped in prose, fenced with
markdown, use alternative field names, or be outright malformed. This module
extracts a JSON object, normalizes it into the canonical evaluation shape, and
reports every repair it had to make. Nothing here performs I/O.

Because the model's own text (rationale, dimension text, list entries) and the
repair/error diagnostics it triggers are persisted and printed by the scorers,
:func:`parse_llm_evaluation` accepts the configured secrets and redacts them
from the normalized payload and diagnostics before returning. Non-secret text
is left untouched, so useful diagnostics survive.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import redact_secrets
from ..models import PUBLIC_RECOMMENDATIONS, DimensionScore, Recommendation

PROMPT_VERSION = "v1"

ALLOWED_RECOMMENDATIONS = {item.value for item in Recommendation}

_RECOMMENDATION_ALIASES = {
    "apply": Recommendation.APPLY.value,
    "strong_apply": Recommendation.APPLY.value,
    "strongly_apply": Recommendation.APPLY.value,
    "yes": Recommendation.APPLY.value,
    "consider": Recommendation.CONSIDER.value,
    "maybe": Recommendation.CONSIDER.value,
    "possible": Recommendation.CONSIDER.value,
    "skip": Recommendation.SKIP.value,
    "no": Recommendation.SKIP.value,
    "reject": Recommendation.SKIP.value,
    "not_a_fit": Recommendation.SKIP.value,
    "need_more_info": Recommendation.NEED_MORE_INFO.value,
    "insufficient_information": Recommendation.NEED_MORE_INFO.value,
    "unknown": Recommendation.NEED_MORE_INFO.value,
}

_SCORE_ALIASES = ("overall_score", "overallScore", "score", "total_score", "match_score")
_CONFIDENCE_ALIASES = ("confidence", "confidence_score", "certainty")
_DIMENSION_ALIASES = ("dimensions", "dimension_scores", "scores", "breakdown")
_BLOCKER_ALIASES = ("blockers", "hard_blockers", "hardBlockers", "disqualifiers")
_STRENGTH_ALIASES = ("strengths", "pros", "positives")
_GAP_ALIASES = ("gaps", "cons", "negatives", "shortfalls")
_UNKNOWN_ALIASES = ("unknowns", "uncertainties", "open_questions")
_RATIONALE_ALIASES = ("rationale", "reasoning", "summary", "explanation")

_MAX_RAW_CHARS = 200_000
_MAX_STRING_CHARS = 4_000
_MAX_LIST_ITEMS = 50
_MAX_DIMENSIONS = 25

# Models often answer "none" instead of an empty list; that is not a finding.
_NEGATIVE_PLACEHOLDERS = frozenset(
    {"none", "n/a", "na", "no", "no blockers", "none found", "nothing", "-", "not applicable", "ninguno"}
)


@dataclass
class ParseOutcome:
    """Result of parsing an LLM response into the canonical shape."""

    ok: bool
    payload: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    raw_text: str = ""
    raw_excerpt: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


def _strip_code_fences(text: str) -> tuple[str, bool]:
    stripped = text.strip()
    fence = re.match(r"^```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?```\s*$", stripped, re.DOTALL)
    if fence:
        return fence.group(1).strip(), True
    if stripped.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_+-]*\s*", "", stripped)
        cleaned = re.sub(r"```\s*$", "", cleaned)
        return cleaned.strip(), True
    return stripped, False


def extract_json_object(text: str) -> tuple[Any | None, list[str]]:
    """Find the first balanced JSON value in a free-form string.

    Returns ``(value, errors)``. The scanner is string- and escape-aware so
    braces inside string literals do not confuse it.
    """
    errors: list[str] = []
    if text is None:
        return None, ["empty response"]
    candidate, _ = _strip_code_fences(str(text))
    if len(candidate) > _MAX_RAW_CHARS:
        candidate = candidate[:_MAX_RAW_CHARS]
        errors.append("response truncated before parsing (size limit)")
    if "{" not in candidate:
        return None, errors + ["no JSON object found in response"]

    # Scan every possible object boundary. Model output may contain an
    # unrelated brace pair in prose or an event wrapper before the evaluation;
    # one bad candidate must not hide a later, complete JSON block.
    invalid: list[str] = []
    saw_unbalanced = False
    for start, opening in enumerate(candidate):
        if opening != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    fragment = candidate[start : index + 1]
                    try:
                        return json.loads(fragment), errors
                    except json.JSONDecodeError as exc:
                        invalid.append(f"invalid JSON: {exc.msg} (line {exc.lineno})")
                    break
        else:
            saw_unbalanced = True

    if saw_unbalanced:
        return None, errors + ["unbalanced JSON object in response"]
    return None, errors + (invalid or ["could not parse JSON object in response"])


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:[.,]\d+)?", value)
        if match:
            return float(match.group(0).replace(",", "."))
    return None


def _coerce_score(value: Any, *, repairs: list[str], field_name: str) -> float | None:
    number = _coerce_number(value)
    if number is None:
        repairs.append(f"{field_name}: could not read a number, value dropped")
        return None
    original = number
    if 0 < number <= 1 and isinstance(value, float):
        number *= 100.0
        repairs.append(f"{field_name}: interpreted fraction {original} as {number}/100")
    if number < 0:
        number = 0.0
        repairs.append(f"{field_name}: negative score clamped to 0")
    if number > 100:
        number = 100.0
        repairs.append(f"{field_name}: score above 100 clamped to 100")
    return round(number, 2)


def _coerce_confidence(value: Any, *, repairs: list[str]) -> float:
    number = _coerce_number(value)
    if number is None:
        repairs.append("confidence: missing or unreadable, defaulted to 0.5")
        return 0.5
    if number > 1:
        number = number / 100.0
        repairs.append("confidence: treated values above 1 as percentages")
    if number < 0:
        number = 0.0
        repairs.append("confidence: negative value clamped to 0")
    if number > 1:
        number = 1.0
        repairs.append("confidence: value above 1 clamped to 1")
    return round(number, 3)


def _coerce_str_list(value: Any, *, field_name: str, repairs: list[str]) -> list[str]:
    if value is None:
        return []
    items: list[Any]
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        items = list(value.values())
    else:
        repairs.append(f"{field_name}: unsupported type {type(value).__name__}, ignored")
        return []
    result: list[str] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, dict):
            text = (
                item.get("text")
                or item.get("reason")
                or item.get("name")
                or item.get("label")
                or json.dumps(item, ensure_ascii=False)
            )
        else:
            text = str(item)
        text = text.strip()
        if text.lower().strip(" .!") in _NEGATIVE_PLACEHOLDERS:
            repairs.append(f"{field_name}: dropped placeholder value '{text}'")
            continue
        if text:
            result.append(text[:_MAX_STRING_CHARS])
    if len(result) > _MAX_LIST_ITEMS:
        repairs.append(f"{field_name}: truncated to {_MAX_LIST_ITEMS} entries")
        result = result[:_MAX_LIST_ITEMS]
    return result


def _coerce_dimensions(value: Any, *, repairs: list[str]) -> list[DimensionScore]:
    raw_items: list[Any]
    if value is None:
        return []
    if isinstance(value, dict):
        raw_items = []
        for key, item in value.items():
            if isinstance(item, dict):
                merged: dict[str, Any] = {"name": key}
                merged.update(item)
                raw_items.append(merged)
            else:
                raw_items.append({"name": key, "score": item})
    elif isinstance(value, list):
        raw_items = value
    else:
        repairs.append("dimensions: unsupported type, ignored")
        return []

    dimensions: list[DimensionScore] = []
    for item in raw_items:
        if isinstance(item, dict):
            name = str(
                item.get("name") or item.get("dimension") or item.get("label") or ""
            ).strip()
            score = _coerce_score(
                item.get("score", item.get("value")), repairs=repairs, field_name=f"dimension[{name or '?'}]"
            )
            weight = _coerce_number(item.get("weight"))
            rationale = str(item.get("rationale") or item.get("reason") or "").strip()
            evidence = _coerce_str_list(
                item.get("evidence") or item.get("signals"),
                field_name=f"dimension[{name or '?'}].evidence",
                repairs=repairs,
            )
        elif isinstance(item, (int, float, str)):
            name = f"dimension_{len(dimensions) + 1}"
            score = _coerce_score(item, repairs=repairs, field_name=f"dimension[{name}]")
            weight = None
            rationale = ""
            evidence = []
            repairs.append(f"dimensions: bare value at index {len(dimensions)} given a placeholder name")
        else:
            repairs.append("dimensions: skipped an unsupported entry")
            continue

        if not name:
            name = f"dimension_{len(dimensions) + 1}"
            repairs.append("dimensions: missing name, a placeholder was assigned")
        if score is None:
            repairs.append(f"dimensions: '{name}' had no usable score and was dropped")
            continue
        dimensions.append(
            DimensionScore(
                name=name[:120],
                score=score,
                weight=float(weight) if weight else 1.0,
                rationale=rationale[:_MAX_STRING_CHARS],
                evidence=evidence,
            )
        )
        if len(dimensions) >= _MAX_DIMENSIONS:
            repairs.append(f"dimensions: truncated to {_MAX_DIMENSIONS} entries")
            break
    return dimensions


def _find(data: dict[str, Any], aliases: tuple[str, ...]) -> tuple[Any, str | None]:
    for alias in aliases:
        if alias in data and data[alias] is not None:
            return data[alias], alias
    return None, None


def validate_payload(data: Any, *, strict: bool = False) -> ParseOutcome:
    """Normalize a decoded JSON payload into the canonical evaluation shape."""
    errors: list[str] = []
    repairs: list[str] = []

    if not isinstance(data, dict):
        return ParseOutcome(
            ok=False, errors=["top-level JSON value must be an object"], repairs=repairs
        )

    payload: dict[str, Any] = {}
    score_value, score_key = _find(data, _SCORE_ALIASES)
    score = None
    if score_value is not None:
        score = _coerce_score(score_value, repairs=repairs, field_name="overall_score")
        if score_key != "overall_score":
            repairs.append(f"overall_score: read from alias '{score_key}'")
    payload["overall_score"] = score

    dimensions_value, dimensions_key = _find(data, _DIMENSION_ALIASES)
    dimensions = _coerce_dimensions(dimensions_value, repairs=repairs)
    if dimensions_key and dimensions_key != "dimensions":
        repairs.append(f"dimensions: read from alias '{dimensions_key}'")
    payload["dimensions"] = [dimension.to_dict() for dimension in dimensions]

    for target, aliases in (
        ("blockers", _BLOCKER_ALIASES),
        ("strengths", _STRENGTH_ALIASES),
        ("gaps", _GAP_ALIASES),
        ("unknowns", _UNKNOWN_ALIASES),
    ):
        value, key = _find(data, aliases)
        payload[target] = _coerce_str_list(value, field_name=target, repairs=repairs)
        if key and key != target:
            repairs.append(f"{target}: read from alias '{key}'")

    confidence_value, _ = _find(data, _CONFIDENCE_ALIASES)
    payload["confidence"] = _coerce_confidence(confidence_value, repairs=repairs)

    rationale_value, rationale_key = _find(data, _RATIONALE_ALIASES)
    payload["rationale"] = str(rationale_value or "").strip()[:_MAX_STRING_CHARS]
    if rationale_key and rationale_key != "rationale":
        repairs.append(f"rationale: read from alias '{rationale_key}'")

    recommendation_value, _ = _find(data, ("recommendation", "verdict", "decision"))
    recommendation = str(recommendation_value or "").strip().lower().replace(" ", "_")
    if recommendation in _RECOMMENDATION_ALIASES:
        payload["recommendation"] = _RECOMMENDATION_ALIASES[recommendation]
        if payload["recommendation"] != recommendation:
            repairs.append(f"recommendation: mapped '{recommendation}' to a canonical value")
    elif recommendation:
        payload["recommendation"] = Recommendation.NEED_MORE_INFO.value
        repairs.append(f"recommendation: unknown value '{recommendation}' mapped to need_more_info")
    else:
        payload["recommendation"] = Recommendation.NEED_MORE_INFO.value
        repairs.append("recommendation: missing, defaulted to need_more_info")

    known_keys = {
        *_SCORE_ALIASES,
        *_DIMENSION_ALIASES,
        *_BLOCKER_ALIASES,
        *_STRENGTH_ALIASES,
        *_GAP_ALIASES,
        *_UNKNOWN_ALIASES,
        *_CONFIDENCE_ALIASES,
        *_RATIONALE_ALIASES,
        "recommendation",
        "verdict",
        "decision",
    }
    extra = {
        str(key): value
        for key, value in data.items()
        if key not in known_keys
    }
    payload["extra"] = extra

    # Cross-field consistency.
    if payload["blockers"] and payload["recommendation"] != Recommendation.SKIP.value:
        payload["recommendation"] = Recommendation.SKIP.value
        repairs.append("recommendation: forced to 'skip' because hard blockers are present")
    if score is None and not dimensions:
        errors.append("response contains neither an overall score nor any dimensions")
    if payload["overall_score"] is None and dimensions:
        weighted = [d for d in dimensions if d.weight > 0]
        if weighted:
            total_weight = sum(d.weight for d in weighted)
            payload["overall_score"] = round(
                sum(d.score * d.weight for d in weighted) / total_weight, 2
            )
            repairs.append("overall_score: derived from dimension scores")

    if strict and repairs:
        errors.extend(f"strict mode rejected repair: {repair}" for repair in repairs)

    return ParseOutcome(
        ok=not errors,
        payload=payload if not errors else None,
        errors=errors,
        repairs=repairs,
    )


def success_contract_errors(payload: dict[str, Any] | None) -> list[str]:
    """Reasons a normalized payload cannot be a successful public evaluation.

    This is the payload-shaped half of the canonical invariant enforced by
    :func:`jobscout.models.evaluation_contract_errors`: a successful evaluation
    needs a numeric ``overall_score`` and a public recommendation
    (apply/consider/skip). ``need_more_info`` remains a legitimate diagnostic in
    the payload, but it is never a successful public recommendation, so a payload
    that resolves to it is rejected here and the scorer reports a contract-invalid
    response instead of publishing an unresolved success.

    An empty list means the payload satisfies the invariant.
    """
    if payload is None:
        return ["no normalized payload to validate"]
    errors: list[str] = []
    if payload.get("overall_score") is None:
        errors.append("a successful evaluation requires a numeric overall_score")
    recommendation = str(payload.get("recommendation") or "")
    if recommendation not in PUBLIC_RECOMMENDATIONS:
        errors.append(
            "a successful evaluation requires an apply/consider/skip recommendation "
            f"(got '{recommendation or 'missing'}')"
        )
    return errors


def _redact_strings(value: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively redact configured secrets from every string in ``value``."""
    if isinstance(value, str):
        return redact_secrets(value, secrets)
    if isinstance(value, list):
        return [_redact_strings(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_strings(item, secrets) for key, item in value.items()}
    return value


def redact_outcome(outcome: ParseOutcome, secrets: tuple[str, ...]) -> ParseOutcome:
    """Redact configured secrets from model-derived evaluation text.

    Covers both the parse diagnostics (``errors`` and ``repairs``, which embed
    model-supplied values) and the normalized payload (rationale, dimension
    text, and list entries) because the scorers persist and print both. The
    raw text/excerpt are left verbatim: raw provenance is governed separately by
    :func:`jobscout.config.is_safe_to_store`.
    """
    outcome.errors = [redact_secrets(error, secrets) for error in outcome.errors]
    outcome.repairs = [redact_secrets(repair, secrets) for repair in outcome.repairs]
    if outcome.payload is not None:
        outcome.payload = _redact_strings(outcome.payload, secrets)
    return outcome


def parse_llm_evaluation(
    text: str,
    *,
    strict: bool = False,
    secrets: tuple[str, ...] = (),
) -> ParseOutcome:
    """Full pipeline: strip fences, extract JSON, validate and normalize.

    ``secrets`` is the configured secret list (for example
    ``Settings.llm_secrets``); any occurrence is redacted from the model-derived
    payload and from the error/repair diagnostics before they are returned.
    """
    raw = text or ""
    value, errors = extract_json_object(raw)
    if value is None:
        return redact_outcome(
            ParseOutcome(
                ok=False,
                errors=errors or ["could not parse JSON"],
                raw_text=raw,
                raw_excerpt=raw[:400],
            ),
            secrets,
        )
    outcome = validate_payload(value, strict=strict)
    outcome.raw_text = raw
    outcome.raw_excerpt = raw[:400]
    if errors:
        outcome.errors = errors + outcome.errors
    return redact_outcome(outcome, secrets)


__all__ = [
    "ALLOWED_RECOMMENDATIONS",
    "PROMPT_VERSION",
    "ParseOutcome",
    "extract_json_object",
    "parse_llm_evaluation",
    "redact_outcome",
    "success_contract_errors",
    "validate_payload",
]
