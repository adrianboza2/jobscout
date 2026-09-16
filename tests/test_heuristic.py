"""Heuristic scorer tests: deterministic, offline, explainable."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from conftest import FIXED_NOW, no_network

from jobscout.models import ContentStatus, JobPosting, Recommendation
from jobscout.normalize import finalize_posting
from jobscout.scoring.heuristic import APPLY_THRESHOLD, score_job
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
