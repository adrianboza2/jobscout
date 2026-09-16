"""Schema and persistence tests."""

from __future__ import annotations

import sqlite3

import pytest

from jobscout.db import (
    SCHEMA_VERSION,
    all_active_jobs,
    count_jobs,
    create_search_run,
    evaluation_count,
    find_duplicate,
    get_job,
    init_db,
    insert_evaluation,
    insert_raw_document,
    jobs_needing_evaluation,
    latest_evaluation,
    list_jobs,
    profile_evidence,
    record_run_item,
    store_posting,
    upsert_profile,
)
from jobscout.models import (
    DimensionScore,
    DuplicateKind,
    Evaluation,
    JobPosting,
    RawDocument,
    RawDocumentKind,
    Recommendation,
    ScorerName,
    SearchRun,
    SearchRunItem,
    SearchRunStatus,
)
from jobscout.normalize import finalize_posting
from jobscout.scoring.profile import build_snapshot

REQUIRED_TABLES = {
    "jobs",
    "search_runs",
    "search_run_items",
    "raw_documents",
    "evaluations",
    "profiles",
    "profile_evidence",
    "job_detail_fetches",
}


def _posting(**overrides) -> JobPosting:
    payload = {
        "source": "linkedin",
        "title": "Senior Backend Engineer",
        "company": "Acme Data Inc.",
        "location": "Remote, Argentina",
        "url": "https://www.linkedin.com/jobs/view/3901234567/",
        "source_job_id": "3901234567",
        "description": "Python, SQL, PostgreSQL.",
    }
    payload.update(overrides)
    return finalize_posting(JobPosting(**payload))


def test_init_db_is_idempotent_and_versioned(conn) -> None:
    assert init_db(conn) == SCHEMA_VERSION
    assert init_db(conn) == SCHEMA_VERSION
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert REQUIRED_TABLES <= tables
    views = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
    }
    assert "latest_evaluations" in views


def test_init_db_rejects_newer_schema(conn) -> None:
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    with pytest.raises(RuntimeError):
        init_db(conn)


def test_store_posting_persists_and_detects_exact_duplicate(conn) -> None:
    posting = _posting()
    job, decision = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    assert decision.kind is DuplicateKind.NEW
    assert job.id is not None
    assert job.first_seen_at == job.last_seen_at == "2026-09-15T10:00:00Z"

    same, second_decision = store_posting(conn, posting, now="2026-09-16T10:00:00Z")
    assert second_decision.kind is DuplicateKind.EXACT_DUPLICATE
    assert same.id == job.id
    assert same.sighting_count == 2
    assert same.last_seen_at == "2026-09-16T10:00:00Z"
    assert count_jobs(conn) == 1


def test_store_posting_detects_probable_duplicate(conn) -> None:
    first, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
    incoming = _posting(
        source_job_id="9999999999",
        url="https://www.linkedin.com/jobs/view/9999999999/senior-backend-engineer-at-acme",
    )
    assert find_duplicate(conn, incoming).kind is DuplicateKind.PROBABLE_DUPLICATE
    matched, decision = store_posting(conn, incoming, now="2026-09-15T11:00:00Z")
    assert decision.kind is DuplicateKind.PROBABLE_DUPLICATE
    assert matched.id == first.id
    assert count_jobs(conn) == 1


def test_store_posting_keeps_stored_description_when_new_fetch_is_empty(conn) -> None:
    posting = _posting()
    job, _ = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    empty = _posting(description=None)
    merged, _ = store_posting(conn, empty, now="2026-09-15T12:00:00Z")
    assert merged.id == job.id
    assert merged.description == "Python, SQL, PostgreSQL."


def test_raw_documents_are_reused_by_hash(conn) -> None:
    document = RawDocument(
        kind=RawDocumentKind.FIXTURE,
        source="fixture",
        url="file:///tmp/fixture.json",
        sha256="a" * 64,
        byte_size=10,
        fetched_at="2026-09-15T10:00:00Z",
        stored_at="2026-09-15T10:00:00Z",
        body="{}",
    )
    first_id = insert_raw_document(conn, document)
    second_id = insert_raw_document(conn, document)
    assert first_id == second_id
    row = conn.execute("SELECT * FROM raw_documents WHERE id = ?", (first_id,)).fetchone()
    assert row["sha256"] == "a" * 64
    assert row["body"] == "{}"


def test_search_run_accounting_and_items(conn) -> None:
    job, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
    run = create_search_run(
        conn,
        SearchRun(
            provider="fixture",
            label="fixture:test",
            query="backend",
            location="remote",
            params={"max_pages": 1},
            status=SearchRunStatus.RUNNING,
            started_at="2026-09-15T10:00:00Z",
        ),
    )
    assert run.id is not None
    record_run_item(
        conn,
        SearchRunItem(
            run_id=run.id,
            job_id=job.id,
            decision=DuplicateKind.NEW,
            rank_in_run=1,
            created_at="2026-09-15T10:00:00Z",
        ),
    )
    # A later page may re-observe the same job; the sighting is recorded again.
    record_run_item(
        conn,
        SearchRunItem(
            run_id=run.id,
            job_id=job.id,
            decision=DuplicateKind.EXACT_DUPLICATE,
            rank_in_run=2,
            created_at="2026-09-15T10:00:00Z",
        ),
    )
    rows = conn.execute("SELECT * FROM search_run_items WHERE run_id = ?", (run.id,)).fetchall()
    assert [row["decision"] for row in rows] == ["new", "exact_duplicate"]


def test_profile_upsert_is_idempotent_and_stores_evidence(conn, profile) -> None:
    snapshot = build_snapshot(profile)
    stored = upsert_profile(conn, snapshot)
    assert stored.id is not None
    evidence = profile_evidence(conn, stored.id)
    kinds = {item.kind for item in evidence}
    assert {"skill_must", "seniority", "location", "language", "dealbreaker", "exclusion"} <= kinds

    again = upsert_profile(conn, build_snapshot(profile))
    assert again.id == stored.id
    assert conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 1


def test_evaluation_round_trip_and_latest_view(conn, stored_job) -> None:
    evaluation = Evaluation(
        job_id=stored_job.id,
        scorer=ScorerName.HEURISTIC,
        overall_score=81.5,
        recommendation=Recommendation.APPLY,
        confidence=0.8,
        dimensions=[
            DimensionScore(name="skills", score=90, weight=1.0, rationale="matches", evidence=["python"])
        ],
        blockers=[],
        strengths=["python"],
        gaps=["no salary"],
        unknowns=["team size"],
        rationale="strong fit",
        model="heuristic-v1",
        prompt_version="v1",
        input_fingerprint="fp",
    )
    stored = insert_evaluation(conn, evaluation)
    assert stored.id is not None

    fetched = latest_evaluation(conn, stored_job.id)
    assert fetched is not None
    assert fetched.overall_score == 81.5
    assert fetched.dimensions[0].name == "skills"
    assert fetched.dimensions[0].evidence == ["python"]
    assert fetched.unknowns == ["team size"]

    older = insert_evaluation(conn, Evaluation(job_id=stored_job.id, scorer=ScorerName.LLM, overall_score=10))
    assert latest_evaluation(conn, stored_job.id).id == older.id
    assert latest_evaluation(conn, stored_job.id, scorer=ScorerName.HEURISTIC).id == stored.id
    assert evaluation_count(conn) == 2


def test_list_jobs_joins_latest_score_and_ranks(conn) -> None:
    high, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
    low, _ = store_posting(
        conn,
        _posting(
            source_job_id="5555555555",
            url="https://www.linkedin.com/jobs/view/5555555555/other",
            title="Data Engineer",
        ),
        now="2026-09-15T09:00:00Z",
    )
    insert_evaluation(conn, Evaluation(job_id=high.id, scorer=ScorerName.HEURISTIC, overall_score=90))
    insert_evaluation(conn, Evaluation(job_id=low.id, scorer=ScorerName.HEURISTIC, overall_score=40))

    recent = list_jobs(conn, limit=10)
    assert {item.job.id for item in recent} == {high.id, low.id}

    ranked = list_jobs(conn, limit=10, rank_by_score=True)
    assert [item.job.id for item in ranked] == [high.id, low.id]
    assert ranked[0].score == 90
    assert ranked[0].recommendation is not None

    filtered = list_jobs(conn, limit=10, min_score=50)
    assert [item.job.id for item in filtered] == [high.id]

    scored_only = list_jobs(conn, limit=10, scored_only=True)
    assert len(scored_only) == 2


def test_failed_evaluations_are_excluded_from_latest_view(conn, stored_job) -> None:
    from jobscout.models import EvaluationStatus

    insert_evaluation(
        conn,
        Evaluation(
            job_id=stored_job.id,
            scorer=ScorerName.LLM,
            status=EvaluationStatus.PARSE_ERROR,
            error="bad json",
        ),
    )
    assert latest_evaluation(conn, stored_job.id) is None
    assert list_jobs(conn, limit=5)[0].score is None


def test_jobs_needing_evaluation_respects_scorer(conn, stored_job) -> None:
    assert [job.id for job in jobs_needing_evaluation(conn, scorer="heuristic")] == [stored_job.id]
    insert_evaluation(
        conn, Evaluation(job_id=stored_job.id, scorer=ScorerName.HEURISTIC, overall_score=50)
    )
    assert jobs_needing_evaluation(conn, scorer="heuristic") == []
    assert [job.id for job in jobs_needing_evaluation(conn, scorer="llm")] == [stored_job.id]
    assert [job.id for job in all_active_jobs(conn)] == [stored_job.id]


def test_foreign_keys_cascade_on_job_delete(conn, stored_job) -> None:
    insert_evaluation(
        conn, Evaluation(job_id=stored_job.id, scorer=ScorerName.HEURISTIC, overall_score=50)
    )
    with conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (stored_job.id,))
    assert evaluation_count(conn) == 0
    assert get_job(conn, stored_job.id) is None


def test_connection_enforces_foreign_keys(conn) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_store_posting_requires_finalized_posting(conn) -> None:
    with pytest.raises(ValueError):
        store_posting(conn, JobPosting(source="s", title="t", company="c"))


def test_missing_database_path_is_created(tmp_path) -> None:
    from jobscout.db import connect

    nested = tmp_path / "nested" / "dir" / "jobs.sqlite3"
    connection = connect(nested)
    try:
        init_db(connection)
    finally:
        connection.close()
    assert nested.is_file()


def test_row_factory_returns_named_rows(conn) -> None:
    assert isinstance(conn, sqlite3.Connection)
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1
