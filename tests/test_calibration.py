"""Calibration report tests: mapping, agreement, filtering, and CLI wiring.

Everything runs offline against temporary SQLite databases. The report is a
read-only ``SELECT`` pass, so no test here touches the network or mutates the
corpus it is reading.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobscout.calibration import (
    HUMAN_LABELS,
    LEGACY_HUMAN_DECISIONS,
    MODEL_TO_HUMAN,
    SCORE_BUCKETS,
    CalibrationError,
    build_report,
    equivalent_human_decision,
    score_bucket,
)
from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.db import (
    connect,
    evaluation_count,
    feedback_count,
    get_feedback,
    init_db,
    insert_evaluation,
    store_posting,
    upsert_feedback,
)
from jobscout.models import (
    Evaluation,
    EvaluationStatus,
    JobFeedback,
    JobPosting,
    Recommendation,
    ScorerName,
)
from jobscout.normalize import finalize_posting

DECIDED_AT = "2026-09-16T14:00:00.000000Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _posting(index: int) -> JobPosting:
    return finalize_posting(
        JobPosting(
            source="linkedin",
            title=f"Role {index}",
            company=f"Company {index}",
            url=f"https://www.linkedin.com/jobs/view/39123456{index:02d}/role",
            source_job_id=f"39123456{index:02d}",
            description="Python, SQL, PostgreSQL.",
        )
    )


def _seed_job(conn, index: int) -> int:
    job, _ = store_posting(conn, _posting(index), now="2026-09-15T10:00:00Z")
    return int(job.id)


def _evaluate(
    conn,
    job_id: int,
    *,
    scorer: ScorerName | str = ScorerName.OPENCODE,
    recommendation: Recommendation | str,
    score: float | None = 70.0,
    status: EvaluationStatus | str = EvaluationStatus.OK,
) -> int:
    # A scored ``need_more_info`` result is an unresolved evaluation, not a
    # successful one: the persistence invariant stores it with its own status.
    if (
        str(recommendation) == Recommendation.NEED_MORE_INFO.value
        and str(status) == EvaluationStatus.OK.value
    ):
        status = EvaluationStatus.NEEDS_MORE_INFO
    evaluation = insert_evaluation(
        conn,
        Evaluation(
            job_id=job_id,
            scorer=scorer,
            overall_score=score,
            recommendation=recommendation,
            status=status,
        ),
    )
    return int(evaluation.id)


def _decide(conn, job_id: int, decision: str, *, at: str = DECIDED_AT) -> None:
    upsert_feedback(
        conn,
        JobFeedback(job_id=job_id, decision=decision, decision_updated_at=at),
    )


def _seed_file(path: Path) -> None:
    """A small corpus: two agreeing jobs, one disagreeing, one unresolved."""
    conn = connect(path)
    try:
        init_db(conn)
        apply_id = _seed_job(conn, 1)
        _evaluate(conn, apply_id, recommendation=Recommendation.APPLY, score=88.0)
        _decide(conn, apply_id, "apply")

        skip_id = _seed_job(conn, 2)
        _evaluate(conn, skip_id, recommendation=Recommendation.SKIP, score=21.0)
        _decide(conn, skip_id, "reject")

        disagree_id = _seed_job(conn, 3)
        _evaluate(conn, disagree_id, recommendation=Recommendation.APPLY, score=64.0)
        _decide(conn, disagree_id, "reject")

        unresolved_id = _seed_job(conn, 4)
        _evaluate(
            conn,
            unresolved_id,
            recommendation=Recommendation.NEED_MORE_INFO,
            score=50.0,
        )
        _decide(conn, unresolved_id, "consider")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The explicit bridge
# ---------------------------------------------------------------------------


def test_equivalent_human_decision_maps_the_three_way() -> None:
    assert equivalent_human_decision("apply") == "apply"
    assert equivalent_human_decision("consider") == "consider"
    assert equivalent_human_decision("skip") == "reject"
    assert MODEL_TO_HUMAN == {"apply": "apply", "consider": "consider", "skip": "reject"}


def test_need_more_info_has_no_human_equivalent() -> None:
    assert equivalent_human_decision("need_more_info") is None
    assert equivalent_human_decision(None) is None


def test_score_bucket_clamps_edges() -> None:
    assert score_bucket(0.0) == SCORE_BUCKETS[0]
    assert score_bucket(55.0) == "50-59"
    assert score_bucket(100.0) == "90-100"
    assert score_bucket(-3.0) == SCORE_BUCKETS[0]
    assert len(SCORE_BUCKETS) == 10


# ---------------------------------------------------------------------------
# Agreement and confusion
# ---------------------------------------------------------------------------


def test_report_compares_the_latest_valid_evaluation(conn) -> None:
    job_id = _seed_job(conn, 1)
    _evaluate(conn, job_id, recommendation=Recommendation.SKIP, score=10.0)
    latest = _evaluate(conn, job_id, recommendation=Recommendation.APPLY, score=91.0)
    _decide(conn, job_id, "apply")

    report = build_report(conn)

    assert report.feedback_total == 1
    assert report.matched == 1
    assert report.comparable == 1
    assert report.exact == 1
    assert report.agreement_rate == 1.0
    assert report.samples[0].evaluation_id == latest


def test_failed_evaluation_is_ignored_in_favor_of_the_latest_valid(conn) -> None:
    job_id = _seed_job(conn, 1)
    valid = _evaluate(conn, job_id, recommendation=Recommendation.APPLY, score=91.0)
    _evaluate(
        conn,
        job_id,
        recommendation=Recommendation.SKIP,
        score=5.0,
        status=EvaluationStatus.ERROR,
    )
    _decide(conn, job_id, "apply")

    report = build_report(conn)

    assert report.comparable == 1
    assert report.exact == 1
    assert report.samples[0].evaluation_id == valid


def test_three_way_confusion_and_agreement(conn) -> None:
    cases = [
        ("apply", "apply", True),
        ("consider", "consider", True),
        ("skip", "reject", True),
        ("apply", "reject", False),
        ("consider", "reject", False),
    ]
    for index, (recommendation, human, _agrees) in enumerate(cases):
        job_id = _seed_job(conn, index)
        _evaluate(conn, job_id, recommendation=recommendation, score=50.0 + index)
        _decide(conn, job_id, human)

    report = build_report(conn)

    assert report.feedback_total == 5
    assert report.comparable == 5
    assert report.exact == 3
    assert report.agreement_rate == pytest.approx(0.6)
    assert report.confusion == {
        "apply": {"apply": 1, "consider": 0, "reject": 1},
        "consider": {"apply": 0, "consider": 1, "reject": 1},
        "skip": {"apply": 0, "consider": 0, "reject": 1},
    }


def test_legacy_consider_is_counted_and_flagged_as_legacy(conn) -> None:
    """A pre-binary row stays comparable and is flagged as legacy, not erased."""
    legacy_id = _seed_job(conn, 1)
    _evaluate(conn, legacy_id, recommendation=Recommendation.NEED_MORE_INFO, score=45.0)
    _decide(conn, legacy_id, "consider")
    active_id = _seed_job(conn, 2)
    _evaluate(conn, active_id, recommendation=Recommendation.APPLY, score=88.0)
    _decide(conn, active_id, "apply")

    report = build_report(conn)
    payload = report.to_dict()

    assert LEGACY_HUMAN_DECISIONS == ("consider",)
    assert report.legacy_total == 1
    assert payload["legacy_total"] == 1
    assert payload["legacy_human_decisions"] == ["consider"]
    # The legacy row is still counted in the report and its score distribution.
    assert report.feedback_total == 2
    assert payload["score_distribution"]["consider"]["count"] == 1
    legacy_sample = next(
        item for item in payload["samples"] if item["human_decision"] == "consider"
    )
    assert legacy_sample["legacy"] is True
    active_sample = next(
        item for item in payload["samples"] if item["human_decision"] == "apply"
    )
    assert active_sample["legacy"] is False
    # The text report tells a reader the legacy row was kept, not renamed.
    assert "legacy human decisions" in report.to_text()


# ---------------------------------------------------------------------------
# need_more_info stays unresolved
# ---------------------------------------------------------------------------


def test_need_more_info_is_unresolved_and_never_becomes_skip(conn) -> None:
    """A model that asks for more information must not be counted as a reject."""
    job_id = _seed_job(conn, 1)
    _evaluate(conn, job_id, recommendation=Recommendation.NEED_MORE_INFO, score=55.0)
    _decide(conn, job_id, "reject")

    report = build_report(conn)

    assert report.comparable == 0
    assert report.agreement_rate is None
    assert report.unresolved_total == 1
    assert report.unresolved == {"need_more_info": 1}
    # The whole point: it is not silently mapped to ``skip``.
    assert report.confusion["skip"] == {"apply": 0, "consider": 0, "reject": 0}
    sample = report.samples[0].to_dict()
    assert sample["resolution"] == "unresolved"
    assert sample["equivalent"] is None
    assert sample["agrees"] is None


def test_need_more_info_is_excluded_from_the_three_way_rate(conn) -> None:
    job_id = _seed_job(conn, 1)
    _evaluate(conn, job_id, recommendation=Recommendation.APPLY, score=88.0)
    _decide(conn, job_id, "apply")
    unresolved_id = _seed_job(conn, 2)
    _evaluate(conn, unresolved_id, recommendation=Recommendation.NEED_MORE_INFO, score=50.0)
    _decide(conn, unresolved_id, "apply")

    report = build_report(conn)

    assert report.feedback_total == 2
    assert report.comparable == 1
    assert report.exact == 1
    assert report.agreement_rate == 1.0  # 1/1, not 1/2
    assert report.unresolved_total == 1


# ---------------------------------------------------------------------------
# Scorer filtering
# ---------------------------------------------------------------------------


def test_scorer_filter_selects_that_scorers_latest_evaluation(conn) -> None:
    job_id = _seed_job(conn, 1)
    _evaluate(
        conn,
        job_id,
        scorer=ScorerName.HEURISTIC,
        recommendation=Recommendation.APPLY,
        score=88.0,
    )
    _evaluate(
        conn,
        job_id,
        scorer=ScorerName.OPENCODE,
        recommendation=Recommendation.SKIP,
        score=20.0,
    )
    _decide(conn, job_id, "reject")

    unfiltered = build_report(conn)
    assert unfiltered.samples[0].scorer == "opencode"
    assert unfiltered.exact == 1

    heuristic = build_report(conn, scorer="heuristic")
    assert heuristic.samples[0].scorer == "heuristic"
    assert heuristic.comparable == 1
    assert heuristic.exact == 0


def test_scorer_without_evaluations_is_counted_as_without_evaluation(conn) -> None:
    job_id = _seed_job(conn, 1)
    _evaluate(
        conn,
        job_id,
        scorer=ScorerName.OPENCODE,
        recommendation=Recommendation.APPLY,
        score=88.0,
    )
    _decide(conn, job_id, "apply")

    report = build_report(conn, scorer="llm")

    assert report.feedback_total == 1
    assert report.matched == 0
    assert report.without_evaluation == 1
    assert report.comparable == 0


# ---------------------------------------------------------------------------
# Empty and partial corpora
# ---------------------------------------------------------------------------


def test_no_feedback_reports_zeros(conn) -> None:
    job_id = _seed_job(conn, 1)
    _evaluate(conn, job_id, recommendation=Recommendation.APPLY, score=88.0)

    report = build_report(conn)

    assert report.feedback_total == 0
    assert report.comparable == 0
    assert report.agreement_rate is None
    assert report.unresolved_total == 0
    assert report.without_evaluation == 0
    assert report.confusion["apply"] == {human: 0 for human in HUMAN_LABELS}


def test_feedback_without_a_valid_evaluation_is_counted_separately(conn) -> None:
    job_id = _seed_job(conn, 1)
    _decide(conn, job_id, "apply")

    report = build_report(conn)

    assert report.feedback_total == 1
    assert report.matched == 0
    assert report.without_evaluation == 1
    assert report.comparable == 0
    assert report.samples[0].resolution == "without_evaluation"


# ---------------------------------------------------------------------------
# Score distribution
# ---------------------------------------------------------------------------


def test_score_distribution_by_human_decision(conn) -> None:
    apply_scores = [55.0, 92.0]
    for index, score in enumerate(apply_scores):
        job_id = _seed_job(conn, index)
        _evaluate(conn, job_id, recommendation=Recommendation.APPLY, score=score)
        _decide(conn, job_id, "apply")

    missing_id = _seed_job(conn, 10)
    # A scorable evaluation with no overall_score is unresolved, never a
    # scoreless success: the persistence invariant forbids the latter.
    _evaluate(conn, missing_id, recommendation=Recommendation.NEED_MORE_INFO, score=None)
    _decide(conn, missing_id, "reject")

    report = build_report(conn)
    distribution = report.to_dict()["score_distribution"]

    assert distribution["apply"]["count"] == 2
    assert distribution["apply"]["with_score"] == 2
    assert distribution["apply"]["min"] == 55.0
    assert distribution["apply"]["max"] == 92.0
    assert distribution["apply"]["mean"] == pytest.approx(73.5)
    assert distribution["apply"]["buckets"]["50-59"] == 1
    assert distribution["apply"]["buckets"]["90-100"] == 1

    assert distribution["reject"]["count"] == 1
    assert distribution["reject"]["with_score"] == 0
    assert distribution["reject"]["mean"] is None


def test_unresolved_rows_still_appear_in_the_score_distribution(conn) -> None:
    job_id = _seed_job(conn, 1)
    _evaluate(conn, job_id, recommendation=Recommendation.NEED_MORE_INFO, score=45.0)
    _decide(conn, job_id, "consider")

    distribution = build_report(conn).to_dict()["score_distribution"]

    assert distribution["consider"]["count"] == 1
    assert distribution["consider"]["buckets"]["40-49"] == 1


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


def test_build_report_does_not_write_to_the_connection(conn) -> None:
    job_id = _seed_job(conn, 1)
    _evaluate(conn, job_id, recommendation=Recommendation.APPLY, score=88.0)
    _decide(conn, job_id, "apply")
    before = conn.total_changes

    build_report(conn)
    build_report(conn, scorer="opencode")

    assert conn.total_changes == before


def test_missing_schema_raises_a_clear_error() -> None:
    bare = connect(":memory:")  # no ``init_db``: no jobscout tables
    try:
        with pytest.raises(CalibrationError, match="not a jobscout database"):
            build_report(bare)
    finally:
        bare.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "calibration.sqlite3"
    _seed_file(path)
    return path


def test_cli_calibrate_text(db_file: Path, capsys) -> None:
    assert main(["--db", str(db_file), "calibrate"]) == EXIT_OK
    output = capsys.readouterr().out

    assert "calibration [all scorers]" in output
    assert "4 feedback row(s), 3 comparable, 1 unresolved" in output
    assert "agreement: 2/3 exact (66.7%)" in output
    assert "confusion (model -> human):" in output
    assert "need_more_info=1" in output
    assert "overall_score by human decision:" in output


def test_cli_calibrate_json(db_file: Path, capsys) -> None:
    assert main(["--db", str(db_file), "calibrate", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["scorer"] is None
    assert payload["feedback_total"] == 4
    assert payload["comparable"] == 3
    assert payload["agreement"]["exact"] == 2
    assert payload["unresolved"] == {"need_more_info": 1}
    assert payload["confusion"]["skip"]["reject"] == 1
    assert payload["score_distribution"]["apply"]["count"] == 1
    assert len(payload["samples"]) == 4


def test_cli_calibrate_scorer_flag_changes_the_comparison(
    db_file: Path, capsys
) -> None:
    assert main(["--db", str(db_file), "calibrate", "--scorer", "heuristic"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "calibration [heuristic]: 4 feedback row(s), 0 comparable" in output
    assert "4 without a valid evaluation" in output


def test_cli_calibrate_missing_database(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "absent" / "jobscout.sqlite3"
    assert main(["--db", str(missing), "calibrate"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "no database found" in err
    assert not missing.exists()


def test_cli_calibrate_empty_database(tmp_path: Path, capsys) -> None:
    path = tmp_path / "empty.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
    finally:
        conn.close()

    assert main(["--db", str(path), "calibrate"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "0 feedback row(s), 0 comparable" in output
    assert "agreement: no comparable samples" in output


def test_cli_calibrate_uninitialized_database(tmp_path: Path, capsys) -> None:
    path = tmp_path / "not-sqlite.sqlite3"
    path.write_text("", encoding="utf-8")

    assert main(["--db", str(path), "calibrate"]) == EXIT_ERROR
    assert "calibration error" in capsys.readouterr().err


def test_cli_calibrate_leaves_the_corpus_untouched(db_file: Path, capsys) -> None:
    conn = connect(db_file)
    try:
        jobs_before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        evaluations_before = evaluation_count(conn)
        feedback_before = feedback_count(conn)
        stored_before = get_feedback(conn, 1)
    finally:
        conn.close()

    assert main(["--db", str(db_file), "calibrate"]) == EXIT_OK
    capsys.readouterr()

    conn = connect(db_file)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs_before
        assert evaluation_count(conn) == evaluations_before
        assert feedback_count(conn) == feedback_before
        stored_after = get_feedback(conn, 1)
        assert stored_after is not None and stored_before is not None
        assert stored_after.decision == stored_before.decision
        assert stored_after.reason == stored_before.reason
    finally:
        conn.close()
