"""Schema and persistence tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jobscout.db import (
    SCHEMA_SQL,
    SCHEMA_VERSION,
    EvaluationContractError,
    all_active_jobs,
    connect,
    count_jobs,
    create_search_run,
    deactivate_job,
    detail_fetches_for_job,
    evaluation_count,
    evaluations_for_job,
    find_duplicate,
    get_feedback,
    get_job,
    get_search_run,
    init_db,
    insert_detail_fetch,
    insert_evaluation,
    insert_raw_document,
    job_detail_fetch_count,
    jobs_needing_evaluation,
    latest_detail_fetch,
    latest_detail_fetches_for_jobs,
    latest_evaluation,
    list_jobs,
    profile_evidence,
    record_run_item,
    store_posting,
    upsert_feedback,
    upsert_profile,
)
from jobscout.models import (
    DetailFetchStatus,
    DimensionScore,
    DuplicateKind,
    Evaluation,
    FeedbackDecision,
    JobDetailFetch,
    JobFeedback,
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
    "job_feedback",
}

#: The historical v3 shape of ``job_detail_fetches`` (no ``final_url``). Kept
#: verbatim so the migration test exercises a real pre-v4 table instead of a
#: current one with the column already present.
V3_JOB_DETAIL_FETCHES_SQL = """
CREATE TABLE job_detail_fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES search_runs(id) ON DELETE SET NULL,
    source TEXT,
    url TEXT,
    status TEXT NOT NULL DEFAULT 'failed',
    http_status INTEGER,
    content_status TEXT,
    description_chars INTEGER NOT NULL DEFAULT 0,
    byte_size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    content_hash TEXT,
    fetched_at TEXT,
    stored_at TEXT NOT NULL,
    raw_document_id INTEGER REFERENCES raw_documents(id) ON DELETE SET NULL,
    error TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_detail_fetches_job ON job_detail_fetches(job_id, id);
CREATE INDEX IF NOT EXISTS idx_detail_fetches_run ON job_detail_fetches(run_id);
"""


def _create_v3_database(path: Path) -> sqlite3.Connection:
    """Build a database that looks like a real pre-v4 install.

    The current ``SCHEMA_SQL`` is applied first (it creates the sibling tables
    the migration must leave untouched), then ``job_detail_fetches`` is rebuilt
    in its v3 shape and ``user_version`` is dialed back to 3.
    """
    connection = connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.executescript(
        "DROP TABLE job_detail_fetches;" + V3_JOB_DETAIL_FETCHES_SQL
    )
    connection.execute("PRAGMA user_version = 3")
    connection.commit()
    return connection


#: The historical v4 shape of ``search_runs``/``search_run_items`` (no
#: ``cycle_id``/``position_kind``). Kept verbatim so the v5 migration test
#: exercises a real pre-v5 table instead of a current one.
V4_SEARCH_RUNS_SQL = """
CREATE TABLE search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    label TEXT,
    query TEXT,
    location TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    items_found INTEGER NOT NULL DEFAULT 0,
    new_jobs INTEGER NOT NULL DEFAULT 0,
    duplicate_jobs INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    raw_document_id INTEGER,
    notes TEXT
);
"""

V4_SEARCH_RUN_ITEMS_SQL = """
CREATE TABLE search_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    decision TEXT NOT NULL DEFAULT 'new',
    rank_in_run INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_items_job ON search_run_items(job_id);
"""


def _create_v4_database(path: Path) -> sqlite3.Connection:
    """Build a database that looks like a real pre-v5 install."""
    connection = connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.executescript(
        "DROP TABLE search_run_items; DROP TABLE search_runs;"
        + V4_SEARCH_RUNS_SQL
        + V4_SEARCH_RUN_ITEMS_SQL
    )
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, int]:
    """Column name -> ``notnull`` flag, for schema-shape assertions."""
    return {
        row["name"]: int(row["notnull"])
        for row in connection.execute(f"PRAGMA table_info({table})")
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


def test_new_database_has_nullable_final_url_column(conn) -> None:
    assert SCHEMA_VERSION == 5
    columns = _columns(conn, "job_detail_fetches")
    assert "final_url" in columns
    # Nullable: a fresh install never invents a redirect target.
    assert columns["final_url"] == 0


def test_migration_v4_adds_final_url_without_losing_history(tmp_path: Path) -> None:
    connection = _create_v3_database(tmp_path / "legacy-v3.sqlite3")
    try:
        job, _ = store_posting(
            connection,
            _posting(source_job_id="3911111111"),
            now="2026-09-15T10:00:00Z",
        )
        insert_evaluation(
            connection,
            Evaluation(
                job_id=job.id,
                scorer=ScorerName.HEURISTIC,
                overall_score=88,
                recommendation=Recommendation.APPLY,
                rationale="keep me",
            ),
        )
        upsert_feedback(
            connection,
            JobFeedback(
                job_id=job.id,
                decision=FeedbackDecision.REJECT,
                reason="not a fit",
                decision_updated_at="2026-09-15T11:00:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO job_detail_fetches (
                job_id, source, url, status, http_status, content_status,
                description_chars, byte_size, fetched_at, stored_at, error, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                "linkedin",
                "https://www.linkedin.com/jobs/view/3911111111/",
                "failed",
                404,
                "unknown",
                0,
                0,
                "2026-09-15T10:00:00Z",
                "2026-09-15T10:00:00Z",
                "not found",
                "legacy row",
            ),
        )
        connection.commit()
        # The pre-migration table genuinely lacks the column.
        assert "final_url" not in _columns(connection, "job_detail_fetches")

        assert init_db(connection) == SCHEMA_VERSION
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _columns(connection, "job_detail_fetches")["final_url"] == 0

        # Re-running init_db is a no-op, not a second migration.
        assert init_db(connection) == SCHEMA_VERSION

        # Every pre-existing row survived, including its provenance.
        assert count_jobs(connection) == 1
        assert get_job(connection, job.id).source_job_id == "3911111111"
        assert evaluation_count(connection) == 1
        assert latest_evaluation(connection, job.id).overall_score == 88
        feedback = get_feedback(connection, job.id)
        assert feedback is not None and feedback.reason == "not a fit"
        assert job_detail_fetch_count(connection) == 1

        legacy = latest_detail_fetch(connection, job.id)
        assert legacy is not None
        assert legacy.url == "https://www.linkedin.com/jobs/view/3911111111/"
        assert legacy.http_status == 404
        # The migrated column is NULL for history; OL-5 fills it later.
        assert legacy.final_url is None
    finally:
        connection.close()


def test_migration_v5_adds_cycle_and_position_without_losing_history(
    tmp_path: Path,
) -> None:
    connection = _create_v4_database(tmp_path / "legacy-v4.sqlite3")
    try:
        job, _ = store_posting(
            connection,
            _posting(source_job_id="3922222222"),
            now="2026-09-15T10:00:00Z",
        )
        connection.execute(
            """
            INSERT INTO search_runs (
                provider, label, query, location, params_json, status, started_at,
                new_jobs
            ) VALUES ('linkedin', 'legacy:kw', 'kw', 'BA', '{}', 'completed', ?, 1)
            """,
            ("2026-09-15T10:00:00Z",),
        )
        run_id = int(connection.execute("SELECT MAX(id) FROM search_runs").fetchone()[0])
        connection.execute(
            """
            INSERT INTO search_run_items (run_id, job_id, decision, rank_in_run, created_at)
            VALUES (?, ?, 'new', 3, ?)
            """,
            (run_id, job.id, "2026-09-15T10:00:00Z"),
        )
        connection.commit()

        # The pre-migration tables genuinely lack the new columns.
        assert "cycle_id" not in _columns(connection, "search_runs")
        assert "position_kind" not in _columns(connection, "search_run_items")

        assert init_db(connection) == SCHEMA_VERSION
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _columns(connection, "search_runs")["cycle_id"] == 0
        assert _columns(connection, "search_run_items")["position_kind"] == 0

        # Re-running init_db is a no-op, not a second migration.
        assert init_db(connection) == SCHEMA_VERSION

        # Historical rows survive and read as NULL, never as a guessed value.
        assert count_jobs(connection) == 1
        run = get_search_run(connection, run_id)
        assert run is not None
        assert run.query == "kw"
        assert run.cycle_id is None
        item = connection.execute(
            "SELECT position_kind, rank_in_run FROM search_run_items WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert item["position_kind"] is None
        assert item["rank_in_run"] == 3
    finally:
        connection.close()


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

    older = insert_evaluation(
        conn,
        Evaluation(
            job_id=stored_job.id,
            scorer=ScorerName.LLM,
            overall_score=10,
            recommendation=Recommendation.SKIP,
        ),
    )
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
    insert_evaluation(
        conn,
        Evaluation(
            job_id=high.id,
            scorer=ScorerName.HEURISTIC,
            overall_score=90,
            recommendation=Recommendation.APPLY,
        ),
    )
    insert_evaluation(
        conn,
        Evaluation(
            job_id=low.id,
            scorer=ScorerName.HEURISTIC,
            overall_score=40,
            recommendation=Recommendation.SKIP,
        ),
    )

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
        conn,
        Evaluation(
            job_id=stored_job.id,
            scorer=ScorerName.HEURISTIC,
            overall_score=50,
            recommendation=Recommendation.CONSIDER,
        ),
    )
    assert jobs_needing_evaluation(conn, scorer="heuristic") == []
    assert [job.id for job in jobs_needing_evaluation(conn, scorer="llm")] == [stored_job.id]
    assert [job.id for job in all_active_jobs(conn)] == [stored_job.id]


def test_foreign_keys_cascade_on_job_delete(conn, stored_job) -> None:
    insert_evaluation(
        conn,
        Evaluation(
            job_id=stored_job.id,
            scorer=ScorerName.HEURISTIC,
            overall_score=50,
            recommendation=Recommendation.CONSIDER,
        ),
    )
    with conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (stored_job.id,))
    assert evaluation_count(conn) == 0
    assert get_job(conn, stored_job.id) is None


def test_connection_enforces_foreign_keys(conn) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_deactivate_job_hides_without_deleting_history(conn) -> None:
    job, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
    insert_evaluation(
        conn,
        Evaluation(
            job_id=job.id,
            scorer=ScorerName.HEURISTIC,
            overall_score=88,
            recommendation=Recommendation.APPLY,
            rationale="keep me",
        ),
    )
    upsert_feedback(
        conn,
        JobFeedback(
            job_id=job.id,
            decision=FeedbackDecision.REJECT,
            reason="not a fit",
            decision_updated_at="2026-09-15T11:00:00Z",
        ),
    )

    assert deactivate_job(conn, job.id) is True
    # Idempotent: a second call is a no-op, not a second transition.
    assert deactivate_job(conn, job.id) is False

    refreshed = get_job(conn, job.id)
    assert refreshed is not None
    assert refreshed.is_active is False
    # Nothing was deleted: the job row and its history survive.
    assert count_jobs(conn) == 1
    assert evaluation_count(conn) == 1
    assert evaluations_for_job(conn, job.id)[0].rationale == "keep me"
    assert latest_evaluation(conn, job.id).overall_score == 88
    feedback = get_feedback(conn, job.id)
    assert feedback is not None and feedback.decision == "reject"

    # Default listings exclude the inactive job; the explicit opt-in includes it.
    assert list_jobs(conn, limit=10) == []
    assert [item.job.id for item in list_jobs(conn, limit=10, include_inactive=True)] == [job.id]
    assert all_active_jobs(conn) == []


def test_resighting_reactivates_a_deactivated_job(conn) -> None:
    posting = _posting()
    job, _ = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    assert deactivate_job(conn, job.id) is True
    assert get_job(conn, job.id).is_active is False

    again, decision = store_posting(conn, posting, now="2026-09-16T10:00:00Z")
    assert decision.kind is DuplicateKind.EXACT_DUPLICATE
    assert again.id == job.id
    assert again.is_active is True
    assert again.sighting_count == 2
    assert [item.job.id for item in list_jobs(conn, limit=10)] == [job.id]


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


def test_detail_fetch_final_url_round_trip(conn, stored_job) -> None:
    inserted = insert_detail_fetch(
        conn,
        JobDetailFetch(
            job_id=stored_job.id,
            source="linkedin",
            url="https://www.linkedin.com/jobs/view/3901234567/",
            final_url="https://www.linkedin.com/jobs/view/3909999999/",
            status=DetailFetchStatus.OK,
            http_status=200,
            fetched_at="2026-09-15T10:00:00Z",
            stored_at="2026-09-15T10:00:00Z",
        ),
    )
    assert inserted.id is not None
    assert inserted.final_url == "https://www.linkedin.com/jobs/view/3909999999/"

    latest = latest_detail_fetch(conn, stored_job.id)
    assert latest is not None
    assert latest.url == "https://www.linkedin.com/jobs/view/3901234567/"
    assert latest.final_url == "https://www.linkedin.com/jobs/view/3909999999/"
    # ``to_dict`` exposes the round-tripped value for JSON output.
    assert latest.to_dict()["final_url"] == "https://www.linkedin.com/jobs/view/3909999999/"

    # A fetch that records no redirect keeps the nullable default.
    insert_detail_fetch(
        conn,
        JobDetailFetch(
            job_id=stored_job.id,
            source="linkedin",
            url="https://www.linkedin.com/jobs/view/3901234567/",
            status=DetailFetchStatus.FAILED,
            error="temporary failure",
        ),
    )
    history = detail_fetches_for_job(conn, stored_job.id)
    assert history[0].final_url is None
    assert history[1].final_url == "https://www.linkedin.com/jobs/view/3909999999/"


def test_latest_detail_fetches_for_jobs_is_batched_and_latest_only(conn) -> None:
    first, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
    second, _ = store_posting(
        conn,
        _posting(
            source_job_id="5555555555",
            url="https://www.linkedin.com/jobs/view/5555555555/other",
            title="Data Engineer",
            company="Beta Corp",
        ),
        now="2026-09-15T11:00:00Z",
    )
    never_fetched, _ = store_posting(
        conn,
        _posting(
            source_job_id="6666666666",
            url="https://www.linkedin.com/jobs/view/6666666666/third",
            title="Platform Engineer",
            company="Gamma LLC",
        ),
        now="2026-09-15T12:00:00Z",
    )
    # Two attempts for one job: the reader must report only the highest id.
    insert_detail_fetch(
        conn,
        JobDetailFetch(
            job_id=first.id,
            status=DetailFetchStatus.OK,
            http_status=200,
            fetched_at="2026-09-14T10:00:00Z",
            stored_at="2026-09-14T10:00:00Z",
        ),
    )
    insert_detail_fetch(
        conn,
        JobDetailFetch(
            job_id=first.id,
            status=DetailFetchStatus.SKIPPED,
            stored_at="2026-09-15T09:00:00Z",
        ),
    )
    insert_detail_fetch(
        conn,
        JobDetailFetch(
            job_id=second.id,
            status=DetailFetchStatus.FAILED,
            http_status=410,
            stored_at="2026-09-15T09:00:00Z",
        ),
    )

    # The reader costs exactly one statement, even across several jobs.
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        latest = latest_detail_fetches_for_jobs(
            conn, [first.id, second.id, never_fetched.id, first.id]
        )
    finally:
        conn.set_trace_callback(None)

    assert set(latest) == {first.id, second.id}
    assert latest[first.id].status == DetailFetchStatus.SKIPPED
    assert latest[second.id].http_status == 410
    # The full history still holds both attempts for the first job.
    assert len(detail_fetches_for_job(conn, first.id)) == 2
    detail_statements = [item for item in statements if "job_detail_fetches" in item]
    assert len(detail_statements) == 1
    # Read-only by construction.
    assert detail_statements[0].strip().lower().startswith("select")


def test_latest_detail_fetches_for_jobs_handles_empty_and_unfetched(conn) -> None:
    job, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")

    assert latest_detail_fetches_for_jobs(conn, []) == {}
    # A job with no attempts is absent, so callers can tell "never checked".
    assert latest_detail_fetches_for_jobs(conn, [job.id]) == {}
    assert latest_detail_fetches_for_jobs(conn, [job.id, job.id]) == {}


# ---------------------------------------------------------------------------
# Canonical successful-evaluation invariant
# ---------------------------------------------------------------------------


def test_insert_evaluation_refuses_ok_without_a_score(conn, stored_job) -> None:
    with pytest.raises(EvaluationContractError):
        insert_evaluation(
            conn,
            Evaluation(
                job_id=stored_job.id,
                scorer=ScorerName.LLM,
                overall_score=None,
                recommendation=Recommendation.APPLY,
            ),
        )
    assert evaluation_count(conn) == 0


def test_insert_evaluation_refuses_ok_need_more_info(conn, stored_job) -> None:
    """The ambiguous ``need_more_info`` success can never be persisted."""
    with pytest.raises(EvaluationContractError):
        insert_evaluation(
            conn,
            Evaluation(
                job_id=stored_job.id,
                scorer=ScorerName.LLM,
                overall_score=55.0,
                recommendation=Recommendation.NEED_MORE_INFO,
            ),
        )
    assert evaluation_count(conn) == 0


def test_insert_evaluation_accepts_a_real_zero_score(conn, stored_job) -> None:
    """Zero is a valid score for a real incompatibility, not a placeholder."""
    stored = insert_evaluation(
        conn,
        Evaluation(
            job_id=stored_job.id,
            scorer=ScorerName.HEURISTIC,
            overall_score=0.0,
            recommendation=Recommendation.SKIP,
        ),
    )
    assert stored.overall_score == 0.0
    assert latest_evaluation(conn, stored_job.id).overall_score == 0.0


def test_insert_evaluation_accepts_explicit_needs_more_info_status(conn, stored_job) -> None:
    """An unresolved evaluation is stored honestly, never as a success."""
    from jobscout.models import EvaluationStatus

    stored = insert_evaluation(
        conn,
        Evaluation(
            job_id=stored_job.id,
            scorer=ScorerName.OPENCODE,
            overall_score=30.0,
            recommendation=Recommendation.NEED_MORE_INFO,
            status=EvaluationStatus.NEEDS_MORE_INFO,
        ),
    )
    assert str(stored.status) == EvaluationStatus.NEEDS_MORE_INFO.value
    # It is not a successful public evaluation and carries no invented score.
    assert latest_evaluation(conn, stored_job.id) is None
    assert stored.overall_score == 30.0
