"""Dashboard tests: XSS safety, data minimization, aggregation, and CLI paths.

Everything runs offline against temporary SQLite fixtures. A fixture-only smoke
run exercises the same code path the CLI uses, and one test blocks sockets
outright to prove the report never touches the network.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import FIXTURES, no_network

from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.dashboard import (
    DASHBOARD_FEEDBACK_DECISIONS,
    DASHBOARD_LEGACY_FEEDBACK_DECISIONS,
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
    all_active_jobs,
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
    DimensionScore,
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
from jobscout.scoring import stale_job_ids
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


#: The pairs the owner retired (2026-09-29 decision, the CP-8 additions, and
#: the 2026-10-27 function-browse replacement), provider-keyed. Kept local to
#: the test module on purpose: the dashboard tests must fail if the production
#: registry changes, not silently follow it.
RETIRED_KEYWORD_PAIRS: tuple[tuple[str, str], ...] = (
    ("tecnoempleo", "Platform Engineer"),
    ("tecnoempleo", "Azure Security Engineer"),
    ("tecnoempleo", "IAM Engineer"),
    ("tecnoempleo", "Junior Cloud Security Engineer"),
    ("tecnoempleo", "Security Infrastructure Engineer"),
    ("tecnoempleo", "Junior Cloud Engineer"),
    ("tecnoempleo", "Junior DevOps Engineer"),
    ("tecnoempleo", "Cloud Security Engineer"),
    ("tecnoempleo", "DevSecOps Engineer"),
    ("tecnoempleo", "Junior DevSecOps Engineer"),
    ("tecnoempleo", "Azure Cloud Security"),
    ("tecnoempleo", "Azure DevSecOps"),
    ("linkedin", "Identity and Access Management Engineer"),
    ("linkedin", "IAM Engineer"),
)

_SERIES_JOB_SEQ = itertools.count(1)


def _seed_keyword_series(
    conn: sqlite3.Connection,
    *,
    provider: str,
    keyword: str,
    scores: tuple[float, ...] = (50.0,),
) -> None:
    """One run for ``(provider, keyword)`` and one scored posting per score."""
    run = _run(conn, keyword, provider=provider)
    for index, score in enumerate(scores):
        serial = next(_SERIES_JOB_SEQ)
        job, _ = store_posting(
            conn,
            _posting(
                source=provider,
                source_job_id=f"series-{serial}",
                url=f"https://example.test/jobs/series-{serial}",
                title=f"Role {serial}",
            ),
            now="2026-09-15T10:00:00Z",
        )
        _observe(conn, run.id, job.id, rank=index + 1)
        insert_evaluation(conn, _score(job.id, score, Recommendation.APPLY))


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    path = tmp_path / "dash" / "dashboard.sqlite3"
    _seed_dashboard_db(path)
    return path


def _seed_dr1_db(path: Path) -> dict[str, int]:
    """Offers, cycles, variants and failures for the DR-1 payload behaviours."""
    conn = connect(path)
    try:
        init_db(conn)

        def store(serial: str, source: str, title: str, first_seen: str, salary=None) -> int:
            job, _ = store_posting(
                conn,
                _posting(
                    source=source,
                    source_job_id=serial,
                    url=f"https://example.test/jobs/{serial}",
                    title=title,
                    salary=salary,
                ),
                now=first_seen,
            )
            return int(job.id)

        job_a = store(
            "dr1-a", "linkedin", "Offer A", "2026-10-19T07:05:00Z", salary="€35-45K"
        )
        job_b = store("dr1-b", "linkedin", "Offer B", "2026-10-19T07:00:00Z")
        job_c = store(
            "dr1-c",
            "tecnoempleo",
            "Offer C",
            "2026-10-18T23:59:00Z",
            salary="   ",
        )
        job_d = store(
            "dr1-d",
            "getmanfred",
            "Offer D",
            "2026-10-20T10:05:00Z",
            salary="  €35  -45K ",
        )
        job_e = store("dr1-e", "linkedin", "Offer E", "2026-10-19T08:00:00Z")

        variant_state = {
            (job_a, "linkedin"): ("ok", 500),
            (job_b, "linkedin"): ("unknown", 500),
            (job_c, "tecnoempleo"): ("unknown", 0),
            (job_d, "getmanfred"): ("ok", 500),
            (job_e, "linkedin"): ("unknown", 500),
        }
        for (job_id, source), (content_status, description_chars) in variant_state.items():
            conn.execute(
                """
                UPDATE job_variants
                SET content_status = ?, description_chars = ?
                WHERE job_id = ? AND source = ?
                """,
                (content_status, description_chars, job_id, source),
            )
        conn.execute("UPDATE jobs SET is_active = 0 WHERE id = ?", (job_e,))
        conn.execute("UPDATE job_variants SET is_active = 0 WHERE job_id = ?", (job_e,))

        insert_evaluation(
            conn,
            _score(
                job_a,
                90.0,
                Recommendation.APPLY,
                dimensions=[
                    DimensionScore(
                        name="role_fit",
                        score=90.0,
                        weight=0.3,
                        rationale="r",
                        evidence=["q1"],
                    )
                ],
                rationale="r",
            ),
        )
        insert_evaluation(conn, _score(job_d, 75.0, Recommendation.CONSIDER))

        def run_at(
            provider: str,
            cycle_id: str | None,
            started_at: str,
            status: SearchRunStatus = SearchRunStatus.COMPLETED,
        ) -> None:
            create_search_run(
                conn,
                SearchRun(
                    provider=provider,
                    label=f"{provider}:{started_at}",
                    cycle_id=cycle_id,
                    status=status,
                    started_at=started_at,
                ),
            )

        run_at("linkedin", "2026-10-16-sweep", "2026-10-16T07:00:00Z")
        run_at("linkedin", "2026-10-19-sweep", "2026-10-19T07:00:00Z")
        run_at(
            "tecnoempleo",
            "2026-10-19-sweep",
            "2026-10-19T07:20:00Z",
            SearchRunStatus.FAILED_CLOSED,
        )
        run_at(
            "getmanfred",
            "2026-10-19-sweep",
            "2026-10-19T07:40:00Z",
            SearchRunStatus.FAILED,
        )
        run_at("linkedin", "smoke-1", "2026-10-20T09:00:00Z")
        run_at("linkedin", None, "2026-10-20T10:00:00Z")
        run_at(
            "linkedin-details",
            None,
            "2026-10-19T09:00:00Z",
            SearchRunStatus.FAILED,
        )
        run_at(
            "linkedin-details",
            None,
            "2026-10-18T09:00:00Z",
            SearchRunStatus.FAILED,
        )

        # One active and one retired keyword series, so the dropped keyword
        # fields are checked on real rows rather than on empty lists.
        active_run = _run(conn, "Python Developer", provider="linkedin")
        _observe(conn, active_run.id, job_a, rank=1)
        retired_run = _run(conn, "Platform Engineer", provider="tecnoempleo")
        _observe(conn, retired_run.id, job_c, rank=1)

        conn.commit()
    finally:
        conn.close()
    return {
        "a": job_a,
        "b": job_b,
        "c": job_c,
        "d": job_d,
        "e": job_e,
    }


@pytest.fixture
def dr1_db(tmp_path: Path) -> Path:
    path = tmp_path / "dash" / "dr1.sqlite3"
    _seed_dr1_db(path)
    return path


def _seed_dr1_db_ids(db_file: Path) -> dict[str, int]:
    """Resolve the DR-1 fixture's offer ids by their ``dr1-*`` serials."""
    conn = connect(db_file)
    try:
        rows = conn.execute(
            "SELECT id, source_job_id FROM jobs WHERE source_job_id LIKE 'dr1-%'"
        ).fetchall()
        return {
            str(row["source_job_id"]).removeprefix("dr1-"): int(row["id"])
            for row in rows
        }
    finally:
        conn.close()


def _seed_many_active_offers(path: Path, count: int, *, inactive: int = 0) -> None:
    """Insert ``count`` active offers directly, plus ``inactive`` deactivated ones.

    A bulk insert keeps a >1000-row fixture fast while exercising the same
    dashboard read path; the dashboard only needs identity plus ``is_active``.
    """
    rows = [
        (
            f"fp-active-{index}",
            f"dk-active-{index}",
            "linkedin",
            "2026-09-15T10:00:00Z",
            "2026-09-15T10:00:00Z",
            1,
        )
        for index in range(count)
    ]
    rows += [
        (
            f"fp-inactive-{index}",
            f"dk-inactive-{index}",
            "linkedin",
            "2026-09-15T10:00:00Z",
            "2026-09-15T10:00:00Z",
            0,
        )
        for index in range(inactive)
    ]
    conn = connect(path)
    try:
        init_db(conn)
        conn.executemany(
            """
            INSERT INTO jobs (
                fingerprint, dedupe_key, source, first_seen_at, last_seen_at, is_active
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()



def _payload(db_file: Path, *, top: int | None = DEFAULT_TOP) -> dict:
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


def test_default_dashboard_includes_every_active_offer_and_no_top_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "many.sqlite3"
    _seed_many_active_offers(path, 30, inactive=3)

    payload = _payload(path)

    assert len(payload["offers"]) == 30
    assert payload["summary"]["jobs_shown"] == 30
    assert payload["summary"]["active_jobs"] == 30
    assert payload["limits"]["top"] is None


def test_default_dashboard_has_no_hidden_max_above_the_explicit_cap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "huge.sqlite3"
    _seed_many_active_offers(path, MAX_TOP + 1)

    payload = _payload(path)

    assert len(payload["offers"]) == MAX_TOP + 1
    assert payload["limits"]["top"] is None


def test_explicit_top_still_bounds_the_all_offers_default(tmp_path: Path) -> None:
    path = tmp_path / "many.sqlite3"
    _seed_many_active_offers(path, 30)

    payload = _payload(path, top=5)

    assert len(payload["offers"]) == 5
    assert payload["limits"]["top"] == 5


def test_default_scope_omits_the_top_limit_label(tmp_path: Path) -> None:
    path = tmp_path / "many.sqlite3"
    _seed_many_active_offers(path, 30)
    default_output = tmp_path / "default.html"
    bounded_output = tmp_path / "bounded.html"

    summary = generate_dashboard(path, default_output)
    bounded = generate_dashboard(path, bounded_output, top=7)

    assert summary.offers == 30
    assert "(l\u00edmite top" not in default_output.read_text(encoding="utf-8")
    assert "(l\u00edmite top 7)" in bounded_output.read_text(encoding="utf-8")
    assert bounded.offers == 7


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
    # The unscored job has no posted_at, so its age falls back to first_seen_at
    # (2026-09-15T10:00Z, 26 h before NOW).
    unscored = payload["offers"][2]
    assert unscored["score"] is None
    assert unscored["recommendation"] is None
    assert unscored["age_days"] == 1.08


def test_top_limits_the_embedded_offers(db_file: Path) -> None:
    payload = _payload(db_file, top=2)
    assert len(payload["offers"]) == 2
    assert payload["summary"]["jobs_shown"] == 2
    # Aggregation still covers every job in the database, not just the shown ones.
    assert payload["summary"]["keywords"] == 2


def test_offer_carries_every_field_the_report_promises(db_file: Path) -> None:
    offer = _payload(db_file, top=1)["offers"][0]

    for key in (
        "id", "title", "company", "location", "url", "age_days",
        "score", "recommendation", "confidence", "model", "rationale",
        "strengths", "gaps", "blockers", "unknowns", "keyword_keys",
    ):
        assert key in offer, key
    assert offer["url"].startswith("https://www.linkedin.com/jobs/view/")
    assert offer["age_days"] is not None
    assert offer["keyword_keys"] == ["linkedin|senior backend engineer"]


def test_rationale_is_kept_in_full(db_file: Path) -> None:
    conn = connect(db_file)
    try:
        job, _ = store_posting(
            conn,
            _posting(source_job_id="5555555555", url="https://www.linkedin.com/jobs/view/5555555555/long"),
            now="2026-09-15T11:00:00Z",
        )
        from jobscout.dashboard import MAX_FIELD_CHARS

        rationale = "x" * MAX_FIELD_CHARS + " \n\t " + "y" * MAX_FIELD_CHARS
        insert_evaluation(
            conn,
            _score(job.id, 60.0, Recommendation.CONSIDER, rationale=rationale),
        )
    finally:
        conn.close()

    payload = _payload(db_file, top=10)
    rated = next(offer for offer in payload["offers"] if offer["score"] == 60.0)
    assert rated["rationale"] == "x" * MAX_FIELD_CHARS + " " + "y" * MAX_FIELD_CHARS
    assert len(rated["rationale"]) > MAX_FIELD_CHARS


# ---------------------------------------------------------------------------
# DR-1 payload
# ---------------------------------------------------------------------------

DR1_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _dr1_payload(db_file: Path) -> dict:
    conn = open_readonly_database(db_file)
    try:
        return build_payload(
            conn, project_id=project_key(db_file), now=DR1_NOW
        )
    finally:
        conn.close()


def test_status_reports_the_latest_non_smoke_sweep(dr1_db: Path) -> None:
    """Behaviour 1: the newest dated cycle wins; smoke and null cycles are ignored."""
    status = _dr1_payload(dr1_db)["status"]

    assert status["last_sweep_cycle"] == "2026-10-19-sweep"
    assert status["last_sweep_at"] == "2026-10-19T07:00:00Z"


def test_is_new_uses_the_sweep_start_inclusively(dr1_db: Path) -> None:
    """Behaviour 2: offers at or after the first run of the sweep are new."""
    jobs = _seed_dr1_db_ids(dr1_db)
    offers = {offer["id"]: offer for offer in _dr1_payload(dr1_db)["offers"]}

    assert offers[jobs["a"]]["is_new"] is True
    assert offers[jobs["b"]]["is_new"] is True
    assert offers[jobs["c"]]["is_new"] is False
    assert offers[jobs["d"]]["is_new"] is True


def test_new_jobs_and_new_scored_cover_every_active_offer(dr1_db: Path) -> None:
    """Behaviour 3: counts use all active offers, not the embedded ``top`` slice."""
    status = _dr1_payload(dr1_db)["status"]

    assert status["new_jobs"] == 3
    assert status["new_scored"] == 2


def test_failed_runs_split_searches_from_detail_runs(dr1_db: Path) -> None:
    """Behaviour 4: only failures since the sweep start, split by provider suffix."""
    status = _dr1_payload(dr1_db)["status"]

    assert status["failed_runs"] == {"searches": 2, "details": 1}


def test_pending_details_lists_every_active_variant_source(dr1_db: Path) -> None:
    """Behaviour 5: one sorted count per source, including zero."""
    status = _dr1_payload(dr1_db)["status"]

    assert status["pending_details"] == {
        "getmanfred": 0,
        "linkedin": 1,
        "tecnoempleo": 1,
    }


def test_offer_payload_marks_offers_waiting_for_details(db_file: Path) -> None:
    """Rule 1: a scoreless offer without a description says so in the payload."""
    conn = connect(db_file)
    try:
        with conn:
            conn.execute(
                "UPDATE jobs SET content_status = 'partial', description = NULL "
                "WHERE title = 'Platform Engineer'"
            )
    finally:
        conn.close()

    payload = _payload(db_file)
    offers = {offer["title"]: offer for offer in payload["offers"]}

    assert offers["Platform Engineer"]["pending_details"] is True
    assert offers["Senior Backend Engineer"]["pending_details"] is False
    # The notice counts pending-details offers apart from the rest.
    assert payload["summary"]["pending_details_unscored"] == 1
    assert any("pendientes de detalles" in item for item in payload["warnings"])
    assert "Pendiente de detalles" in render_dashboard_html(payload)


def test_offer_payload_clears_pending_details_after_failed_attempts(
    db_file: Path,
) -> None:
    """The scorer's exception is visible in the report, so both agree."""
    conn = connect(db_file)
    try:
        conn.execute(
            "UPDATE jobs SET content_status = 'partial', description = NULL "
            "WHERE title = 'Platform Engineer'"
        )
        job_id = int(
            conn.execute(
                "SELECT id FROM jobs WHERE title = 'Platform Engineer'"
            ).fetchone()[0]
        )
        for http_status in (403, None):
            insert_detail_fetch(
                conn,
                JobDetailFetch(
                    job_id=job_id,
                    source="linkedin",
                    status=DetailFetchStatus.FAILED,
                    http_status=http_status,
                    stored_at="2026-09-15T12:00:00Z",
                ),
            )
    finally:
        conn.close()

    payload = _payload(db_file)
    offer = next(
        item for item in payload["offers"] if item["title"] == "Platform Engineer"
    )

    assert offer["pending_details"] is False
    assert payload["summary"]["pending_details_unscored"] == 0


def test_status_without_any_sweep_cycle_reports_nothing_new(db_file: Path) -> None:
    """Behaviour 6: no dated cycle means no latest sweep and no new offers."""
    payload = _payload(db_file)
    status = payload["status"]

    assert status["last_sweep_cycle"] is None
    assert status["last_sweep_at"] is None
    assert status["new_jobs"] == 0
    assert status["new_scored"] == 0
    assert status["failed_runs"] == {"searches": 0, "details": 0}
    assert all(offer["is_new"] is False for offer in payload["offers"])


def test_unparseable_first_seen_is_not_new_and_does_not_raise(dr1_db: Path) -> None:
    """Behaviour 7: an unusable timestamp is not evidence of a new offer."""
    jobs = _seed_dr1_db_ids(dr1_db)
    conn = connect(dr1_db)
    try:
        conn.execute(
            "UPDATE jobs SET first_seen_at = 'not-a-date' WHERE id = ?", (jobs["a"],)
        )
        conn.commit()
    finally:
        conn.close()

    offers = {offer["id"]: offer for offer in _dr1_payload(dr1_db)["offers"]}
    assert offers[jobs["a"]]["is_new"] is False


def test_dimensions_are_parsed_from_the_successful_evaluation(dr1_db: Path) -> None:
    """Behaviour 8: dimensions carry all five fields with numeric values."""
    jobs = _seed_dr1_db_ids(dr1_db)
    offers = {offer["id"]: offer for offer in _dr1_payload(dr1_db)["offers"]}

    assert offers[jobs["a"]]["dimensions"] == [
        {
            "name": "role_fit",
            "score": 90.0,
            "weight": 0.3,
            "rationale": "r",
            "evidence": ["q1"],
        }
    ]


def test_broken_empty_and_missing_dimensions_are_empty_lists(dr1_db: Path) -> None:
    """Behaviour 9: ``[]``, broken JSON and no evaluation all yield ``[]``."""
    jobs = _seed_dr1_db_ids(dr1_db)
    conn = connect(dr1_db)
    try:
        conn.execute(
            "UPDATE evaluations SET dimensions_json = '[]' WHERE job_id = ?", (jobs["a"],)
        )
        conn.execute(
            "UPDATE evaluations SET dimensions_json = '{' WHERE job_id = ?", (jobs["d"],)
        )
        conn.commit()
    finally:
        conn.close()

    offers = {offer["id"]: offer for offer in _dr1_payload(dr1_db)["offers"]}
    assert offers[jobs["a"]]["dimensions"] == []
    assert offers[jobs["d"]]["dimensions"] == []
    assert offers[jobs["b"]]["dimensions"] == []


def test_malformed_dimension_entries_are_dropped_without_inventing_scores(
    dr1_db: Path,
) -> None:
    """Behaviour 10: no name or no numeric score means the entry is dropped."""
    jobs = _seed_dr1_db_ids(dr1_db)
    conn = connect(dr1_db)
    try:
        conn.execute(
            "UPDATE evaluations SET dimensions_json = ? WHERE job_id = ?",
            (
                json.dumps(
                    [
                        {"score": 80},
                        {"name": "x", "score": "alto"},
                        "texto",
                        {"name": "ok", "score": 70},
                    ]
                ),
                jobs["a"],
            ),
        )
        conn.commit()
    finally:
        conn.close()

    offers = {offer["id"]: offer for offer in _dr1_payload(dr1_db)["offers"]}
    assert offers[jobs["a"]]["dimensions"] == [
        {
            "name": "ok",
            "score": 70.0,
            "weight": 1.0,
            "rationale": "",
            "evidence": [],
        }
    ]


def test_dimensions_come_from_the_latest_successful_evaluation(dr1_db: Path) -> None:
    """Behaviour 11: a newer failed evaluation never hides the successful one."""
    jobs = _seed_dr1_db_ids(dr1_db)
    conn = connect(dr1_db)
    try:
        insert_evaluation(
            conn,
            _score(
                jobs["a"],
                0.0,
                Recommendation.NEED_MORE_INFO,
                overall_score=None,
                status=EvaluationStatus.ERROR,
                dimensions=[DimensionScore(name="error_dim", score=1.0)],
                rationale="newer error",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    offers = {offer["id"]: offer for offer in _dr1_payload(dr1_db)["offers"]}
    assert offers[jobs["a"]]["dimensions"][0]["name"] == "role_fit"


def test_salary_is_collapsed_or_none(dr1_db: Path) -> None:
    """Behaviour 12: the stated salary is trimmed/collapsed, never parsed."""
    jobs = _seed_dr1_db_ids(dr1_db)
    offers = {offer["id"]: offer for offer in _dr1_payload(dr1_db)["offers"]}

    assert offers[jobs["a"]]["salary"] == "€35-45K"
    assert offers[jobs["b"]]["salary"] is None
    assert offers[jobs["c"]]["salary"] is None
    assert offers[jobs["d"]]["salary"] == "€35 -45K"


def test_salary_estimate_ships_compact_or_null(dr1_db: Path) -> None:
    """The model's estimate ships as [low, mid, high, tier, basis], else null."""
    jobs = _seed_dr1_db_ids(dr1_db)
    conn = connect(dr1_db)
    try:
        insert_evaluation(
            conn,
            _score(
                jobs["b"],
                72.0,
                Recommendation.APPLY,
                salary_estimate_low=22_000,
                salary_estimate_mid=25_000,
                salary_estimate_high=28_000,
                employer_tier="consultora_grande",
                salary_basis="Junior cloud at a large consultancy, hybrid.",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    offers = {offer["id"]: offer for offer in _dr1_payload(dr1_db)["offers"]}
    assert offers[jobs["b"]]["salary_est"] == [
        22_000,
        25_000,
        28_000,
        "consultora_grande",
        "Junior cloud at a large consultancy, hybrid.",
    ]
    assert offers[jobs["c"]]["salary_est"] is None


def test_dropped_payload_fields_are_absent(dr1_db: Path) -> None:
    """Behaviour 13: removed offer, detail and keyword fields no longer ship."""
    jobs = _seed_dr1_db_ids(dr1_db)
    payload = _dr1_payload(dr1_db)
    offer = next(item for item in payload["offers"] if item["id"] == jobs["a"])

    assert "source" not in offer
    assert "sighting_count" not in offer
    # DR-6: fields the page never read, and per-sighting keyword provenance.
    for key in (
        "posted_at", "first_seen_at", "last_seen_at", "age_source", "seniority",
        "employment_type", "content_status", "scorer", "keywords",
    ):
        assert key not in offer, key
    assert set(offer["preference"]) == {"outcome", "reason", "evidence"}
    assert "detail" not in offer
    keyword_rows = payload["keywords"] + payload["retired_keywords"]
    assert keyword_rows
    for item in keyword_rows:
        assert "retired_on" not in item
        assert "retired_reason" not in item


def test_dr1_payload_is_read_only(dr1_db: Path) -> None:
    """Behaviour 14: building the new payload leaves the database bytes unchanged."""
    before_hash = hashlib.sha256(dr1_db.read_bytes()).hexdigest()
    before_fingerprint = _database_fingerprint(dr1_db)

    _dr1_payload(dr1_db)

    assert hashlib.sha256(dr1_db.read_bytes()).hexdigest() == before_hash
    assert _database_fingerprint(dr1_db) == before_fingerprint


# ---------------------------------------------------------------------------
# Client-side filter surface
# ---------------------------------------------------------------------------


def test_report_exposes_filters_search_feedback_and_summary(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    for element_id in (
        "filter-text",
        "filter-recommendation",
        "filter-new",
        "filter-min-score",
        "filter-keyword",
        "filter-sort",
        "result-count",
        "export-feedback",
        "feedback-count",
    ):
        assert f'id="{element_id}"' in html_text, element_id

    # Removed toolbar controls must stay gone: they were not in the mockup and
    # their filters could hide offers without any visible control.
    for removed_id in ("filter-review", "filter-imported-review", "reset-filters"):
        assert f'id="{removed_id}"' not in html_text, removed_id

    # The toolbar keeps the existing ids; the recommendation axis is a segmented
    # button group, so the option values become data attributes on the buttons.
    assert 'id="filter-recommendation" role="group"' in html_text
    assert 'data-value="apply" aria-pressed="false">Aplicar</button>' in html_text
    assert 'data-value="consider" aria-pressed="false">Considerar</button>' in html_text
    assert 'data-value="skip" aria-pressed="false">Descartar</button>' in html_text
    # The minimum score is the mockup's select, with the same three thresholds.
    assert re.search(
        r'<select id="filter-min-score"[^>]*>\s*'
        r'<option value="0">Score ≥ 0</option>\s*'
        r'<option value="50">≥ 50</option>\s*'
        r'<option value="70">≥ 70</option>',
        html_text,
    )
    # The KPI summary is gone; the status line replaces it.
    assert 'id="summary"' not in html_text
    assert 'id="summary-note"' not in html_text
    # Human feedback labels stay separate from the model recommendation labels.
    # The human surface is binary; the legacy value has a display-only label.
    assert 'var FEEDBACK_LABELS = { apply: "Aplicar", reject: "Descartar" };' in html_text
    assert 'var LEGACY_FEEDBACK_LABELS = { consider: "Considerar (histórico)" };' in html_text
    assert 'var FEEDBACK_DECISIONS = ["apply", "reject"];' in html_text
    assert "localStorage" in html_text
    assert "escribe en la base de datos" in html_text

    payload = _extract_payload(html_text)
    assert payload["summary"]["recommendations"]["apply"] == 1
    assert payload["summary"]["recommendations"]["consider"] == 1
    assert payload["feedback_schema"] == "jobscout.feedback.v1"
    assert payload["project_id"] == project_key(db_file)
    # The storage key is namespaced by project so two databases never collide.
    assert 'var STORAGE_KEY = payload.feedback_schema + ":" + payload.project_id;' in html_text


def test_visible_ui_text_is_neutral_spanish(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert '<html lang="es">' in html_text
    for label in (
        "jobscout",
        "Ofertas",
        "Palabras clave",
        "Buscar",
        "Todas",
        "Aplicar",
        "Considerar",
        "Descartar",
        "Solo nuevas",
        "limpiar",
        "Mostrar 50 más",
        "Puntos fuertes",
        "Carencias",
        "Bloqueos",
        "Incógnitas",
        "Justificación del modelo",
        "Salario · bruto anual",
        "Ver la oferta",
        "sin evaluar",
        "sin desglose en esta evaluación",
        "indicado",
        "sin dato",
        "Último barrido",
        "Sin barridos",
        "Detalles pendientes:",
        "fallos",
    ):
        assert label in html_text, label
    # The old card copy, the rail copy, and the KPI labels must not come back.
    for retired in (
        "Buscar y filtrar",
        "Privacidad y funcionamiento sin conexión",
        "Restablecer filtros",
        "Ofertas mostradas",
        "Revisadas (tu decisión)",
        "Dos ejes distintos",
        "Avisos",
        "Sin avisos.",
        "Explicabilidad del puntaje y procedencia",
        "Mi decisión (feedback)",
        "Brechas",
        "Sin puntaje",
        "Top 10 por calidad del puntaje",
        "Metodología de las métricas",
        "Desempeño por palabra clave",
    ):
        assert retired not in html_text, retired

    # The embedded payload keeps its English schema keys and values.
    payload = _extract_payload(html_text)
    assert payload["schema"] == "jobscout.dashboard.v1"
    assert "recommendation" in payload["offers"][0]
    assert payload["offers"][0]["recommendation"] == "apply"


def test_dashboard_exposes_accessible_tabs_and_status_structure(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    # The shell exposes the tab pattern and one status line instead of KPI cards.
    assert 'role="tablist"' in html_text
    assert html_text.count('role="tab" ') == 2
    assert 'aria-selected="true"' in html_text
    assert 'aria-selected="false"' in html_text
    assert 'aria-controls="panel-offers"' in html_text
    assert 'aria-controls="panel-keywords"' in html_text
    assert 'id="status-line" aria-label="Estado del último barrido"' in html_text
    assert 'id="summary"' not in html_text
    assert 'id="kpi-reviewed-value"' not in html_text
    assert 'role="search"' in html_text
    assert 'id="keyword-table"' in html_text
    assert 'class="skip-link"' in html_text
    # Feedback buttons expose a pressed state so colour is not the only signal.
    assert 'setAttribute("aria-pressed"' in html_text


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
        "--paper:#f3f0e8",
        "--sheet:#fbfaf6",
        "--ink:#17160f",
        "--ink-2:#55524a",
        "--ink-3:#7a766c",
        "--rule:#d8d3c6",
        "--rule-2:#e8e4d9",
        "--signal:#bd3319",
        "--signal-wash:#f6e3dc",
        "--mid:#74651d",
        "--mid-wash:#f0ead2",
        "--low:#7a766c",
        "--money:#2f6b3a",
        "--money-wash:#e3ede2",
    ):
        assert token in css, token
    for family in ("--serif:", "--sans:", "--mono:"):
        assert family in css, family
    assert '--serif:"Charis SIL","Iowan Old Style","Charter","Georgia",serif;' in css
    assert '--sans:"Inter",system-ui,-apple-system,"Segoe UI",sans-serif;' in css
    assert '--mono:"DM Mono",ui-monospace,"SF Mono","Cascadia Mono",monospace;' in css

    # The old rail/KPI token set must not come back.
    for retired in (
        "--canvas", "--surface", "--soft-ink", "--muted", "--hairline",
        "--rule-strong", "--ember", "--hover", "--rail-width", "--topbar-h",
        "--panel-max", "--font-sans", "--font-mono", "--fs-micro", "--fs-small",
        "--fs-body", "--fs-title", "--fs-heading", "--fs-display", "--gap-card",
        "--gap-block", "--radius", "--pad-block", "--space", "--accent",
        "--ok-soft", "--warn-soft", "prefers-color",
    ):
        assert retired not in html_text, retired

    # Embedded fonts only: every face is a packaged data: URI, so the report has
    # no webfont host and no remote asset URL.
    assert "Geist" not in html_text
    assert "@font-face" in css
    assert not re.search(r'url\(\s*["\']?https?://', html_text)
    # A data: URI is allowed only for the embedded woff2 faces.
    for match in re.findall(r"url\(\s*([^)]*)\)", css):
        assert match.startswith("data:font/woff2;base64,"), match

    # Closed type scale: 11/12/13/14/15/16/22/26, and no rule hard-codes a size.
    scale = re.findall(r"--fs-[a-z0-9]+:(\d+)px", css)
    assert scale == ["11", "12", "13", "14", "15", "16", "22", "26"]
    for declared in re.findall(r"font-size:\s*([^;]+);", css):
        declared = declared.strip()
        assert declared.startswith("var(--fs-"), declared
    for declared in re.findall(r"font:\s*([^;]+);", css):
        assert declared.strip() == "inherit", declared

    # Spacing tokens keep the mockup's exact pixel values available without
    # hard-coding them in a rule.
    spacing = re.findall(r"--sp-[a-z0-9]+:(\d+)px", css)
    assert spacing == [
        "2", "4", "5", "6", "7", "8", "9", "10", "12", "14", "16", "18",
        "24", "32", "36", "48", "90",
    ]
    for declared in re.findall(r"(?:padding|margin|gap)[^:;{]*:\s*([^;{}]+);", css):
        assert "px" not in declared, declared

    # Corners are square or at most 2px, and elevation is a hairline only.
    for declared in re.findall(r"border-radius:\s*([^;]+);", css):
        for value in re.findall(r"(\d+)px", declared):
            assert int(value) <= 2, declared
    assert "box-shadow" not in css
    assert "gradient" not in css

    # The panel shell keeps its structure: sticky masthead, tabs, toolbar.
    for hook in (
        'class="panel"',
        'class="masthead"',
        'class="tabs"',
        'class="toolbar"',
        'class="content"',
        'id="panel-offers"',
        'id="panel-keywords"',
        'id="keyword-table"',
    ):
        assert hook in html_text, hook


def test_report_embeds_the_three_font_families_as_data_uris(db_file: Path) -> None:
    """The report ships its typefaces inline, so it renders offline from file://."""
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    faces = re.findall(r"@font-face \{(.*?)\}", css, re.DOTALL)
    assert len(faces) == 5
    assert sum('font-family:"Charis SIL";' in face for face in faces) == 2
    assert sum('font-family:"Inter";' in face for face in faces) == 1
    assert sum('font-family:"DM Mono";' in face for face in faces) == 2
    assert any("font-weight:100 900;" in face for face in faces)
    assert any("font-weight:500 900;" in face for face in faces)
    for face in faces:
        assert 'src:url(data:font/woff2;base64,' in face
        assert 'format("woff2");' in face
        assert "font-display:swap;" in face


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


def test_accent_is_scoped_to_the_model_axis_only(db_file: Path) -> None:
    """The accent marks the model recommendation and nothing else.

    Conflating the two axes is the failure mode this guards: a reader must never be
    able to read the accent as their own decision, so every rule that reaches for the
    signal token is the model recommendation surface, and no human-feedback rule uses
    it. Human feedback carries weight, shape, and an ink rule instead.
    """
    css = _embedded_style(generate_and_read(db_file))

    accent_rules = [rule for rule in css.split("}") if "--signal" in rule]
    assert accent_rules, "the signal token must be defined and used"
    allowed = (
        ":root",
        ".status-line .bad",
        ".s-hi",
        ".chip.new",
        ".chip.veto",
        ".chip.rec-apply",
        ".lists .blk h3",
    )
    for rule in accent_rules:
        assert any(hook in rule for hook in allowed), rule

    human_rules = [
        rule
        for rule in css.split("}")
        if ".decision-btn" in rule or ".feedback" in rule
    ]
    assert human_rules, "the human feedback surface must be styled"
    for rule in human_rules:
        assert "--signal" not in rule, rule

    # The two axes are still named in visible copy, not only in CSS.
    html_text = generate_and_read(db_file)
    assert "Justificación del modelo" in html_text
    assert "Mi decisión sobre la oferta" in html_text


def test_explainability_is_a_native_progressive_disclosure(db_file: Path) -> None:
    """Each offer is a native <details> row whose body is built on first open.

    Native <details>/<summary> is used on purpose: the platform owns the keyboard
    and screen-reader behaviour, so no custom ARIA and no enhancement pass has to
    reimplement it. The body is added only when the row is opened.
    """
    html_text = generate_and_read(db_file)

    assert 'var details = el("details", "row");' in html_text
    assert 'details.setAttribute("data-job-id", String(offer.id));' in html_text
    assert 'details.addEventListener("toggle", function () {' in html_text
    assert 'details.appendChild(renderBody(offer));' in html_text
    assert "function renderBody(offer)" in html_text
    # The bulk expand/collapse controls and the old enhancement pass are gone.
    assert 'id="explain-controls"' not in html_text
    assert "Expandir todo" not in html_text
    assert "Contraer todo" not in html_text
    assert "MutationObserver" not in html_text
    assert "function enhance(" not in html_text
    assert "Explicabilidad del puntaje y procedencia" not in html_text
    # Additive only: the report script is still written exactly once.
    assert html_text.count("var EXPECTED_SCHEMA =") == 1

    css = _embedded_style(html_text)
    assert ".row[open] .chev" in css
    # The section-level methodology disclosure went with the old keyword panel.
    assert "Metodología de las métricas" not in html_text


def test_report_orders_its_regions_for_a_desktop_scan(db_file: Path) -> None:
    """Required order: masthead/status, tabs, toolbar, offers, keywords."""
    html_text = generate_and_read(db_file)

    positions = [
        html_text.index(marker)
        for marker in (
            'class="masthead"',
            'id="status-line"',
            'role="tablist"',
            'class="toolbar"',
            'id="offers"',
            'id="panel-keywords"',
        )
    ]
    assert positions == sorted(positions), positions

    css = _embedded_style(html_text)
    assert "header.masthead { position:sticky; top:0;" in css
    # The offers view owns the toolbar; the keyword view is its sibling panel.
    assert 'id="panel-offers" role="tabpanel"' in html_text
    assert 'id="panel-keywords" role="tabpanel"' in html_text


def test_sticky_shell_reaches_the_controls_without_covering_content(db_file: Path) -> None:
    """The masthead stays reachable while the offer list scrolls.

    It is the only sticky element and keeps the paper background, so rows never
    show through. The back-to-top control floats in the bottom-right corner,
    outside the masthead.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert "header.masthead { position:sticky; top:0; z-index:20;" in css
    assert "background:var(--paper);" in css
    assert "--topbar-h" not in css
    assert 'id="back-to-top" aria-label="Volver arriba"' in html_text
    header = html_text.split('<header class="masthead">')[1].split("</header>")[0]
    assert "back-to-top" not in header
    assert "function bindBackToTop()" in html_text
    assert "window.scrollY <= window.innerHeight" in html_text
    assert "window.scrollTo(0, 0)" in html_text
    assert ".to-top { position:fixed; right:var(--sp-24); bottom:var(--sp-24);" in css


def test_each_offer_is_an_independent_block_separated_by_gap_and_outline(
    db_file: Path,
) -> None:
    """Offers form one flat list whose rows are separated by a hairline.

    There is no floating card: the list removes its own chrome, and each row
    closes with the same restrained rule instead of elevation.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert ".list { margin:0; padding:0; list-style:none; }" in css
    assert ".row { border-bottom:1px solid var(--rule); }" in css
    assert ".row > summary { list-style:none; cursor:pointer; display:grid;" in css
    assert "grid-template-columns:64px 1fr 190px 120px 80px 28px;" in css
    # No elevation and no gradient: the boundary is a hairline, not a shadow.
    assert "box-shadow" not in css
    assert "gradient" not in css
    assert ".card {" not in css


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

    # Region boundaries are hairlines, never shadows.
    assert "gap:var(--sp-12); padding-bottom:var(--sp-8); border-bottom:1px solid var(--ink); }" in css
    assert "box-shadow" not in css
    for region in ('id="panel-offers"', 'id="offers"', 'id="keyword-table"'):
        assert region in html_text, region

    # Inside one row: summary | lists | foot.
    assert ".row { border-bottom:1px solid var(--rule); }" in css
    assert re.search(r"\.lists \{[^}]*border-top:1px solid var\(--rule\);", css)
    assert re.search(r"\.foot \{[^}]*border-top:1px solid var\(--rule\);", css)

    # The keyword table keeps the mockup's header rule and row hairline.
    assert "th { font-family:var(--mono); font-size:var(--fs-11); font-weight:600;" in css
    assert "border-bottom:1px solid var(--ink)" in css
    assert "td { padding:var(--sp-10) var(--sp-8); border-bottom:1px solid var(--rule);" in css


def test_offer_row_keeps_a_title_score_action_hierarchy(db_file: Path) -> None:
    """Title, score and the outbound link each read at their own level.

    The title gets its own type step so it outranks body text without competing with
    a section heading; company and location stay in the subtitle and are not repeated
    in the facts grid underneath; the only outbound link is a bordered control in the
    row's action column instead of underlined prose inside the facts.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert "--fs-26:26px" in css
    assert ".score { font-family:var(--mono); font-size:var(--fs-26); font-weight:600; line-height:1;" in css
    assert ".t { font-family:var(--sans); font-size:var(--fs-15); font-weight:600; line-height:1.3; }" in css
    assert ".c { color:var(--ink-2); font-size:var(--fs-13); margin-top:var(--sp-2); }" in css
    # Company and location appear once, in the subtitle.
    assert "var subtitle = offer.company;" in html_text
    assert 'if (offer.location) { subtitle += " \\u00b7 " + offer.location; }' in html_text

    # The action is a real control in the row's footer, still guarded.
    assert ".foot a { color:var(--ink); margin-left:auto; }" in css
    assert 'var link = el("a", null, "Ver la oferta \\u2197");' in html_text
    assert 'link.href = href;' in html_text
    assert 'link.rel = "noopener noreferrer";' in html_text
    assert 'link.target = "_blank";' in html_text


def test_disclosure_summaries_draw_a_chevron_and_show_their_state(db_file: Path) -> None:
    """Both disclosures advertise themselves as controls and state open/closed.

    The chevron is drawn with borders rather than a glyph, so no font is needed and
    the project's no-external-font rule keeps holding. The two behaviours the previous
    skin left implicit are asserted here: a legible label size, and the same affordance
    on the section-level methodology disclosure.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert ".chev { color:var(--ink-3); transition:transform .15s; }" in css
    assert ".row[open] .chev { transform:rotate(90deg); }" in css
    assert ".row > summary::-webkit-details-marker { display:none; }" in css
    # The label is a row-sized control with hover and keyboard focus states.
    assert ".row > summary { list-style:none; cursor:pointer; display:grid;" in css
    assert ".row > summary:hover, .row > summary:focus-visible { background:var(--sheet); }" in css
    assert ".row > summary:focus-visible { outline:2px solid var(--ink); outline-offset:-2px; }" in css
    # The state is a glyph plus a rotation, never a font-dependent +/- swap.
    assert 'content:"+"' not in css and 'content:"−"' not in css


def test_human_feedback_shows_selection_hover_and_a_saved_confirmation(db_file: Path) -> None:
    """Own marks are confirmed, keyboard-readable, and never hue-only.

    Selection carries ``aria-pressed`` on top of the ink fill, the pressed/hover
    states are explicit, and the confirmation is a polite live region that removes
    itself so it never stacks or blocks the next mark. It is only written when
    ``localStorage`` actually accepted the write.
    """
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert ".mini:hover, .mini:focus-visible { border-color:var(--ink); color:var(--ink); }" in css
    assert '.mini[aria-pressed="true"] { background:var(--ink); border-color:var(--ink); color:var(--paper); }' in css
    assert ".decision-state.legacy { font-style:italic; border-left:2px solid var(--rule);" in css

    assert "function confirmSaved(jobId)" in html_text
    assert 'marker.setAttribute("role", "status");' in html_text
    assert "var stored = saveFeedback();" in html_text
    assert "if (stored) { confirmSaved(offer.id); }" in html_text
    assert "savedTimer = window.setTimeout(function () { marker.remove(); }, 2600);" in html_text
    # The decision state text has its own node, so the marker is not wiped by updates.
    assert 'el("span", "decision-state-text",' in html_text
    assert 'row.querySelector(".decision-state-text")' in html_text
    assert 'var button = el("button", "mini decision-btn", visibleText);' in html_text


def test_report_keeps_empty_error_and_local_storage_states(db_file: Path) -> None:
    """The states a reader can land in are still visible in this variant."""
    html_text = generate_and_read(db_file)

    assert 'id="empty-state"' in html_text
    assert "Ninguna oferta con estos filtros · " in html_text
    assert 'id="empty-reset" class="mini">limpiar</button>' in html_text
    assert "function resetFilters()" in html_text
    assert 'document.getElementById("empty-reset").addEventListener("click", resetFilters);' in html_text
    assert "No se pudieron leer los datos del panel." in html_text
    assert "versión incompatible de jobscout" in html_text
    assert "El almacenamiento local no está disponible" in html_text
    assert "<noscript>" in html_text


def test_ember_is_reserved_for_warning_and_error_states(db_file: Path) -> None:
    """Every colour is a token: no raw hex survives outside ``:root``."""
    html_text = generate_and_read(db_file)
    style = re.search(r"<style>(.*?)</style>", html_text, re.DOTALL)
    assert style is not None, "the report must embed its stylesheet"
    css = style.group(1)

    root = re.search(r":root \{(.*?)\n\}", css, re.DOTALL)
    assert root is not None, "the report must define its token set"
    outside_root = css.replace(root.group(0), "")
    assert not re.findall(r"#[0-9a-fA-F]{3,8}(?=[\s;])", outside_root)
    assert "--ember" not in css

    # Recommendation chips carry weight and their own token, never a raw warning
    # colour.
    chips = [rule for rule in css.split("}") if ".chip" in rule]
    assert chips, "the recommendation chips must be styled"
    for rule in chips:
        assert "#" not in rule, rule


def test_offers_are_painted_in_batches_of_fifty(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert "var PAGE_SIZE = 50;" in html_text
    assert 'id="show-more" class="mini" hidden>Mostrar 50 más</button>' in html_text
    assert 'document.getElementById("show-more").addEventListener("click", renderBatch);' in html_text
    assert "shownOffers = 0;" in html_text
    assert "renderBatch();" in html_text


def test_offer_body_is_built_lazily_on_toggle(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert "function renderBody(offer)" in html_text
    assert "var bodyBuilt = false;" in html_text
    assert 'details.addEventListener("toggle", function () {' in html_text
    assert "if (!details.open || bodyBuilt) { return; }" in html_text
    assert "details.appendChild(renderBody(offer));" in html_text
    # Only the definition and the toggle handler call the body builder.
    assert html_text.count("renderBody(offer)") == 2


def test_dimension_weight_is_rendered_as_a_share_of_the_weight_sum(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert "var weightSum = 0;" in html_text
    assert "weightSum += Number(dim.weight) || 0;" in html_text
    assert '"peso " + Math.round(dim.weight / weightSum * 100) + "%"' in html_text
    assert "if (weightSum !== 0)" in html_text


def test_salary_provenance_is_labelled_indicado_or_sin_dato(db_file: Path) -> None:
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert 'salaryNode.appendChild(el("em", null, "indicado"));' in html_text
    assert 'salaryNode.appendChild(el("em", null, "sin dato"));' in html_text
    assert 'salaryBox.appendChild(el("dt", null, "indicado"));' in html_text
    assert 'salaryBox.appendChild(el("dd", "sal-stated", offer.salary));' in html_text
    # Without a stated salary the model's estimate renders neutral as "estimado",
    # with the employer tier and basis spelled out in the expanded body.
    assert 'salaryNode = el("div", "sal est", salaryRangeK(offer.salary_est));' in html_text
    assert 'salaryNode.appendChild(el("em", null, "estimado"));' in html_text
    assert 'salaryBox.appendChild(el("dt", null, "estimado"));' in html_text
    assert 'consultora_grande: "gran consultora / corporación"' in html_text
    # A stated salary is money-green and bold in both the row and the body.
    assert "--money:#2f6b3a;" in css
    assert "--money-wash:#e3ede2;" in css
    assert ".sal.est { color:var(--ink-3); font-weight:400; }" in css
    assert ".salbox dd.sal-stated { color:var(--money); font-weight:600; }" in css


def test_missing_dimensions_state_that_no_breakdown_exists(db_file: Path) -> None:
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    assert '"sin desglose en esta evaluación"' in html_text
    assert 'dims.length ? "body" : "body nodims"' in html_text
    assert ".body.nodims { grid-template-columns:1fr; }" in css


def test_status_line_sits_at_the_right_of_the_masthead(db_file: Path) -> None:
    css = _embedded_style(generate_and_read(db_file))

    assert ".mast .status-line { margin-left:auto; }" in css


def test_preference_outcomes_surface_as_flag_and_veto_chips(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert 'preference.outcome === "flag"' in html_text
    assert 'preference.outcome === "fail"' in html_text
    assert 'el("span", "chip flag", "\\u26a0 flag")' in html_text
    assert 'el("span", "chip veto", "veto")' in html_text
    assert 'flag.title = preference.reason || "";' in html_text
    assert 'veto.title = preference.reason || "";' in html_text


def test_warnings_are_plain_paragraphs_without_the_old_notice_box(db_file: Path) -> None:
    payload = _payload(db_file)
    payload["warnings"] = ["Aviso <uno>"]
    html_text = render_dashboard_html(payload)

    assert '<div class="report-warnings"><p>Aviso &lt;uno&gt;</p></div>' in html_text
    assert html_text.index('id="report-meta"') < html_text.index('class="report-warnings"')
    assert 'id="warnings"' not in html_text
    assert "Avisos" not in html_text
    assert "Sin avisos." not in html_text
    assert 'id="imported-review-note"' not in html_text

    payload["warnings"] = []
    empty = render_dashboard_html(payload)
    assert '<div class="report-warnings">' not in empty


def test_empty_lists_strip_is_left_out_of_the_body(db_file: Path) -> None:
    """Four columns of dashes add nothing; the strip needs a list or a preference line."""
    html_text = generate_and_read(db_file)

    assert "if (hasLists || blockerFirst || unknownFirst) {" in html_text
    assert '["strengths", "gaps", "blockers", "unknowns"].some(' in html_text


def test_masthead_rule_spans_the_window_like_the_mockup(db_file: Path) -> None:
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    # The header sits before the column, so its ink rule is not cut at 1280 px.
    assert html_text.index('<header class="masthead">') < html_text.index('<div class="panel">')
    assert '<div class="masthead-wrap">' in html_text
    assert ".masthead-wrap { max-width:1280px; margin:0 auto; padding:0 var(--sp-32); }" in css


def test_only_new_toggle_is_a_one_button_segmented_control(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert re.search(
        r'<div class="seg">\s*<button type="button" id="filter-new" aria-pressed="false"',
        html_text,
    )
    assert 'id="filter-new" class="btn' not in html_text


def test_filter_rail_reports_what_is_narrowing_the_list(db_file: Path) -> None:
    """The toolbar reports the result count; the only reset is the empty state's."""
    html_text = generate_and_read(db_file)

    assert 'id="result-count"' in html_text
    assert 'id="reset-filters"' not in html_text
    assert "function applyFilters()" in html_text
    assert '" ofertas"' in html_text
    assert '" de "' in html_text
    # Every narrowing control goes through the one path, so the list and the
    # result count can never disagree; ordering is not counted as a filter.
    assert html_text.count("applyFilters();") >= 6
    assert "filters.sort" not in re.search(
        r"function resetFilters\(\) \{(.*?)\n  \}", html_text, re.DOTALL
    ).group(1)


def test_min_score_is_a_select_with_the_mockup_thresholds(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert re.search(
        r'<select id="filter-min-score" aria-label="Puntaje mínimo">\s*'
        r'<option value="0">Score ≥ 0</option>\s*'
        r'<option value="50">≥ 50</option>\s*'
        r'<option value="70">≥ 70</option>\s*</select>',
        html_text,
    )
    # The zero option clears the threshold instead of filtering everything out.
    assert "filters.minScore = parsed > 0 ? parsed : null;" in html_text
    assert 'document.getElementById("filter-min-score").value = "0";' in html_text


def test_removed_toolbar_controls_are_absent_from_markup_css_and_script(
    db_file: Path,
) -> None:
    html_text = generate_and_read(db_file)

    for removed in (
        'id="filter-review"',
        'id="filter-imported-review"',
        'id="reset-filters"',
        "filters.review",
        "filters.importedReview",
        "activeFilterCount",
        "Estado de revisión",
    ):
        assert removed not in html_text, removed
    # The empty-state reset and its handler stay.
    assert 'id="empty-reset" class="mini">limpiar</button>' in html_text
    assert 'document.getElementById("empty-reset").addEventListener("click", resetFilters);' in html_text


def test_status_line_is_built_from_payload_status_with_conditional_signal(
    db_file: Path,
) -> None:
    """The status line is textContent only, and signal marks just the failures."""
    html_text = generate_and_read(db_file)
    match = re.search(
        r"function renderStatusLine\(\) \{(.*?)\n  \}", html_text, re.DOTALL
    )
    assert match is not None, "the header status line must be built client-side"
    body = match.group(1)

    assert 'host.textContent = "";' in body
    assert "payload.status" in body
    assert "last_sweep_at" in body
    assert "Sin barridos" in body
    assert "new_jobs" in body
    assert "new_scored" in body
    assert "sin evaluar" in body
    assert "pending_details" in body
    assert "Detalles pendientes:" in body
    assert "failed.searches" in body and "failed.details" in body
    # The signal class is attached to exactly the two conditional branches:
    # unscored new offers and failed runs.
    assert body.count('"bad"') == 2
    assert "if (newScored < newJobs)" in body
    assert 'totalFailed > 0 ? "bad" : "calm"' in body


def test_no_discrepancias_or_vetos_tabs_before_their_data_exists(db_file: Path) -> None:
    """DR-2 ships only the two views the payload can currently fill."""
    html_text = generate_and_read(db_file)

    assert ">Discrepancias<" not in html_text
    assert ">Vetos<" not in html_text
    assert 'data-view="div"' not in html_text
    assert 'data-view="gates"' not in html_text


def test_filter_new_filters_offers_by_is_new(db_file: Path) -> None:
    """The new toggle narrows the list through the existing filter path."""
    html_text = generate_and_read(db_file)

    assert 'id="filter-new"' in html_text
    assert "Solo nuevas" in html_text
    matches_block = re.search(
        r"function matches\(offer\) \{(.*?)\n  \}", html_text, re.DOTALL
    )
    assert matches_block is not None
    assert "if (filters.isNew && !offer.is_new) { return false; }" in matches_block.group(1)
    assert 'document.getElementById("filter-new").addEventListener("click"' in html_text
    assert "filters.isNew = !filters.isNew;" in html_text


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
    # The average-score column opens in descending order, declared in the markup.
    assert '<th class="num" data-initial-sort="descending">Score medio</th>' in html_text
    assert 'th[aria-sort="ascending"] .sort-btn::after { content:" \\2191"; }' in css
    assert 'th[aria-sort="descending"] .sort-btn::after { content:" \\2193"; }' in css
    # The first header is the keyword label and the sort target.
    assert "<th>Keyword</th>" in html_text


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
    # The visible heading is gone; the accessible names stay.
    assert "Mi decisión (feedback)" not in html_text
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


def test_export_appears_only_with_local_decisions(db_file: Path) -> None:
    """The header export is hidden until this browser holds a decision, and names N."""
    html_text = generate_and_read(db_file)

    assert re.search(r'<button type="button" id="export-feedback" class="export"\s+title="[^"]*"\s+hidden>', html_text)
    assert 'Exportar <span id="feedback-count" aria-live="polite">0 decisiones</span></button>' in html_text
    assert 'document.getElementById("export-feedback").hidden = count === 0;' in html_text
    assert 'count + (count === 1 ? " decisión" : " decisiones");' in html_text
    # The old count text and disabled state are gone.
    assert "revisadas\";" not in html_text
    assert ".export:disabled" not in _embedded_style(html_text)


def test_decision_reason_is_capped_at_500(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert "var MAX_REASON_CHARS = 500;" in html_text
    assert "reason.maxLength = MAX_REASON_CHARS;" in html_text
    assert '"Motivo opcional, máx. " + MAX_REASON_CHARS' in html_text


def test_saved_confirmation_fades_before_removal(db_file: Path) -> None:
    html_text = generate_and_read(db_file)

    assert ".decision-saved.fading { opacity:0; }" in _embedded_style(html_text)
    assert 'window.setTimeout(function () { marker.classList.add("fading"); }, 2450);' in html_text


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
    # signal), including its "Considerar" segmented-control button.
    assert '>Considerar</button>' in html_text
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
    # The keyword table counts only imported "apply" decisions, so the analytics
    # never present ``consider`` as a current human option.
    assert "String(item.human_apply)" in html_text
    assert "item.human_consider" not in html_text
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
    # Imported decisions are displayed in the footer, not offered as a filter.
    assert 'id="filter-imported-review"' not in html_text
    assert 'var FEEDBACK_DECISIONS = ["apply", "consider", "reject"];' not in html_text


def test_toolbar_controls_add_no_media_query_and_keep_one_reduced_motion_guard(
    db_file: Path,
) -> None:
    """The toolbar wraps instead of adding a breakpoint, and stays hue-free."""
    css = _embedded_style(generate_and_read(db_file))

    assert css.count("@media") == 1
    assert re.findall(r"@media\s*\(([^)]*)\)", css) == ["prefers-reduced-motion:reduce"]
    assert "* { transition:none !important; animation:none !important; }" in css

    # The segmented controls are styled without a second accent or ember.
    toggle_rules = [rule for rule in css.split("}") if ".seg" in rule]
    assert toggle_rules
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
    assert '"imported_review":{"reviewed":true' in html_text


def test_imported_decision_survives_a_blocked_localstorage(db_file: Path) -> None:
    """The imported decision is payload data, so it survives a blocked localStorage.

    Some browsers restrict ``localStorage`` on ``file://`` pages. The report still
    renders in that case: the footer reads ``offer.imported_review`` from the
    embedded payload, and browser storage is only needed for local marks.
    """
    html_text = generate_and_read(db_file)

    assert "offer.imported_review" in html_text
    # Browser storage is still read defensively and is never required to render.
    assert "window.localStorage.getItem(STORAGE_KEY)" in html_text
    assert "catch (err) {" in html_text


# ---------------------------------------------------------------------------
# Keyword analytics
# ---------------------------------------------------------------------------


def test_keyword_aggregation_counts_and_quality(db_file: Path) -> None:
    payload = _payload(db_file)
    stats = {item["keyword"]: item for item in payload["keywords"]}

    keyword_a = stats["senior backend engineer"]
    assert keyword_a["sightings"] == 3
    # job1 and job2 were surfaced by this search at least once.
    assert keyword_a["seen_jobs"] == 2
    # Only job1 (80) reaches the 70 bar; job2 scored 40.
    assert keyword_a["high_score_jobs"] == 1
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
    # job2 and job3, both surfaced by this search.
    assert keyword_b["seen_jobs"] == 2
    # job2 scored 40 and job3 has no score, so none reaches 70.
    assert keyword_b["high_score_jobs"] == 0
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

    # The three top lists are gone; the table is the only keyword surface.
    assert "top_quality" not in payload


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
    """The rendered header row is the surface the README documents."""
    html_text = generate_and_read(db_file)
    css = _embedded_style(html_text)

    header_row = re.search(r"<thead>.*?</thead>", html_text, re.DOTALL)
    assert header_row is not None, "the keyword table must render its header row"
    assert re.findall(r"<th[^>]*>([^<]+)</th>", header_row.group(0)) == [
        "Keyword",
        "Proveedor",
        "Vistas",
        "Activas",
        "Score medio",
        "≥ 70",
        "Aplicadas",
    ]
    # A retired row is marked in place instead of disappearing.
    assert 'tr.retired td:first-child::after { content:" · retirada";' in css
    assert 'id="top-human"' not in html_text

    payload = _extract_payload(html_text)
    assert "seen_jobs" in payload["keywords"][0]
    assert "high_score_jobs" in payload["keywords"][0]
    assert "human_apply" in payload["keywords"][0]


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


def test_keyword_without_scores_has_no_average(tmp_path: Path) -> None:
    """A series with no scored job reports no average, so the table says "sin dato"."""
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
        insert_evaluation(conn, _score(scored.id, 55.0, Recommendation.SKIP))
    finally:
        conn.close()

    stats = {item["keyword"]: item for item in _payload(path)["keywords"]}
    assert stats["scored keyword"]["avg_score"] == 55.0
    assert stats["scored keyword"]["high_score_jobs"] == 0
    assert stats["unscored keyword"]["avg_score"] is None
    assert stats["unscored keyword"]["high_score_jobs"] == 0
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
    # Each offer key is a series the keyword filter offers.
    series_keys = {item["source_key"] for item in data["keywords"]}
    assert data["offers"][0]["keyword_keys"][0] in series_keys


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

    assert "sin evaluar (error)" in html_text
    assert "sin evaluar (falta información)" in html_text
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


def test_dashboard_flags_stale_scores_and_keeps_the_report_self_contained(
    tmp_path: Path, profile, settings
) -> None:
    path = tmp_path / "stale.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        stale, _ = store_posting(conn, _posting(), now="2026-09-15T10:00:00Z")
        unscored, _ = store_posting(
            conn,
            _posting(
                source_job_id="3901234568",
                url="https://www.linkedin.com/jobs/view/3901234568/unscored",
                title="Unscored Role",
            ),
            now="2026-09-15T10:00:00Z",
        )
        insert_evaluation(
            conn,
            _score(
                stale.id,
                80.0,
                Recommendation.APPLY,
                input_fingerprint="old-input",
            ),
        )
    finally:
        conn.close()

    conn = open_readonly_database(path)
    try:
        jobs = all_active_jobs(conn)
        stale_ids = stale_job_ids(conn, jobs, profile, settings)
        payload = build_payload(
            conn, stale_job_ids=stale_ids | {int(unscored.id)}, now=NOW
        )
    finally:
        conn.close()

    by_id = {offer["id"]: offer for offer in payload["offers"]}
    assert by_id[stale.id]["needs_rescore"] is True
    # An offer with no shown score is never stale, even if it were in the set.
    assert by_id[unscored.id]["score"] is None
    assert by_id[unscored.id]["needs_rescore"] is False

    html_text = render_dashboard_html(payload)
    assert "Re-score pendiente" in html_text
    assert "needs_rescore" in html_text
    assert ".score.stale" in html_text
    assert not re.search(
        r'<(?:link|script|img|iframe)[^>]+(?:src|href)="https?://', html_text
    )
    assert "cdn." not in html_text
    assert "fetch(" not in html_text


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


# ---------------------------------------------------------------------------
# Retired keyword series in the analytics
# ---------------------------------------------------------------------------


def test_dashboard_marks_retired_keyword_series(tmp_path: Path) -> None:
    """Every series carries a truthful ``retired`` flag; the summary keeps the total."""
    path = tmp_path / "dash" / "retired.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        for provider, keyword in RETIRED_KEYWORD_PAIRS:
            _seed_keyword_series(conn, provider=provider, keyword=keyword)
        _seed_keyword_series(
            conn, provider="linkedin", keyword="Junior Cloud Engineer", scores=(70.0,)
        )
    finally:
        conn.close()

    payload = _payload(path)
    retired = payload["retired_keywords"]
    assert {(item["provider"], item["keyword"]) for item in retired} == set(
        RETIRED_KEYWORD_PAIRS
    )
    assert all(item["retired"] is True for item in retired)
    assert all("retired_on" not in item for item in retired)
    assert all("retired_reason" not in item for item in retired)

    active = payload["keywords"]
    assert [item["keyword"] for item in active] == ["Junior Cloud Engineer"]
    assert all(item["retired"] is False for item in active)

    # ``summary.keywords`` still counts every analyzed series, retired included.
    assert payload["summary"]["keywords"] == len(RETIRED_KEYWORD_PAIRS) + 1
    assert payload["summary"]["retired_keywords"] == len(RETIRED_KEYWORD_PAIRS)


def test_dashboard_keyword_table_greys_retired_series(tmp_path: Path) -> None:
    """A retired series stays in the table, marked instead of ranked."""
    path = tmp_path / "dash" / "omit.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        _seed_keyword_series(
            conn,
            provider="linkedin",
            keyword="Identity and Access Management Engineer",
            scores=(95.0, 90.0),
        )
        _seed_keyword_series(
            conn,
            provider="linkedin",
            keyword="Senior Backend Engineer",
            scores=(40.0,),
        )
    finally:
        conn.close()

    html_text = generate_and_read(path)
    payload = _extract_payload(html_text)

    assert [item["keyword"] for item in payload["keywords"]] == ["Senior Backend Engineer"]
    assert "Identity and Access Management Engineer" not in [
        item["keyword"] for item in payload["keywords"]
    ]
    # The retired row is preserved and marked in its own collection.
    assert [item["keyword"] for item in payload["retired_keywords"]] == [
        "Identity and Access Management Engineer"
    ]
    # The renderer concatenates both collections and greys the retired rows.
    assert "payload.keywords.concat(payload.retired_keywords || [])" in html_text
    assert 'row.className = "retired";' in html_text
    assert "tr.retired td { color:var(--ink-3); }" in _embedded_style(html_text)


def test_dashboard_all_retired_keyword_series_still_fill_the_table(tmp_path: Path) -> None:
    """If every series is retired, they still fill the table instead of an empty state."""
    path = tmp_path / "dash" / "all-retired.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        for provider, keyword in RETIRED_KEYWORD_PAIRS:
            _seed_keyword_series(conn, provider=provider, keyword=keyword)
    finally:
        conn.close()

    html_text = generate_and_read(path)
    payload = _extract_payload(html_text)

    assert payload["keywords"] == []
    assert len(payload["retired_keywords"]) == len(RETIRED_KEYWORD_PAIRS)
    assert (
        '<p class="note" id="keyword-empty" hidden>'
        "Todavía no hay procedencia de palabras clave registrada.</p>"
    ) in html_text


def test_dashboard_retired_flag_is_provider_scoped_and_normalized(tmp_path: Path) -> None:
    """The flag follows the registry: normalized equality, scoped to its provider."""
    path = tmp_path / "dash" / "scope.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # Retired on LinkedIn only: the same literal stays active on Tecnoempleo.
        _seed_keyword_series(
            conn, provider="linkedin", keyword="Identity and Access Management Engineer"
        )
        _seed_keyword_series(
            conn, provider="tecnoempleo", keyword="Identity and Access Management Engineer"
        )
        # Retired on Tecnoempleo, and a case variant matches by normalized equality.
        _seed_keyword_series(conn, provider="tecnoempleo", keyword="AZURE SECURITY ENGINEER")
        # Retired on Tecnoempleo only: stays active on LinkedIn.
        _seed_keyword_series(conn, provider="linkedin", keyword="Azure Security Engineer")
        # The function browse retired this Tecnoempleo literal; LinkedIn keeps it.
        _seed_keyword_series(conn, provider="tecnoempleo", keyword="Cloud Security Engineer")
        # CP-8 restored this LinkedIn literal.
        _seed_keyword_series(conn, provider="linkedin", keyword="Cloud Security Engineer")
    finally:
        conn.close()

    payload = _payload(path)
    retired = {(item["provider"], item["keyword"]) for item in payload["retired_keywords"]}
    active = {(item["provider"], item["keyword"]) for item in payload["keywords"]}

    assert ("linkedin", "Identity and Access Management Engineer") in retired
    assert ("tecnoempleo", "Identity and Access Management Engineer") in active
    assert ("tecnoempleo", "AZURE SECURITY ENGINEER") in retired
    assert ("linkedin", "Azure Security Engineer") in active
    assert ("tecnoempleo", "Cloud Security Engineer") in retired
    assert ("linkedin", "Cloud Security Engineer") in active


# ---------------------------------------------------------------------------
# Seniority pre-filter (owner reversed CP-10 on 2026-10-27)
# ---------------------------------------------------------------------------


def test_dashboard_marks_senior_titles_and_keeps_every_offer(tmp_path: Path) -> None:
    """Filtered offers stay in the payload; only the client hides them."""
    path = tmp_path / "dash" / "seniority.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        senior, _ = store_posting(
            conn,
            _posting(
                source_job_id="seniority-senior",
                url="https://example.test/jobs/seniority-senior",
                title="Senior Platform Engineer",
            ),
            now="2026-09-15T10:00:00Z",
        )
        exception, _ = store_posting(
            conn,
            _posting(
                source_job_id="seniority-junior",
                url="https://example.test/jobs/seniority-junior",
                title="Junior Lead Developer",
            ),
            now="2026-09-15T10:00:00Z",
        )
        plain, _ = store_posting(
            conn,
            _posting(
                source_job_id="seniority-plain",
                url="https://example.test/jobs/seniority-plain",
                title="Cloud Engineer",
            ),
            now="2026-09-15T10:00:00Z",
        )
    finally:
        conn.close()

    payload = _payload(path)
    offers = {offer["id"]: offer for offer in payload["offers"]}

    assert offers[senior.id]["seniority_filter"] == "title: senior"
    assert offers[exception.id]["seniority_filter"] is None
    assert offers[plain.id]["seniority_filter"] is None
    assert payload["summary"]["seniority_filtered"] == 1
    assert payload["summary"]["jobs_shown"] == len(payload["offers"]) == 3


def test_dashboard_html_has_a_seniority_toggle_hidden_by_default(tmp_path: Path) -> None:
    """The rail exposes the count and a keyboard-accessible toggle; reset hides."""
    path = tmp_path / "dash" / "seniority-toggle.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        store_posting(
            conn,
            _posting(
                source_job_id="seniority-toggle",
                url="https://example.test/jobs/seniority-toggle",
                title="Senior Platform Engineer",
            ),
            now="2026-09-15T10:00:00Z",
        )
    finally:
        conn.close()

    html_text = generate_and_read(path)

    assert 'id="filter-show-seniority"' in html_text
    assert 'aria-pressed="false"' in html_text
    assert "ocultas por seniority" in html_text
    assert "showSeniority" in html_text
    assert "seniority_filter" in html_text
    # The reset action restores the hidden-by-default state.
    assert html_text.count("showSeniority: false") >= 2
    # The removed CP-9 filter must not come back under its old names.
    assert "filter-show-noise" not in html_text
    assert "noise-note" not in html_text
    assert "showNoise" not in html_text
