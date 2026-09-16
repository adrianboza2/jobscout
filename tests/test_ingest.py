"""End-to-end offline ingestion tests using the JSON fixture."""

from __future__ import annotations

import json

import pytest

from conftest import FIXTURES, no_network

from jobscout.db import count_jobs, list_jobs, list_search_runs, connect, init_db
from jobscout.pipeline import ingest_fixture

FIXTURE = FIXTURES / "linkedin_search_sample.json"


def test_fixture_ingestion_creates_jobs_and_provenance(conn, settings, monkeypatch) -> None:
    no_network(monkeypatch)
    outcome = ingest_fixture(conn, FIXTURE, label="fixture:test", settings=settings, now="2026-09-15T12:00:00Z")

    assert outcome.items_found == 6
    assert outcome.new_jobs == 4
    assert outcome.duplicate_jobs == 2
    assert count_jobs(conn) == 4
    assert outcome.run_id is not None

    raw = conn.execute("SELECT * FROM raw_documents").fetchall()
    assert len(raw) == 1
    assert raw[0]["kind"] == "fixture"
    assert raw[0]["body"] is not None
    assert raw[0]["sha256"] and raw[0]["byte_size"] > 0
    assert raw[0]["url"].startswith("file:")

    run = list_search_runs(conn)[0]
    assert run.id == outcome.run_id
    assert run.status == "completed"
    assert run.items_found == 6
    assert run.new_jobs == 4
    assert run.duplicate_jobs == 2
    assert run.params["fixture_sha256"] == raw[0]["sha256"]

    decisions = [
        row["decision"]
        for row in conn.execute(
            "SELECT decision FROM search_run_items WHERE run_id = ? ORDER BY id", (run.id,)
        )
    ]
    assert decisions == [
        "new",
        "exact_duplicate",
        "new",
        "new",
        "new",
        "probable_duplicate",
    ]


def test_ingesting_the_same_fixture_twice_only_adds_duplicates(conn, settings) -> None:
    first = ingest_fixture(conn, FIXTURE, settings=settings, now="2026-09-15T12:00:00Z")
    second = ingest_fixture(conn, FIXTURE, settings=settings, now="2026-09-16T12:00:00Z")

    assert first.new_jobs == 4
    assert second.new_jobs == 0
    assert second.duplicate_jobs == 6
    assert count_jobs(conn) == 4
    assert len(list_search_runs(conn)) == 2

    # Sightings count observations, not runs: the fixture publishes job
    # 3901234567 twice, so each ingest adds two sightings.
    sighting = conn.execute(
        "SELECT sighting_count FROM jobs WHERE source_job_id = '3901234567'"
    ).fetchone()
    assert sighting["sighting_count"] == 4


def test_ingested_jobs_keep_source_fields_and_normalized_values(conn, settings) -> None:
    ingest_fixture(conn, FIXTURE, settings=settings, now="2026-09-15T12:00:00Z")
    rows = {
        row["source_job_id"]: row
        for row in conn.execute("SELECT * FROM jobs").fetchall()
    }
    acme = rows["3901234567"]
    assert acme["company"] == "Acme Data Inc."
    assert acme["url"] == "https://www.linkedin.com/jobs/view/3901234567/senior-backend-engineer-at-acme"
    assert acme["seniority"] == "senior"
    assert acme["employment_type"] == "full_time"
    assert "Python" in acme["description"]
    assert acme["posted_at"] == "2026-09-08T09:30:00Z"
    assert acme["first_seen_at"] == "2026-09-15T12:00:00Z"
    assert acme["content_status"] == "ok"

    platform = rows["3905555555"]
    assert platform["content_status"] == "ok"
    # The last fixture job is the same role with a fuller description, so the
    # probable-duplicate merge enriched the stored record instead of replacing it.
    assert platform["description"].startswith("Platform work: Kubernetes")
    assert platform["posted_at"] == "2026-09-07T15:45:00Z"


def test_partial_posting_stays_partial_without_a_richer_sighting(conn, settings) -> None:
    ingest_fixture(conn, FIXTURE, settings=settings, now="2026-09-15T12:00:00Z")
    umbrella = conn.execute("SELECT * FROM jobs WHERE source_job_id = '3906666666'").fetchone()
    assert umbrella["content_status"] == "ok"

    from jobscout.db import store_posting
    from jobscout.normalize import normalize_job_posting

    sparse = normalize_job_posting(
        {"title": "Solo Role", "company": "Solo Co", "jobUrl": "https://example.test/solo"},
        source="fixture",
    )
    stored, _ = store_posting(conn, sparse, now="2026-09-15T12:00:00Z")
    assert stored.content_status == "partial"
    assert stored.description is None


def test_ingestion_rejects_a_missing_fixture(conn, settings, tmp_path) -> None:
    from jobscout.collectors.base import CollectorError

    with pytest.raises(CollectorError):
        ingest_fixture(conn, tmp_path / "nope.json", settings=settings)


def test_ingestion_rejects_invalid_json(conn, settings, tmp_path) -> None:
    from jobscout.collectors.base import CollectorError

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(CollectorError):
        ingest_fixture(conn, bad, settings=settings)


def test_oversized_fixture_body_is_not_stored(conn, settings, tmp_path) -> None:
    big = tmp_path / "big.json"
    jobs = [{"title": f"Role {index}", "company": "Acme", "source_job_id": str(index)} for index in range(200)]
    big.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    tiny_limits = settings.with_overrides(raw_max_bytes=64)

    outcome = ingest_fixture(conn, big, settings=tiny_limits, now="2026-09-15T12:00:00Z")
    assert outcome.new_jobs == 200
    row = conn.execute("SELECT * FROM raw_documents").fetchone()
    assert row["body"] is None
    assert row["truncated"] == 1
    assert "not stored" in row["notes"]


def test_list_jobs_returns_ingested_rows(conn, settings) -> None:
    ingest_fixture(conn, FIXTURE, settings=settings, now="2026-09-15T12:00:00Z")
    items = list_jobs(conn, limit=10)
    assert len(items) == 4
    assert all(item.score is None for item in items)


def test_fixture_is_valid_json_and_marked_synthetic() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "Synthetic" in payload["note"]
    assert len(payload["jobs"]) == 6


def test_connection_fixture_helper(tmp_path) -> None:
    connection = connect(tmp_path / "ingest.sqlite3")
    try:
        init_db(connection)
        assert count_jobs(connection) == 0
    finally:
        connection.close()
