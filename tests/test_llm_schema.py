"""LLM response validation tests: robust JSON handling, no network."""

from __future__ import annotations

import json

import pytest

from conftest import fixture_text

from jobscout.models import Recommendation
from jobscout.scoring.schema import (
    extract_json_object,
    parse_llm_evaluation,
    success_contract_errors,
    validate_payload,
)


def test_extracts_plain_json() -> None:
    value, errors = extract_json_object('{"overall_score": 70, "recommendation": "apply"}')
    assert value == {"overall_score": 70, "recommendation": "apply"}
    assert errors == []


def test_extracts_json_from_prose_and_fences() -> None:
    text = 'Here you go:\n```json\n{"score": 55}\n```\nThanks!'
    value, errors = extract_json_object(text)
    assert value == {"score": 55}
    assert errors == []


def test_ignores_braces_inside_strings() -> None:
    text = 'prefix {"rationale": "uses {braces} safely", "overall_score": 42} suffix'
    value, _ = extract_json_object(text)
    assert value["overall_score"] == 42


def test_reports_unbalanced_and_missing_json() -> None:
    value, errors = extract_json_object("no json here")
    assert value is None
    assert errors
    value, errors = extract_json_object('{"overall_score": 60, "recommendation": "consider"')
    assert value is None
    assert any("unbalanced" in error for error in errors)


def test_validate_payload_normalizes_field_aliases() -> None:
    outcome = validate_payload(
        {
            "score": "74.5 points",
            "verdict": "Strong Apply",
            "confidence": 82,
            "scores": {"skills": {"score": 0.8, "rationale": "ok"}, "seniority": 70},
            "hard_blockers": "none",
            "pros": ["great team", 42],
            "uncertainties": ["salary"],
        }
    )
    assert outcome.ok
    payload = outcome.payload
    assert payload["overall_score"] == 74.5
    assert payload["recommendation"] == Recommendation.APPLY.value
    assert payload["confidence"] == 0.82
    assert payload["dimensions"][0]["score"] == 80.0
    assert payload["blockers"] == []  # the literal "none" is a placeholder, not a blocker
    assert payload["strengths"] == ["great team", "42"]
    assert payload["unknowns"] == ["salary"]
    assert outcome.repairs


def test_blockers_force_skip_recommendation() -> None:
    outcome = validate_payload(
        {
            "overall_score": 90,
            "recommendation": "apply",
            "blockers": ["requires an active security clearance"],
            "dimensions": [{"name": "skills", "score": 90}],
        }
    )
    assert outcome.ok
    assert outcome.payload["recommendation"] == Recommendation.SKIP.value
    assert any("forced to 'skip'" in repair for repair in outcome.repairs)


def test_overall_score_derived_from_dimensions_when_missing() -> None:
    outcome = validate_payload(
        {
            "recommendation": "consider",
            "dimensions": [
                {"name": "a", "score": 80, "weight": 1},
                {"name": "b", "score": 40, "weight": 1},
            ],
        }
    )
    assert outcome.ok
    assert outcome.payload["overall_score"] == 60.0


def test_scores_are_clamped() -> None:
    outcome = validate_payload(
        {"overall_score": 250, "confidence": -3, "dimensions": [{"name": "x", "score": -10}]}
    )
    assert outcome.payload["overall_score"] == 100.0
    assert outcome.payload["confidence"] == 0.0
    assert outcome.payload["dimensions"][0]["score"] == 0.0


def test_missing_score_and_dimensions_is_an_error() -> None:
    outcome = validate_payload({"recommendation": "apply"})
    assert outcome.ok is False
    assert any("neither an overall score nor any dimensions" in error for error in outcome.errors)


def test_non_object_payload_is_rejected() -> None:
    outcome = validate_payload([1, 2, 3])
    assert outcome.ok is False
    assert outcome.errors


def test_strict_mode_rejects_repairs() -> None:
    payload = {"score": 70, "recommendation": "apply", "dimensions": [{"name": "x", "score": 70}]}
    lenient = validate_payload(payload)
    strict = validate_payload(payload, strict=True)
    assert lenient.ok is True
    assert strict.ok is False
    assert any("strict mode rejected repair" in error for error in strict.errors)


def test_extra_keys_are_preserved_separately() -> None:
    outcome = validate_payload(
        {"overall_score": 50, "recommendation": "consider", "notes_from_model": "hi"}
    )
    assert outcome.payload["extra"] == {"notes_from_model": "hi"}


def test_valid_fixture_parses() -> None:
    outcome = parse_llm_evaluation(fixture_text("llm_response_valid.json"))
    assert outcome.ok
    assert outcome.payload["overall_score"] == 82
    assert len(outcome.payload["dimensions"]) == 2


def test_repairable_fixture_parses_with_repairs() -> None:
    outcome = parse_llm_evaluation(fixture_text("llm_response_repairable.txt"))
    assert outcome.ok
    payload = outcome.payload
    assert payload["overall_score"] == 74.5
    assert payload["recommendation"] == Recommendation.APPLY.value
    assert payload["confidence"] == 0.82
    assert payload["dimensions"][0]["score"] == 80.0
    assert outcome.repairs


def test_malformed_fixture_fails_cleanly() -> None:
    outcome = parse_llm_evaluation(fixture_text("llm_response_malformed.txt"))
    assert outcome.ok is False
    assert outcome.errors
    assert outcome.raw_excerpt


def test_empty_and_non_string_input_fail_cleanly() -> None:
    assert parse_llm_evaluation("").ok is False
    assert parse_llm_evaluation(None).ok is False  # type: ignore[arg-type]


def test_huge_response_is_truncated_before_parsing() -> None:
    payload = {"overall_score": 10, "recommendation": "skip", "padding": "x" * 300_000}
    outcome = parse_llm_evaluation(json.dumps(payload))
    assert outcome.ok is False
    assert any("truncated" in error for error in outcome.errors)


def test_dimension_entries_without_scores_are_dropped() -> None:
    outcome = validate_payload(
        {
            "overall_score": 50,
            "recommendation": "consider",
            "dimensions": [{"name": "broken", "rationale": "no score"}, {"name": "ok", "score": 55}],
        }
    )
    assert [dimension["name"] for dimension in outcome.payload["dimensions"]] == ["ok"]


def test_unknown_recommendation_falls_back_safely() -> None:
    outcome = validate_payload(
        {"overall_score": 50, "recommendation": "definitely hire them", "dimensions": [{"name": "x", "score": 50}]}
    )
    assert outcome.payload["recommendation"] == Recommendation.NEED_MORE_INFO.value
    assert pytest.approx(outcome.payload["confidence"]) == 0.5


# ---------------------------------------------------------------------------
# Canonical successful-evaluation contract
# ---------------------------------------------------------------------------


def test_success_contract_rejects_need_more_info_even_with_a_score() -> None:
    """A scored ``need_more_info`` payload is never a successful evaluation.

    This is the exact ambiguous state the reliability work removes: a numeric
    score (even 0) must not make an unresolved recommendation publishable.
    """
    outcome = validate_payload(
        {
            "overall_score": 0,
            "recommendation": "need_more_info",
            "dimensions": [{"name": "skills", "score": 0}],
        }
    )
    assert outcome.ok  # normalization still succeeds; the payload is diagnosable
    errors = success_contract_errors(outcome.payload)
    assert errors
    assert any("apply/consider/skip recommendation" in error for error in errors)


def test_success_contract_rejects_a_missing_score() -> None:
    assert success_contract_errors(
        {"overall_score": None, "recommendation": "consider"}
    ) == ["a successful evaluation requires a numeric overall_score"]
    # A score derived from dimensions is a real score and satisfies the contract.
    derived = validate_payload(
        {
            "recommendation": "consider",
            "dimensions": [{"name": "skills", "score": 50}],
        }
    ).payload
    assert derived["overall_score"] == 50.0
    assert success_contract_errors(derived) == []


def test_success_contract_accepts_a_real_zero_score() -> None:
    """Zero is a legitimate score for a real incompatibility, not a placeholder."""
    errors = success_contract_errors(
        {"overall_score": 0.0, "recommendation": Recommendation.SKIP.value}
    )
    assert errors == []


def test_success_contract_accepts_every_public_recommendation() -> None:
    for recommendation in ("apply", "consider", "skip"):
        assert (
            success_contract_errors(
                {"overall_score": 42.0, "recommendation": recommendation}
            )
            == []
        )


# A configured credential that matches none of the generic patterns in
# ``config._SECRET_PATTERNS``. Only the explicit secret list can redact it.
CONFIGURED_SECRET = "zzq7-local-configured-credential"


def test_parse_redacts_configured_secret_from_model_text_and_repairs() -> None:
    response = json.dumps(
        {
            "overall_score": "74.5 points",
            "recommendation": CONFIGURED_SECRET,
            "dimensions": {
                "skills": {"score": 80, "rationale": f"leak {CONFIGURED_SECRET}"}
            },
            "blockers": [CONFIGURED_SECRET],
            "rationale": f"note: {CONFIGURED_SECRET} echoed",
        }
    )

    outcome = parse_llm_evaluation(response, secrets=(CONFIGURED_SECRET,))

    assert outcome.ok
    assert outcome.payload["rationale"] == "note: ***REDACTED*** echoed"
    assert outcome.payload["dimensions"][0]["rationale"] == "leak ***REDACTED***"
    assert outcome.payload["blockers"] == ["***REDACTED***"]
    assert all(CONFIGURED_SECRET not in repair for repair in outcome.repairs)
    assert any("***REDACTED***" in repair for repair in outcome.repairs)
    # The repair still explains what happened, without the secret value.
    assert any("unknown value" in repair for repair in outcome.repairs)


def test_parse_without_configured_secrets_keeps_non_pattern_text() -> None:
    response = json.dumps(
        {"overall_score": 50, "recommendation": CONFIGURED_SECRET, "rationale": CONFIGURED_SECRET}
    )
    outcome = parse_llm_evaluation(response)
    assert outcome.payload["rationale"] == CONFIGURED_SECRET


def test_parse_redacts_json_quoted_credential_from_model_text() -> None:
    """A model echoing a JSON-quoted credential must not persist it.

    The response is itself JSON, so a rationale that quotes the credential as
    ``{"password": "..."}`` is the realistic shape. No configured secret is
    passed: the generic detector must catch it.
    """
    embedded = '{"password": "abcdefghijklmnop"}'
    response = json.dumps(
        {
            "overall_score": 60,
            "recommendation": "consider",
            "dimensions": {"skills": {"score": 60, "rationale": f"logged {embedded}"}},
            "rationale": f"note: {embedded}",
            "blockers": [embedded],
        }
    )

    outcome = parse_llm_evaluation(response)

    assert outcome.ok
    serialized = json.dumps(outcome.payload)
    assert "abcdefghijklmnop" not in serialized
    assert "abcdefghijklmnop" not in outcome.payload["rationale"]
    assert "abcdefghijklmnop" not in outcome.payload["dimensions"][0]["rationale"]
    assert "***REDACTED***" in outcome.payload["rationale"]
