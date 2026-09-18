"""Feedback import tests: validation, idempotency, staleness, and CLI wiring.

Everything runs offline against temporary SQLite databases and temporary export
files. The import is a local file read plus local SQLite writes, so no test here
touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.dashboard import project_key
from jobscout.db import (
    SCHEMA_VERSION,
    connect,
    feedback_count,
    get_feedback,
    init_db,
    insert_evaluation,
    list_feedback,
    store_posting,
)
from jobscout.feedback import (
    ACTIVE_DECISIONS,
    DECISIONS,
    LEGACY_DECISIONS,
    FeedbackError,
    import_feedback,
    import_feedback_document,
    load_feedback_export,
    validate_feedback_export,
)
from jobscout.models import Evaluation, JobPosting, Recommendation, ScorerName
from jobscout.normalize import finalize_posting

PROJECT = "abc123def456"
APPLY_AT = "2026-09-16T14:00:00.000Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _posting(**overrides) -> JobPosting:
    payload = {
        "source": "linkedin",
        "title": "Senior Backend Engineer",
        "company": "Acme Data Inc.",
        "url": "https://www.linkedin.com/jobs/view/3901234567/",
        "source_job_id": "3901234567",
        "description": "Python, SQL, PostgreSQL.",
    }
    payload.update(overrides)
    return finalize_posting(JobPosting(**payload))


def _seed(path: Path, *, jobs: int = 3) -> list[int]:
    """Create a database with ``jobs`` distinct, scored jobs."""
    conn = connect(path)
    try:
        init_db(conn)
        ids: list[int] = []
        for index in range(jobs):
            job, _ = store_posting(
                conn,
                _posting(
                    title=f"Role {index}",
                    company=f"Company {index}",
                    source_job_id=f"39012345{index:02d}",
                    url=f"https://www.linkedin.com/jobs/view/39012345{index:02d}/role",
                ),
                now="2026-09-15T10:00:00Z",
            )
            insert_evaluation(
                conn,
                Evaluation(
                    job_id=int(job.id),
                    scorer=ScorerName.LLM,
                    overall_score=70.0 + index,
                    recommendation=Recommendation.APPLY,
                    confidence=0.7,
                ),
            )
            ids.append(int(job.id))
        return ids
    finally:
        conn.close()


def _export(*entries: dict, **overrides) -> dict:
    document = {
        "schema": "jobscout.feedback.v1",
        "project_id": PROJECT,
        "exported_at": "2026-09-16T15:12:04.064Z",
        "source": "jobscout-dashboard",
        "note": "Exported locally.",
        "feedback": {
            str(entry["job_id"]): {key: value for key, value in entry.items() if key != "job_id"}
            for entry in entries
        },
    }
    document.update(overrides)
    return document


def _write_export(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "feedback.sqlite3"
    _seed(path)
    return path


@pytest.fixture
def project_id(db_file: Path) -> str:
    """The project id the CLI derives from ``db_file``."""
    return project_key(db_file)


@pytest.fixture
def export_file(db_file: Path, project_id: str, tmp_path: Path) -> Path:
    return _write_export(
        tmp_path / "jobscout-feedback.json",
        _export(
            {"job_id": 1, "decision": "apply", "reason": "Great fit", "updated_at": APPLY_AT},
            {
                "job_id": 2,
                "decision": "consider",
                "reason": "Remote only",
                "updated_at": "2026-09-16T14:05:00.500Z",
            },
            {
                "job_id": 3,
                "decision": "",
                "reason": "Undecided note",
                "updated_at": "2026-09-16T14:06:00.000Z",
            },
            project_id=project_id,
        ),
    )


def _import(db_file: Path, export_file: Path, **kwargs):
    conn = connect(db_file)
    try:
        init_db(conn)
        return import_feedback(conn, export_file, **kwargs)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_human_decision_is_stored_next_to_the_model_recommendation(db_file: Path) -> None:
    _import(db_file, _write_export(db_file.parent / "e.json", _export(
        {"job_id": 1, "decision": "reject", "reason": "Different stack", "updated_at": APPLY_AT}
    )))

    conn = connect(db_file)
    try:
        feedback = get_feedback(conn, 1)
        assert feedback is not None
        assert feedback.decision == "reject"
        assert feedback.reason == "Different stack"
        assert feedback.project_id == PROJECT
        assert feedback.source == "jobscout-dashboard"
        # The model recommendation is untouched: two signals, two tables.
        recommendation = conn.execute(
            "SELECT recommendation FROM evaluations WHERE job_id = 1"
        ).fetchone()[0]
        assert recommendation == "apply"
    finally:
        conn.close()


def test_schema_version_matches_the_stored_database(conn) -> None:
    assert init_db(conn) == SCHEMA_VERSION
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "job_feedback" in tables


def test_active_vocabulary_is_binary_while_consider_stays_importable() -> None:
    """New decisions are apply/reject; the legacy value is still accepted."""
    assert ACTIVE_DECISIONS == ("apply", "reject")
    assert LEGACY_DECISIONS == ("consider",)
    # The importer validates against the full vocabulary, so a pre-binary export
    # is still valid even though the active UI can never create ``consider``.
    assert set(DECISIONS) == {"apply", "consider", "reject"}


def test_legacy_consider_row_is_imported_and_preserved_verbatim(db_file: Path) -> None:
    outcome = _import(db_file, _write_export(db_file.parent / "legacy.json", _export(
        {
            "job_id": 1,
            "decision": "consider",
            "reason": "Historic choice",
            "updated_at": APPLY_AT,
        }
    )))
    assert outcome.imported == 1
    assert outcome.rejected == []

    conn = connect(db_file)
    try:
        stored = get_feedback(conn, 1)
        assert stored is not None
        # The legacy value is stored exactly as exported, never converted.
        assert stored.decision == "consider"
        assert stored.reason == "Historic choice"
    finally:
        conn.close()


def test_legacy_consider_can_be_replaced_by_an_active_binary_decision(db_file: Path) -> None:
    """A newer apply/reject replaces the legacy value; the history is not silent."""
    _import(db_file, _write_export(db_file.parent / "legacy.json", _export(
        {"job_id": 1, "decision": "consider", "reason": "Historic", "updated_at": APPLY_AT}
    )))
    outcome = _import(db_file, _write_export(db_file.parent / "active.json", _export(
        {
            "job_id": 1,
            "decision": "reject",
            "reason": "Reconsidered",
            "updated_at": "2026-09-16T14:30:00.000Z",
        }
    )))
    assert outcome.updated == 1

    conn = connect(db_file)
    try:
        stored = get_feedback(conn, 1)
        assert stored is not None
        assert stored.decision == "reject"
        assert stored.reason == "Reconsidered"
    finally:
        conn.close()


def test_deleting_a_job_removes_its_feedback(db_file: Path) -> None:
    _import(db_file, _write_export(db_file.parent / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT}
    )))
    conn = connect(db_file)
    try:
        with conn:
            conn.execute("DELETE FROM jobs WHERE id = 1")
        assert feedback_count(conn) == 0
    finally:
        conn.close()


def test_one_row_per_job_and_listing_orders_newest_first(db_file: Path) -> None:
    _import(db_file, _write_export(db_file.parent / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": "2026-09-16T14:00:00.000Z"},
        {"job_id": 2, "decision": "consider", "updated_at": "2026-09-16T15:00:00.000Z"},
    )))

    conn = connect(db_file)
    try:
        assert feedback_count(conn) == 2
        assert [item.job_id for item in list_feedback(conn)] == [2, 1]
        assert [item.job_id for item in list_feedback(conn, limit=1)] == [2]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Import semantics
# ---------------------------------------------------------------------------


def test_import_is_idempotent(
    db_file: Path, export_file: Path, project_id: str
) -> None:
    first = _import(db_file, export_file, expected_project_id=project_id)
    assert first.imported == 2
    assert first.skipped_no_decision == 1
    assert first.rejected == []

    second = _import(db_file, export_file, expected_project_id=project_id)
    assert second.imported == 0
    assert second.updated == 0
    assert second.unchanged == 2
    assert second.job_ids == []

    conn = connect(db_file)
    try:
        assert feedback_count(conn) == 2
    finally:
        conn.close()


def test_newer_decision_replaces_and_older_decision_is_stale(db_file: Path) -> None:
    _import(db_file, _write_export(db_file.parent / "a.json", _export(
        {"job_id": 1, "decision": "apply", "reason": "First", "updated_at": APPLY_AT}
    )))

    newer = _import(db_file, _write_export(db_file.parent / "b.json", _export(
        {
            "job_id": 1,
            "decision": "consider",
            "reason": "Second",
            "updated_at": "2026-09-16T14:00:01.000Z",
        }
    )))
    assert newer.updated == 1
    assert newer.job_ids == [1]

    older = _import(db_file, _write_export(db_file.parent / "c.json", _export(
        {
            "job_id": 1,
            "decision": "reject",
            "reason": "Stale",
            "updated_at": "2026-09-16T13:59:59.000Z",
        }
    )))
    assert older.stale == 1
    assert older.updated == 0
    assert older.imported == 0

    conn = connect(db_file)
    try:
        stored = get_feedback(conn, 1)
        assert stored is not None
        assert stored.decision == "consider"
        assert stored.reason == "Second"
    finally:
        conn.close()


def test_sub_second_timestamps_are_not_collapsed(db_file: Path) -> None:
    """Two edits inside the same second stay distinguishable."""
    _import(db_file, _write_export(db_file.parent / "a.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": "2026-09-16T14:00:00.431Z"}
    )))
    outcome = _import(db_file, _write_export(db_file.parent / "b.json", _export(
        {"job_id": 1, "decision": "consider", "updated_at": "2026-09-16T14:00:00.999Z"}
    )))
    assert outcome.updated == 1

    conn = connect(db_file)
    try:
        stored = get_feedback(conn, 1)
        assert stored is not None
        assert stored.decision == "consider"
        assert stored.decision_updated_at == "2026-09-16T14:00:00.999000Z"
    finally:
        conn.close()


def test_untimestamped_entry_never_replaces_a_timestamped_decision(db_file: Path) -> None:
    _import(db_file, _write_export(db_file.parent / "a.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT}
    )))
    outcome = _import(db_file, _write_export(db_file.parent / "b.json", _export(
        {"job_id": 1, "decision": "reject"}
    )))
    assert outcome.stale == 1
    assert outcome.updated == 0

    conn = connect(db_file)
    try:
        stored = get_feedback(conn, 1)
        assert stored is not None
        assert stored.decision == "apply"
    finally:
        conn.close()


def test_timestamped_entry_replaces_an_untimestamped_decision(db_file: Path) -> None:
    _import(db_file, _write_export(db_file.parent / "a.json", _export(
        {"job_id": 1, "decision": "apply"}
    )))
    outcome = _import(db_file, _write_export(db_file.parent / "b.json", _export(
        {"job_id": 1, "decision": "consider", "updated_at": APPLY_AT}
    )))
    assert outcome.updated == 1

    conn = connect(db_file)
    try:
        stored = get_feedback(conn, 1)
        assert stored is not None
        assert stored.decision == "consider"
    finally:
        conn.close()


def test_entry_without_a_decision_is_skipped_not_stored(db_file: Path) -> None:
    outcome = _import(db_file, _write_export(db_file.parent / "e.json", _export(
        {"job_id": 1, "decision": "", "reason": "Not sure yet"},
        {"job_id": 2, "decision": "   ", "updated_at": APPLY_AT},
    )))
    assert outcome.skipped_no_decision == 2
    assert outcome.imported == 0
    assert outcome.rejected == []
    assert outcome.job_ids == []

    conn = connect(db_file)
    try:
        assert feedback_count(conn) == 0
        assert get_feedback(conn, 1) is None
    finally:
        conn.close()


def test_empty_decision_does_not_delete_a_stored_decision(db_file: Path) -> None:
    _import(db_file, _write_export(db_file.parent / "a.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT}
    )))
    _import(db_file, _write_export(db_file.parent / "b.json", _export(
        {"job_id": 1, "decision": "", "updated_at": "2026-09-17T00:00:00.000Z"}
    )))
    conn = connect(db_file)
    try:
        stored = get_feedback(conn, 1)
        assert stored is not None and stored.decision == "apply"
    finally:
        conn.close()


def test_blank_reason_is_stored_as_null_and_text_is_preserved(db_file: Path) -> None:
    _import(db_file, _write_export(db_file.parent / "e.json", _export(
        {"job_id": 1, "decision": "apply", "reason": "   ", "updated_at": APPLY_AT},
        {"job_id": 2, "decision": "apply", "reason": "Keep my spacing ", "updated_at": APPLY_AT},
    )))
    conn = connect(db_file)
    try:
        assert get_feedback(conn, 1).reason is None
        assert get_feedback(conn, 2).reason == "Keep my spacing "
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry-level rejections
# ---------------------------------------------------------------------------


def test_unknown_job_id_is_rejected_and_valid_entries_still_import(db_file: Path) -> None:
    outcome = _import(db_file, _write_export(db_file.parent / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT},
        {"job_id": 999999, "decision": "apply", "updated_at": APPLY_AT},
    )))
    assert outcome.imported == 1
    assert len(outcome.rejected) == 1
    assert outcome.rejected[0].job_id == "999999"
    assert "not found" in outcome.rejected[0].reason

    conn = connect(db_file)
    try:
        assert get_feedback(conn, 1) is not None
        assert feedback_count(conn) == 1
    finally:
        conn.close()


def test_invalid_decision_is_rejected(db_file: Path) -> None:
    outcome = _import(db_file, _write_export(db_file.parent / "e.json", _export(
        {"job_id": 1, "decision": "maybe", "updated_at": APPLY_AT},
        {"job_id": 2, "decision": 42, "updated_at": APPLY_AT},
    )))
    assert outcome.imported == 0
    assert len(outcome.rejected) == 2
    assert "unknown decision" in outcome.rejected[0].reason
    assert "must be a string" in outcome.rejected[1].reason


def test_bad_timestamp_is_rejected(db_file: Path) -> None:
    outcome = _import(db_file, _write_export(db_file.parent / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": "yesterday-ish"},
        {"job_id": 2, "decision": "apply", "updated_at": ""},
    )))
    assert outcome.imported == 0
    assert len(outcome.rejected) == 2
    assert "not a valid ISO-8601 timestamp" in outcome.rejected[0].reason


def test_non_object_entry_and_bad_job_id_are_rejected(db_file: Path) -> None:
    document = _export({"job_id": 1, "decision": "apply", "updated_at": APPLY_AT})
    document["feedback"]["2"] = "not an object"
    document["feedback"]["not-a-number"] = {"decision": "apply", "updated_at": APPLY_AT}
    document["feedback"]["0"] = {"decision": "apply", "updated_at": APPLY_AT}

    outcome = _import(db_file, _write_export(db_file.parent / "e.json", document))
    assert outcome.imported == 1
    reasons = {rejection.job_id: rejection.reason for rejection in outcome.rejected}
    assert "must be a JSON object" in reasons["2"]
    assert "positive integer" in reasons["not-a-number"]
    assert "positive integer" in reasons["0"]


def test_non_string_reason_is_rejected(db_file: Path) -> None:
    outcome = _import(db_file, _write_export(db_file.parent / "e.json", _export(
        {"job_id": 1, "decision": "apply", "reason": ["list"], "updated_at": APPLY_AT}
    )))
    assert outcome.imported == 0
    assert "reason must be a string" in outcome.rejected[0].reason


# ---------------------------------------------------------------------------
# Document-level errors
# ---------------------------------------------------------------------------


def test_foreign_schema_is_rejected_without_writing(db_file: Path, tmp_path: Path) -> None:
    path = _write_export(tmp_path / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT}, schema="jobscout.feedback.v2"
    ))
    with pytest.raises(FeedbackError) as excinfo:
        _import(db_file, path, expected_project_id=PROJECT)
    assert "unsupported export schema" in str(excinfo.value)

    conn = connect(db_file)
    try:
        assert feedback_count(conn) == 0
    finally:
        conn.close()


def test_project_id_mismatch_is_rejected_without_writing(db_file: Path, tmp_path: Path) -> None:
    path = _write_export(tmp_path / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT}
    ))
    with pytest.raises(FeedbackError) as excinfo:
        _import(db_file, path, expected_project_id="different-project")
    message = str(excinfo.value)
    assert "project_id mismatch" in message
    assert PROJECT in message
    assert "different-project" in message

    conn = connect(db_file)
    try:
        assert feedback_count(conn) == 0
    finally:
        conn.close()


def test_missing_project_id_and_missing_feedback_are_rejected(db_file: Path, tmp_path: Path) -> None:
    no_project = _export({"job_id": 1, "decision": "apply", "updated_at": APPLY_AT})
    del no_project["project_id"]
    with pytest.raises(FeedbackError, match="no project_id"):
        _import(db_file, _write_export(tmp_path / "a.json", no_project))

    no_feedback = _export({"job_id": 1, "decision": "apply", "updated_at": APPLY_AT})
    del no_feedback["feedback"]
    with pytest.raises(FeedbackError, match="no 'feedback' object"):
        _import(db_file, _write_export(tmp_path / "b.json", no_feedback))

    bad_feedback = _export()
    bad_feedback["feedback"] = ["apply"]
    with pytest.raises(FeedbackError, match="keyed by job id"):
        _import(db_file, _write_export(tmp_path / "c.json", bad_feedback))


def test_malformed_json_and_non_object_document_are_clear_errors(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(FeedbackError, match="not valid JSON"):
        load_feedback_export(broken)

    with pytest.raises(FeedbackError, match="must be a JSON object"):
        validate_feedback_export([1, 2, 3], expected_project_id=PROJECT)


def test_missing_export_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FeedbackError, match="could not be read"):
        load_feedback_export(tmp_path / "absent.json")


def test_exported_at_and_source_are_recorded_in_the_outcome(
    db_file: Path, export_file: Path, project_id: str
) -> None:
    outcome = _import(db_file, export_file, expected_project_id=project_id)
    assert outcome.project_id == project_id
    assert outcome.source == "jobscout-dashboard"
    assert outcome.exported_at == "2026-09-16T15:12:04.064000Z"
    assert outcome.entries == 3
    assert outcome.path == str(export_file)


def test_project_check_can_be_skipped_when_no_expectation_is_given(
    db_file: Path, tmp_path: Path
) -> None:
    path = _write_export(tmp_path / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT}
    ))
    outcome = _import(db_file, path)
    assert outcome.imported == 1


def test_repeated_import_keeps_one_row_and_the_current_schema_version(
    tmp_path: Path,
) -> None:
    """A re-import neither duplicates rows nor changes the schema version."""
    path = tmp_path / "fresh.sqlite3"
    _seed(path, jobs=1)
    export = _write_export(tmp_path / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT}
    ))
    assert _import(path, export, expected_project_id=PROJECT).imported == 1
    assert _import(path, export, expected_project_id=PROJECT).unchanged == 1

    conn = connect(path)
    try:
        assert feedback_count(conn) == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_import_feedback_imports_and_reports(db_file: Path, export_file: Path, capsys) -> None:
    assert main(["--db", str(db_file), "import-feedback", str(export_file)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "2 imported" in output
    assert "1 without decision" in output
    assert "0 rejected" in output
    assert "2 decision(s) stored" in output

    conn = connect(db_file)
    try:
        assert feedback_count(conn) == 2
    finally:
        conn.close()


def test_cli_import_feedback_json_payload(
    db_file: Path, export_file: Path, project_id: str, capsys
) -> None:
    assert (
        main(["--db", str(db_file), "import-feedback", str(export_file), "--json"]) == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["imported"] == 2
    assert payload["skipped_no_decision"] == 1
    assert payload["project_id"] == project_id
    assert payload["stored_total"] == 2
    assert payload["job_ids"] == [1, 2]
    assert payload["rejected"] == []


def test_cli_import_feedback_reports_rejected_entries_on_stderr(
    db_file: Path, project_id: str, tmp_path: Path, capsys
) -> None:
    path = _write_export(tmp_path / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT},
        {"job_id": 424242, "decision": "apply", "updated_at": APPLY_AT},
        project_id=project_id,
    ))
    assert main(["--db", str(db_file), "import-feedback", str(path)]) == EXIT_OK
    captured = capsys.readouterr()
    assert "1 rejected" in captured.out
    assert "feedback entry 424242: rejected" in captured.err


def test_cli_import_feedback_refuses_a_project_mismatch(
    db_file: Path, export_file: Path, capsys
) -> None:
    exit_code = main(
        ["--db", str(db_file), "import-feedback", str(export_file), "--project-id", "other"]
    )
    assert exit_code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "feedback import error" in err
    assert "project_id mismatch" in err

    conn = connect(db_file)
    try:
        assert feedback_count(conn) == 0
    finally:
        conn.close()


def test_cli_import_feedback_accepts_an_explicit_project_id(
    db_file: Path, tmp_path: Path, capsys
) -> None:
    """An export survives a moved database when its project is asserted."""
    path = _write_export(tmp_path / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT},
        project_id="moved-database-key",
    ))
    base = ["--db", str(db_file), "import-feedback", str(path)]
    assert main(base) == EXIT_ERROR
    assert "project_id mismatch" in capsys.readouterr().err

    assert main([*base, "--project-id", "moved-database-key"]) == EXIT_OK
    assert "1 imported" in capsys.readouterr().out


def test_cli_import_feedback_requires_an_existing_database(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "absent" / "jobscout.sqlite3"
    export = _write_export(tmp_path / "e.json", _export(
        {"job_id": 1, "decision": "apply", "updated_at": APPLY_AT}
    ))
    assert main(["--db", str(missing), "import-feedback", str(export)]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "no database found" in err
    assert not missing.exists()


def test_cli_import_feedback_reports_a_missing_export(db_file: Path, capsys) -> None:
    assert main(["--db", str(db_file), "import-feedback", "no-such-export.json"]) == EXIT_ERROR
    assert "feedback import error" in capsys.readouterr().err


def test_cli_import_feedback_is_idempotent_end_to_end(
    db_file: Path, export_file: Path, capsys
) -> None:
    args = ["--db", str(db_file), "import-feedback", str(export_file)]
    assert main(args) == EXIT_OK
    assert "2 imported" in capsys.readouterr().out
    assert main(args) == EXIT_OK
    output = capsys.readouterr().out
    assert "2 unchanged" in output
    assert "2 decision(s) stored" in output
