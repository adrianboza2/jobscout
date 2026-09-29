"""Provider/cycle/position provenance and analytics comparability tests.

These tests pin three contracts from the multi-platform work unit:

1. ingestion accepts an explicit ``provider`` and never hardcodes LinkedIn;
2. ``cycle_id`` and ``position_kind`` round-trip through SQLite;
3. dashboard keyword analytics aggregate by ``(provider, keyword)``: a cycle is
   provenance, not identity, so the same literal keyword from two cycles of one
   platform is one series, while the same keyword on two platforms stays two.
   Offer provenance still exposes each observation's real ``cycle_id``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from conftest import FakeHttpClient, FIXTURES, fixture_text, response

from jobscout.collectors.linkedin import LinkedInSearchConfig, search_linkedin
from jobscout.dashboard import build_payload
from jobscout.db import (
    connect,
    count_jobs,
    create_search_run,
    get_search_run,
    init_db,
    insert_evaluation,
    list_search_runs,
    record_run_item,
    store_posting,
)
from jobscout.models import (
    DuplicateKind,
    Evaluation,
    JobPosting,
    PositionKind,
    Recommendation,
    ScorerName,
    SearchRun,
    SearchRunItem,
    SearchRunStatus,
)
from jobscout.normalize import finalize_posting
from jobscout.pipeline import ingest_collector_result, ingest_fixture

DRIFT_FIXTURE = FIXTURES / "linkedin_search_page_drift.html"
NOW = "2026-09-15T12:00:00Z"


def _posting(source: str, job_id: str, title: str, company: str) -> JobPosting:
    return finalize_posting(
        JobPosting(
            source=source,
            source_job_id=job_id,
            url=f"https://example.test/{source}/{job_id}",
            title=title,
            company=company,
            location="Remote",
            description=f"{title} at {company}",
        )
    )


def _seed_provider_run(
    conn: sqlite3.Connection,
    *,
    provider: str,
    keyword: str,
    cycle_id: str | None,
    postings: list[JobPosting],
    position_kind: str = "search_rank",
) -> tuple[int, list[int]]:
    run = create_search_run(
        conn,
        SearchRun(
            provider=provider,
            query=keyword,
            cycle_id=cycle_id,
            status=SearchRunStatus.COMPLETED,
            started_at=NOW,
        ),
    )
    job_ids: list[int] = []
    for rank, posting in enumerate(postings, start=1):
        job, _ = store_posting(conn, posting, now=NOW)
        job_ids.append(int(job.id))
        record_run_item(
            conn,
            SearchRunItem(
                run_id=int(run.id),
                job_id=int(job.id),
                decision=DuplicateKind.NEW,
                rank_in_run=rank if position_kind != "none" else 0,
                position_kind=position_kind,
                created_at=NOW,
            ),
        )
    return int(run.id), job_ids


def test_ingest_collector_result_accepts_an_explicit_provider(conn, settings) -> None:
    client = FakeHttpClient(
        [response(200, fixture_text("linkedin_search_page_drift.html"), headers={"content-type": "text/html"})]
    )
    config = LinkedInSearchConfig(keywords="cloud", max_pages=1, max_items=10)
    result = search_linkedin(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    outcome = ingest_collector_result(
        conn,
        result,
        config=config,
        settings=settings,
        provider="getmanfred",
        cycle_id="c1",
    )
    run = get_search_run(conn, outcome.run_id)
    assert run.provider == "getmanfred"
    assert run.cycle_id == "c1"
    # Explicit provider overrides the config's own provider, and the strategy
    # still comes from the config (search_rank here).
    position_kinds = {
        row["position_kind"]
        for row in conn.execute("SELECT position_kind FROM search_run_items")
    }
    assert position_kinds == {"search_rank"}


def test_ingest_collector_result_without_config_defaults_to_linkedin(conn) -> None:
    from jobscout.collectors.base import CollectorResult

    result = CollectorResult(postings=[], planned_urls=["https://example.test/plan"])
    outcome = ingest_collector_result(conn, result)
    run = get_search_run(conn, outcome.run_id)
    assert run.provider == "linkedin"
    assert run.label == "linkedin:inventory"


def test_cycle_id_and_position_kind_round_trip(conn) -> None:
    run = create_search_run(
        conn,
        SearchRun(provider="tecnoempleo", query="python", cycle_id="cycle-A", started_at=NOW),
    )
    posting = _posting("tecnoempleo", "1", "Python Dev", "Acme")
    job, _ = store_posting(conn, posting, now=NOW)
    record_run_item(
        conn,
        SearchRunItem(
            run_id=int(run.id),
            job_id=int(job.id),
            rank_in_run=2,
            position_kind=PositionKind.SEARCH_RANK,
            created_at=NOW,
        ),
    )
    stored = get_search_run(conn, int(run.id))
    assert stored.cycle_id == "cycle-A"
    row = conn.execute("SELECT position_kind, rank_in_run FROM search_run_items").fetchone()
    assert row["position_kind"] == "search_rank"
    assert row["rank_in_run"] == 2


def test_ingest_fixture_carries_provider_cycle_and_position(tmp_path: Path, conn, settings) -> None:
    fixture = tmp_path / "getmanfred.json"
    fixture.write_text(
        json.dumps(
            {
                "provider": "getmanfred",
                "query": None,
                "jobs": [
                    {"title": "Backend Engineer", "company": "Acme", "source_job_id": "1001"},
                    {"title": "SRE", "company": "Beta", "source_job_id": "1002"},
                ],
            }
        ),
        encoding="utf-8",
    )
    outcome = ingest_fixture(
        conn,
        fixture,
        # ``source=""`` lets the fixture's own ``provider`` metadata win instead
        # of the CLI's default ``fixture`` label.
        source="",
        settings=settings,
        cycle_id="cycle-fixture",
        position_kind=PositionKind.NONE,
        now=NOW,
    )
    run = get_search_run(conn, outcome.run_id)
    assert run.provider == "getmanfred"
    assert run.cycle_id == "cycle-fixture"
    rows = conn.execute("SELECT position_kind, rank_in_run FROM search_run_items").fetchall()
    assert [row["position_kind"] for row in rows] == ["none", "none"]
    assert [row["rank_in_run"] for row in rows] == [0, 0]


def test_analytics_separate_same_keyword_across_providers(conn) -> None:
    _, linkedin_jobs = _seed_provider_run(
        conn,
        provider="linkedin",
        keyword="backend engineer",
        cycle_id=None,
        postings=[_posting("linkedin", "l1", "Backend Engineer", "Acme")],
    )
    _, getmanfred_jobs = _seed_provider_run(
        conn,
        provider="getmanfred",
        keyword="backend engineer",
        cycle_id=None,
        postings=[_posting("getmanfred", "g1", "Backend Engineer", "Beta")],
        position_kind="none",
    )
    insert_evaluation(
        conn,
        Evaluation(
            job_id=linkedin_jobs[0],
            scorer=ScorerName.HEURISTIC,
            overall_score=80.0,
            recommendation=Recommendation.APPLY,
            created_at=NOW,
        ),
    )
    insert_evaluation(
        conn,
        Evaluation(
            job_id=getmanfred_jobs[0],
            scorer=ScorerName.HEURISTIC,
            overall_score=40.0,
            recommendation=Recommendation.CONSIDER,
            created_at=NOW,
        ),
    )

    payload = build_payload(conn, top=10, now=None)
    series = {(item["provider"], item["keyword"]): item for item in payload["keywords"]}
    assert set(series) == {("linkedin", "backend engineer"), ("getmanfred", "backend engineer")}
    assert series[("linkedin", "backend engineer")]["unique_jobs"] == 1
    assert series[("getmanfred", "backend engineer")]["unique_jobs"] == 1
    assert series[("linkedin", "backend engineer")]["avg_score"] == 80.0
    assert series[("getmanfred", "backend engineer")]["avg_score"] == 40.0
    # Distinct series keys, so the dashboard filter never merges them.
    assert (
        series[("linkedin", "backend engineer")]["source_key"]
        != series[("getmanfred", "backend engineer")]["source_key"]
    )
    # The offer provenance entry carries the series key and the position kind.
    offers = {offer["source"]: offer for offer in payload["offers"]}
    assert offers["getmanfred"]["keywords"][0]["source_key"].startswith("getmanfred|")
    assert offers["getmanfred"]["keywords"][0]["position_kind"] == "none"
    assert offers["linkedin"]["keywords"][0]["position_kind"] == "search_rank"


def test_analytics_merge_cycles_for_same_provider_and_keyword(conn) -> None:
    """A cycle is provenance, not identity: one row and key per provider/keyword."""
    _, cycle_1_jobs = _seed_provider_run(
        conn,
        provider="linkedin",
        keyword="platform engineer",
        cycle_id="cycle-1",
        postings=[_posting("linkedin", "1", "Platform Engineer", "Acme")],
    )
    cycle_2_run, cycle_2_jobs = _seed_provider_run(
        conn,
        provider="linkedin",
        keyword="platform engineer",
        cycle_id="cycle-2",
        postings=[_posting("linkedin", "2", "Platform Engineer 2", "Beta")],
    )
    # One posting observed in both cycles must remain one scored observation.
    record_run_item(
        conn,
        SearchRunItem(
            run_id=cycle_2_run,
            job_id=cycle_1_jobs[0],
            decision=DuplicateKind.PROBABLE_DUPLICATE,
            rank_in_run=2,
            position_kind="search_rank",
            created_at=NOW,
        ),
    )
    insert_evaluation(
        conn,
        Evaluation(
            job_id=cycle_1_jobs[0],
            scorer=ScorerName.HEURISTIC,
            overall_score=80.0,
            recommendation=Recommendation.APPLY,
            created_at=NOW,
        ),
    )
    # The same literal keyword on another provider must stay a distinct series.
    _, getmanfred_jobs = _seed_provider_run(
        conn,
        provider="getmanfred",
        keyword="platform engineer",
        cycle_id=None,
        postings=[_posting("getmanfred", "g1", "Platform Engineer", "Gamma")],
        position_kind="none",
    )

    payload = build_payload(conn, top=10, now=None)

    series = {(item["provider"], item["keyword"]): item for item in payload["keywords"]}
    assert set(series) == {
        ("linkedin", "platform engineer"),
        ("getmanfred", "platform engineer"),
    }
    linkedin = series[("linkedin", "platform engineer")]
    # Both LinkedIn cycles collapse into one row, one shared filter key, and no
    # per-cycle identity on the metric.
    assert linkedin["source_key"] == "linkedin|platform engineer"
    assert linkedin["unique_jobs"] == 2
    assert linkedin["sightings"] == 3
    assert linkedin["scored_jobs"] == 1
    assert linkedin["avg_score"] == 80.0
    assert "cycle_id" not in linkedin
    assert (
        series[("getmanfred", "platform engineer")]["source_key"]
        == "getmanfred|platform engineer"
    )

    # Offer provenance keeps the real cycle ID of every observation.
    offers = {offer["id"]: offer for offer in payload["offers"]}
    assert offers[cycle_1_jobs[0]]["keywords"][0]["cycle_id"] == "cycle-1"
    assert offers[cycle_1_jobs[0]]["keywords"][1]["cycle_id"] == "cycle-2"
    assert offers[cycle_2_jobs[0]]["keywords"][0]["cycle_id"] == "cycle-2"
    assert offers[getmanfred_jobs[0]]["keywords"][0]["cycle_id"] is None
    assert (
        offers[cycle_1_jobs[0]]["keywords"][0]["source_key"]
        == "linkedin|platform engineer"
    )


def test_linkedin_regression_provenance_and_rank(conn, settings) -> None:
    client = FakeHttpClient(
        [response(200, fixture_text("linkedin_search_page_drift.html"), headers={"content-type": "text/html"})]
    )
    config = LinkedInSearchConfig(keywords="cloud", max_pages=1, max_items=10)
    result = search_linkedin(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    outcome = ingest_collector_result(
        conn, result, config=config, settings=settings, now=NOW
    )
    run = get_search_run(conn, outcome.run_id)
    assert run.provider == "linkedin"
    assert run.cycle_id is None
    assert run.params["keywords"] == "cloud"
    assert run.params["page_size"] == 25
    assert run.params["planned_urls"]
    rows = conn.execute(
        "SELECT position_kind, rank_in_run FROM search_run_items ORDER BY rank_in_run"
    ).fetchall()
    assert [row["position_kind"] for row in rows] == ["search_rank"] * len(rows)
    assert [row["rank_in_run"] for row in rows] == list(range(1, len(rows) + 1))
    assert count_jobs(conn) == 3
    assert [r.provider for r in list_search_runs(conn)] == ["linkedin"]


def test_new_schema_columns_are_nullable(conn) -> None:
    run = create_search_run(conn, SearchRun(provider="linkedin", started_at=NOW))
    assert run.cycle_id is None
    columns = {
        row["name"]: int(row["notnull"])
        for row in conn.execute("PRAGMA table_info(search_runs)")
    }
    assert columns["cycle_id"] == 0
    item_columns = {
        row["name"]: int(row["notnull"])
        for row in conn.execute("PRAGMA table_info(search_run_items)")
    }
    assert item_columns["position_kind"] == 0
