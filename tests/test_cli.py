"""CLI smoke tests.

Every command runs against a temporary database and the local fixture. The live
LinkedIn path is only exercised through ``--dry-run`` and the disabled-by-default
refusal, never with a real request.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import FIXTURES, no_network

from jobscout.cli import EXIT_ERROR, EXIT_FAIL_CLOSED, EXIT_OK, main
from jobscout.config import HARD_MAX_ITEMS, HARD_MAX_PAGES, Settings
from jobscout.db import (
    all_active_jobs,
    connect,
    evaluations_for_job,
    insert_detail_fetch,
    insert_evaluation,
    upsert_feedback,
)
from jobscout.models import (
    DetailFetchStatus,
    Evaluation,
    EvaluationStatus,
    FeedbackDecision,
    JobDetailFetch,
    JobFeedback,
    Recommendation,
)
from jobscout.scoring import effective_fingerprint
from jobscout.scoring.profile import load_profile

FIXTURE = FIXTURES / "linkedin_search_sample.json"
PROFILE = FIXTURES / "profile_test.yaml"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cli.sqlite3"


def test_init_db_accepts_global_and_subcommand_flags(db_path: Path, capsys) -> None:
    assert main(["--db", str(db_path), "init-db"]) == EXIT_OK
    assert main(["init-db", "--db", str(db_path)]) == EXIT_OK
    assert db_path.is_file()
    assert "initialized" in capsys.readouterr().out


def test_ingest_fixture_then_list_and_rank(db_path: Path, capsys) -> None:
    assert main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "4 new" in output and "2 duplicate" in output

    assert main(["--db", str(db_path), "list"]) == EXIT_OK
    listing = capsys.readouterr().out
    assert "Senior Backend Engineer" in listing
    assert "Platform Engineer" in listing

    assert main(["--db", str(db_path), "rank", "--top", "5"]) == EXIT_OK
    assert "no scored jobs yet" in capsys.readouterr().out


def test_list_json_output_is_machine_readable(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 4
    assert payload["jobs"][0]["job"]["title"]


def test_score_heuristic_then_rank_and_list(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert "scorer=heuristic" in output
    assert "recommendation=" in output
    assert "blockers:" in output
    assert "scored 3 job(s)" in output

    assert main(["--db", str(db_path), "rank", "--top", "2", "--json"]) == EXIT_OK
    ranked = json.loads(capsys.readouterr().out)
    assert ranked["count"] == 2
    assert ranked["jobs"][0]["score"] is not None
    assert ranked["jobs"][0]["score"] >= ranked["jobs"][1]["score"]

    assert main(["--db", str(db_path), "list", "--json", "--min-score", "0"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["count"] >= 1


def test_list_flags_scores_that_no_longer_match_the_input(
    db_path: Path, capsys
) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()

    assert (
        main(["--db", str(db_path), "--profile", str(PROFILE), "list", "--json"])
        == EXIT_OK
    )
    before = json.loads(capsys.readouterr().out)
    scored = [item for item in before["jobs"] if item["score"] is not None]
    assert scored and all(item["needs_rescore"] is False for item in scored)
    assert all(item["stage"] == "scored" for item in scored)
    stale_id = scored[0]["job"]["id"]

    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE jobs SET description = description || ' changed' WHERE id = ?",
            (stale_id,),
        )
        conn.commit()
    finally:
        conn.close()

    assert (
        main(["--db", str(db_path), "--profile", str(PROFILE), "list", "--json"])
        == EXIT_OK
    )
    after = {item["job"]["id"]: item for item in json.loads(capsys.readouterr().out)["jobs"]}
    assert after[stale_id]["needs_rescore"] is True
    assert after[stale_id]["stage"] == "needs_rescore"
    assert all(
        item["needs_rescore"] is False
        for job_id, item in after.items()
        if job_id != stale_id and item["score"] is not None
    )

    assert main(["--db", str(db_path), "--profile", str(PROFILE), "list"]) == EXIT_OK
    text = capsys.readouterr().out
    assert text.count("[needs re-score]") == 1
    stale_line = next(line for line in text.splitlines() if f"#{stale_id}" in line)
    assert "[needs re-score]" in stale_line


def test_list_with_a_missing_profile_still_works(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()

    exit_code = main(
        ["--db", str(db_path), "--profile", "does-not-exist.yaml", "list", "--json"]
    )
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["jobs"]
    assert all(item["needs_rescore"] is None for item in payload["jobs"])
    # Staleness is unknown without a profile, so no offer reads "needs_rescore".
    assert all(item["stage"] != "needs_rescore" for item in payload["jobs"])

    assert (
        main(["--db", str(db_path), "--profile", "does-not-exist.yaml", "list"])
        == EXIT_OK
    )
    assert "[needs re-score]" not in capsys.readouterr().out


def test_rank_hides_offers_with_imported_feedback(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()
    assert main(["--db", str(db_path), "rank", "--top", "5", "--json"]) == EXIT_OK
    ranked = json.loads(capsys.readouterr().out)
    decided = ranked["jobs"][0]["job"]["id"]

    # The human's imported decision is final: rank stops showing the offer.
    conn = connect(db_path)
    try:
        upsert_feedback(
            conn,
            JobFeedback(
                job_id=decided,
                decision=FeedbackDecision.REJECT,
                reason="not a fit",
                decision_updated_at="2026-10-18T10:00:00Z",
            ),
        )
    finally:
        conn.close()

    assert main(["--db", str(db_path), "rank", "--top", "5", "--json"]) == EXIT_OK
    after = json.loads(capsys.readouterr().out)
    shown = [item["job"]["id"] for item in after["jobs"]]
    assert decided not in shown
    assert after["count"] == len(ranked["jobs"]) - 1
    assert after["hidden_decided"] == 1

    assert main(["--db", str(db_path), "rank", "--top", "5"]) == EXIT_OK
    assert "already decided" in capsys.readouterr().out


def test_score_is_idempotent_without_rescore(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    args = [
        "--db",
        str(db_path),
        "--profile",
        str(PROFILE),
        "score",
        "--all",
        "--scorer",
        "heuristic",
    ]
    assert main(args) == EXIT_OK
    capsys.readouterr()
    assert main(args) == EXIT_OK
    assert "scored 0 job(s)" in capsys.readouterr().out

    rescore = [
        "--db",
        str(db_path),
        "--profile",
        str(PROFILE),
        "score",
        "--all",
        "--scorer",
        "heuristic",
        "--rescore",
    ]
    assert main(rescore) == EXIT_OK
    assert "scored 3 job(s)" in capsys.readouterr().out


def test_score_specific_job_and_unknown_job(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    job_id = json.loads(capsys.readouterr().out)["jobs"][0]["job"]["id"]

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--job-id",
                str(job_id),
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    assert f"job #{job_id}" in capsys.readouterr().out

    assert main(["--db", str(db_path), "--profile", str(PROFILE), "score", "--job-id", "999999"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "not found" in captured.err


def _senior_job_id(db_path: Path, capsys) -> int:
    """Return the fixture's clearly senior offer id (kept out of score runs)."""
    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    listing = json.loads(capsys.readouterr().out)
    return next(
        item["job"]["id"]
        for item in listing["jobs"]
        if item["job"]["title"] == "Senior Backend Engineer"
    )


def test_score_skips_senior_offers_before_the_limit(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    senior_id = _senior_job_id(db_path, capsys)

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--limit",
                "1",
                "--scorer",
                "heuristic",
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)

    # The filtered offer is removed before --limit, so it never takes the slot.
    assert payload["evaluated"] == 1
    assert payload["skipped_seniority"] == 1
    assert payload["evaluations"][0]["job_id"] != senior_id


def test_score_reports_skipped_seniority_in_text(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--limit",
                "1",
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    assert "skipped 1 offer(s) filtered by seniority" in capsys.readouterr().out


def test_score_rescore_skips_senior_offers_before_the_limit(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    senior_id = _senior_job_id(db_path, capsys)

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--rescore",
                "--limit",
                "2",
                "--scorer",
                "heuristic",
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["evaluated"] == 2
    assert payload["skipped_seniority"] == 1
    assert senior_id not in {item["job_id"] for item in payload["evaluations"]}


def test_score_job_id_forces_a_filtered_senior_offer(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    senior_id = _senior_job_id(db_path, capsys)

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--job-id",
                str(senior_id),
                "--scorer",
                "heuristic",
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)

    # An explicit --job-id is the owner's override: it is scored even when filtered.
    assert payload["evaluated"] == 1
    assert payload["skipped_seniority"] == 0
    assert payload["evaluations"][0]["job_id"] == senior_id


def _job_id_by_title(db_path: Path, capsys, title: str) -> int:
    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    listing = json.loads(capsys.readouterr().out)
    return next(
        item["job"]["id"]
        for item in listing["jobs"]
        if item["job"]["title"] == title
    )


def _mark_pending_details(db_path: Path, job_id: int) -> None:
    """Turn an offer into a search card: no description, partial content."""
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE jobs SET content_status = 'partial', description = NULL "
                "WHERE id = ?",
                (job_id,),
            )
    finally:
        conn.close()


def _record_detail_attempt(db_path: Path, job_id: int, *, status: str, http_status) -> None:
    conn = connect(db_path)
    try:
        insert_detail_fetch(
            conn,
            JobDetailFetch(
                job_id=job_id,
                source="linkedin",
                status=status,
                http_status=http_status,
                stored_at="2026-09-15T12:00:00Z",
            ),
        )
    finally:
        conn.close()


def _insert_extra_offer(db_path: Path, index: int) -> int:
    """Insert one more scoreable offer, oldest in the selection order."""
    conn = connect(db_path)
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    fingerprint, dedupe_key, source, title, company, location,
                    description, first_seen_at, last_seen_at, content_status,
                    content_hash, is_active
                ) VALUES (?, ?, 'linkedin', ?, 'Extra Co', 'Remote',
                          'Build and test Python services.',
                          '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'ok', '', 1)
                """,
                (f"fp-extra-{index}", f"dk-extra-{index}", f"Python Developer {index}"),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def _fake_status_sequence(statuses: list[str]):
    """Build a fake ``evaluate_job`` returning *statuses* in call order.

    Like the real scorer, every attempt is persisted, so a test can check which
    offers ended up with an evaluation row.
    """
    calls: list[int] = []

    def fake_evaluate_job(conn, job, profile, *, scorer_name, settings, **kwargs):
        if len(calls) >= len(statuses):
            raise AssertionError("unexpected extra scoring attempt")
        calls.append(int(job.id))
        status = statuses[len(calls) - 1]
        succeeded = status == EvaluationStatus.OK.value
        evaluation = Evaluation(
            job_id=int(job.id),
            scorer=scorer_name,
            status=status,
            overall_score=70.0 if succeeded else None,
            recommendation=(
                Recommendation.APPLY if succeeded else Recommendation.NEED_MORE_INFO
            ),
            confidence=0.7 if succeeded else 0.0,
            error="provider.no-route" if status == EvaluationStatus.ERROR.value else None,
            model=settings.opencode_model,
            input_fingerprint=f"fake-{status}-{job.id}",
        )
        insert_evaluation(conn, evaluation)
        return evaluation

    return fake_evaluate_job, calls


def _score_all(db_path: Path, capsys, *extra: str) -> dict:
    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--scorer",
                "heuristic",
                "--json",
                *extra,
            ]
        )
        == EXIT_OK
    )
    return json.loads(capsys.readouterr().out)


def test_score_all_skips_offers_without_details(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    pending_id = _job_id_by_title(db_path, capsys, "Platform Engineer")
    _mark_pending_details(db_path, pending_id)

    payload = _score_all(db_path, capsys)

    assert payload["skipped_pending_details"] == 1
    assert payload["evaluated"] == 2
    assert pending_id not in {item["job_id"] for item in payload["evaluations"]}


def test_score_all_scores_an_offer_after_two_failed_detail_attempts(
    db_path: Path, capsys
) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    pending_id = _job_id_by_title(db_path, capsys, "Platform Engineer")
    _mark_pending_details(db_path, pending_id)
    _record_detail_attempt(db_path, pending_id, status="failed", http_status=403)
    _record_detail_attempt(db_path, pending_id, status="failed", http_status=None)

    payload = _score_all(db_path, capsys)

    # Two real failures are the safety net: the offer is scored on its card.
    assert payload["skipped_pending_details"] == 0
    assert pending_id in {item["job_id"] for item in payload["evaluations"]}


def test_score_skipped_and_404_detail_rows_do_not_count_as_attempts(
    db_path: Path, capsys
) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    pending_id = _job_id_by_title(db_path, capsys, "Platform Engineer")
    _mark_pending_details(db_path, pending_id)
    _record_detail_attempt(db_path, pending_id, status="skipped", http_status=None)
    _record_detail_attempt(db_path, pending_id, status="skipped", http_status=None)
    _record_detail_attempt(db_path, pending_id, status="failed", http_status=404)
    _record_detail_attempt(db_path, pending_id, status="failed", http_status=410)

    payload = _score_all(db_path, capsys)

    assert payload["skipped_pending_details"] == 1
    assert pending_id not in {item["job_id"] for item in payload["evaluations"]}


def test_score_job_id_forces_an_offer_waiting_for_details(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    pending_id = _job_id_by_title(db_path, capsys, "Platform Engineer")
    _mark_pending_details(db_path, pending_id)

    # Like seniority, the explicit --job-id is the owner's override: the offer is
    # attempted even without details. The heuristic cannot score a card, so the
    # run reports its usual "nothing succeeded" exit code.
    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--job-id",
            str(pending_id),
            "--scorer",
            "heuristic",
            "--json",
        ]
    )
    assert exit_code == EXIT_ERROR
    payload = json.loads(capsys.readouterr().out)

    assert payload["evaluated"] == 1
    assert payload["skipped_pending_details"] == 0
    assert payload["evaluations"][0]["job_id"] == pending_id


def test_score_pending_details_gate_runs_before_the_limit(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    # id 4 is the newest offer, so it is first in the selection order.
    newest_id = _job_id_by_title(db_path, capsys, "Junior Frontend Developer")
    _mark_pending_details(db_path, newest_id)

    payload = _score_all(db_path, capsys, "--limit", "1")

    # The pending offer is removed before --limit, so it never takes the slot.
    assert payload["evaluated"] == 1
    assert payload["skipped_pending_details"] == 1
    assert payload["evaluations"][0]["job_id"] != newest_id


def test_score_rescore_is_also_gated_by_pending_details(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    pending_id = _job_id_by_title(db_path, capsys, "Platform Engineer")
    _mark_pending_details(db_path, pending_id)

    payload = _score_all(db_path, capsys, "--rescore")

    assert payload["skipped_pending_details"] == 1
    assert pending_id not in {item["job_id"] for item in payload["evaluations"]}


def test_score_reports_skipped_pending_details_in_text(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    pending_id = _job_id_by_title(db_path, capsys, "Platform Engineer")
    _mark_pending_details(db_path, pending_id)

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert "skipped 1 offer(s) waiting for details" in output


def test_score_json_reports_zero_skipped_pending_details(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()

    payload = _score_all(db_path, capsys)

    # Always present, so a consumer never has to guess.
    assert payload["skipped_pending_details"] == 0


def _score_opencode(db_path: Path, capsys, *extra: str) -> tuple[int, dict]:
    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--all",
            "--scorer",
            "opencode",
            "--json",
            *extra,
        ]
    )
    return exit_code, json.loads(capsys.readouterr().out)


def test_score_stops_after_three_consecutive_provider_errors(
    db_path: Path, capsys, monkeypatch
) -> None:
    import jobscout.cli as cli

    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    untouched_id = _insert_extra_offer(db_path, 1)

    fake_evaluate_job, calls = _fake_status_sequence(["error", "error", "error"])
    monkeypatch.setattr(cli, "evaluate_job", fake_evaluate_job)

    exit_code, payload = _score_opencode(db_path, capsys)

    # Four offers are selected; the provider is down after the third attempt.
    assert exit_code == EXIT_ERROR
    assert payload["stopped_early"] is True
    assert payload["not_attempted"] == 1
    assert payload["evaluated"] == 3
    assert [item["status"] for item in payload["evaluations"]] == ["error"] * 3
    assert calls == [item["job_id"] for item in payload["evaluations"]]
    assert untouched_id not in calls

    # The three attempts keep their rows; the offer left for the next run has
    # none, so the next score run picks it up untouched.
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT job_id FROM evaluations WHERE job_id = ?", (untouched_id,)
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def test_score_does_not_stop_when_errors_are_not_consecutive(
    db_path: Path, capsys, monkeypatch
) -> None:
    import jobscout.cli as cli

    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    _insert_extra_offer(db_path, 1)

    fake_evaluate_job, calls = _fake_status_sequence(["error", "ok", "error", "error"])
    monkeypatch.setattr(cli, "evaluate_job", fake_evaluate_job)

    exit_code, payload = _score_opencode(db_path, capsys)

    # A success breaks the streak, so the batch runs to the end.
    assert exit_code == EXIT_OK
    assert payload["evaluated"] == 4
    assert payload["succeeded"] == 1
    assert payload["stopped_early"] is False
    assert payload["not_attempted"] == 0
    assert len(calls) == 4


def test_score_parse_error_resets_the_consecutive_error_counter(
    db_path: Path, capsys, monkeypatch
) -> None:
    import jobscout.cli as cli

    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    _insert_extra_offer(db_path, 1)
    _insert_extra_offer(db_path, 2)

    # A parse_error is the model's output, not the provider: it breaks the
    # streak. Counting it would stop the run after the third offer.
    fake_evaluate_job, calls = _fake_status_sequence(
        ["error", "error", "parse_error", "error", "error"]
    )
    monkeypatch.setattr(cli, "evaluate_job", fake_evaluate_job)

    exit_code, payload = _score_opencode(db_path, capsys)

    assert exit_code == EXIT_ERROR  # nothing succeeded: the usual exit code
    assert payload["evaluated"] == 5
    assert payload["stopped_early"] is False
    assert payload["not_attempted"] == 0
    assert payload["evaluations"][2]["status"] == "parse_error"
    assert len(calls) == 5


def test_score_does_not_count_a_primary_error_rescued_by_the_fallback(
    db_path: Path, capsys, monkeypatch
) -> None:
    import jobscout.cli as cli

    monkeypatch.setenv("JOBSCOUT_OPENCODE_FALLBACK_MODEL", FALLBACK_MODEL)
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    _insert_extra_offer(db_path, 1)

    # Every offer fails on the primary model and is retried with the fallback,
    # which succeeds: the final status is ok, so the streak never grows.
    fake_evaluate_job, calls = _fake_opencode_evaluate()
    monkeypatch.setattr(cli, "evaluate_job", fake_evaluate_job)

    exit_code, payload = _score_opencode(db_path, capsys)

    assert exit_code == EXIT_OK
    assert payload["evaluated"] == 4
    assert payload["succeeded"] == 4
    assert payload["stopped_early"] is False
    assert payload["not_attempted"] == 0
    # The switch is sticky: the first offer is tried on the primary, retried on
    # the fallback, and the rest never touch the primary at all.
    primary_calls = [model for _, model in calls if model == "opencode-go/deepseek-v4.1-flash#low"]
    assert len(primary_calls) == 1
    assert len(calls) == 5


def test_score_text_reports_the_stopped_run(db_path: Path, capsys, monkeypatch) -> None:
    import jobscout.cli as cli

    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    _insert_extra_offer(db_path, 1)

    fake_evaluate_job, _calls = _fake_status_sequence(["error", "error", "error"])
    monkeypatch.setattr(cli, "evaluate_job", fake_evaluate_job)

    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--all",
            "--scorer",
            "opencode",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == EXIT_ERROR
    assert (
        "stopped after 3 consecutive provider errors: 3 of 4 offer(s) attempted, "
        "1 left for the next run"
    ) in output


def test_score_with_llm_without_configuration_fails_cleanly(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(["--db", str(db_path), "--profile", str(PROFILE), "score", "--all", "--scorer", "llm"])
    assert exit_code == EXIT_ERROR
    output = capsys.readouterr().out
    assert "JOBSCOUT_LLM_BASE_URL" in output


def test_score_with_opencode_scorer_uses_mocked_subprocess(
    db_path: Path, capsys, monkeypatch, tmp_path: Path
) -> None:
    """The CLI wiring works end to end without starting the real CLI."""
    from conftest import fake_popen_factory
    from jobscout.scoring import opencode as opencode_module

    payload = (FIXTURES / "llm_response_valid.json").read_text(encoding="utf-8")
    event = json.dumps(
        {"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": payload}}
    )

    factory, created = fake_popen_factory(stdout_text=event)
    monkeypatch.setattr(opencode_module.subprocess, "Popen", factory)
    fake_exe = tmp_path / "opencode-fake"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("JOBSCOUT_OPENCODE_COMMAND", str(fake_exe))

    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--all",
            "--scorer",
            "opencode",
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "scorer=opencode" in output
    assert "scored 3 job(s)" in output
    # The default runner was used: an argument array with no shell.
    assert created and created[0].kwargs.get("shell") is False


def test_score_flags_override_the_opencode_model_and_effort(
    db_path: Path, capsys, monkeypatch, tmp_path: Path
) -> None:
    from conftest import fake_popen_factory
    from jobscout.scoring import opencode as opencode_module
    from jobscout.scoring.opencode import OPENCODE_PROMPT_VERSION

    payload = (FIXTURES / "llm_response_valid.json").read_text(encoding="utf-8")
    event = json.dumps(
        {"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": payload}}
    )

    factory, created = fake_popen_factory(stdout_text=event)
    monkeypatch.setattr(opencode_module.subprocess, "Popen", factory)
    fake_exe = tmp_path / "opencode-fake"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("JOBSCOUT_OPENCODE_COMMAND", str(fake_exe))

    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--job-id",
            "1",
            "--scorer",
            "opencode",
            "--scorer-model",
            "foo/bar",
            "--effort",
            "high",
        ]
    )
    capsys.readouterr()

    assert exit_code == EXIT_OK
    argv = created[0].argv
    assert argv[argv.index("--model") + 1] == "foo/bar#high"
    conn = connect(db_path)
    try:
        evaluations = evaluations_for_job(conn, 1)
    finally:
        conn.close()
    assert evaluations[0].prompt_version == f"{OPENCODE_PROMPT_VERSION}+high"


def test_score_with_empty_effort_sends_no_variant(
    db_path: Path, capsys, monkeypatch, tmp_path: Path
) -> None:
    from conftest import fake_popen_factory
    from jobscout.scoring import opencode as opencode_module

    payload = (FIXTURES / "llm_response_valid.json").read_text(encoding="utf-8")
    event = json.dumps(
        {"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": payload}}
    )

    factory, created = fake_popen_factory(stdout_text=event)
    monkeypatch.setattr(opencode_module.subprocess, "Popen", factory)
    fake_exe = tmp_path / "opencode-fake"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("JOBSCOUT_OPENCODE_COMMAND", str(fake_exe))

    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--job-id",
            "1",
            "--scorer",
            "opencode",
            "--effort",
            "",
        ]
    )
    capsys.readouterr()

    assert exit_code == EXIT_OK
    argv = created[0].argv
    assert argv[argv.index("--model") + 1] == "opencode-go/deepseek-v4.1-flash"


def test_score_model_and_effort_flags_require_the_opencode_scorer(
    db_path: Path, capsys
) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()

    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--job-id",
            "1",
            "--scorer",
            "heuristic",
            "--scorer-model",
            "foo/bar",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == EXIT_ERROR
    assert "only apply to the opencode scorer" in captured.err
    assert "scored " not in captured.out


def test_score_with_opencode_scorer_missing_cli_fails_cleanly(
    db_path: Path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    monkeypatch.setenv("JOBSCOUT_OPENCODE_COMMAND", "jobscout-definitely-not-installed-xyz")
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--all",
            "--scorer",
            "opencode",
        ]
    )
    assert exit_code == EXIT_ERROR
    output = capsys.readouterr().out
    assert "OpenCode CLI" in output
    assert "auth login" in output


FALLBACK_MODEL = "opencode-go/backup#high"


def _fake_opencode_evaluate(primary_error: bool = True):
    """Build a fake ``evaluate_job`` that fails on the primary model only."""
    calls: list[tuple[int, str]] = []

    def fake_evaluate_job(conn, job, profile, *, scorer_name, settings, **kwargs):
        calls.append((int(job.id), f"{settings.opencode_model}#{settings.opencode_variant}"))
        if primary_error and settings.opencode_model == "opencode-go/deepseek-v4.1-flash":
            return Evaluation(
                job_id=int(job.id),
                scorer=scorer_name,
                status=EvaluationStatus.ERROR,
                error="provider.no-route",
                recommendation=Recommendation.NEED_MORE_INFO,
                model=settings.opencode_model,
                input_fingerprint="primary-fingerprint",
            )
        return Evaluation(
            job_id=int(job.id),
            scorer=scorer_name,
            status=EvaluationStatus.OK,
            overall_score=80.0,
            recommendation=Recommendation.APPLY,
            confidence=0.8,
            model=settings.opencode_model,
            input_fingerprint="fallback-fingerprint",
        )

    return fake_evaluate_job, calls


def test_score_switches_to_fallback_and_keeps_it_for_the_rest_of_the_run(
    db_path: Path, capsys, monkeypatch
) -> None:
    import jobscout.cli as cli

    monkeypatch.setenv("JOBSCOUT_OPENCODE_FALLBACK_MODEL", FALLBACK_MODEL)
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()

    fake_evaluate_job, calls = _fake_opencode_evaluate()
    monkeypatch.setattr(cli, "evaluate_job", fake_evaluate_job)

    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--all",
            "--scorer",
            "opencode",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == EXIT_OK
    assert payload["fallback_model"] == FALLBACK_MODEL
    assert payload["succeeded"] == 3
    assert "primary model opencode-go/deepseek-v4.1-flash failed" in captured.err
    assert f"using fallback {FALLBACK_MODEL} for the rest of this run" in captured.err
    # The primary is attempted exactly once; every later offer (including the
    # failed one, re-evaluated) uses the fallback model.
    primary_calls = [model for _, model in calls if model == "opencode-go/deepseek-v4.1-flash#low"]
    assert len(primary_calls) == 1
    assert [model for _, model in calls].count(FALLBACK_MODEL) == 3


def test_fallback_settings_size_their_timeout_from_the_fallback_variant() -> None:
    import jobscout.cli as cli

    settings = Settings.from_env(env={}, dotenv_path=None)
    fallback = cli._fallback_settings(settings, FALLBACK_MODEL)
    assert fallback.opencode_timeout_seconds is None
    assert fallback.effective_opencode_timeout_seconds == 360.0

    explicit = settings.with_overrides(opencode_timeout_seconds=42.0)
    fallback = cli._fallback_settings(explicit, FALLBACK_MODEL)
    assert fallback.effective_opencode_timeout_seconds == 42.0


def test_fallback_skips_offers_already_scored_with_the_fallback_input(
    db_path: Path, capsys, monkeypatch
) -> None:
    import jobscout.cli as cli

    monkeypatch.setenv("JOBSCOUT_OPENCODE_FALLBACK_MODEL", FALLBACK_MODEL)
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()

    settings = Settings.from_env(
        env={"JOBSCOUT_OPENCODE_FALLBACK_MODEL": FALLBACK_MODEL}, dotenv_path=None
    ).with_overrides(db_path=db_path, profile_path=PROFILE)
    profile = load_profile(PROFILE)
    fallback_settings = cli._fallback_settings(settings, FALLBACK_MODEL)

    conn = connect(db_path)
    try:
        jobs = all_active_jobs(conn)
        first, already_scored = jobs[0], jobs[1]
        fingerprint = effective_fingerprint(
            "opencode", already_scored, profile, fallback_settings
        )
        insert_evaluation(
            conn,
            Evaluation(
                job_id=int(already_scored.id),
                scorer="opencode",
                status=EvaluationStatus.OK,
                overall_score=75.0,
                recommendation=Recommendation.CONSIDER,
                model=fallback_settings.opencode_model,
                input_fingerprint=fingerprint,
            ),
        )
    finally:
        conn.close()

    fake_evaluate_job, calls = _fake_opencode_evaluate()
    monkeypatch.setattr(cli, "evaluate_job", fake_evaluate_job)

    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--job-id",
            str(first.id),
            "--job-id",
            str(already_scored.id),
            "--scorer",
            "opencode",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "scored 1 job(s), 1 succeeded, 0 failed" in output
    assert [job_id for job_id, _ in calls] == [int(first.id), int(first.id)]
    assert all(model == FALLBACK_MODEL for _, model in calls if model != "opencode-go/deepseek-v4.1-flash#low")


def test_score_without_a_fallback_keeps_primary_failures(
    db_path: Path, capsys, monkeypatch
) -> None:
    import jobscout.cli as cli

    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()

    fake_evaluate_job, calls = _fake_opencode_evaluate()
    monkeypatch.setattr(cli, "evaluate_job", fake_evaluate_job)

    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--all",
            "--scorer",
            "opencode",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == EXIT_ERROR
    assert payload["fallback_model"] is None
    assert payload["succeeded"] == 0
    assert all(model == "opencode-go/deepseek-v4.1-flash#low" for _, model in calls)
    assert "using fallback" not in captured.err


def test_score_with_a_missing_profile_reports_an_error(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(
        ["--db", str(db_path), "--profile", "does-not-exist.yaml", "score", "--all"]
    )
    assert exit_code == EXIT_ERROR
    assert "profile error" in capsys.readouterr().err


def test_search_linkedin_refuses_without_confirmation(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    exit_code = main(["--db", str(db_path), "search-linkedin", "--keywords", "python"])
    assert exit_code == EXIT_FAIL_CLOSED
    err = capsys.readouterr().err
    assert "--confirm-network" in err
    assert "--dry-run" in err
    # Nothing was recorded because no run happened.
    assert main(["--db", str(db_path), "runs", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["runs"] == []


def test_search_linkedin_dry_run_needs_no_network(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    exit_code = main(
        [
            "--db",
            str(db_path),
            "search-linkedin",
            "--keywords",
            "senior backend engineer",
            "--location",
            "Buenos Aires",
            "--max-pages",
            "2",
            "--dry-run",
        ]
    )
    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "dry run" in output
    assert output.count("seeMoreJobPostings") == 2
    # The guest endpoint serves 10 cards per page, so page 2 starts at 10.
    assert "start=0" in output and "start=10" in output
    assert "start=25" not in output


def test_search_linkedin_dry_run_respects_settings_bounds(db_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("JOBSCOUT_MAX_PAGES", "3")
    monkeypatch.setenv("JOBSCOUT_DB_PATH", str(db_path))
    assert main(["search-linkedin", "--keywords", "python", "--dry-run", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["planned_urls"]) == 3


def test_search_linkedin_dry_run_enforces_hard_caps(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    # Exactly at the cap: the dry run plans that many requests.
    assert (
        main(
            [
                "--db",
                str(db_path),
                "search-linkedin",
                "--keywords",
                "python",
                "--max-pages",
                str(HARD_MAX_PAGES),
                "--dry-run",
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["planned_urls"]) == HARD_MAX_PAGES

    # Above the cap: refuse clearly instead of planning an unbounded run.
    assert (
        main(
            [
                "--db",
                str(db_path),
                "search-linkedin",
                "--keywords",
                "python",
                "--max-pages",
                str(HARD_MAX_PAGES + 1),
                "--dry-run",
            ]
        )
        == EXIT_ERROR
    )
    assert "absolute upper bound" in capsys.readouterr().err

    assert (
        main(
            [
                "--db",
                str(db_path),
                "search-linkedin",
                "--keywords",
                "python",
                "--max-items",
                str(HARD_MAX_ITEMS + 1),
                "--dry-run",
            ]
        )
        == EXIT_ERROR
    )
    assert "absolute upper bound" in capsys.readouterr().err


def test_runs_command_reports_counts(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert main(["--db", str(db_path), "runs", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["jobs"] == 4
    assert payload["evaluations"] == 0
    assert payload["runs"][0]["status"] == "completed"


def test_missing_command_is_a_usage_error(db_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--db", str(db_path)])


def test_score_requires_a_selection(db_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--db", str(db_path), "score"])


def test_notify_is_disabled_by_default_and_creates_no_database(
    db_path: Path, capsys
) -> None:
    """The opt-in default reads nothing, sends nothing, and writes no file."""
    assert main(["--db", str(db_path), "notify"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "disabled" in output
    assert not db_path.exists()

    assert main(["--db", str(db_path), "notify", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is False
    assert payload["count"] == 0
    assert not db_path.exists()
