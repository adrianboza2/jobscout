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
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import FIXTURES, no_network

from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.dashboard import (
    DEFAULT_TOP,
    MAX_TOP,
    DashboardError,
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
    insert_evaluation,
    record_run_item,
    store_posting,
    upsert_feedback,
    upsert_profile,
)
from jobscout.models import (
    DuplicateKind,
    Evaluation,
    FeedbackDecision,
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


def _run(conn: sqlite3.Connection, keyword: str, provider: str = "linkedin") -> SearchRun:
    return create_search_run(
        conn,
        SearchRun(
            provider=provider,
            label=f"{provider}:{keyword}",
            query=keyword,
            location="Buenos Aires",
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
    # The report's own script block is written exactly once.
    assert html_text.count('"use strict";') == 1


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
    assert html_text.count('"use strict";') == 1


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
    assert (
        'var FEEDBACK_LABELS = { apply: "Aplicar", consider: "Revisar", reject: "Descartar" };'
        in html_text
    )
    assert '["apply", "consider", "reject"]' in html_text
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
        'var FEEDBACK_LABELS = { apply: "Aplicar", consider: "Revisar", reject: "Descartar" };'
        in html_text
    )
    assert 'var STORAGE_KEY = payload.feedback_schema + ":" + payload.project_id;' in html_text


def test_export_button_is_visible_and_disabled_until_reviewed(db_file: Path) -> None:
    html_text = generate_and_read(db_file)
    assert "Exportar feedback local (JSON)" in html_text
    # The export payload shape is written by the page, not by the DB.
    assert "jobscout-feedback.json" in html_text
    assert "source: \"jobscout-dashboard\"" in html_text


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
