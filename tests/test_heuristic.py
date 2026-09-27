"""Heuristic scorer tests: deterministic, offline, explainable."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from conftest import FIXED_NOW, no_network

from jobscout.models import ContentStatus, JobPosting, Recommendation
from jobscout.normalize import finalize_posting
from jobscout.scoring.heuristic import (
    APPLY_THRESHOLD,
    STALE_RECENCY_SCORE,
    UNKNOWN_DATE_RECENCY_SCORE,
    score_job,
)
from jobscout.scoring.profile import Profile, profile_from_dict


def test_strong_match_recommends_apply(profile, strong_posting, monkeypatch) -> None:
    no_network(monkeypatch)
    evaluation = score_job(strong_posting, profile, now=FIXED_NOW)

    assert evaluation.status == "ok"
    assert evaluation.blockers == []
    assert evaluation.overall_score is not None
    assert evaluation.overall_score >= APPLY_THRESHOLD
    assert evaluation.recommendation is Recommendation.APPLY
    assert evaluation.confidence > 0.5
    assert {dimension.name for dimension in evaluation.dimensions} >= {"must_have_skills", "recency"}
    assert any("Python" in strength or "python" in strength.lower() for strength in evaluation.strengths)
    assert evaluation.rationale
    assert evaluation.input_fingerprint


def test_excluded_company_and_dealbreaker_block(profile) -> None:
    posting = finalize_posting(
        JobPosting(
            source="fixture",
            title="Data Engineer",
            company="Globex Corporation",
            url="https://example.test/jobs/globex",
            location="Buenos Aires, Argentina",
            description="Python and SQL. This role requires 5 days in office attendance.",
            seniority="senior",
            posted_at=(FIXED_NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z"),
        )
    )
    evaluation = score_job(posting, profile, now=FIXED_NOW)
    assert evaluation.recommendation is Recommendation.SKIP
    assert any("exclusion list" in blocker for blocker in evaluation.blockers)
    assert any("dealbreaker" in blocker for blocker in evaluation.blockers)
    assert evaluation.overall_score is not None


def test_missing_description_lowers_confidence_and_records_unknown(profile, strong_posting) -> None:
    sparse = finalize_posting(
        replace(
            strong_posting,
            description=None,
            posted_at=None,
            content_status=ContentStatus.PARTIAL,
        )
    )
    evaluation = score_job(sparse, profile, now=FIXED_NOW)
    assert any("description" in unknown for unknown in evaluation.unknowns)
    assert any("posting date" in unknown for unknown in evaluation.unknowns)
    assert evaluation.confidence < 0.6
    assert evaluation.recommendation is not Recommendation.APPLY


def test_seniority_mismatch_is_penalized(profile, weak_posting) -> None:
    evaluation = score_job(weak_posting, profile, now=FIXED_NOW)
    seniority = next(d for d in evaluation.dimensions if d.name == "seniority")
    assert seniority.score < 40
    assert evaluation.overall_score < APPLY_THRESHOLD
    assert any("Seniority" in gap for gap in evaluation.gaps)


def test_remote_only_profile_blocks_onsite_posting(profile) -> None:
    remote_only = profile_from_dict({**profile.to_dict(), "remote_preference": "remote_only"})
    onsite = finalize_posting(
        JobPosting(
            source="fixture",
            title="Senior Backend Engineer",
            company="Initech",
            url="https://example.test/jobs/onsite",
            location="Buenos Aires, Argentina",
            description="Python, SQL, PostgreSQL. This is an on-site role, five days a week in the office.",
            seniority="senior",
            posted_at=FIXED_NOW.isoformat().replace("+00:00", "Z"),
        )
    )
    evaluation = score_job(onsite, remote_only, now=FIXED_NOW)
    assert any("remote" in blocker for blocker in evaluation.blockers)
    assert evaluation.recommendation is Recommendation.SKIP


def test_require_all_must_have_becomes_a_blocker(profile, strong_posting) -> None:
    strict = profile_from_dict({**profile.to_dict(), "require_all_must_have": True})
    partial = finalize_posting(
        replace(strong_posting, description="We use Python and SQL every day.", source_job_id="job-partial")
    )
    evaluation = score_job(partial, strict, now=FIXED_NOW)
    assert any("missing required skills" in blocker for blocker in evaluation.blockers)


def test_output_is_deterministic(profile, strong_posting) -> None:
    first = score_job(strong_posting, profile, now=FIXED_NOW)
    second = score_job(strong_posting, profile, now=FIXED_NOW)
    assert first.to_dict() == second.to_dict()


def test_evaluation_payload_has_every_explainability_field(profile, strong_posting) -> None:
    payload = score_job(strong_posting, profile, now=FIXED_NOW).to_dict()
    for key in (
        "overall_score",
        "dimensions",
        "blockers",
        "strengths",
        "gaps",
        "unknowns",
        "confidence",
        "recommendation",
        "rationale",
        "status",
    ):
        assert key in payload
    for dimension in payload["dimensions"]:
        assert set(dimension) == {"name", "score", "weight", "rationale", "evidence"}
        assert 0 <= dimension["score"] <= 100
    assert 0 <= payload["confidence"] <= 1


def test_salary_below_minimum_is_a_gap(profile) -> None:
    posting = finalize_posting(
        JobPosting(
            source="fixture",
            title="Senior Backend Engineer",
            company="Acme Data Inc.",
            url="https://example.test/jobs/low-pay",
            location="Remote",
            description="Python SQL PostgreSQL remote role.",
            seniority="senior",
            salary="USD 1500 per month",
            posted_at=FIXED_NOW.isoformat().replace("+00:00", "Z"),
        )
    )
    evaluation = score_job(posting, profile, now=FIXED_NOW)
    compensation = next((d for d in evaluation.dimensions if d.name == "compensation"), None)
    assert compensation is not None
    assert compensation.score == 20.0


def test_stale_posting_scores_low_on_recency(profile, strong_posting) -> None:
    stale = finalize_posting(
        replace(
            strong_posting,
            source_job_id="job-stale",
            posted_at=(FIXED_NOW - timedelta(days=120)).isoformat().replace("+00:00", "Z"),
        )
    )
    evaluation = score_job(stale, profile, now=FIXED_NOW)
    recency = next(d for d in evaluation.dimensions if d.name == "recency")
    assert recency.score == 25.0
    assert any("stale" in unknown for unknown in evaluation.unknowns)


def test_profile_without_languages_omits_language_dimension(profile, strong_posting) -> None:
    minimal = profile_from_dict(
        {**profile.to_dict(), "languages": [], "min_salary": None, "nice_to_have_skills": []}
    )
    evaluation = score_job(strong_posting, minimal, now=FIXED_NOW)
    names = {dimension.name for dimension in evaluation.dimensions}
    assert "language" not in names
    assert "compensation" not in names
    assert "skills" not in names


def test_empty_profile_is_still_scoreable(strong_posting) -> None:
    empty = Profile(name="empty")
    evaluation = score_job(strong_posting, empty, now=FIXED_NOW)
    assert evaluation.overall_score is not None
    assert evaluation.recommendation is not None
    assert evaluation.dimensions


def test_score_without_description_or_dates_warns_and_does_not_crash(profile) -> None:
    posting = finalize_posting(JobPosting(source="fixture", title="Mystery Role", company="Unknown Co"))
    evaluation = score_job(posting, profile, now=FIXED_NOW)
    assert evaluation.overall_score is not None
    assert evaluation.unknowns
    assert evaluation.confidence <= 0.5


def _recency_of(evaluation):
    return next(d for d in evaluation.dimensions if d.name == "recency").score


def _posting_with_age(strong_posting, days: int | None):
    if days is None:
        return replace(strong_posting, source_job_id="job-age-none", posted_at=None)
    stamp = (FIXED_NOW - timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return replace(strong_posting, source_job_id=f"job-age-{days}", posted_at=stamp)


def test_max_posting_age_days_marks_an_older_known_date_stale(profile, strong_posting) -> None:
    limited = profile_from_dict({**profile.to_dict(), "max_posting_age_days": 30})
    evaluation = score_job(_posting_with_age(strong_posting, 45), limited, now=FIXED_NOW)
    assert _recency_of(evaluation) == STALE_RECENCY_SCORE
    assert any("max_posting_age_days" in unknown for unknown in evaluation.unknowns)


def test_max_posting_age_days_zero_marks_any_known_date_stale(profile, strong_posting) -> None:
    zero = profile_from_dict({**profile.to_dict(), "max_posting_age_days": 0})
    evaluation = score_job(_posting_with_age(strong_posting, 1), zero, now=FIXED_NOW)
    assert _recency_of(evaluation) == STALE_RECENCY_SCORE


def test_null_max_posting_age_keeps_the_graduated_tiers(profile, strong_posting) -> None:
    unbounded = profile_from_dict({**profile.to_dict(), "max_posting_age_days": None})
    evaluation = score_job(_posting_with_age(strong_posting, 45), unbounded, now=FIXED_NOW)
    # 45 days falls in the 31-60 tier: the limit is disabled, so no hard stale
    # penalty and no max_posting_age_days unknown.
    assert _recency_of(evaluation) == 50.0
    assert not any("max_posting_age_days" in unknown for unknown in evaluation.unknowns)


def test_unknown_posting_date_is_neutral_and_never_stale(profile, strong_posting) -> None:
    evaluation = score_job(_posting_with_age(strong_posting, None), profile, now=FIXED_NOW)
    assert _recency_of(evaluation) == UNKNOWN_DATE_RECENCY_SCORE
    assert any("posting date is unknown" in unknown for unknown in evaluation.unknowns)


def _score_with_salary(profile, strong_posting, salary: str):
    posting = replace(strong_posting, source_job_id=f"job-salary-{abs(hash(salary))}", salary=salary)
    return score_job(posting, profile, now=FIXED_NOW)


def _compensation_of(evaluation):
    return next((d for d in evaluation.dimensions if d.name == "compensation"), None)


def test_unsupported_currency_is_hard_rejected(profile, strong_posting) -> None:
    evaluation = _score_with_salary(profile, strong_posting, "GBP 5000 per month")
    assert evaluation.recommendation is Recommendation.SKIP
    assert any("unsupported currency" in blocker for blocker in evaluation.blockers)
    assert _compensation_of(evaluation) is None


def test_unsupported_currency_is_rejected_even_without_a_salary_floor(
    profile, strong_posting
) -> None:
    no_floor = profile_from_dict({**profile.to_dict(), "min_salary": None})
    evaluation = _score_with_salary(no_floor, strong_posting, "ARS 900000 per month")
    assert evaluation.recommendation is Recommendation.SKIP
    assert any("unsupported currency" in blocker for blocker in evaluation.blockers)


def test_ambiguous_currency_is_unknown_and_not_compared(profile, strong_posting) -> None:
    evaluation = _score_with_salary(profile, strong_posting, "USD 5000 per month / EUR 4500 per month")
    compensation = _compensation_of(evaluation)
    assert compensation is not None and compensation.score == 50.0
    assert evaluation.blockers == []
    assert any("ambiguous" in unknown for unknown in evaluation.unknowns)


def test_missing_currency_is_unknown_and_not_compared(profile, strong_posting) -> None:
    evaluation = _score_with_salary(profile, strong_posting, "5000 per month")
    compensation = _compensation_of(evaluation)
    assert compensation is not None and compensation.score == 50.0
    assert evaluation.blockers == []


def test_missing_period_is_unknown_and_not_compared(profile, strong_posting) -> None:
    evaluation = _score_with_salary(profile, strong_posting, "USD 9000")
    compensation = _compensation_of(evaluation)
    assert compensation is not None and compensation.score == 50.0
    assert any("period is not stated" in unknown for unknown in evaluation.unknowns)


def test_usd_vs_eur_mismatch_is_not_numerically_compared(profile, strong_posting) -> None:
    # The same numeric amount in the profile's currency would meet the floor
    # exactly; a EUR amount must stay a neutral unknown, never a 100 or a 20.
    usd = _score_with_salary(profile, strong_posting, "USD 4000 per month")
    assert _compensation_of(usd).score == 100.0

    eur = _score_with_salary(profile, strong_posting, "EUR 4000 per month")
    compensation = _compensation_of(eur)
    assert compensation.score == 50.0
    assert compensation.score not in (100.0, 20.0)
    assert any("no FX conversion" in unknown for unknown in eur.unknowns)
