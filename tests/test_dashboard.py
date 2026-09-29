"""Dashboard tests: XSS safety, data minimization, aggregation, and CLI paths.

Everything runs offline against temporary SQLite fixtures. A fixture-only smoke
run exercises the same code path the CLI uses, and one test blocks sockets
outright to prove the report never touches the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import FIXTURES, no_network

from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.collectors.linkedin_details import (
    DEFINITIVE_MISSING_STATUSES as FETCHER_DEFINITIVE_STATUSES,
)
from jobscout.dashboard import (
    DASHBOARD_FEEDBACK_DECISIONS,
    DASHBOARD_LEGACY_FEEDBACK_DECISIONS,
    DEFAULT_TOP,
    DETAIL_EVIDENCE_WINDOW_DAYS,
    DETAIL_STATES,
    DEFINITIVE_MISSING_HTTP_STATUSES,
    MAX_TOP,
    DashboardError,
    _classify_detail_state,
    _detail_evidence,
    build_payload,
    embed_json,
    generate_dashboard,
    open_readonly_database,
    project_key,
    render_dashboard_html,
)
from jobscout.db import (
    connect,
    connect_readonly,
    create_search_run,
    init_db,
    insert_detail_fetch,
    insert_evaluation,
    record_run_item,
    store_posting,
    upsert_feedback,
    upsert_profile,
)
from jobscout.models import (
    DetailFetchStatus,
    DuplicateKind,
    Evaluation,
    EvaluationStatus,
    FeedbackDecision,
    JobDetailFetch,
    JobFeedback,
    JobPosting,
    Recommendation,
    ScorerName,
    SearchRun,
    SearchRunItem,
    SearchRunStatus,
)
from jobscout.normalize import finalize_posting
from jobscout.scoring.profile import build_snapshot, load_profile

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

XSS_TITLE = "</script><img src=x onerror=alert('pwned')>"
XSS_COMPANY = "Acme & Sons <b>Ltd</b>"
DESCRIPTION_TOKEN = "DESCRIPTION-TOKEN-9f3a1c-should-never-appear"
PROFILE_PATH_TOKEN = "C:/private/secrets/profile.yaml"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


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


def _score(job_id: int, score: float, recommendation: str, **overrides) -> Evaluation:
    payload = {
        "job_id": job_id,
        "scorer": ScorerName.LLM,
        "overall_score": score,
        "recommendation": recommendation,
        "confidence": 0.7,
        "strengths": ["python", "sql"],
        "gaps": ["no salary"],
        "blockers": [],
        "unknowns": ["team size"],
        "rationale": "solid match for the must-have skills",
        "model": "deepseek-v4.1-flash",
    }
    payload.update(overrides)
    return Evaluation(**payload)


def _run(
    conn: sqlite3.Connection,
    keyword: str,
    provider: str = "linkedin",
    cycle_id: str | None = None,
) -> SearchRun:
    return create_search_run(
        conn,
        SearchRun(
            provider=provider,
            label=f"{provider}:{keyword}",
            query=keyword,
            location="Buenos Aires",
            cycle_id=cycle_id,
            params={"max_pages": 1, "fixture_path": PROFILE_PATH_TOKEN},
            status=SearchRunStatus.COMPLETED,
            started_at="2026-09-15T10:00:00Z",
        ),
    )


def _observe(
    conn: sqlite3.Connection,
    run_id: int,
    job_id: int,
    *,
    rank: int,
    decision: DuplicateKind = DuplicateKind.NEW,
) -> None:
    record_run_item(
        conn,
        SearchRunItem(
            run_id=run_id,
            job_id=job_id,
            decision=decision,
            rank_in_run=rank,
            created_at="2026-09-15T10:00:00Z",
        ),
    )


def _detail_fetch(**overrides) -> JobDetailFetch:
    """An in-memory fetch attempt, for the pure classification tests."""
    payload = {
        "job_id": 1,
        "source": "linkedin",
        "url": "https://www.linkedin.com/jobs/view/1/",
        "status": DetailFetchStatus.FAILED,
        "stored_at": "2026-09-15T12:00:00Z",
    }
    payload.update(overrides)
    return JobDetailFetch(**payload)


def _insert_fetch(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: DetailFetchStatus,
    http_status: int | None = None,
    fetched_at: str | None = None,
    stored_at: str = "2026-09-15T12:00:00Z",
    raw_document_id: int | None = None,
    url: str | None = None,
    final_url: str | None = None,
) -> None:
    """Persist one detail-fetch attempt with explicit timestamps/provenance."""
    insert_detail_fetch(
        conn,
        JobDetailFetch(
            job_id=int(job_id),
            source="linkedin",
            url=url or f"https://www.linkedin.com/jobs/view/{job_id}/role",
            final_url=final_url,
            status=status,
            http_status=http_status,
            content_status="ok" if status is DetailFetchStatus.OK else None,
            fetched_at=fetched_at,
            stored_at=stored_at,
            raw_document_id=raw_document_id,
        ),
    )


def _resight_dashboard_job(conn: sqlite3.Connection, source_job_id: str, *, now: str) -> None:
    """Re-observe a stored posting through the normal ``store_posting`` path."""
    store_posting(conn, _posting(source_job_id=source_job_id), now=now)


def _seed_dashboard_db(path: Path) -> dict[str, int]:
    """A small but complete database: 3 jobs, 2 keywords, 2 evaluations.

    keyword A observes job1 (twice) and job2; keyword B observes job2 and the
    unscored job3.
    """
    conn = connect(path)
    try:
        init_db(conn)
        job1, _ = store_posting(
            conn,
            _posting(posted_at="2026-09-14T10:00:00Z"),
            now="2026-09-15T10:00:00Z",
        )
        job2, _ = store_posting(
            conn,
            _posting(
                source_job_id="2222222222",
                url="https://www.linkedin.com/jobs/view/2222222222/data-engineer",
                title="Data Engineer",
                company="Beta Corp",
                posted_at="2026-09-10T10:00:00Z",
            ),
            now="2026-09-15T10:00:00Z",
        )
        job3, _ = store_posting(
            conn,
            _posting(
                source_job_id="3333333333",
                url="https://www.linkedin.com/jobs/view/3333333333/platform",
                title="Platform Engineer",
                company="Gamma LLC",
                posted_at=None,
            ),
            now="2026-09-15T10:00:00Z",
        )

        run_a = _run(conn, "senior backend engineer")
        run_b = _run(conn, "data platform", provider="fixture")
        _observe(conn, run_a.id, job1.id, rank=1)
        _observe(conn, run_a.id, job2.id, rank=2)
        _observe(conn, run_a.id, job1.id, rank=3, decision=DuplicateKind.PROBABLE_DUPLICATE)
        _observe(conn, run_b.id, job2.id, rank=1)
        _observe(conn, run_b.id, job3.id, rank=2)

        insert_evaluation(conn, _score(job1.id, 80.0, Recommendation.APPLY))
        insert_evaluation(conn, _score(job2.id, 40.0, Recommendation.CONSIDER))
    finally:
        conn.close()
    return {"job1": int(job1.id), "job2": int(job2.id), "job3": int(job3.id)}


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "dash" / "dashboard.sqlite3"
    _seed_dashboard_db(path)
    return path


def _payload(db_file: Path, *, top: int = DEFAULT_TOP) -> dict:
    conn = open_readonly_database(db_file)
    try:
        return build_payload(
            conn, top=top, project_id=project_key(db_file), now=NOW
        )
    finally:
        conn.close()


def _extract_payload(html_text: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="jobscout-data">(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    assert match is not None, "the report must embed its data as JSON"
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# XSS / escaping
# ---------------------------------------------------------------------------


def test_embed_json_cannot_close_its_script_tag() -> None:
    hostile = {"title": "</script><img src=x onerror=alert(1)>", "note": "a & b"}

    embedded = embed_json(hostile)

    assert "<" not in embedded and ">" not in embedded and "&" not in embedded
    assert "</script" not in embedded.lower()
    assert json.loads(embedded) == hostile  # same value, just escaped


def test_embed_json_escapes_line_and_paragraph_separators() -> None:
    embedded = embed_json({"text": "a\u2028b\u2029c"})
    assert "\u2028" not in embedded and "\u2029" not in embedded
    assert json.loads(embedded) == {"text": "a\u2028b\u2029c"}


def test_hostile_job_text_never_reaches_the_report_unescaped(db_file: Path) -> None:
    conn = connect(db_file)
    try:
        store_posting(
            conn,
            _posting(
                source_job_id="9999999999",
                url="https://www.linkedin.com/jobs/view/9999999999/hostile",
                title=XSS_TITLE,
                company=XSS_COMPANY,
            ),
            now="2026-09-15T11:00:00Z",
        )
    finally:
        conn.close()

    html_text = generate_and_read(db_file)

    assert "</script><img" not in html_text
    assert "<img src=x onerror" not in html_text
    assert "\\u003c/script\\u003e" in html_text
    # The hostile value survives JSON round-tripping: it is escaped, not dropped.
    data = _extract_payload(html_text)
    titles = {offer["title"] for offer in data["offers"]}
    assert XSS_TITLE in titles


def test_report_builds_the_dom_without_inner_html(db_file: Path) -> None:
    html_text = generate_and_read(db_file)
    # Untrusted values can only reach the DOM through createElement/textContent.
    assert "innerHTML" not in html_text
    assert "document.createElement" in html_text
    assert "textContent" in html_text


# ---------------------------------------------------------------------------
# Template token substitution
# ---------------------------------------------------------------------------


def test_render_template_never_rescans_replacement_values() -> None:
    """A literal token inside a value must survive; only the template is scanned."""
    from jobscout.dashboard import _render_template

    rendered = _render_template(
        "A=__A__ B=__B__ C=__C__",
        {"A": "__B__", "B": "__C__", "C": "done"},
    )

    assert rendered == "A=__B__ B=__C__ C=done"


def test_literal_template_tokens_in_metadata_and_offers_survive(db_file: Path) -> None:
    payload = _payload(db_file)
    payload["database_label"] = "__DATA__"
    payload["warnings"] = ["__SCRIPT__ warning"]
    payload["offers"][0]["title"] = "__SCRIPT__"
    payload["offers"][0]["rationale"] = "__DATA__ rationale __STYLE__"

    html_text = render_dashboard_html(payload)

    # Metadata and warnings keep the literal token instead of the payload/script.
    assert "Base de datos: __DATA__" in html_text
    assert "<p>__SCRIPT__ warning</p>" in html_text
    # The embedded payload still parses and the tokens survive as data.
    data = _extract_payload(html_text)
    assert data["offers"][0]["title"] == "__SCRIPT__"
    assert data["offers"][0]["rationale"] == "__DATA__ rationale __STYLE__"
    # The report's own script block is written exactly once. This marker exists only
    # inside that block, so a substituted __SCRIPT__ token would double the count.
    assert html_text.count("var EXPECTED_SCHEMA =") == 1


def test_token_like_job_values_do_not_corrupt_the_generated_report(db_file: Path) -> None:
    conn = connect(db_file)
    try:
        job, _ = store_posting(
            conn,
            _posting(
                source_job_id="8000000000",
                url="https://www.linkedin.com/jobs/view/8000000000/tokens",
                title="__SCRIPT__",
                company="__DATA__",
            ),
            now="2026-09-15T11:00:00Z",
        )
        insert_evaluation(
            conn,
            _score(job.id, 95.0, Recommendation.APPLY, rationale="__STYLE__ token"),
        )
    finally:
        conn.close()

    html_text = generate_and_read(db_file)

    # The JSON block survived: a mangled payload would fail to parse here.
    data = _extract_payload(html_text)
    hostile = next(offer for offer in data["offers"] if offer["title"] == "__SCRIPT__")
    assert hostile["company"] == "__DATA__"
    assert hostile["rationale"] == "__STYLE__ token"
    # The script block stayed its own: a substituted token would have injected
    # extra copies of the page script into the JSON payload.
    assert html_text.count("var EXPECTED_SCHEMA =") == 1


# ---------------------------------------------------------------------------
# Data minimization
# ---------------------------------------------------------------------------


def test_report_never_contains_description_profile_or_absolute_paths(
    db_file: Path, tmp_path: Path
) -> None:
    conn = connect(db_file)
    try:
        store_posting(
            conn,
            _posting(
                source_job_id="4444444444",
                url="https://www.linkedin.com/jobs/view/4444444444/private",
                description=DESCRIPTION_TOKEN,
            ),
            now="2026-09-15T11:00:00Z",
        )
        profile = load_profile(FIXTURES / "profile_test.yaml")
        snapshot = build_snapshot(profile)
        snapshot.source_path = PROFILE_PATH_TOKEN
        upsert_profile(conn, snapshot)
    finally:
        conn.close()

    html_text = generate_and_read(db_file)

    assert DESCRIPTION_TOKEN not in html_text
    assert PROFILE_PATH_TOKEN not in html_text
    assert str(tmp_path) not in html_text
    assert str(db_file.resolve()) not in html_text
    assert snapshot.source_path == PROFILE_PATH_TOKEN  # the fixture really was seeded
    # The provenance params of a run are never serialized either.
    assert "fixture_path" not in html_text


def test_report_is_self_contained_and_needs_no_network(db_file: Path, monkeypatch) -> None:
    no_network(monkeypatch)
    html_text = generate_and_read(db_file)

    assert html_text.startswith("<!DOCTYPE html>")
    assert "<style>" in html_text
    assert '<script type="application/json" id="jobscout-data">' in html_text
    # No external asset, font, stylesheet, or fetch target.
    assert not re.search(r'<(?:link|script|img|iframe)[^>]+(?:src|href)="https?://', html_text)
    assert "cdn." not in html_text
    assert "fetch(" not in html_text


# ---------------------------------------------------------------------------
# Generation behaviour
# ---------------------------------------------------------------------------


def test_generate_creates_the_output_parent_directory(db_file: Path, tmp_path: Path) -> None:
    output = tmp_path / "nested" / "reports" / "dashboard.html"
    assert not output.parent.exists()

    summary = generate_dashboard(db_file, output, top=5)

    assert output.is_file()
    assert summary.output == output
    assert summary.offers == 3
    assert summary.scored == 2
    assert summary.keywords == 2
    assert summary.bytes_written == output.stat().st_size


def test_generate_never_writes_to_the_database(db_file: Path) -> None:
    before = _database_fingerprint(db_file)
    size_before = db_file.stat().st_size

    generate_dashboard(db_file, db_file.parent / "report.html")

    assert _database_fingerprint(db_file) == before
    assert db_file.stat().st_size >= size_before


# ---------------------------------------------------------------------------
# Output path safety
# ---------------------------------------------------------------------------


def test_output_resolving_to_the_database_is_rejected(db_file: Path) -> None:
    before = db_file.read_bytes()

    with pytest.raises(DashboardError) as excinfo:
        generate_dashboard(db_file, db_file)

    assert "resolves to the SQLite database" in str(excinfo.value)
    assert db_file.read_bytes() == before


def test_output_rejected_when_a_relative_path_resolves_to_the_database(
    db_file: Path, monkeypatch
) -> None:
    monkeypatch.chdir(db_file.parent)
    before = db_file.read_bytes()

    with pytest.raises(DashboardError):
        generate_dashboard(db_file, db_file.name)

    assert db_file.read_bytes() == before


def test_output_resolving_to_a_database_sidecar_is_rejected(db_file: Path) -> None:
    before = db_file.read_bytes()

    with pytest.raises(DashboardError):
        generate_dashboard(db_file, Path(f"{db_file}-wal"))

    assert db_file.read_bytes() == before


def test_empty_output_is_rejected(db_file: Path) -> None:
    for empty in ("", "   "):
        with pytest.raises(DashboardError) as excinfo:
            generate_dashboard(db_file, empty)
        assert "must not be empty" in str(excinfo.value)


def test_output_parent_that_is_a_file_is_a_clean_error(db_file: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("in the way", encoding="utf-8")

    with pytest.raises(DashboardError) as excinfo:
        generate_dashboard(db_file, blocker / "report.html")

    assert "could not be written" in str(excinfo.value)


def test_output_path_that_is_a_directory_is_a_clean_error(db_file: Path, tmp_path: Path) -> None:
    directory = tmp_path / "a-directory"
    directory.mkdir()

    with pytest.raises(DashboardError) as excinfo:
        generate_dashboard(db_file, directory)

    assert "could not be written" in str(excinfo.value)


def test_permission_error_becomes_a_dashboard_error(
    db_file: Path, tmp_path: Path, monkeypatch
) -> None:
    def denied(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "write_text", denied)

    with pytest.raises(DashboardError) as excinfo:
        generate_dashboard(db_file, tmp_path / "report.html")

    assert "could not be written" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Top bound
# ---------------------------------------------------------------------------


def test_top_above_the_maximum_is_rejected(db_file: Path) -> None:
    output = db_file.parent / "too-many.html"

    with pytest.raises(DashboardError) as excinfo:
        generate_dashboard(db_file, output, top=MAX_TOP + 1)

    assert "must be <=" in str(excinfo.value)
    assert not output.exists()


def test_top_at_the_maximum_is_accepted(db_file: Path) -> None:
    output = db_file.parent / "max-top.html"

    summary = generate_dashboard(db_file, output, top=MAX_TOP)

    assert output.is_file()
    assert summary.offers == 3


# ---------------------------------------------------------------------------
# Read-only database access
# ---------------------------------------------------------------------------


def test_connect_readonly_refuses_writes_at_the_driver_level(db_file: Path) -> None:
    conn = connect_readonly(db_file)
    assert conn is not None
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            conn.execute(
                "INSERT INTO jobs (fingerprint, dedupe_key, source, first_seen_at, last_seen_at) "
                "VALUES ('x', 'y', 'z', 'now', 'now')"
            )
        assert "readonly" in str(excinfo.value).lower()
    finally:
        conn.close()


def test_readonly_uri_cannot_be_escaped_by_disabling_query_only(db_file: Path) -> None:
    """A writable connection pinned only by ``query_only`` could be re-enabled."""
    before = hashlib.sha256(db_file.read_bytes()).hexdigest()
    conn = connect_readonly(db_file)
    assert conn is not None
    try:
        conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            conn.execute("UPDATE jobs SET is_active = 0")
        assert "readonly" in str(excinfo.value).lower()
    finally:
        conn.close()

    assert hashlib.sha256(db_file.read_bytes()).hexdigest() == before


def test_connect_readonly_returns_none_when_there_is_nothing_to_read(tmp_path: Path) -> None:
    assert connect_readonly(tmp_path / "missing.sqlite3") is None
    assert connect_readonly(":memory:") is None


def test_readonly_read_leaves_database_bytes_unchanged(db_file: Path) -> None:
    before = hashlib.sha256(db_file.read_bytes()).hexdigest()
    records_before = _database_fingerprint(db_file)

    conn = connect_readonly(db_file)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    finally:
        conn.close()

    assert hashlib.sha256(db_file.read_bytes()).hexdigest() == before
    assert _database_fingerprint(db_file) == records_before
    # SQLite may add an empty -wal while reading a WAL database; it must carry no
    # committed frames (a non-empty WAL would mean data was staged or replayed).
    wal = Path(f"{db_file}-wal")
    if wal.exists():
        assert wal.stat().st_size == 0


def test_readonly_open_of_a_clean_wal_database_creates_no_sidecar(db_file: Path) -> None:
    # ``db_file`` is built through ``connect()`` (WAL) and cleanly closed, so it
    # is fully checkpointed and has no -wal before the read.
    assert not Path(f"{db_file}-wal").exists()

    conn = connect_readonly(db_file)
    assert conn is not None
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    finally:
        conn.close()

    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{db_file}{suffix}").exists(), suffix


def test_readonly_reads_the_latest_committed_data_with_an_active_writer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hot.sqlite3"
    writer = sqlite3.connect(str(path))
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        init_db(writer)
        store_posting(writer, _posting(), now="2026-09-15T10:00:00Z")
        # The writer stays open, so this committed row lives in a hot -wal file.
        store_posting(
            writer,
            _posting(
                source_job_id="9090909090",
                url="https://www.linkedin.com/jobs/view/9090909090/hot",
                title="Hot Job",
            ),
            now="2026-09-15T11:00:00Z",
        )

        conn = connect_readonly(path)
        assert conn is not None
        try:
            titles = {row["title"] for row in conn.execute("SELECT title FROM jobs")}
        finally:
            conn.close()
    finally:
        writer.close()

    assert "Hot Job" in titles


def test_readonly_open_of_a_plain_database_creates_no_journal(tmp_path: Path) -> None:
    path = tmp_path / "plain.sqlite3"
    # Deliberately not a ``connect()`` database: no WAL, so any sidecar found
    # after a read must have been created by the read itself.
    writer = sqlite3.connect(str(path))
    try:
        init_db(writer)
        store_posting(writer, _posting(), now="2026-09-15T10:00:00Z")
    finally:
        writer.close()
    before = path.read_bytes()

    conn = connect_readonly(path)
    assert conn is not None
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        conn.close()

    assert path.read_bytes() == before
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(f"{path}{suffix}").exists(), suffix


def test_readonly_connection_refuses_writes(db_file: Path) -> None:
    conn = open_readonly_database(db_file)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO jobs (fingerprint, dedupe_key, source, first_seen_at, last_seen_at) "
                         "VALUES ('x', 'y', 'z', 'now', 'now')")
    finally:
        conn.close()


def test_missing_database_is_a_useful_error(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "dashboard.sqlite3"

    with pytest.raises(DashboardError) as excinfo:
        generate_dashboard(missing, tmp_path / "report.html")

    assert "no database found" in str(excinfo.value)
    assert not (tmp_path / "report.html").exists()
    # A read-only open must not create the database or its parent directory.
    assert not missing.parent.exists()


def test_uninitialized_database_is_a_useful_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(str(empty)).close()

    with pytest.raises(DashboardError) as excinfo:
        generate_dashboard(empty, tmp_path / "report.html")

    assert "not a jobscout database" in str(excinfo.value)


def test_non_positive_top_is_rejected(db_file: Path) -> None:
    with pytest.raises(DashboardError):
        generate_dashboard(db_file, db_file.parent / "report.html", top=0)


def test_offers_are_ranked_by_score_then_freshness(db_file: Path) -> None:
    payload = _payload(db_file, top=3)

    assert [offer["score"] for offer in payload["offers"]] == [80.0, 40.0, None]
    assert payload["offers"][0]["recommendation"] == "apply"
    assert payload["offers"][1]["recommendation"] == "consider"
    # The unscored job falls back to its first_seen_at for age provenance.
    unscored = payload["offers"][2]
    assert unscored["score"] is None
    assert unscored["recommendation"] is None
    assert unscored["age_source"] == "first_seen"


def test_top_limits_the_embedded_offers(db_file: Path) -> None:
    payload = _payload(db_file, top=2)
    assert len(payload["offers"]) == 2
    assert payload["summary"]["jobs_shown"] == 2
    # Aggregation still covers every job in the database, not just the shown ones.
    assert payload["summary"]["keywords"] == 2


def test_offer_carries_every_field_the_report_promises(db_file: Path) -> None:
    offer = _payload(db_file, top=1)["offers"][0]

    for key in (
        "id", "title", "company", "location", "url", "posted_at", "age_days",
        "score", "recommendation", "confidence", "scorer", "model", "rationale",
        "strengths", "gaps", "blockers", "unknowns", "keywords",
    ):
        assert key in offer, key
    assert offer["url"].startswith("https://www.linkedin.com/jobs/view/")
    assert offer["age_days"] is not None
    assert offer["keywords"][0]["keyword"] == "senior backend engineer"
    assert offer["keywords"][0]["rank"] == 1


def test_rationale_is_bounded(db_file: Path) -> None:
    conn = connect(db_file)
    try:
        job, _ = store_posting(
            conn,
            _posting(source_job_id="5555555555", url="https://www.linkedin.com/jobs/view/5555555555/long"),
            now="2026-09-15T11:00:00Z",
        )
        from jobscout.dashboard import MAX_RATIONALE_CHARS

        insert_evaluation(
            conn,
            _score(job.id, 60.0, Recommendation.CONSIDER, rationale="x" * (MAX_RATIONALE_CHARS * 2)),
        )
    finally:
        conn.close()

    payload = _payload(db_file, top=10)
    rated = next(offer for offer in payload["offers"] if offer["score"] == 60.0)
    assert len(rated["rationale"]) <= MAX_RATIONALE_CHARS
    assert rated["rationale"].endswith("\u2026")


# ---------------------------------------------------------------------------
# Client-side filter surface
# ---------------------------------------------------------------------------


def test_report_exposes_filters_search_feedback_and_summary(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    for element_id in (
        "filter-text",
        "filter-recommendation",
        "filter-min-score",
        "filter-keyword",
        "filter-review",
        "filter-sort",
        "reset-filters",
        "export-feedback",
        "feedback-count",
    ):
        assert f'id="{element_id}"' in html_text, element_id

    # Visible labels are neutral Spanish; the option values stay English keys.
    assert ">Aplicar</option>" in html_text
    assert ">Considerar</option>" in html_text
    assert ">Descartar</option>" in html_text
    assert ">Requiere más información</option>" in html_text
    assert ">Sin puntaje</option>" in html_text
    # Human feedback labels stay separate from the model recommendation labels.
    # The human surface is binary; the legacy value has a display-only label.
    assert 'var FEEDBACK_LABELS = { apply: "Aplicar", reject: "Descartar" };' in html_text
    assert 'var LEGACY_FEEDBACK_LABELS = { consider: "Considerar (histórico)" };' in html_text
    assert 'var FEEDBACK_DECISIONS = ["apply", "reject"];' in html_text
    assert "localStorage" in html_text
    assert "no se escribe en la base" in html_text
    assert "SQLite" in html_text

    payload = _extract_payload(html_text)
    assert payload["summary"]["recommendations"]["apply"] == 1
    assert payload["summary"]["recommendations"]["consider"] == 1
    assert payload["feedback_schema"] == "jobscout.feedback.v1"
    assert payload["project_id"] == project_key(db_file)
    # The storage key is namespaced by project so two databases never collide.
    assert f"jobscout.feedback.v1:{payload['project_id']}" in html_text


def test_visible_ui_text_is_neutral_spanish(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert '<html lang="es">' in html_text
    for label in (
        "Panel de ofertas",
        "Resumen",
        "Buscar y filtrar",
        "Privacidad y funcionamiento sin conexión",
        "Desempeño por palabra clave",
        "Restablecer filtros",
        "Ofertas mostradas",
        "Revisadas (tu decisión)",
        "Puntos fuertes",
        "Brechas",
        "Bloqueos",
        "Incógnitas",
        "Justificación del modelo:",
        "Ver la oferta",
        "Sin puntaje",
    ):
        assert label in html_text, label

    # The embedded payload keeps its English schema keys and values.
    payload = _extract_payload(html_text)
    assert payload["schema"] == "jobscout.dashboard.v1"
    assert "recommendation" in payload["offers"][0]
    assert payload["offers"][0]["recommendation"] == "apply"


def test_dashboard_exposes_accessible_kpi_and_provenance_structure(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    # Section landmarks carry stable ids and explicit accessible names.
    assert 'id="summary"' in html_text
    assert 'id="summary-note"' in html_text
    assert 'id="kpi-reviewed-value"' not in html_text  # created by the client script
    assert 'value.id = "kpi-reviewed-value";' in html_text
    assert 'role="search"' in html_text
    assert 'aria-labelledby="privacy-title"' in html_text
    assert 'aria-labelledby="analytics-title"' in html_text
    assert 'class="skip-link"' in html_text
    # Feedback buttons expose a pressed state so colour is not the only signal.
    assert 'setAttribute("aria-pressed"' in html_text
    # Observed rank is framed as provenance, not as quality.
    assert "La posición observada es procedencia" in html_text


# ---------------------------------------------------------------------------
# Visual system: desktop technical panel (second UI variant)
#
# The first variant ("clinical blueprint on frosted paper") and its token
# contract live in commit 2a805da; these tests describe the variant that
# replaced it on this branch.
# ---------------------------------------------------------------------------


def _embedded_style(html_text: str) -> str:
    match = re.search(r"<style>(.*?)</style>", html_text, re.DOTALL)
    assert match is not None, "the report must embed its stylesheet"
    return match.group(1)


def test_report_uses_the_desktop_technical_panel_design_tokens(db_file: Path) -> None:
    """One light desktop token set, a fixed shell, and no dark mode.

    Asserted so a later style change cannot silently reintroduce a dark palette, a
    second accent, or an external font, all of which would break the single-file
    offline promise.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert "color-scheme: light;" in css
    assert "prefers-color-scheme" not in html_text
    for token in (
        "--canvas:#e7ebee",
        "--paper:#ffffff",
        "--surface:#f4f6f7",
        "--ink:#0c1216",
        "--soft-ink:#20303a",
        "--muted:#5c6a74",
        "--hairline:#ccd4da",
        "--ember:#e7000b",
        "--signal:#0f6b4f",
        "--space:4px",
        "--radius:3px",
        "--panel-max:1440px",
        "--rail-width:320px",
    ):
        assert token in css, token

    # The retired dark-mode/soft-state palette must not come back.
    for retired in ("--accent", "--ok-soft", "--warn-soft", "prefers-color"):
        assert retired not in html_text, retired

    # System font stack only: no webfont, no remote asset URL.
    assert "--font-sans:system-ui, -apple-system" in css
    assert "Geist" not in html_text
    assert "@font-face" not in css
    assert not re.search(r'url\(\s*["\']?https?://', html_text)

    # One six-step type scale; no rule outside it hard-codes a size. The title step
    # exists so an offer title outranks body text without competing with a section
    # heading: 15px body, 17px row title, 20px section heading.
    scale = re.findall(r"--fs-([a-z]+):(\d+)px", css)
    assert scale == [
        ("micro", "11"),
        ("small", "13"),
        ("body", "15"),
        ("title", "17"),
        ("heading", "20"),
        ("display", "28"),
    ]
    for declared in re.findall(r"font-size:\s*([^;]+);", css):
        declared = declared.strip()
        assert declared.startswith("var(--fs-") or declared == ".9em", declared

    # The panel shell keeps its structure: top bar, content list, control rail.
    for hook in (
        'class="panel"',
        'class="topbar"',
        'class="content"',
        'class="rail"',
        'class="kpi-grid"',
        'class="filters"',
        'class="two-col"',
        'class="method"',
    ):
        assert hook in html_text, hook


def test_report_declares_no_narrow_screen_breakpoints(db_file: Path) -> None:
    """This variant targets a desktop browser window, so it ships no mobile layout.

    Any width-based query would reintroduce a responsive variant, so the only media
    query allowed in the stylesheet is the reduced-motion guard.
    """
    css = _embedded_style(generate_and_read(db_file))

    assert re.findall(r"@media\s*\(([^)]*)\)", css) == ["prefers-reduced-motion:reduce"]
    assert css.count("@media") == 1
    for forbidden in ("max-width", "min-width"):
        assert f"@media ({forbidden}" not in css, forbidden

    # The desktop shell is a fixed content + rail grid, with fixed strip columns.
    assert "grid-template-columns:minmax(0, 1fr) var(--rail-width)" in css
    assert "grid-template-columns:repeat(6, minmax(0, 1fr))" in css
    assert "grid-template-columns:repeat(3, minmax(0, 1fr))" in css


def test_accent_is_scoped_to_the_model_axis_only(db_file: Path) -> None:
    """The accent marks the model recommendation and nothing else.

    Conflating the two axes is the failure mode this guards: a reader must never be
    able to read the accent as their own decision, so every rule that reaches for the
    signal token is the model recommendation surface, and no human-feedback rule uses
    it. Human feedback carries weight, shape, and an ink rule instead.
    """
    css = _embedded_style(generate_and_read(db_file))

    accent_rules = [rule for rule in css.split("}") if "--signal" in rule]
    assert accent_rules, "the model accent must be defined and used"
    allowed = (":root", ".axis-model", ".card.rec-apply", ".badge-apply")
    for rule in accent_rules:
        assert any(hook in rule for hook in allowed), rule

    human_rules = [
        rule
        for rule in css.split("}")
        if ".decision-btn" in rule or ".feedback" in rule or ".kpi-reviewed" in rule
    ]
    assert human_rules, "the human feedback surface must be styled"
    for rule in human_rules:
        assert "--signal" not in rule, rule

    # The two axes are also named in visible copy, not only in CSS.
    html_text = generate_and_read(db_file)
    assert "Dos ejes distintos" in html_text
    assert "Recomendación del modelo" in html_text
    assert "Mi decisión (feedback)" in html_text


def test_explainability_is_a_native_progressive_disclosure(db_file: Path) -> None:
    """Per-offer explainability is a <details> disclosure added as enhancement.

    The base script builds every card, so the disclosure is attached afterwards by an
    appended block. Native <details>/<summary> is used on purpose: the platform owns
    the keyboard and screen-reader behaviour, and without JavaScript the rationale and
    provenance lists the base script renders are still fully visible.
    """
    html_text = generate_and_read(db_file)

    assert 'block.className = "explain";' in html_text
    assert 'document.createElement("summary")' in html_text
    assert "Explicabilidad del puntaje y procedencia" in html_text
    assert 'id="explain-controls"' in html_text
    assert "Expandir todo" in html_text
    assert "Contraer todo" in html_text
    assert "MutationObserver" in html_text
    # Additive only: the base report script is still written exactly once.
    assert html_text.count("var EXPECTED_SCHEMA =") == 1

    css = _embedded_style(html_text)
    assert ".explain[open] > summary::before" in css
    assert ".method > summary" in css
    # Methodology text is expandable at the section level as well.
    assert "Metodología de las métricas" in html_text


def test_report_orders_its_regions_for_a_desktop_scan(db_file: Path) -> None:
    """Required order: summary, offers, keywords, actions/filters, footer/scope.

    The control rail is emitted after the content so the DOM, tab, and required
    order agree, and CSS places it on the right so the visual order matches too.
    """
    html_text = generate_and_read(db_file)

    positions = [
        html_text.index(marker)
        for marker in (
            'id="resumen"',
            'id="contenido"',
            'id="analitica"',
            'id="filtros"',
            "<footer",
        )
    ]
    assert positions == sorted(positions), positions

    css = _embedded_style(html_text)
    assert ".content { grid-column:1; grid-row:2;" in css
    assert ".rail { grid-column:2; grid-row:2;" in css
    assert "position:sticky" in css


def test_sticky_shell_reaches_the_controls_without_covering_content(db_file: Path) -> None:
    """The top bar and the control rail stay reachable while the offer list scrolls.

    ``--topbar-h`` is the measured height of the sticky bar at the shell's desktop
    widths, and it is the only thing keeping the rail clear of it. Changing the top
    bar's padding without updating the token re-creates the overlap this guards, so
    the literal is asserted on purpose.
    """
    css = _embedded_style(generate_and_read(db_file))

    assert "--topbar-h:78px" in css
    assert ".topbar { grid-column:1 / -1; grid-row:1; position:sticky; top:0; z-index:20;" in css
    assert "min-height:var(--topbar-h)" in css
    assert "top:calc(var(--topbar-h) + 14px)" in css
    assert "max-height:calc(100vh - var(--topbar-h) - 28px)" in css
    # The sticky layer is opaque, so a row scrolling under it never shows through, and
    # the rail keeps its own column instead of overlaying the content.
    assert "background:var(--canvas); min-height:var(--topbar-h)" in css
    assert "scrollbar-gutter:stable;" in css


def test_each_offer_is_an_independent_block_separated_by_gap_and_outline(
    db_file: Path,
) -> None:
    """Offers must not merge into one continuous list, and must not float either.

    Separation is the canvas showing through a real vertical gap between blocks,
    plus one restrained outline per block. The block keeps a radius so it reads as
    a discrete paper surface, but no blurred elevation is used as a separator: the
    only ``box-shadow`` left on a card is the flat inset ink rule that marks a
    reviewed offer.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    # The list container paints nothing at all; the gap is the separator.
    assert "#offers { display:flex; flex-direction:column; gap:var(--gap-card); }" in css

    # The gap sits in the 20-28px band a desktop reader asked for.
    card_gap = re.search(r"--gap-card:(\d+)px;", css)
    assert card_gap is not None, "the offer separation step must be a named token"
    assert 20 <= int(card_gap.group(1)) <= 28, card_gap.group(0)

    card = re.search(r"\.card \{([^}]*)\}", css, re.DOTALL)
    assert card is not None, "the base offer block must be styled"
    body = card.group(1)
    assert "background:var(--paper);" in body
    assert "border:1px solid var(--rule-strong);" in body
    assert "border-radius:var(--radius);" in body
    assert "padding:24px 26px;" in body
    assert "gap:var(--gap-block);" in body
    # No elevation and no gradient: the boundary is a hairline, not a shadow.
    assert "box-shadow" not in body
    assert "gradient" not in css
    # A per-block bottom rule is unnecessary once the gap separates the blocks.
    assert ".card:last-child { border-bottom:0; }" not in css

    # Hover still lifts the block, and reviewed keeps its flat ink rule.
    assert ".card:hover { background:var(--hover); border-color:var(--ink); }" in css
    assert ".card.reviewed:hover { background:var(--hover-strong); }" in css
    assert "box-shadow:inset 3px 0 0 var(--ink)" in css
    assert "--hover:#f8fafb" in css and "--hover-strong:#eef1f3" in css


def test_regions_are_bounded_and_offer_bands_are_separated_by_rules(
    db_file: Path,
) -> None:
    """Each major section is bounded, and the bands inside an offer never blur.

    Region boundaries use one device only: a hard ink rule under the region
    heading. Inside an offer the header, the facts, the explanation and the human
    feedback are separated by hairlines and whitespace rather than by colour, so
    the only hue left on a card is the model accent and the warning ember.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    # Region boundary: summary, offers and analytics all carry the same head rule.
    assert "gap:12px; padding-bottom:8px; border-bottom:2px solid var(--ink); }" in css
    assert html_text.count('class="section-head"') == 3
    for region in ('id="resumen"', 'id="contenido"', 'id="analitica"'):
        assert region in html_text, region

    # The offers region heading is a real heading, not a muted micro label.
    assert '<h2 id="offers-title">Ofertas</h2>' in html_text
    assert '<h2 id="kpis-title">Resumen</h2>' in html_text

    # Inside one offer: header | facts | explanation | human feedback.
    assert re.search(r"\.meta \{[^}]*border-top:1px solid var\(--hairline\);", css)
    assert re.search(r"\.meta \{[^}]*padding:14px 0 0;", css)
    assert ".explain { border-top:1px solid var(--hairline); }" in css
    assert re.search(r"\.card > \.grid \{[^}]*border-top:1px solid var\(--hairline\);", css)
    assert re.search(r"\.feedback \{[^}]*border-top:2px solid var\(--ink\);", css)
    assert re.search(r"\.card-side \{[^}]*border-left:1px solid var\(--hairline\);", css)

    # Offers are paper blocks on the canvas; the other regions are not that shape.
    assert re.search(r"\.block \{[^}]*background:var\(--surface\);", css)
    assert re.search(r"\.kpi-grid \{[^}]*background:var\(--paper\);", css)
    assert re.search(r"\.rail-block \{[^}]*background:var\(--paper\);", css)
    # Inside the sunken analytics panel, the readable surfaces come back to paper.
    assert re.search(r"\.table-wrap \{[^}]*background:var\(--paper\);", css)
    assert re.search(r"\.two-col > section \{[^}]*background:var\(--paper\);", css)


def test_offer_row_keeps_a_title_score_action_hierarchy(db_file: Path) -> None:
    """Title, score and the outbound link each read at their own level.

    The title gets its own type step so it outranks body text without competing with
    a section heading; company and location stay in the subtitle and are not repeated
    in the facts grid underneath; the only outbound link is a bordered control in the
    row's action column instead of underlined prose inside the facts.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert "--fs-title:17px" in css
    assert ".card-title h3 { font-size:var(--fs-title); line-height:1.3;" in css
    assert ".card-sub .company { color:var(--soft-ink); font-weight:600; }" in css
    # Company and location appear once, in the subtitle.
    assert 'el("span", "company", offer.company)' in html_text
    assert 'metaItem("Empresa"' not in html_text
    assert 'metaItem("Ubicación"' not in html_text

    # The action is a real control in the row's action column, still guarded.
    assert ".card-side { display:flex; flex-direction:column; align-items:flex-end;" in css
    assert 'var link = el("a", "offer-link", "Ver la oferta");' in html_text
    assert 'link.href = href;' in html_text
    assert 'link.rel = "noopener noreferrer";' in html_text
    assert 'link.target = "_blank";' in html_text
    assert 'content:"\\2197"' in css
    assert ".offer-link:hover { background:var(--ink); border-color:var(--ink); color:var(--paper); }" in css
    # The link is no longer appended to the facts list.
    assert 'var linkItem = el("li");' not in html_text


def test_disclosure_summaries_draw_a_chevron_and_show_their_state(db_file: Path) -> None:
    """Both disclosures advertise themselves as controls and state open/closed.

    The chevron is drawn with borders rather than a glyph, so no font is needed and
    the project's no-external-font rule keeps holding. The two behaviours the previous
    skin left implicit are asserted here: a legible label size, and the same affordance
    on the section-level methodology disclosure.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    for selector in (".explain > summary::before", ".method > summary::before"):
        assert selector in css, selector
    assert "border-left:7px solid var(--rule-strong);" in css
    assert ".explain[open] > summary::before {" in css
    assert "transform:rotate(90deg)" in css
    assert ".method[open] > summary::before {" in css
    # The label is a sentence-sized control with a hover and a pressed state.
    assert ".explain > summary { display:flex; align-items:center; gap:9px; cursor:pointer;" in css
    assert ".explain > summary:hover { background:var(--hover-strong); }" in css
    assert ".explain > summary:active { background:var(--hairline); }" in css
    assert ".method > summary:hover { background:var(--paper); }" in css
    # No glyph is used for the state, so nothing depends on a symbol font.
    assert 'content:"+"' not in css and 'content:"−"' not in css


def test_human_feedback_shows_selection_hover_and_a_saved_confirmation(db_file: Path) -> None:
    """Own marks are confirmed, keyboard-readable, and never hue-only.

    Selection carries a check mark and ``aria-pressed`` on top of the ink fill, the
    pressed/hover states are explicit, and the confirmation is a polite live region
    that removes itself so it never stacks or blocks the next mark. It is only written
    when ``localStorage`` actually accepted the write.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert '.decision-btn.selected::before { content:"\\2713"; margin-right:7px; }' in css
    assert ".decision-btn:hover:not(:disabled) { background:var(--hover-strong); border-color:var(--ink); }" in css
    assert ".decision-btn:active:not(:disabled) { background:var(--hairline); }" in css
    assert ".decision-btn.selected:hover:not(:disabled) { background:var(--soft-ink);" in css
    # The block heading repeats the ink square that marks the human axis in the legend.
    assert ".feedback h4::before { content:\"\"; flex:0 0 auto; width:9px; height:9px;" in css

    assert "function confirmSaved(jobId)" in html_text
    assert 'marker.setAttribute("role", "status");' in html_text
    assert "var stored = saveFeedback();" in html_text
    assert "if (stored) { confirmSaved(offer.id); }" in html_text
    assert "savedTimer = window.setTimeout(function () { marker.remove(); }, 2600);" in html_text
    # The decision state text has its own node, so the marker is not wiped by updates.
    assert 'el("span", "decision-state-text",' in html_text
    assert 'card.querySelector(".decision-state-text")' in html_text


def test_filter_rail_reports_what_is_narrowing_the_list(db_file: Path) -> None:
    """The rail says how much is filtering, and Reset is only offered when it can act."""
    html_text = generate_and_read(db_file)

    assert 'id="filters-summary"' in html_text
    assert 'aria-live="polite">Sin filtros activos</p>' in html_text
    assert '"Sin filtros activos"' in html_text
    assert '"1 filtro activo"' in html_text
    assert "function activeFilterCount()" in html_text
    assert "function applyFilters()" in html_text
    assert 'document.getElementById("reset-filters").disabled =' in html_text
    # Every narrowing control goes through the one path, so the list and the summary
    # can never disagree; ordering is not counted as a filter.
    assert html_text.count("applyFilters();") >= 7
    # Ordering is reported by its own control, so it is not counted as a filter.
    counted = re.search(r"function activeFilterCount\(\) \{(.*?)\n  \}", html_text, re.DOTALL)
    assert counted is not None
    assert "filters.sort" not in counted.group(1)
    assert "filters.review" in counted.group(1)


def test_keyword_table_headers_sort_the_rendered_rows(db_file: Path) -> None:
    """The analytics table is sortable client-side, without touching the metrics.

    Sorting only reorders the rows already in the DOM, so no value is recomputed and
    the Python analytics stay the single source of truth. A cell the stored data cannot
    support (``sin dato``) sorts last in both directions rather than passing as a low
    number, and the direction is mirrored in ``aria-sort`` rather than only in a glyph.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert "function makeSortable(table)" in html_text
    assert 'button.className = "sort-btn";' in html_text
    assert 'header.setAttribute("aria-sort", "none");' in html_text
    assert 'header.setAttribute("aria-sort", descending ? "descending" : "ascending");' in html_text
    assert 'return (a === null) ? 1 : -1;' in html_text
    assert '"sin dato" && !isNaN(parsed)' in html_text
    assert "Haga clic en un encabezado para ordenar las filas." in html_text
    assert ".sort-btn::after { content:\"\\2191\";" in css
    assert 'th[aria-sort="ascending"] .sort-btn::after { content:"\\2191"; color:var(--ink); }' in css
    assert 'th[aria-sort="descending"] .sort-btn::after { content:"\\2193"; color:var(--ink); }' in css
    # The documented header labels are still the same row of strings.
    headers = re.search(r'\["Palabra clave".*?\]', html_text, re.DOTALL)
    assert headers is not None
    assert re.findall(r'"([^"]+)"', headers.group(0))[0] == "Palabra clave"


def test_posted_at_is_shown_readably_but_the_payload_keeps_it_verbatim(db_file: Path) -> None:
    """Display-only formatting of the stored ISO timestamp, never a rewrite of it."""
    html_text = generate_and_read(db_file)
    payload = _extract_payload(html_text)

    stored = payload["offers"][0]["posted_at"]
    assert stored == "2026-09-14T10:00:00Z"
    assert "function displayTimestamp(value)" in html_text
    assert "displayTimestamp(offer.posted_at)" in html_text
    assert 'return match ? (match[1] + " " + match[2] + " UTC") : value;' in html_text
    # Anything the pattern does not fully match falls back to the stored string.
    assert "return match ? (" in html_text and ": value;" in html_text


def test_report_keeps_empty_error_and_local_storage_states(db_file: Path) -> None:
    """The states a reader can land in are still visible in this variant."""
    html_text = generate_and_read(db_file)

    assert 'id="empty-state"' in html_text
    assert "Ninguna oferta coincide con la búsqueda y los filtros actuales." in html_text
    assert "No se pudieron leer los datos del panel." in html_text
    assert "versión incompatible de jobscout" in html_text
    assert "El almacenamiento local no está disponible" in html_text
    assert "<noscript>" in html_text



def test_ember_is_reserved_for_warning_and_error_states(db_file: Path) -> None:
    """Red marks only real warning/error UI, never a recommendation state.

    Every rule that reaches for the ember token must be the warning notice (the
    token definition itself lives in ``:root``). Recommendation badges and KPI
    modifiers stay monochromatic, so the reader never learns to read a hue as a
    score.
    """
    html_text = generate_and_read(db_file)
    style = re.search(r"<style>(.*?)</style>", html_text, re.DOTALL)
    assert style is not None, "the report must embed its stylesheet"
    css = style.group(1)

    # One literal definition, referenced by token everywhere else.
    assert css.count("#e7000b") == 1
    ember_rules = [rule for rule in css.split("}") if "--ember" in rule]
    assert ember_rules, "the ember token must be defined and used"
    for rule in ember_rules:
        assert ":root" in rule or ".notice.warn" in rule, rule

    # Recommendation styling carries weight, not hue.
    badges = [rule for rule in css.split("}") if ".badge-" in rule]
    assert badges, "the recommendation badges must be styled"
    for rule in badges:
        assert "--ember" not in rule and "#e7000b" not in rule, rule


def test_feedback_region_accessible_name_identifies_the_offer(db_file: Path) -> None:
    """Each card's feedback region is named with its own title and company.

    The cards are built client-side, so the accessible name is asserted where the
    script composes it: it must derive from ``offer.title`` and ``offer.company``
    instead of a card-independent constant, so a screen-reader user can tell the
    feedback controls of one offer from another.
    """
    html_text = generate_and_read(db_file)

    region = re.search(r'box\.setAttribute\("aria-label",\s*([^;]+)\);', html_text)
    assert region is not None, "the feedback region must carry an accessible name"
    assert "Mi decisión sobre la oferta: " in region.group(1)
    assert "offerLabel" in region.group(1)

    scoped = re.search(r"var offerLabel = ([^;]+);", html_text)
    assert scoped is not None, "the scoped label must be built from the offer"
    assert "offer.title" in scoped.group(1)
    assert "offer.company" in scoped.group(1)

    controls = re.search(r'controls\.setAttribute\("aria-label",\s*([^;]+)\);', html_text)
    assert controls is not None, "the button group must carry an accessible name"
    assert "offerLabel" in controls.group(1)

    # The card-independent label must not come back.
    assert 'aria-label", "Mi decisión sobre esta oferta"' not in html_text
    # Visible Spanish text and the stored feedback surface stay unchanged.
    assert "Mi decisión (feedback)" in html_text
    assert (
        'var FEEDBACK_LABELS = { apply: "Aplicar", reject: "Descartar" };'
        in html_text
    )
    assert 'var STORAGE_KEY = payload.feedback_schema + ":" + payload.project_id;' in html_text


def test_export_button_is_visible_and_disabled_until_reviewed(db_file: Path) -> None:
    html_text = generate_and_read(db_file)
    assert "Exportar feedback local (JSON)" in html_text
    # The export payload shape is written by the page, not by the DB.
    assert "jobscout-feedback.json" in html_text
    assert "source: \"jobscout-dashboard\"" in html_text


def test_dashboard_feedback_is_binary_and_keeps_consider_legacy(db_file: Path) -> None:
    """The active human decision is Aplicar/Descartar; consider is legacy only."""
    html_text = generate_and_read(db_file)

    # The canonical Python vocabulary and the rendered UI agree.
    assert DASHBOARD_FEEDBACK_DECISIONS == ("apply", "reject")
    assert DASHBOARD_LEGACY_FEEDBACK_DECISIONS == ("consider",)
    assert 'var FEEDBACK_DECISIONS = ["apply", "reject"];' in html_text
    assert 'var FEEDBACK_LABELS = { apply: "Aplicar", reject: "Descartar" };' in html_text
    # The old human label must not come back as a clickable option.
    assert 'consider: "Revisar"' not in html_text
    assert 'var FEEDBACK_DECISIONS = ["apply", "consider", "reject"];' not in html_text
    # The legacy value still has a display-only label.
    assert 'var LEGACY_FEEDBACK_LABELS = { consider: "Considerar (histórico)" };' in html_text
    # The model recommendation axis keeps its own three-way labels (a different
    # signal), including its "Considerar" filter option.
    assert '>Considerar</option>' in html_text
    assert 'consider: "Considerar"' in html_text


def test_legacy_consider_is_displayed_as_historical_and_replaceable(db_file: Path) -> None:
    """A stored legacy decision is labelled historical and can be replaced."""
    html_text = generate_and_read(db_file)

    assert "function decisionStateText(decision)" in html_text
    assert "Decisión histórica (legacy): " in html_text
    assert "Puede reemplazarla con Aplicar o Descartar." in html_text
    # The legacy path is explicit, not an accidental fallback of the active UI.
    assert "function isLegacyFeedback(decision)" in html_text
    assert "function feedbackLabel(decision)" in html_text
    # Replacing is the ordinary active-button assignment; there is no third button.
    assert 'var FEEDBACK_DECISIONS = ["apply", "reject"];' in html_text
    # The human-approval ranking names the historical count as legacy, so the
    # analytics never present ``consider`` as a current human option.
    assert "considerar (legacy)" in html_text
    # The legacy state is marked by shape, never by the model accent or the ember.
    css = _embedded_style(html_text)
    legacy_rules = [rule for rule in css.split("}") if ".decision-state.legacy" in rule]
    assert legacy_rules, "the legacy state must have its own marker"
    for rule in legacy_rules:
        assert "--signal" not in rule and "--ember" not in rule, rule


def test_export_keeps_the_v1_schema_and_ships_legacy_verbatim(db_file: Path) -> None:
    """The export shape is unchanged and no stored value is rewritten on export."""
    html_text = generate_and_read(db_file)

    assert "schema: payload.feedback_schema" in html_text
    assert "feedback: feedback" in html_text
    assert 'var FEEDBACK_DECISIONS = ["apply", "reject"];' in html_text
    payload = _extract_payload(html_text)
    assert payload["feedback_schema"] == "jobscout.feedback.v1"


def test_storage_key_is_derived_from_the_payload_schema(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    # The key literal lives only in the payload, never duplicated in the script.
    assert 'var STORAGE_KEY = payload.feedback_schema + ":" + payload.project_id;' in html_text
    assert 'var STORAGE_KEY = "jobscout.feedback.v1' not in html_text
    payload = _extract_payload(html_text)
    assert payload["feedback_schema"] == "jobscout.feedback.v1"


def test_report_guards_against_an_incompatible_payload_schema(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert 'var EXPECTED_SCHEMA = "jobscout.dashboard.v1";' in html_text
    assert "payload.schema !== EXPECTED_SCHEMA" in html_text
    assert "versión incompatible de jobscout" in html_text


def test_client_url_guard_allows_only_http_s(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert "function safeHttpUrl" in html_text
    assert 'parsed.protocol === "https:"' in html_text
    assert 'parsed.protocol === "http:"' in html_text
    # Links go through the guard, never straight from the payload value.
    assert "link.href = href;" in html_text
    assert "link.href = offer.url;" not in html_text


def test_safe_url_drops_non_http_schemes() -> None:
    from jobscout.dashboard import _safe_url

    assert _safe_url("javascript:alert(1)") is None
    assert _safe_url("data:text/html,<script>alert(1)</script>") is None
    assert _safe_url("//evil.example/jobs/1") is None
    assert _safe_url("ftp://evil.example/jobs/1") is None
    assert _safe_url("  ") is None
    assert (
        _safe_url(" https://www.linkedin.com/jobs/view/1 ")
        == "https://www.linkedin.com/jobs/view/1"
    )


# ---------------------------------------------------------------------------
# Detail-fetch link evidence (read-only, OL-7)
# ---------------------------------------------------------------------------


def test_detail_state_classification_is_honest_for_every_state() -> None:
    assert _classify_detail_state(None, now=NOW) == "unchecked"
    assert (
        _classify_detail_state(_detail_fetch(status=DetailFetchStatus.SKIPPED), now=NOW)
        == "skipped"
    )
    for success in (DetailFetchStatus.OK, DetailFetchStatus.PARTIAL):
        fetch = _detail_fetch(
            status=success, http_status=200, fetched_at="2026-09-10T12:00:00Z"
        )
        assert _classify_detail_state(fetch, now=NOW) == "ok"
        old = _detail_fetch(
            status=success, http_status=200, fetched_at="2026-07-01T12:00:00Z"
        )
        assert _classify_detail_state(old, now=NOW) == "stale"
    assert _classify_detail_state(_detail_fetch(http_status=404), now=NOW) == "definitive_missing"
    assert _classify_detail_state(_detail_fetch(http_status=410), now=NOW) == "definitive_missing"
    # A failure that never received an HTTP response is retryable...
    assert _classify_detail_state(_detail_fetch(http_status=None), now=NOW) == "retryable_failure"
    # ...unless a raw body proves a response arrived, or a status was recorded.
    assert (
        _classify_detail_state(_detail_fetch(http_status=None, raw_document_id=7), now=NOW)
        == "failed"
    )
    assert _classify_detail_state(_detail_fetch(http_status=500), now=NOW) == "failed"
    assert _classify_detail_state(_detail_fetch(http_status=403), now=NOW) == "failed"
    # The seven states are the complete, documented vocabulary.
    assert set(DETAIL_STATES) == {
        "unchecked",
        "skipped",
        "ok",
        "stale",
        "definitive_missing",
        "retryable_failure",
        "failed",
    }


def test_detail_state_uses_the_explicit_window_and_injectable_now() -> None:
    assert DETAIL_EVIDENCE_WINDOW_DAYS == 21

    fetch = _detail_fetch(status=DetailFetchStatus.OK, fetched_at="2026-09-10T12:00:00Z")
    # The same stored evidence is current now and stale twenty days later.
    assert _classify_detail_state(fetch, now=NOW) == "ok"
    assert _classify_detail_state(fetch, now=NOW + timedelta(days=20)) == "stale"

    # Exactly the window is still current; one second past it is not.
    exactly = _detail_fetch(status=DetailFetchStatus.OK, fetched_at="2026-08-26T12:00:00Z")
    assert _classify_detail_state(exactly, now=NOW) == "ok"
    past = _detail_fetch(status=DetailFetchStatus.OK, fetched_at="2026-08-26T11:59:59Z")
    assert _classify_detail_state(past, now=NOW) == "stale"


def test_dashboard_definitive_statuses_match_the_detail_fetcher() -> None:
    # The dashboard does not import the collector, so a contract pins them.
    assert DEFINITIVE_MISSING_HTTP_STATUSES == FETCHER_DEFINITIVE_STATUSES == frozenset({404, 410})


def test_detail_evidence_refutes_resighted_404_and_keeps_redirect_evidence_safe() -> None:
    missing = _detail_fetch(http_status=404, stored_at="2026-09-10T12:00:00Z")

    refuted = _detail_evidence(missing, now=NOW, last_seen_at="2026-09-15T12:00:00Z")
    assert refuted["state"] == "definitive_missing"
    assert refuted["refuted"] is True

    unrefuted = _detail_evidence(missing, now=NOW, last_seen_at="2026-09-01T12:00:00Z")
    assert unrefuted["refuted"] is False
    assert _detail_evidence(missing, now=NOW)["refuted"] is False

    # A failed attempt never exposes a redirect, so it can never read as a link.
    broken = _detail_fetch(
        http_status=404, final_url="https://www.linkedin.com/jobs/view/999/"
    )
    assert _detail_evidence(broken, now=NOW, last_seen_at="2026-09-15T12:00:00Z")["final_url"] is None

    # A safe redirect on a successful attempt is kept as evidence.
    redirect = _detail_fetch(
        status=DetailFetchStatus.OK,
        http_status=200,
        fetched_at="2026-09-10T12:00:00Z",
        url="https://www.linkedin.com/jobs/view/1/",
        final_url="https://www.linkedin.com/jobs/view/999/",
    )
    assert (
        _detail_evidence(redirect, now=NOW)["final_url"]
        == "https://www.linkedin.com/jobs/view/999/"
    )
    # An echo of the requested URL is not a redirect, and neither is an unsafe one.
    echo = _detail_fetch(
        status=DetailFetchStatus.OK,
        http_status=200,
        fetched_at="2026-09-10T12:00:00Z",
        url="https://www.linkedin.com/jobs/view/1/",
        final_url="https://www.linkedin.com/jobs/view/1/",
    )
    assert _detail_evidence(echo, now=NOW)["final_url"] is None
    unsafe = _detail_fetch(
        status=DetailFetchStatus.OK,
        http_status=200,
        fetched_at="2026-09-10T12:00:00Z",
        final_url="javascript:alert(1)",
    )
    assert _detail_evidence(unsafe, now=NOW)["final_url"] is None


def test_detail_evidence_for_an_unchecked_offer_is_serializable() -> None:
    evidence = _detail_evidence(None, now=NOW)

    assert evidence == {
        "state": "unchecked",
        "checked_at": None,
        "age_days": None,
        "window_days": 21,
        "http_status": None,
        "content_status": None,
        "refuted": False,
        "final_url": None,
    }
    # Every value is already a JSON primitive, so the payload stays serializable.
    assert json.loads(json.dumps(evidence)) == evidence


def test_payload_detail_reports_only_the_latest_fetch(db_file: Path) -> None:
    conn = connect(db_file)
    try:
        job, _ = store_posting(
            conn,
            _posting(
                source_job_id="7777777777",
                url="https://www.linkedin.com/jobs/view/7777777777/latest",
                title="Staff Data Engineer",
                company="Delta Corp",
            ),
            now="2026-09-15T10:00:00Z",
        )
        # A stale success followed by a newer skip: only the skip may surface.
        _insert_fetch(
            conn,
            int(job.id),
            status=DetailFetchStatus.OK,
            http_status=200,
            fetched_at="2026-07-01T12:00:00Z",
        )
        _insert_fetch(
            conn,
            int(job.id),
            status=DetailFetchStatus.SKIPPED,
            stored_at="2026-09-15T12:00:00Z",
        )
    finally:
        conn.close()

    payload = _payload(db_file, top=10)
    detail = next(offer for offer in payload["offers"] if offer["id"] == job.id)["detail"]

    assert detail["state"] == "skipped"
    assert detail["window_days"] == DETAIL_EVIDENCE_WINDOW_DAYS == 21
    assert set(detail) == {
        "state",
        "checked_at",
        "age_days",
        "window_days",
        "http_status",
        "content_status",
        "refuted",
        "final_url",
    }


def test_report_renders_neutral_detail_evidence_near_the_title(db_file: Path) -> None:
    conn = connect(db_file)
    try:
        refuted, _ = store_posting(
            conn,
            _posting(
                source_job_id="7777777777",
                url="https://www.linkedin.com/jobs/view/7777777777/refuted",
                title="Staff Data Engineer",
                company="Delta Corp",
            ),
            now="2026-09-10T10:00:00Z",
        )
        # A 404 at T1, then an ordinary re-sighting at T2 refutes the evidence.
        _insert_fetch(
            conn,
            int(refuted.id),
            status=DetailFetchStatus.FAILED,
            http_status=404,
            stored_at="2026-09-10T11:00:00Z",
        )
        _resight_dashboard_job(conn, "7777777777", now="2026-09-15T10:00:00Z")

        redirected, _ = store_posting(
            conn,
            _posting(
                source_job_id="8888888888",
                url="https://www.linkedin.com/jobs/view/8888888888/redirected",
                title="Site Reliability Engineer",
                company="Epsilon LLC",
            ),
            now="2026-09-15T10:00:00Z",
        )
        _insert_fetch(
            conn,
            int(redirected.id),
            status=DetailFetchStatus.OK,
            http_status=200,
            fetched_at="2026-09-10T12:00:00Z",
            url="https://www.linkedin.com/jobs/view/8888888888/redirected",
            final_url="https://www.linkedin.com/jobs/view/8888888888/site-reliability-engineer",
        )
    finally:
        conn.close()

    html_text = generate_and_read(db_file)
    payload = _extract_payload(html_text)
    refuted_offer = next(offer for offer in payload["offers"] if offer["id"] == refuted.id)
    redirect_offer = next(offer for offer in payload["offers"] if offer["id"] == redirected.id)

    assert refuted_offer["detail"]["state"] == "definitive_missing"
    assert refuted_offer["detail"]["refuted"] is True
    assert (
        redirect_offer["detail"]["final_url"]
        == "https://www.linkedin.com/jobs/view/8888888888/site-reliability-engineer"
    )

    # The evidence is built with DOM APIs and inserted with the title, not in the
    # model axis (.card-side) nor the human axis (.feedback).
    assert "function renderDetailEvidence(offer)" in html_text
    assert "if (detailBox) { titleBox.appendChild(detailBox); }" in html_text
    assert 'el("p", "detail-evidence")' in html_text
    assert "La evidencia fue refutada por un re-avistamiento posterior de la oferta." in html_text
    # Any redirect href goes through the same client-side URL guard.
    assert "var finalHref = safeHttpUrl(detail.final_url);" in html_text
    assert "link.href = finalHref;" in html_text
    assert "card-side.appendChild(detailBox)" not in html_text
    assert "renderFeedback(offer)" in html_text and "detailBox" not in re.search(
        r"function renderFeedback\(offer\) \{(.*?)\n  \}", html_text, re.DOTALL
    ).group(1)


def test_detail_evidence_never_writes_to_the_database_or_local_storage(db_file: Path) -> None:
    conn = connect(db_file)
    try:
        job, _ = store_posting(
            conn,
            _posting(
                source_job_id="7777777777",
                url="https://www.linkedin.com/jobs/view/7777777777/write",
                title="Staff Data Engineer",
                company="Delta Corp",
            ),
            now="2026-09-15T10:00:00Z",
        )
        _insert_fetch(
            conn,
            int(job.id),
            status=DetailFetchStatus.OK,
            http_status=200,
            fetched_at="2026-09-10T12:00:00Z",
        )
    finally:
        conn.close()

    before_records = _database_fingerprint(db_file)
    before_bytes = db_file.read_bytes()

    html_text = generate_and_read(db_file)

    assert _database_fingerprint(db_file) == before_records
    assert db_file.read_bytes() == before_bytes
    # The evidence renderer never touches browser storage either.
    renderer = re.search(
        r"function renderDetailEvidence\(offer\) \{(.*?)\n  \}", html_text, re.DOTALL
    )
    assert renderer is not None
    assert "localStorage" not in renderer.group(1)
    assert "setItem" not in renderer.group(1)


def test_detail_evidence_css_stays_neutral_and_keeps_the_six_kpi_shell(db_file: Path) -> None:
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    detail_rules = [rule for rule in css.split("}") if ".detail-evidence" in rule]
    assert detail_rules, "the detail evidence band must be styled"
    for rule in detail_rules:
        assert "--signal" not in rule, rule
        assert "--ember" not in rule, rule
        assert "#e7000b" not in rule, rule
        assert "@media" not in rule, rule

    # No new media query and no seventh KPI.
    assert css.count("@media") == 1
    assert re.findall(r"@media\s*\(([^)]*)\)", css) == ["prefers-reduced-motion:reduce"]
    assert "grid-template-columns:repeat(6, minmax(0, 1fr))" in css
    cards = re.search(r"var cards = \[(.*?)\];", html_text, re.DOTALL)
    assert cards is not None
    assert cards.group(1).count('["') == 6


def test_detail_state_labels_cover_every_state_in_neutral_spanish(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    block = re.search(r"var DETAIL_STATE_LABELS = \{(.*?)\n  \};", html_text, re.DOTALL)
    assert block is not None
    assert re.findall(r"([a-z_]+):", block.group(1)) == list(DETAIL_STATES)
    for label in (
        "sin consultar el detalle",
        "consulta de detalle omitida",
        "evidencia vigente",
        "evidencia anterior a la ventana",
        "la última consulta respondió 404/410",
        "fallo de transporte reintentable",
        "fallo con respuesta HTTP",
    ):
        assert label in html_text, label


# ---------------------------------------------------------------------------
# Imported-decision view (offers without an imported human decision)
# ---------------------------------------------------------------------------


def _import_decision(
    conn: sqlite3.Connection,
    job_id: int,
    decision: str,
    *,
    updated_at: str = "2026-09-15T11:00:00.000000Z",
    reason: str | None = None,
) -> None:
    """Store one imported human decision, exactly as ``import-feedback`` would."""
    upsert_feedback(
        conn,
        JobFeedback(
            job_id=int(job_id),
            decision=decision,
            reason=reason,
            decision_updated_at=updated_at,
            project_id="test-project",
            source="test",
        ),
    )


def test_unreviewed_view_isolates_offers_without_imported_decision(db_file: Path) -> None:
    """Only the absence of a ``job_feedback`` row isolates an offer."""
    jobs = _seed_dashboard_db(db_file)
    conn = connect(db_file)
    try:
        _import_decision(conn, jobs["job1"], FeedbackDecision.APPLY)
    finally:
        conn.close()

    payload = _payload(db_file)
    by_id = {offer["id"]: offer for offer in payload["offers"]}

    assert by_id[jobs["job1"]]["imported_review"] == {"reviewed": True, "decision": "apply"}
    assert by_id[jobs["job2"]]["imported_review"] == {"reviewed": False, "decision": None}
    assert by_id[jobs["job3"]]["imported_review"] == {"reviewed": False, "decision": None}

    isolated = [
        offer["id"]
        for offer in payload["offers"]
        if not offer["imported_review"]["reviewed"]
    ]
    assert isolated == [offer["id"] for offer in payload["offers"] if offer["id"] != jobs["job1"]]


def test_imported_review_is_a_single_readonly_select(db_file: Path) -> None:
    """The per-offer read is one batched ``SELECT`` and never writes."""
    from jobscout.dashboard import _fetch_imported_feedback

    jobs = _seed_dashboard_db(db_file)
    conn = connect(db_file)
    try:
        _import_decision(conn, jobs["job1"], FeedbackDecision.APPLY)
    finally:
        conn.close()

    conn = open_readonly_database(db_file)
    statements: list[str] = []
    try:
        conn.set_trace_callback(statements.append)
        decisions = _fetch_imported_feedback(conn, list(jobs.values()))
        conn.set_trace_callback(None)
    finally:
        conn.close()

    assert decisions == {jobs["job1"]: "apply"}
    assert len(statements) == 1, statements
    statement = statements[0]
    assert statement.lstrip().upper().startswith("SELECT")
    assert "job_feedback" in statement
    for verb in ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "PRAGMA"):
        assert verb not in statement.upper(), statement


def test_unreviewed_view_is_honest_when_no_feedback_is_imported(db_file: Path) -> None:
    """An empty import never becomes an imaginary backlog metric."""
    payload = _payload(db_file)

    assert payload["summary"]["human_reviewed_jobs"] == 0
    assert all(
        offer["imported_review"] == {"reviewed": False, "decision": None}
        for offer in payload["offers"]
    )
    # No fabricated backlog, and no metric derived from the free-form cycle_id.
    assert "unreviewed_backlog" not in payload
    assert "new_in_latest_cycle" not in payload
    assert "latest_cycle" not in json.dumps(payload)

    html_text = render_dashboard_html(payload)
    assert "Todavía no hay ninguna decisión humana importada" in html_text
    assert "jobscout import-feedback" in html_text
    assert "no son una medida de backlog" in html_text


def test_imported_review_does_not_change_keyword_analytics_or_summary(
    db_file: Path,
) -> None:
    """The new per-offer axis leaves the existing imported-decision metrics intact."""
    jobs = _seed_dashboard_db(db_file)
    conn = connect(db_file)
    try:
        _import_decision(conn, jobs["job1"], FeedbackDecision.APPLY)
        _import_decision(conn, jobs["job2"], FeedbackDecision.REJECT)
    finally:
        conn.close()

    payload = _payload(db_file)
    stats = {item["keyword"]: item for item in payload["keywords"]}

    assert payload["summary"]["human_reviewed_jobs"] == 2
    keyword_a = stats["senior backend engineer"]
    assert keyword_a["human_reviewed"] == 2
    assert keyword_a["human_apply"] == 1
    assert keyword_a["human_reject"] == 1
    assert keyword_a["human_apply_rate"] == 0.5
    assert keyword_a["human_positive_yield"] == 0.5

    reviewed = [offer for offer in payload["offers"] if offer["imported_review"]["reviewed"]]
    assert sorted(offer["id"] for offer in reviewed) == sorted([jobs["job1"], jobs["job2"]])


def test_reviewed_view_keeps_consider_legacy_and_binary_buttons(db_file: Path) -> None:
    """A legacy imported ``consider`` is shown as stored, with no third button."""
    jobs = _seed_dashboard_db(db_file)
    conn = connect(db_file)
    try:
        _import_decision(conn, jobs["job1"], FeedbackDecision.CONSIDER)
    finally:
        conn.close()

    payload = _payload(db_file)
    legacy = next(offer for offer in payload["offers"] if offer["id"] == jobs["job1"])
    assert legacy["imported_review"] == {"reviewed": True, "decision": "consider"}

    html_text = render_dashboard_html(payload)
    assert 'var FEEDBACK_DECISIONS = ["apply", "reject"];' in html_text
    assert 'var FEEDBACK_LABELS = { apply: "Aplicar", reject: "Descartar" };' in html_text
    assert 'var LEGACY_FEEDBACK_LABELS = { consider: "Considerar (histórico)" };' in html_text
    # The imported-decision view adds a filter control, not a decision choice.
    assert 'id="filter-imported-review" class="btn btn-ghost filter-toggle"' in html_text
    assert 'var FEEDBACK_DECISIONS = ["apply", "consider", "reject"];' not in html_text


def test_unreviewed_view_adds_no_media_query_and_keeps_one_reduced_motion_guard(
    db_file: Path,
) -> None:
    """The new control adds no media query and stays hue-free."""
    css = _embedded_style(generate_and_read(db_file))

    assert css.count("@media") == 1
    assert re.findall(r"@media\s*\(([^)]*)\)", css) == ["prefers-reduced-motion:reduce"]
    assert "* { transition:none !important; animation:none !important; }" in css

    toggle_rules = [rule for rule in css.split("}") if ".filter-toggle" in rule]
    assert toggle_rules, "the imported-decision toggle must be styled"
    for rule in toggle_rules:
        assert "--signal" not in rule, rule
        assert "--ember" not in rule, rule
        assert "@media" not in rule, rule


def test_imported_review_never_serializes_the_imported_reason(db_file: Path) -> None:
    """Only the stored decision reaches the payload and the HTML."""
    jobs = _seed_dashboard_db(db_file)
    reason = "REASON-TOKEN-77aa-should-never-appear"
    conn = connect(db_file)
    try:
        _import_decision(conn, jobs["job1"], FeedbackDecision.APPLY, reason=reason)
    finally:
        conn.close()

    html_text = generate_and_read(db_file)
    payload = _extract_payload(html_text)
    offer = next(item for item in payload["offers"] if item["id"] == jobs["job1"])

    assert offer["imported_review"] == {"reviewed": True, "decision": "apply"}
    assert "reason" not in offer["imported_review"]
    assert reason not in html_text
    assert reason not in json.dumps(payload)


def test_imported_review_read_leaves_the_database_unchanged(db_file: Path) -> None:
    """Generating the report with imported decisions writes nothing."""
    jobs = _seed_dashboard_db(db_file)
    conn = connect(db_file)
    try:
        _import_decision(conn, jobs["job1"], FeedbackDecision.APPLY, reason="kept in sqlite")
    finally:
        conn.close()

    before = _database_fingerprint(db_file)
    before_bytes = db_file.read_bytes()

    html_text = generate_and_read(db_file)

    assert _database_fingerprint(db_file) == before
    assert db_file.read_bytes() == before_bytes
    assert "job_feedback" in html_text


def test_unreviewed_view_renders_when_localstorage_is_unavailable(db_file: Path) -> None:
    """The imported axis is payload/SQLite data, so it survives a blocked localStorage.

    Some browsers restrict ``localStorage`` on ``file://`` pages. The report still
    renders and filters in that case: the imported-decision control reads
    ``offer.imported_review`` from the embedded payload, and the browser-local
    ``reviewed``/``unreviewed`` filter stays a separate, guarded path.
    """
    html_text = generate_and_read(db_file)

    assert "offer.imported_review" in html_text
    assert "function refreshImportedReviewNote()" in html_text
    # Browser storage is still read defensively and is never required to render.
    assert "window.localStorage.getItem(STORAGE_KEY)" in html_text
    assert "catch (err) {" in html_text
    assert "filters.review" in html_text  # the browser-local axis stays separate


# ---------------------------------------------------------------------------
# Keyword analytics
# ---------------------------------------------------------------------------


def test_keyword_aggregation_counts_and_quality(db_file: Path) -> None:
    payload = _payload(db_file)
    stats = {item["keyword"]: item for item in payload["keywords"]}

    keyword_a = stats["senior backend engineer"]
    assert keyword_a["sightings"] == 3
    assert keyword_a["unique_jobs"] == 2
    assert keyword_a["duplicate_sightings"] == 1
    assert keyword_a["duplicate_rate"] == 0.3333
    assert keyword_a["scored_jobs"] == 2
    assert keyword_a["unscored_jobs"] == 0
    assert keyword_a["avg_score"] == 60.0
    assert keyword_a["top5_avg_score"] == 60.0
    assert keyword_a["top10_avg_score"] == 60.0
    assert keyword_a["apply_count"] == 1
    assert keyword_a["consider_count"] == 1
    # No human decision was imported, so every human metric stays unknown.
    assert keyword_a["human_reviewed"] == 0
    assert keyword_a["human_apply_rate"] is None
    assert keyword_a["human_positive_yield"] is None

    keyword_b = stats["data platform"]
    assert keyword_b["sightings"] == 2
    assert keyword_b["unique_jobs"] == 2
    assert keyword_b["duplicate_sightings"] == 0
    assert keyword_b["duplicate_rate"] == 0.0
    assert keyword_b["scored_jobs"] == 1
    assert keyword_b["unscored_jobs"] == 1
    assert keyword_b["avg_score"] == 40.0
    assert keyword_b["top5_avg_score"] == 40.0
    assert keyword_b["apply_count"] == 0
    assert keyword_b["consider_count"] == 1

    # Nothing was deactivated and every job has a usable recent date.
    assert keyword_a["inactive_jobs"] == 0
    assert keyword_a["inactive_rate"] == 0.0
    assert keyword_a["fresh_jobs"] == 2
    assert keyword_a["unknown_age_jobs"] == 0
    assert keyword_a["fresh_share"] == 1.0

    # Quality ranks by average score; a duplicate sighting must not double-count.
    assert [item["keyword"] for item in payload["top_quality"]] == [
        "senior backend engineer",
        "data platform",
    ]
    # Yield ranks by positive recommendations.
    assert [item["keyword"] for item in payload["top_yield"]] == [
        "senior backend engineer",
        "data platform",
    ]
    # A keyword with no imported human decision cannot rank in human approval.
    assert payload["top_human"] == []


def _record_feedback(
    conn: sqlite3.Connection, job_id: int, decision: str, *, updated_at: str
) -> None:
    upsert_feedback(
        conn,
        JobFeedback(
            job_id=job_id,
            decision=decision,
            decision_updated_at=updated_at,
            project_id="test-project",
            source="test",
        ),
    )


def test_keyword_human_metrics_use_the_imported_feedback(db_file: Path) -> None:
    """Human rates come from ``job_feedback``, kept apart from the model signal."""
    jobs = _seed_dashboard_db(db_file)
    conn = connect(db_file)
    try:
        _record_feedback(
            conn, jobs["job1"], FeedbackDecision.APPLY, updated_at="2026-09-15T11:00:00.000000Z"
        )
        _record_feedback(
            conn, jobs["job2"], FeedbackDecision.REJECT, updated_at="2026-09-15T11:05:00.000000Z"
        )
    finally:
        conn.close()

    payload = _payload(db_file)
    stats = {item["keyword"]: item for item in payload["keywords"]}

    keyword_a = stats["senior backend engineer"]
    assert keyword_a["human_reviewed"] == 2
    assert keyword_a["human_apply"] == 1
    assert keyword_a["human_consider"] == 0
    assert keyword_a["human_reject"] == 1
    assert keyword_a["human_apply_rate"] == 0.5
    assert keyword_a["human_positive_yield"] == 0.5

    # A real zero is kept distinct from "no data": job2 was reviewed and rejected.
    keyword_b = stats["data platform"]
    assert keyword_b["human_reviewed"] == 1
    assert keyword_b["human_apply"] == 0
    assert keyword_b["human_apply_rate"] == 0.0
    assert keyword_b["human_positive_yield"] == 0.0

    assert [item["keyword"] for item in payload["top_human"]] == [
        "senior backend engineer",
        "data platform",
    ]
    assert payload["summary"]["human_reviewed_jobs"] == 2
    # With feedback imported, the missing-feedback warning must not appear.
    assert not any("no hay feedback humano importado" in item for item in payload["warnings"])


def test_keyword_analytics_report_inactive_and_freshness_honestly(db_file: Path) -> None:
    """Inactive share and freshness use only stored, provable facts."""
    before = _payload(db_file)
    stats_before = {item["keyword"]: item for item in before["keywords"]}
    assert stats_before["data platform"]["inactive_jobs"] == 0

    conn = connect(db_file)
    try:
        with conn:
            conn.execute("UPDATE jobs SET is_active = 0 WHERE title = 'Platform Engineer'")
    finally:
        conn.close()

    after = _payload(db_file)
    stats_after = {item["keyword"]: item for item in after["keywords"]}

    keyword_b = stats_after["data platform"]
    # The deactivated posting leaves the active metrics but is still counted.
    assert keyword_b["unique_jobs"] == 1
    assert keyword_b["inactive_jobs"] == 1
    assert keyword_b["inactive_rate"] == 0.5
    assert keyword_b["fresh_share"] == 1.0
    assert keyword_b["unknown_age_jobs"] == 0

    # A keyword whose only postings were deactivated disappears from the active
    # analytics rather than looking productive with empty numbers.
    assert set(stats_after) == {"senior backend engineer", "data platform"}
    # No human feedback was imported for this database.
    assert after["summary"]["human_reviewed_jobs"] == 0
    assert any("no hay feedback humano importado" in item for item in after["warnings"])


def test_keyword_freshness_is_unknown_without_usable_dates(tmp_path: Path) -> None:
    """No stored date means no freshness claim, not an invented 0%."""
    path = tmp_path / "undated.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        job, _ = store_posting(
            conn,
            _posting(posted_at=None),
            now="2026-09-15T10:00:00Z",
        )
        # Erase the fallback date so neither posted_at nor first_seen_at is usable.
        with conn:
            conn.execute("UPDATE jobs SET first_seen_at = '' WHERE id = ?", (job.id,))
        run = _run(conn, "undated keyword")
        _observe(conn, run.id, job.id, rank=1)
    finally:
        conn.close()

    payload = _payload(path)
    stats = {item["keyword"]: item for item in payload["keywords"]}
    keyword = stats["undated keyword"]

    assert keyword["unknown_age_jobs"] == 1
    assert keyword["fresh_jobs"] == 0
    assert keyword["fresh_share"] is None


def test_keyword_table_renders_the_documented_columns(db_file: Path) -> None:
    """The rendered header row is the surface the README documents.

    ``unscored_jobs`` and ``inactive_jobs`` stay in the payload but have no column
    of their own (the table shows the inactive *rate*), so the documentation must
    not promise those columns.
    """
    html_text = generate_and_read(db_file)

    headers = re.search(r'\["Palabra clave".*?\]', html_text, re.DOTALL)
    assert headers is not None, "the keyword table must render its header row"
    assert re.findall(r'"([^"]+)"', headers.group(0)) == [
        "Palabra clave",
        "Apariciones",
        "Ofertas únicas",
        "Tasa duplicados",
        "Puntaje promedio",
        "Top 5",
        "Top 10",
        "Con puntaje",
        "Aplicar",
        "Considerar",
        "Revisadas",
        "Tasa aplicar (humano)",
        "Frescura",
        "No activas",
    ]
    # Metrics the data cannot support read "sin dato", never a fabricated zero.
    assert 'function pct(value)' in html_text
    assert '? "sin dato" : Math.round(value * 100) + "%"' in html_text
    assert 'function score(value)' in html_text
    assert 'id="top-human"' in html_text

    payload = _extract_payload(html_text)
    assert "unscored_jobs" in payload["keywords"][0]
    assert "inactive_jobs" in payload["keywords"][0]
    assert "human_apply_rate" in payload["keywords"][0]


def test_inactive_jobs_are_excluded_from_keyword_analytics(db_file: Path) -> None:
    before = _payload(db_file)
    stats_before = {item["keyword"]: item for item in before["keywords"]}
    assert stats_before["data platform"]["unique_jobs"] == 2

    conn = connect(db_file)
    try:
        # The Platform Engineer job is only observed by "data platform".
        with conn:
            conn.execute("UPDATE jobs SET is_active = 0 WHERE title = 'Platform Engineer'")
    finally:
        conn.close()

    after = _payload(db_file)
    stats_after = {item["keyword"]: item for item in after["keywords"]}

    assert stats_after["data platform"]["unique_jobs"] == 1
    assert stats_after["data platform"]["sightings"] == 1
    assert after["summary"]["active_jobs"] == 2
    # The offers list is consistent with the analytics: both drop the job.
    assert all(offer["title"] != "Platform Engineer" for offer in after["offers"])


def test_keyword_analytics_exclude_smoke_cycles(tmp_path: Path) -> None:
    """Smoke cycles contribute to no keyword metric, regular cycles still do.

    Every analytics query independently excludes the smoke prefix: sightings,
    the inactive-job totals, and the observation inputs that feed score and
    freshness. A regular cycle whose ID merely *contains* ``smoke`` stays in.
    """
    path = tmp_path / "smoke-cycles.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        regular, _ = store_posting(
            conn, _posting(source_job_id="1000000001"), now="2026-09-15T10:00:00Z"
        )
        regular_unscored, _ = store_posting(
            conn,
            _posting(source_job_id="1000000002", title="Regular Unscored"),
            now="2026-09-15T10:00:00Z",
        )
        substring, _ = store_posting(
            conn,
            _posting(source_job_id="1000000003", title="Contains Smoke"),
            now="2026-09-15T10:00:00Z",
        )
        null_cycle, _ = store_posting(
            conn,
            _posting(source_job_id="1000000004", title="Null Cycle"),
            now="2026-09-15T10:00:00Z",
        )
        smoke_scored, _ = store_posting(
            conn,
            _posting(source_job_id="2000000001", title="Smoke Scored"),
            now="2026-09-15T10:00:00Z",
        )
        smoke_inactive, _ = store_posting(
            conn,
            _posting(source_job_id="2000000002", title="Smoke Inactive"),
            now="2026-09-15T10:00:00Z",
        )
        smoke_only, _ = store_posting(
            conn,
            _posting(source_job_id="2000000003", title="Smoke Only Keyword"),
            now="2026-09-15T10:00:00Z",
        )

        regular_run = _run(conn, "python", cycle_id="cycle-1")
        _observe(conn, regular_run.id, regular.id, rank=1)
        _observe(conn, regular_run.id, regular_unscored.id, rank=2)
        substring_run = _run(conn, "python", cycle_id="cycle-smoke")
        _observe(conn, substring_run.id, substring.id, rank=1)
        null_run = _run(conn, "python", cycle_id=None)
        _observe(conn, null_run.id, null_cycle.id, rank=1)

        # Reproduce the four reported smoke cycles across seven runs.
        smoke_runs = (
            ("smoke-20260928-2231", "python", smoke_scored.id),
            ("smoke-20260928-2231", "python", smoke_scored.id),
            ("smoke-20260928-2232", "python", smoke_inactive.id),
            ("smoke-20260928-2232", "python", smoke_inactive.id),
            ("smoke-20260929-1130", "python", smoke_scored.id),
            ("smoke-prueba", "smoke keyword", smoke_only.id),
            ("smoke-prueba", "smoke keyword", smoke_only.id),
        )
        for cycle_id, keyword, job_id in smoke_runs:
            smoke_run = _run(conn, keyword, cycle_id=cycle_id)
            _observe(conn, smoke_run.id, job_id, rank=1)

        insert_evaluation(conn, _score(regular.id, 80.0, Recommendation.APPLY))
        insert_evaluation(conn, _score(substring.id, 40.0, Recommendation.APPLY))
        insert_evaluation(conn, _score(null_cycle.id, 60.0, Recommendation.CONSIDER))
        # A smoke-only score would move avg_score and scored_jobs if it leaked in.
        insert_evaluation(conn, _score(smoke_scored.id, 10.0, Recommendation.APPLY))
        insert_evaluation(conn, _score(smoke_only.id, 90.0, Recommendation.APPLY))
        with conn:
            conn.execute("UPDATE jobs SET is_active = 0 WHERE id = ?", (smoke_inactive.id,))
    finally:
        conn.close()

    payload = _payload(path)
    stats = {item["keyword"]: item for item in payload["keywords"]}

    # All four smoke cycles / seven runs stay out of the table. A smoke-only
    # keyword row does not exist at all.
    assert list(stats) == ["python"]
    assert all("smoke" not in json.dumps(item).lower() for item in payload["keywords"])

    keyword = stats["python"]
    assert keyword["source_key"] == "linkedin|python"
    # regular(2) + cycle-smoke(1) + null cycle(1); the two smoke runs are gone.
    assert keyword["sightings"] == 4
    assert keyword["unique_jobs"] == 4
    # (80 + 40 + 60) / 3: the smoke-only 10 is not observed.
    assert keyword["scored_jobs"] == 3
    assert keyword["avg_score"] == 60.0
    # The smoke-only inactive offer is absent from the inactive totals.
    assert keyword["inactive_jobs"] == 0
    assert keyword["inactive_rate"] == 0.0


def test_keyword_analytics_are_empty_without_provenance(tmp_path: Path) -> None:
    path = tmp_path / "no-provenance.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        job, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
        insert_evaluation(conn, _score(job.id, 70.0, Recommendation.APPLY))
    finally:
        conn.close()

    payload = _payload(path)

    assert payload["keywords"] == []
    assert payload["top_quality"] == []
    assert payload["top_yield"] == []
    assert payload["top_human"] == []
    assert any("procedencia de palabras clave" in warning for warning in payload["warnings"])


def test_missing_evaluations_warn_but_still_render(tmp_path: Path) -> None:
    path = tmp_path / "unscored.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
    finally:
        conn.close()

    payload = _payload(path)
    assert payload["summary"]["scored"] == 0
    assert payload["summary"]["active_jobs"] == 1
    assert any("ninguna evaluación exitosa" in warning for warning in payload["warnings"])

    html_text = render_dashboard_html(payload)
    assert "ninguna evaluación exitosa" in html_text


def test_empty_database_warns_clearly(tmp_path: Path) -> None:
    path = tmp_path / "empty-init.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
    finally:
        conn.close()

    payload = _payload(path)
    assert payload["summary"]["active_jobs"] == 0
    assert any("no hay ofertas activas" in warning for warning in payload["warnings"])
    # An empty database is still a valid report, not an error.
    html_text = render_dashboard_html(payload)
    assert "jobscout-data" in html_text


def test_top_quality_ignores_keywords_without_scores(tmp_path: Path) -> None:
    path = tmp_path / "mixed.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        scored, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
        unscored, _ = store_posting(
            conn,
            _posting(
                source_job_id="7777777777",
                url="https://www.linkedin.com/jobs/view/7777777777/x",
                title="Unscored Role",
            ),
            now="2026-09-15T10:00:00Z",
        )
        run_scored = _run(conn, "scored keyword")
        run_unscored = _run(conn, "unscored keyword")
        _observe(conn, run_scored.id, scored.id, rank=1)
        _observe(conn, run_unscored.id, unscored.id, rank=1)
        # A scored job with no positive recommendation must not appear in yield.
        insert_evaluation(conn, _score(scored.id, 55.0, Recommendation.SKIP))
    finally:
        conn.close()

    payload = _payload(path)

    assert [item["keyword"] for item in payload["top_quality"]] == ["scored keyword"]
    assert payload["top_yield"] == []
    assert payload["top_human"] == []
    stats = {item["keyword"]: item for item in payload["keywords"]}
    assert stats["unscored keyword"]["avg_score"] is None
    assert stats["unscored keyword"]["top5_avg_score"] is None
    assert stats["unscored keyword"]["scored_jobs"] == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_dashboard(db_file: Path, output: Path, *extra: str) -> int:
    return main(
        [
            "--db",
            str(db_file),
            "dashboard",
            "--output",
            str(output),
            *extra,
        ]
    )


def test_cli_dashboard_writes_the_report(db_file: Path, tmp_path: Path, capsys) -> None:
    output = tmp_path / "reports" / "dashboard.html"

    assert _cli_dashboard(db_file, output) == EXIT_OK

    text = capsys.readouterr().out
    assert "wrote" in text
    assert "3 offer(s)" in text
    assert "2 scored" in text
    assert "localStorage" in text
    assert output.is_file()


def test_cli_dashboard_json_summary(db_file: Path, tmp_path: Path, capsys) -> None:
    output = tmp_path / "reports" / "dashboard.json-run.html"

    assert _cli_dashboard(db_file, output, "--json") == EXIT_OK

    summary = json.loads(capsys.readouterr().out)
    assert summary["offers"] == 3
    assert summary["scored"] == 2
    assert summary["keywords"] == 2
    assert summary["bytes_written"] > 0
    assert Path(summary["output"]) == output


def test_cli_dashboard_respects_top(db_file: Path, tmp_path: Path, capsys) -> None:
    output = tmp_path / "reports" / "top.html"

    assert _cli_dashboard(db_file, output, "--top", "1", "--json") == EXIT_OK

    summary = json.loads(capsys.readouterr().out)
    assert summary["offers"] == 1


def test_cli_dashboard_missing_database_fails_cleanly(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope" / "db.sqlite3"

    assert _cli_dashboard(missing, tmp_path / "report.html") == EXIT_ERROR

    err = capsys.readouterr().err
    assert "dashboard error" in err
    assert "no database found" in err
    assert not (tmp_path / "report.html").exists()


def test_cli_dashboard_rejects_non_positive_top(db_file: Path, tmp_path: Path, capsys) -> None:
    assert _cli_dashboard(db_file, tmp_path / "report.html", "--top", "0") == EXIT_ERROR
    assert "must be >= 1" in capsys.readouterr().err


def test_cli_dashboard_rejects_out_of_range_top(db_file: Path, tmp_path: Path, capsys) -> None:
    assert _cli_dashboard(db_file, tmp_path / "low.html", "--top", "-3") == EXIT_ERROR
    assert "must be >= 1" in capsys.readouterr().err

    assert _cli_dashboard(db_file, tmp_path / "high.html", "--top", "100000") == EXIT_ERROR
    assert "must be <=" in capsys.readouterr().err


def test_cli_dashboard_refuses_to_overwrite_the_database(db_file: Path, capsys) -> None:
    before = db_file.read_bytes()

    assert _cli_dashboard(db_file, db_file) == EXIT_ERROR

    err = capsys.readouterr().err
    assert "dashboard error" in err
    assert "resolves to the SQLite database" in err
    assert db_file.read_bytes() == before


def test_cli_dashboard_rejects_an_explicitly_empty_output(db_file: Path, capsys) -> None:
    assert main(["--db", str(db_file), "dashboard", "--output", ""]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "dashboard error" in err
    assert "must not be empty" in err


def test_cli_dashboard_reports_an_unwritable_output_cleanly(
    db_file: Path, tmp_path: Path, capsys
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("in the way", encoding="utf-8")

    assert _cli_dashboard(db_file, blocker / "report.html") == EXIT_ERROR

    err = capsys.readouterr().err
    assert "dashboard error" in err
    assert "could not be written" in err


def test_cli_dashboard_default_output_path_is_relative_to_cwd(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """The default target is ``reports/jobscout-dashboard.html`` in the cwd."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    db_file = workdir / "data" / "jobscout.sqlite3"
    _seed_dashboard_db(db_file)
    monkeypatch.chdir(workdir)

    assert main(["--db", str(db_file), "dashboard"]) == EXIT_OK

    default_output = workdir / "reports" / "jobscout-dashboard.html"
    assert default_output.is_file()
    output = capsys.readouterr().out
    assert "jobscout-dashboard.html" in output
    # The report itself never carries an absolute local path, not even the one
    # the CLI used to write it.
    assert str(workdir) not in default_output.read_text(encoding="utf-8")


def test_cli_smoke_against_bundled_fixtures(tmp_path: Path, capsys) -> None:
    """Fixture-only end to end: ingest, score, then generate the report."""
    db_file = tmp_path / "smoke.sqlite3"
    output = tmp_path / "reports" / "smoke.html"
    fixture = FIXTURES / "linkedin_search_sample.json"
    profile = FIXTURES / "profile_test.yaml"

    assert main(["--db", str(db_file), "ingest-fixture", str(fixture)]) == EXIT_OK
    assert (
        main(
            [
                "--db",
                str(db_file),
                "--profile",
                str(profile),
                "score",
                "--all",
                "--rescore",
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    capsys.readouterr()

    assert _cli_dashboard(db_file, output, "--top", "3", "--json") == EXIT_OK

    summary = json.loads(capsys.readouterr().out)
    assert summary["offers"] == 3
    assert summary["scored"] >= 1
    assert summary["bytes_written"] > 0
    assert output.is_file()

    data = _extract_payload(output.read_text(encoding="utf-8"))
    assert data["offers"], "the smoke report must contain offers"
    assert all(offer["score"] is not None for offer in data["offers"])
    # Provenance from the fixture's own query is preserved as data, not as truth.
    assert data["keywords"][0]["keyword"]
    assert data["keywords"][0]["sightings"] >= 1
    assert data["offers"][0]["keywords"][0]["rank"] >= 1


# ---------------------------------------------------------------------------
# Honest scoreless / failed / fallback rendering
# ---------------------------------------------------------------------------


def _seed_evaluation_states(path: Path) -> dict[str, int]:
    """Four jobs: a success, an unresolved, a failed and a never-evaluated one."""
    conn = connect(path)
    try:
        init_db(conn)
        good, _ = store_posting(
            conn,
            _posting(
                source_job_id="4111111111",
                url="https://www.linkedin.com/jobs/view/4111111111/good",
            ),
            now="2026-09-15T10:00:00Z",
        )
        unresolved, _ = store_posting(
            conn,
            _posting(
                source_job_id="4222222222",
                url="https://www.linkedin.com/jobs/view/4222222222/unresolved",
                title="Unresolved Role",
            ),
            now="2026-09-15T10:00:00Z",
        )
        failed, _ = store_posting(
            conn,
            _posting(
                source_job_id="4333333333",
                url="https://www.linkedin.com/jobs/view/4333333333/failed",
                title="Failed Role",
            ),
            now="2026-09-15T10:00:00Z",
        )
        never, _ = store_posting(
            conn,
            _posting(
                source_job_id="4444444444",
                url="https://www.linkedin.com/jobs/view/4444444444/never",
                title="Never Evaluated",
            ),
            now="2026-09-15T10:00:00Z",
        )

        insert_evaluation(conn, _score(good.id, 80.0, Recommendation.APPLY))
        insert_evaluation(
            conn,
            Evaluation(
                job_id=unresolved.id,
                scorer=ScorerName.OPENCODE,
                overall_score=30.0,
                recommendation=Recommendation.NEED_MORE_INFO,
                status=EvaluationStatus.NEEDS_MORE_INFO,
            ),
        )
        insert_evaluation(
            conn,
            Evaluation(
                job_id=failed.id,
                scorer=ScorerName.LLM,
                status=EvaluationStatus.PARSE_ERROR,
                error="SECRET-ERROR-TOKEN-xyz-should-not-be-embedded",
            ),
        )
    finally:
        conn.close()
    return {
        "good": int(good.id),
        "unresolved": int(unresolved.id),
        "failed": int(failed.id),
        "never": int(never.id),
    }


def test_scoreless_offers_expose_their_evaluation_state(tmp_path: Path) -> None:
    path = tmp_path / "states.sqlite3"
    ids = _seed_evaluation_states(path)

    payload = _payload(path, top=10)
    by_id = {offer["id"]: offer for offer in payload["offers"]}

    assert by_id[ids["good"]]["score"] == 80.0
    assert by_id[ids["good"]]["recommendation"] == "apply"
    assert by_id[ids["good"]]["evaluation_status"] == "ok"

    # A scoreless offer carries the real reason, never an invented score.
    assert by_id[ids["unresolved"]]["score"] is None
    assert by_id[ids["unresolved"]]["evaluation_status"] == "needs_more_info"
    assert by_id[ids["failed"]]["score"] is None
    assert by_id[ids["failed"]]["evaluation_status"] == "parse_error"
    assert by_id[ids["never"]]["evaluation_status"] is None

    states = payload["summary"]["evaluation_states"]
    assert states["ok"] == 1
    assert states["needs_more_info"] == 1
    assert states["parse_error"] == 1
    assert states["none"] == 1


def test_failed_and_unresolved_states_render_without_leaking_the_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "states.sqlite3"
    _seed_evaluation_states(path)

    html_text = generate_and_read(path)

    assert "Requiere más información" in html_text
    assert "Evaluación fallida" in html_text
    # The stored error text is never embedded: only the status is serialized.
    assert "SECRET-ERROR-TOKEN-xyz" not in html_text


def test_fallback_provenance_is_visible_and_not_a_placeholder(tmp_path: Path) -> None:
    path = tmp_path / "fallback.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        job, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
        insert_evaluation(
            conn,
            Evaluation(
                job_id=job.id,
                scorer=ScorerName.LLM,
                overall_score=63.0,
                recommendation=Recommendation.CONSIDER,
                model="heuristic-v1+fallback",
                error="LLM response was contract-invalid",
                rationale="Deterministic fallback. [fallback: heuristic used]",
            ),
        )
    finally:
        conn.close()

    payload = _payload(path)
    offer = payload["offers"][0]
    assert offer["score"] == 63.0
    assert offer["model"] == "heuristic-v1+fallback"
    assert "fallback" in offer["rationale"].lower()

    html_text = generate_and_read(path)
    assert "heuristic-v1+fallback" in html_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def generate_and_read(db_file: Path) -> str:
    output = db_file.parent / "generated.html"
    generate_dashboard(db_file, output)
    return output.read_text(encoding="utf-8")


def _database_fingerprint(db_file: Path) -> dict[str, int]:
    conn = connect(db_file)
    try:
        tables = ("jobs", "search_runs", "search_run_items", "evaluations", "profiles", "raw_documents")
        fingerprint = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        fingerprint["user_version"] = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return fingerprint
    finally:
        conn.close()
