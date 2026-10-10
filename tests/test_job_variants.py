"""Provider variants: cross-provider identity, migration, and detail linkage.

Every test runs offline against a temporary in-memory database. No real/local
database is touched.
"""

from __future__ import annotations

from jobscout.collectors.linkedin_details import DetailItem, DetailResult
from jobscout.db import (
    SCHEMA_SQL,
    _sync_job_active,
    active_variants,
    connect,
    count_jobs,
    deactivate_job_variant,
    get_job,
    init_db,
    job_variants_for_job,
    latest_detail_fetch,
    resolve_variant_id,
    store_posting,
    variant_job_for_source,
    variants_needing_details,
)
from jobscout.models import (
    ContentStatus,
    DetailFetchStatus,
    JobPosting,
)
from jobscout.normalize import finalize_posting
from jobscout.pipeline import ingest_detail_results

NOW = "2026-09-15T10:00:00Z"


def _posting(
    source: str,
    source_job_id: str,
    url: str,
    *,
    title: str = "Platform Engineer",
    company: str = "Acme Data Inc.",
    location: str = "Remote, Argentina",
    description: str | None = "Python and SQL.",
    content_status: ContentStatus = ContentStatus.OK,
) -> JobPosting:
    return finalize_posting(
        JobPosting(
            source=source,
            title=title,
            company=company,
            location=location,
            url=url,
            source_job_id=source_job_id,
            description=description,
            content_status=content_status,
        )
    )


LINKEDIN = ("linkedin", "1111111111", "https://www.linkedin.com/jobs/view/1111111111/role")
GETMANFRED = (
    "getmanfred",
    "8383",
    "https://www.getmanfred.com/ofertas-empleo/8383/role",
)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

#: A real pre-v6 ``job_detail_fetches`` (no ``variant_id``); v5 is the latest
#: existing schema before this change.
V5_JOB_DETAIL_FETCHES_SQL = """
CREATE TABLE job_detail_fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES search_runs(id) ON DELETE SET NULL,
    source TEXT,
    url TEXT,
    final_url TEXT,
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


def _create_v5_database():
    """Build a database that looks like a real pre-v6 install."""
    connection = connect(":memory:")
    connection.executescript(SCHEMA_SQL)
    connection.executescript("DROP TABLE job_detail_fetches; DROP TABLE job_variants;")
    connection.executescript(V5_JOB_DETAIL_FETCHES_SQL)
    connection.execute("PRAGMA user_version = 5")
    connection.commit()
    return connection


def _insert_legacy_job(
    connection,
    *,
    fingerprint: str,
    source: str,
    source_job_id: str | None,
    url: str | None,
    content_status: str = "partial",
    description: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO jobs (
            fingerprint, dedupe_key, source, source_job_id, url, title, company,
            location, description, first_seen_at, last_seen_at, content_status,
            content_hash, is_active
        ) VALUES (?, ?, ?, ?, ?, 'Legacy Role', 'Legacy Co', 'Remote', ?, ?, ?, ?, '', 1)
        """,
        (
            fingerprint,
            f"dedupe-{fingerprint}",
            source,
            source_job_id,
            url,
            description,
            NOW,
            NOW,
            content_status,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def test_v6_upgrade_backfills_one_variant_per_job_and_keeps_history() -> None:
    connection = _create_v5_database()
    try:
        resolved = _insert_legacy_job(
            connection,
            fingerprint="fp-resolved",
            source="linkedin",
            source_job_id="3911111111",
            url="https://www.linkedin.com/jobs/view/3911111111/role",
            content_status="ok",
            description="A stored full description.",
        )
        unresolved = _insert_legacy_job(
            connection,
            fingerprint="fp-unresolved",
            source="getmanfred",
            source_job_id=None,
            url=None,
        )
        missing_source = _insert_legacy_job(
            connection,
            fingerprint="fp-missing-source",
            source="",
            source_job_id="999",
            url="https://example.test/999",
        )

        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert init_db(connection) == 6
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6

        # Exactly one variant per legacy job; no extra rows are invented.
        assert count_jobs(connection) == 3
        assert len(job_variants_for_job(connection, resolved)) == 1
        assert len(job_variants_for_job(connection, unresolved)) == 1
        assert len(job_variants_for_job(connection, missing_source)) == 1

        variant = job_variants_for_job(connection, resolved)[0]
        assert variant.source == "linkedin"
        assert variant.source_job_id == "3911111111"
        assert variant.url == "https://www.linkedin.com/jobs/view/3911111111/role"
        assert variant.review_state == "resolved"
        assert str(variant.content_status) == "ok"
        # The description length is carried so an already-enriched legacy job is
        # not re-selected for detail fetching.
        assert variant.description_chars == len("A stored full description.")

        # Ambiguous evidence is flagged, never resolved by guessing.
        ambiguous_none = job_variants_for_job(connection, unresolved)[0]
        assert ambiguous_none.review_state == "needs_review"
        assert ambiguous_none.source_job_id is None
        assert ambiguous_none.url is None
        flagged_source = job_variants_for_job(connection, missing_source)[0]
        assert flagged_source.review_state == "needs_review"
        assert flagged_source.source == ""

        # The migration is idempotent: a second init adds nothing.
        assert init_db(connection) == 6
        assert len(job_variants_for_job(connection, resolved)) == 1
        assert len(job_variants_for_job(connection, unresolved)) == 1
    finally:
        connection.close()


def test_v6_upgrade_adds_variant_id_column_without_rewriting_legacy_fetches() -> None:
    connection = _create_v5_database()
    try:
        job_id = _insert_legacy_job(
            connection,
            fingerprint="fp-fetch",
            source="linkedin",
            source_job_id="3922222222",
            url="https://www.linkedin.com/jobs/view/3922222222/role",
        )
        connection.execute(
            """
            INSERT INTO job_detail_fetches (
                job_id, source, url, status, http_status, content_status,
                description_chars, byte_size, fetched_at, stored_at, error, notes
            ) VALUES (?, 'linkedin', 'https://www.linkedin.com/jobs/view/3922222222/role',
                      'failed', 404, 'unknown', 0, 0, ?, ?, 'gone', 'legacy row')
            """,
            (job_id, NOW, NOW),
        )
        connection.commit()

        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(job_detail_fetches)")
        }
        assert "variant_id" not in columns

        init_db(connection)
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(job_detail_fetches)")
        }
        assert "variant_id" in columns

        legacy = latest_detail_fetch(connection, job_id)
        assert legacy is not None
        assert legacy.http_status == 404
        # Historical provenance keeps NULL; it is never back-linked by guessing.
        assert legacy.variant_id is None
    finally:
        connection.close()


def test_fresh_database_migration_is_idempotent(conn) -> None:
    assert init_db(conn) == 6
    assert init_db(conn) == 6
    assert job_variants_for_job(conn, 1) == []
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='job_variants'"
        ).fetchone()[0]
        == 1
    )


def test_legacy_jobs_are_not_cross_linked_by_the_migration() -> None:
    connection = _create_v5_database()
    try:
        first = _insert_legacy_job(
            connection,
            fingerprint="fp-a",
            source="linkedin",
            source_job_id="4000000001",
            url="https://www.linkedin.com/jobs/view/4000000001/role",
        )
        second = _insert_legacy_job(
            connection,
            fingerprint="fp-b",
            source="getmanfred",
            source_job_id="4001",
            url="https://www.getmanfred.com/ofertas-empleo/4001/role",
        )
        init_db(connection)
        # Each legacy job keeps its own single variant; no guessed shared parent.
        assert len(job_variants_for_job(connection, first)) == 1
        assert len(job_variants_for_job(connection, second)) == 1
        assert job_variants_for_job(connection, first)[0].job_id == first
        assert job_variants_for_job(connection, second)[0].job_id == second
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Cross-provider identity
# ---------------------------------------------------------------------------


def test_two_providers_share_a_canonical_job_and_keep_their_own_identity(conn) -> None:
    linkedin, _ = store_posting(conn, _posting(*LINKEDIN), now=NOW)
    getmanfred, decision = store_posting(conn, _posting(*GETMANFRED), now=NOW)

    assert getmanfred.id == linkedin.id
    assert count_jobs(conn) == 1
    assert decision.kind.value == "probable_duplicate"

    # The canonical row still names the first provider; it is never overwritten.
    canonical = get_job(conn, linkedin.id)
    assert canonical.source == "linkedin"
    assert canonical.source_job_id == "1111111111"
    assert canonical.url == LINKEDIN[2]

    variants = {variant.source: variant for variant in job_variants_for_job(conn, linkedin.id)}
    assert set(variants) == {"linkedin", "getmanfred"}
    assert variants["linkedin"].source_job_id == "1111111111"
    assert variants["linkedin"].url == LINKEDIN[2]
    assert variants["getmanfred"].source_job_id == "8383"
    assert variants["getmanfred"].url == GETMANFRED[2]
    assert variants["getmanfred"].review_state == "resolved"


def test_repeated_sighting_updates_the_same_variant(conn) -> None:
    posting = _posting(*LINKEDIN)
    job, first = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    same, second = store_posting(conn, posting, now="2026-09-16T10:00:00Z")

    assert first.kind.value == "new"
    assert same.id == job.id
    variants = job_variants_for_job(conn, job.id)
    assert len(variants) == 1
    assert variants[0].last_seen_at == "2026-09-16T10:00:00Z"


def test_resolve_variant_id_never_crosses_providers(conn) -> None:
    linkedin, _ = store_posting(conn, _posting(*LINKEDIN), now=NOW)
    store_posting(conn, _posting(*GETMANFRED), now=NOW)

    assert resolve_variant_id(
        conn, job_id=linkedin.id, source="getmanfred", source_job_id="8383"
    ) is not None
    assert (
        resolve_variant_id(
            conn, job_id=linkedin.id, source="getmanfred", source_job_id="1111111111"
        )
        is None
    )


# ---------------------------------------------------------------------------
# Detail selection and persistence
# ---------------------------------------------------------------------------


def _partial(source: str, source_job_id: str, url: str) -> JobPosting:
    return _posting(
        source,
        source_job_id,
        url,
        description=None,
        content_status=ContentStatus.PARTIAL,
    )


def test_variant_selection_returns_the_provider_identity(conn) -> None:
    linkedin, _ = store_posting(conn, _partial(*LINKEDIN), now=NOW)
    store_posting(conn, _partial(*GETMANFRED), now=NOW)

    getmanfred_selection = variants_needing_details(conn, source="getmanfred")
    assert [
        (job.id, job.source, job.source_job_id, job.url)
        for job in getmanfred_selection
    ] == [(linkedin.id, "getmanfred", "8383", GETMANFRED[2])]

    linkedin_selection = variants_needing_details(conn, source="linkedin")
    assert [
        (job.id, job.source, job.source_job_id, job.url)
        for job in linkedin_selection
    ] == [(linkedin.id, "linkedin", "1111111111", LINKEDIN[2])]

    assert [job.source for job in active_variants(conn, source="getmanfred")] == [
        "getmanfred"
    ]
    assert variant_job_for_source(conn, linkedin.id, "linkedin") is not None
    assert variant_job_for_source(conn, linkedin.id, "getmanfred") is not None


def test_ingest_links_the_fetch_to_the_chosen_variant(conn, settings) -> None:
    linkedin, _ = store_posting(conn, _partial(*LINKEDIN), now=NOW)

    item = DetailItem(
        job_id=int(linkedin.id),
        source_job_id="1111111111",
        url="https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/1111111111",
        status=DetailFetchStatus.OK,
        description="A complete backend description fetched from LinkedIn.",
        fetched_at="2026-09-15T12:00:00Z",
    )
    result = DetailResult(items=[item], planned_urls=[item.url], requested=1)
    outcome = ingest_detail_results(conn, result, settings=settings, provider="linkedin")

    variant = job_variants_for_job(conn, linkedin.id)[0]
    assert variant.detail_status == "ok"
    assert str(variant.content_status) == "ok"
    assert variant.description_chars == len(item.description or "")

    fetch = latest_detail_fetch(conn, int(linkedin.id))
    assert fetch is not None
    assert fetch.variant_id == variant.id
    assert outcome.enriched == 1
    # The enriched variant is no longer selected.
    assert variants_needing_details(conn, source="linkedin") == []


def test_definitive_missing_deactivates_only_the_variant(conn, settings) -> None:
    linkedin, _ = store_posting(conn, _posting(*LINKEDIN), now=NOW)
    store_posting(conn, _posting(*GETMANFRED), now=NOW)

    getmanfred = variant_job_for_source(conn, linkedin.id, "getmanfred")
    assert getmanfred is not None
    missing = DetailItem(
        job_id=int(linkedin.id),
        source_job_id=getmanfred.source_job_id,
        url=getmanfred.url,
        status=DetailFetchStatus.FAILED,
        http_status=404,
        error="gone",
    )
    outcome = ingest_detail_results(
        conn,
        DetailResult(items=[missing], planned_urls=[getmanfred.url or ""], requested=1),
        settings=settings,
        provider="getmanfred",
    )

    variants = {variant.source: variant for variant in job_variants_for_job(conn, linkedin.id)}
    assert variants["getmanfred"].is_active is False
    assert variants["linkedin"].is_active is True
    assert get_job(conn, linkedin.id).is_active is True
    assert outcome.deactivated == 0

    linkedin_variant = variant_job_for_source(conn, linkedin.id, "linkedin")
    missing_li = DetailItem(
        job_id=int(linkedin.id),
        source_job_id=linkedin_variant.source_job_id,
        url=linkedin_variant.url,
        status=DetailFetchStatus.FAILED,
        http_status=410,
        error="gone",
    )
    outcome_li = ingest_detail_results(
        conn,
        DetailResult(items=[missing_li], planned_urls=[linkedin_variant.url or ""], requested=1),
        settings=settings,
        provider="linkedin",
    )
    assert get_job(conn, linkedin.id).is_active is False
    assert outcome_li.deactivated == 1


def test_deactivate_and_resync_helpers(conn) -> None:
    linkedin, _ = store_posting(conn, _posting(*LINKEDIN), now=NOW)
    store_posting(conn, _posting(*GETMANFRED), now=NOW)
    variants = {v.source: v for v in job_variants_for_job(conn, linkedin.id)}

    assert deactivate_job_variant(conn, variants["getmanfred"].id) is True
    assert deactivate_job_variant(conn, variants["getmanfred"].id) is False
    assert _sync_job_active(conn, linkedin.id) is False  # linkedin still active
    assert get_job(conn, linkedin.id).is_active is True

    deactivate_job_variant(conn, variants["linkedin"].id)
    assert _sync_job_active(conn, linkedin.id) is True
    assert get_job(conn, linkedin.id).is_active is False
