"""SQLite persistence layer.

Only the standard library is used (``sqlite3``). The schema is explicit and
versioned through ``PRAGMA user_version`` so future migrations stay honest
instead of silently drifting.

Tables
------
``search_runs``      one collector invocation (dry-run or live)
``raw_documents``    verbatim payloads kept for provenance (never required)
``jobs``             deduplicated job postings
``search_run_items`` one row per posting *observed* by a run (a run can observe
                     the same job more than once), with its dedupe verdict
``profiles``         versioned snapshots of the local candidate profile
``profile_evidence`` the structured facts extracted from a profile snapshot
``evaluations``      explainable scores, with full provenance
``job_feedback``     the human's own decision, imported from the dashboard export
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from .dedupe import DedupeDecision, decide_duplicate
from .models import (
    ContentStatus,
    DetailFetchStatus,
    DimensionScore,
    Evaluation,
    Job,
    JobDetailFetch,
    JobFeedback,
    JobPosting,
    JobVariant,
    ProfileEvidence,
    ProfileSnapshot,
    RawDocument,
    ScorerName,
    SearchRun,
    SearchRunItem,
    SearchRunStatus,
    _dumps,
    _loads,
    evaluation_contract_errors,
)
from .timeutil import utcnow_iso

SCHEMA_VERSION = 7

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    label TEXT,
    query TEXT,
    location TEXT,
    cycle_id TEXT,
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

CREATE TABLE IF NOT EXISTS raw_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source TEXT,
    url TEXT,
    status_code INTEGER,
    content_type TEXT,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT,
    stored_at TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    run_id INTEGER REFERENCES search_runs(id) ON DELETE SET NULL,
    notes TEXT,
    body TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_documents_sha256 ON raw_documents(sha256);
CREATE INDEX IF NOT EXISTS idx_raw_documents_kind ON raw_documents(kind);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    dedupe_key TEXT NOT NULL,
    source TEXT NOT NULL,
    source_job_id TEXT,
    url TEXT,
    title TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    location TEXT,
    description TEXT,
    employment_type TEXT,
    seniority TEXT,
    salary TEXT,
    language TEXT,
    posted_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    content_status TEXT NOT NULL DEFAULT 'unknown',
    content_hash TEXT NOT NULL DEFAULT '',
    sighting_count INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1,
    raw_document_id INTEGER REFERENCES raw_documents(id) ON DELETE SET NULL,
    extra_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_at);

-- Per-provider identity for a canonical (deduplicated) job. The ``jobs`` row is
-- the cross-provider parent; each variant keeps one provider's external id, its
-- own listing URL, and its own availability/detail state so a second provider
-- observing the same role never overwrites the first provider's identity.
CREATE TABLE IF NOT EXISTS job_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT '',
    source_job_id TEXT,
    url TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    content_status TEXT NOT NULL DEFAULT 'unknown',
    content_hash TEXT NOT NULL DEFAULT '',
    detail_status TEXT,
    detail_fetched_at TEXT,
    description_chars INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    review_state TEXT NOT NULL DEFAULT 'resolved',
    raw_document_id INTEGER REFERENCES raw_documents(id) ON DELETE SET NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_variants_job ON job_variants(job_id);
CREATE INDEX IF NOT EXISTS idx_job_variants_source ON job_variants(source, is_active);
-- Provider rows are unique per external id; NULL ids are allowed (a needs_review
-- variant may have no id) and are looked up by URL instead.
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_variants_provider_id
    ON job_variants(job_id, source, source_job_id)
    WHERE source_job_id IS NOT NULL AND source_job_id <> '';

CREATE TABLE IF NOT EXISTS search_run_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    decision TEXT NOT NULL DEFAULT 'new',
    rank_in_run INTEGER NOT NULL DEFAULT 0,
    position_kind TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_items_job ON search_run_items(job_id);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    source_path TEXT,
    loaded_at TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (name, fingerprint)
);

CREATE TABLE IF NOT EXISTS profile_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    value_json TEXT,
    weight REAL NOT NULL DEFAULT 1.0,
    source_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_profile_evidence_profile ON profile_evidence(profile_id);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    scorer TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    profile_id INTEGER REFERENCES profiles(id) ON DELETE SET NULL,
    overall_score REAL,
    recommendation TEXT NOT NULL DEFAULT 'need_more_info',
    confidence REAL NOT NULL DEFAULT 0.0,
    dimensions_json TEXT NOT NULL DEFAULT '[]',
    blockers_json TEXT NOT NULL DEFAULT '[]',
    strengths_json TEXT NOT NULL DEFAULT '[]',
    gaps_json TEXT NOT NULL DEFAULT '[]',
    unknowns_json TEXT NOT NULL DEFAULT '[]',
    salary_estimate_low INTEGER,
    salary_estimate_mid INTEGER,
    salary_estimate_high INTEGER,
    employer_tier TEXT,
    salary_basis TEXT,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT,
    raw_document_id INTEGER REFERENCES raw_documents(id) ON DELETE SET NULL,
    input_fingerprint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluations_job ON evaluations(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evaluations_scorer ON evaluations(scorer, status);

CREATE VIEW IF NOT EXISTS latest_evaluations AS
SELECT e.*
FROM evaluations e
WHERE e.id = (
    SELECT MAX(x.id) FROM evaluations x
    WHERE x.job_id = e.job_id AND x.status = 'ok'
);

CREATE TABLE IF NOT EXISTS job_detail_fetches (
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
    variant_id INTEGER REFERENCES job_variants(id) ON DELETE SET NULL,
    error TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_detail_fetches_job ON job_detail_fetches(job_id, id);
CREATE INDEX IF NOT EXISTS idx_detail_fetches_run ON job_detail_fetches(run_id);

-- The human's verdict on an offer. Kept in its own table, never inside
-- ``evaluations``: the model recommendation and the human decision are two
-- different signals and must be quotable separately. One row per job (the
-- latest decision wins); ``decision_updated_at`` is the dashboard's timestamp
-- for the decision, so an older export can never replace a newer stored one.
CREATE TABLE IF NOT EXISTS job_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    reason TEXT,
    decision_updated_at TEXT,
    project_id TEXT NOT NULL DEFAULT '',
    source TEXT,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_feedback_decision ON job_feedback(decision);
CREATE INDEX IF NOT EXISTS idx_job_feedback_updated ON job_feedback(decision_updated_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (and create the parent directory of) a SQLite database."""
    path = str(db_path)
    if path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    if path != ":memory:":
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:  # pragma: no cover - filesystem dependent
            pass
    return conn


def connect_readonly(db_path: str | Path) -> sqlite3.Connection | None:
    """Open an existing SQLite file read-only, or return ``None``.

    Unlike :func:`connect`, this never creates the parent directory, never
    creates a missing database file, and never opens WAL journals for a new
    database: it only attaches to a file that already exists.

    The connection is opened through SQLite's own read-only URI
    (``file:...?mode=ro``) instead of a writable connection pinned with
    ``PRAGMA query_only``. That makes the file read-only at the driver level:
    SQLite refuses to create the database, cannot replay a WAL by writing to it,
    and cannot punch through the pragma. ``PRAGMA query_only = ON`` is still set
    as a second line of defence. ``:memory:`` has nothing persisted to read and
    also returns ``None``.

    The absolute path is converted with :meth:`pathlib.Path.as_uri`, which
    percent-encodes spaces, ``#`` and non-ASCII characters and yields the
    ``file:///C:/...`` form SQLite expects on Windows.

    Sidecar handling: a read-only connection cannot checkpoint, so plain
    ``mode=ro`` leaves an empty ``-wal``/``-shm`` pair behind when it reads a
    WAL-mode database that has none. When the database is fully checkpointed (no
    ``-wal`` and no ``-journal`` beside it), ``immutable=1`` is added so the read
    creates no sidecar at all. When a ``-wal``/``-journal`` is present there is
    data SQLite must read from it, so plain ``mode=ro`` is used and correctness
    wins over the empty sidecar. Either way the database bytes and committed
    rows are never modified.
    """
    path = Path(db_path)
    if str(path) == ":memory:" or not path.is_file():
        return None
    try:
        uri = path.resolve().as_uri()
    except (OSError, ValueError):  # pragma: no cover - malformed path
        return None
    has_pending = any(
        Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-journal")
    )
    params = "?mode=ro" if has_pending else "?mode=ro&immutable=1"
    conn = sqlite3.connect(f"{uri}{params}", uri=True, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Names of the columns that actually exist for ``table`` right now.

    ``PRAGMA table_info`` is used instead of trusting ``user_version`` so a
    migration can verify the live shape before changing anything.
    """
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v4_add_final_url(conn: sqlite3.Connection) -> None:
    """Add the nullable ``job_detail_fetches.final_url`` column if missing."""
    if "final_url" in _table_columns(conn, "job_detail_fetches"):
        return
    conn.execute("ALTER TABLE job_detail_fetches ADD COLUMN final_url TEXT")


def _migrate_v5_add_cycle_and_position(conn: sqlite3.Connection) -> None:
    """Add the nullable provider/cycle/position provenance columns if missing.

    Both columns are additive and nullable, so historical rows keep their exact
    values and simply read as ``NULL`` (never guessed or backfilled):

    * ``search_runs.cycle_id`` groups runs from comparable search cycles;
    * ``search_run_items.position_kind`` records how ``rank_in_run`` must be read.
    """
    if "cycle_id" not in _table_columns(conn, "search_runs"):
        conn.execute("ALTER TABLE search_runs ADD COLUMN cycle_id TEXT")
    if "position_kind" not in _table_columns(conn, "search_run_items"):
        conn.execute("ALTER TABLE search_run_items ADD COLUMN position_kind TEXT")


def _migrate_v6_add_job_variants(conn: sqlite3.Connection) -> None:
    """Add the provider-variant layer without touching canonical job identity.

    Two additive steps, both inside the caller's transaction:

    * ``job_detail_fetches.variant_id`` (nullable FK) so new provenance can name
      the variant it belongs to; historical rows keep ``NULL``.
    * Exactly one ``job_variants`` row per existing job, built only from that
      job's own persisted evidence. When the provider id *and* the listing URL
      are both missing, or the source is empty, the variant is written with
      ``review_state = 'needs_review'`` instead of a guessed identity. No
      cross-provider link is ever invented: the variant's parent is the job it
      was already stored as.
    """
    if "variant_id" not in _table_columns(conn, "job_detail_fetches"):
        conn.execute(
            "ALTER TABLE job_detail_fetches ADD COLUMN variant_id INTEGER "
            "REFERENCES job_variants(id) ON DELETE SET NULL"
        )

    rows = conn.execute(
        """
        SELECT j.* FROM jobs j
        WHERE NOT EXISTS (SELECT 1 FROM job_variants v WHERE v.job_id = j.id)
        ORDER BY j.id
        """
    ).fetchall()
    for row in rows:
        source = str(row["source"] or "").strip()
        source_job_id = str(row["source_job_id"] or "").strip() or None
        url = str(row["url"] or "").strip() or None
        if not source:
            review_state = "needs_review"
            notes = "legacy backfill: provider source missing; identity unresolved"
        elif not source_job_id and not url:
            review_state = "needs_review"
            notes = "legacy backfill: no provider id or listing URL; identity unresolved"
        else:
            review_state = "resolved"
            notes = "legacy backfill from the canonical job row"
        conn.execute(
            """
            INSERT INTO job_variants (
                job_id, source, source_job_id, url, first_seen_at, last_seen_at,
                content_status, content_hash, description_chars, is_active,
                review_state, raw_document_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                source,
                source_job_id,
                url,
                row["first_seen_at"],
                row["last_seen_at"],
                row["content_status"],
                row["content_hash"],
                len(str(row["description"] or "")),
                int(row["is_active"]),
                review_state,
                row["raw_document_id"],
                notes,
            ),
        )


def _migrate_v7_add_salary_estimate(conn: sqlite3.Connection) -> None:
    """Add the nullable OpenCode salary-estimate columns if missing.

    Every column is additive and nullable, so historical evaluations keep their
    exact values and simply read as ``NULL``; no estimate is ever backfilled.
    """
    columns = _table_columns(conn, "evaluations")
    for name, column_type in (
        ("salary_estimate_low", "INTEGER"),
        ("salary_estimate_mid", "INTEGER"),
        ("salary_estimate_high", "INTEGER"),
        ("employer_tier", "TEXT"),
        ("salary_basis", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE evaluations ADD COLUMN {name} {column_type}")


def _apply_migrations(conn: sqlite3.Connection, current: int) -> None:
    """Upgrade an existing database in place from ``current`` to v7.

    ``SCHEMA_SQL`` only uses ``CREATE TABLE IF NOT EXISTS``, so it can create a
    brand-new database with the current shape but can never add a column to a
    table that already exists. Every migration here is therefore additive and
    idempotent: it inspects the live schema and only changes what is missing.
    """
    if current < 4:
        _migrate_v4_add_final_url(conn)
    if current < 5:
        _migrate_v5_add_cycle_and_position(conn)
    if current < 6:
        _migrate_v6_add_job_variants(conn)
    if current < 7:
        _migrate_v7_add_salary_estimate(conn)


def init_db(conn: sqlite3.Connection) -> int:
    """Create the schema if needed, migrate older databases, return the version."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {current} is newer than supported version {SCHEMA_VERSION}"
        )
    with conn:
        conn.executescript(SCHEMA_SQL)
        _apply_migrations(conn, current)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        fingerprint=row["fingerprint"],
        dedupe_key=row["dedupe_key"],
        source=row["source"],
        title=row["title"],
        company=row["company"],
        url=row["url"],
        location=row["location"],
        description=row["description"],
        source_job_id=row["source_job_id"],
        employment_type=row["employment_type"],
        seniority=row["seniority"],
        salary=row["salary"],
        language=row["language"],
        posted_at=row["posted_at"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        content_status=ContentStatus(row["content_status"]),
        content_hash=row["content_hash"],
        sighting_count=row["sighting_count"],
        is_active=bool(row["is_active"]),
        raw_document_id=row["raw_document_id"],
        extra=_loads(row["extra_json"], {}),
    )


def _row_to_evaluation(row: sqlite3.Row) -> Evaluation:
    dimensions = [
        DimensionScore.from_dict(item)
        for item in _loads(row["dimensions_json"], [])
        if isinstance(item, dict)
    ]
    return Evaluation(
        id=row["id"],
        job_id=row["job_id"],
        scorer=row["scorer"],
        model=row["model"],
        prompt_version=row["prompt_version"],
        profile_id=row["profile_id"],
        overall_score=row["overall_score"],
        recommendation=row["recommendation"],
        confidence=row["confidence"],
        dimensions=dimensions,
        blockers=_loads(row["blockers_json"], []),
        strengths=_loads(row["strengths_json"], []),
        gaps=_loads(row["gaps_json"], []),
        unknowns=_loads(row["unknowns_json"], []),
        rationale=row["rationale"] or "",
        salary_estimate_low=row["salary_estimate_low"],
        salary_estimate_mid=row["salary_estimate_mid"],
        salary_estimate_high=row["salary_estimate_high"],
        employer_tier=row["employer_tier"],
        salary_basis=row["salary_basis"],
        status=row["status"],
        error=row["error"],
        raw_document_id=row["raw_document_id"],
        input_fingerprint=row["input_fingerprint"],
        created_at=row["created_at"],
    )


def _row_to_search_run(row: sqlite3.Row) -> SearchRun:
    return SearchRun(
        id=row["id"],
        provider=row["provider"],
        label=row["label"],
        query=row["query"],
        location=row["location"],
        cycle_id=row["cycle_id"],
        params=_loads(row["params_json"], {}),
        status=SearchRunStatus(row["status"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        pages_fetched=row["pages_fetched"],
        items_found=row["items_found"],
        new_jobs=row["new_jobs"],
        duplicate_jobs=row["duplicate_jobs"],
        error=row["error"],
        raw_document_id=row["raw_document_id"],
        notes=row["notes"],
    )


def _row_to_detail_fetch(row: sqlite3.Row) -> JobDetailFetch:
    return JobDetailFetch(
        id=row["id"],
        job_id=row["job_id"],
        run_id=row["run_id"],
        source=row["source"],
        url=row["url"],
        final_url=row["final_url"],
        status=DetailFetchStatus(row["status"]),
        http_status=row["http_status"],
        content_status=row["content_status"],
        description_chars=row["description_chars"],
        byte_size=row["byte_size"],
        sha256=row["sha256"],
        content_hash=row["content_hash"],
        fetched_at=row["fetched_at"],
        stored_at=row["stored_at"],
        raw_document_id=row["raw_document_id"],
        variant_id=row["variant_id"],
        error=row["error"],
        notes=row["notes"],
    )


def _row_to_variant(row: sqlite3.Row) -> JobVariant:
    return JobVariant(
        id=row["id"],
        job_id=row["job_id"],
        source=row["source"],
        source_job_id=row["source_job_id"],
        url=row["url"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        content_status=ContentStatus(row["content_status"]),
        content_hash=row["content_hash"],
        detail_status=row["detail_status"],
        detail_fetched_at=row["detail_fetched_at"],
        description_chars=row["description_chars"],
        is_active=bool(row["is_active"]),
        review_state=row["review_state"],
        raw_document_id=row["raw_document_id"],
        notes=row["notes"],
    )


def _row_to_profile(row: sqlite3.Row) -> ProfileSnapshot:
    return ProfileSnapshot(
        id=row["id"],
        name=row["name"],
        fingerprint=row["fingerprint"],
        source_path=row["source_path"],
        loaded_at=row["loaded_at"],
        raw=_loads(row["raw_json"], {}),
    )


def _row_to_feedback(row: sqlite3.Row) -> JobFeedback:
    return JobFeedback(
        id=row["id"],
        job_id=row["job_id"],
        decision=row["decision"],
        reason=row["reason"],
        decision_updated_at=row["decision_updated_at"],
        project_id=row["project_id"],
        source=row["source"],
        imported_at=row["imported_at"],
    )


# ---------------------------------------------------------------------------
# Raw documents
# ---------------------------------------------------------------------------


def insert_raw_document(conn: sqlite3.Connection, document: RawDocument) -> int:
    """Persist a raw payload, reusing an existing identical row when present."""
    if document.notes == "":
        document.notes = None
    existing = conn.execute(
        "SELECT id FROM raw_documents WHERE sha256 = ? AND kind = ? LIMIT 1",
        (document.sha256, str(document.kind)),
    ).fetchone()
    if existing is not None:
        return int(existing["id"])
    stored_at = document.stored_at or utcnow_iso()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO raw_documents (
                kind, source, url, status_code, content_type, sha256, byte_size,
                fetched_at, stored_at, truncated, run_id, notes, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(document.kind),
                document.source,
                document.url,
                document.status_code,
                document.content_type,
                document.sha256,
                document.byte_size,
                document.fetched_at,
                stored_at,
                int(bool(document.truncated)),
                document.run_id,
                document.notes,
                document.body,
            ),
        )
    document.id = int(cursor.lastrowid)
    return document.id


# ---------------------------------------------------------------------------
# Search runs
# ---------------------------------------------------------------------------


def create_search_run(conn: sqlite3.Connection, run: SearchRun) -> SearchRun:
    started = run.started_at or utcnow_iso()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO search_runs (
                provider, label, query, location, cycle_id, params_json, status,
                started_at, finished_at, pages_fetched, items_found, new_jobs,
                duplicate_jobs, error, raw_document_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.provider,
                run.label,
                run.query,
                run.location,
                run.cycle_id,
                _dumps(run.params),
                str(run.status),
                started,
                run.finished_at,
                run.pages_fetched,
                run.items_found,
                run.new_jobs,
                run.duplicate_jobs,
                run.error,
                run.raw_document_id,
                run.notes,
            ),
        )
    run.id = int(cursor.lastrowid)
    run.started_at = started
    return run


def finish_search_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: SearchRunStatus,
    pages_fetched: int | None = None,
    items_found: int | None = None,
    new_jobs: int | None = None,
    duplicate_jobs: int | None = None,
    error: str | None = None,
    notes: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE search_runs
            SET status = ?,
                finished_at = ?,
                pages_fetched = COALESCE(?, pages_fetched),
                items_found = COALESCE(?, items_found),
                new_jobs = COALESCE(?, new_jobs),
                duplicate_jobs = COALESCE(?, duplicate_jobs),
                error = COALESCE(?, error),
                notes = COALESCE(?, notes)
            WHERE id = ?
            """,
            (
                str(status),
                utcnow_iso(),
                pages_fetched,
                items_found,
                new_jobs,
                duplicate_jobs,
                error,
                notes,
                run_id,
            ),
        )


def get_search_run(conn: sqlite3.Connection, run_id: int) -> SearchRun | None:
    row = conn.execute("SELECT * FROM search_runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_search_run(row) if row else None


def list_search_runs(conn: sqlite3.Connection, limit: int = 20) -> list[SearchRun]:
    rows = conn.execute(
        "SELECT * FROM search_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_search_run(row) for row in rows]


def record_run_item(conn: sqlite3.Connection, item: SearchRunItem) -> None:
    """Record one observation of a job inside a search run.

    A run may observe the same job more than once (for example when a later page
    repeats a posting); each observation is kept with its own dedupe verdict.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO search_run_items (
                run_id, job_id, decision, rank_in_run, position_kind, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                item.run_id,
                item.job_id,
                str(item.decision),
                item.rank_in_run,
                str(item.position_kind) if item.position_kind is not None else None,
                item.created_at or utcnow_iso(),
            ),
        )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def find_duplicate(conn: sqlite3.Connection, posting: JobPosting) -> DedupeDecision:
    """Look up strong/weak identity keys and delegate to the pure policy."""
    fingerprint_row = conn.execute(
        "SELECT id FROM jobs WHERE fingerprint = ? LIMIT 1", (posting.fingerprint,)
    ).fetchone()
    dedupe_row = conn.execute(
        "SELECT id FROM jobs WHERE dedupe_key = ? ORDER BY id LIMIT 1", (posting.dedupe_key,)
    ).fetchone()
    fingerprints = (
        {posting.fingerprint: int(fingerprint_row["id"])} if fingerprint_row else {}
    )
    dedupe_keys = {posting.dedupe_key: int(dedupe_row["id"])} if dedupe_row else {}
    return decide_duplicate(posting, fingerprints, dedupe_keys)


def _variant_review_state(
    source: str, source_job_id: str | None, url: str | None
) -> tuple[str, str | None]:
    """Classify a provider identity as resolved or needing review.

    Ambiguity is never resolved by guessing: a missing provider or a missing
    provider id *and* URL yields ``needs_review`` so a human can revisit it.
    """
    if not source:
        return "needs_review", "provider source missing; identity unresolved"
    if not source_job_id and not url:
        return "needs_review", "no provider id or listing URL; identity unresolved"
    return "resolved", None


def resolve_variant_id(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    source: str,
    source_job_id: str | None = None,
    url: str | None = None,
) -> int | None:
    """Find the variant for a provider identity, preferring the external id.

    Lookup order: exact ``(job_id, source, source_job_id)``, then
    ``(job_id, source, url)``, then a provider row that itself carries no
    identity. Returns ``None`` when no variant matches; it never falls back to a
    different provider.
    """
    normalized_source = str(source or "").strip()
    normalized_id = str(source_job_id or "").strip() or None
    normalized_url = str(url or "").strip() or None
    if not normalized_source:
        return None
    if normalized_id:
        row = conn.execute(
            "SELECT id FROM job_variants WHERE job_id = ? AND source = ? AND source_job_id = ?",
            (int(job_id), normalized_source, normalized_id),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    if normalized_url:
        row = conn.execute(
            "SELECT id FROM job_variants WHERE job_id = ? AND source = ? AND url = ?",
            (int(job_id), normalized_source, normalized_url),
        ).fetchone()
        if row is not None:
            return int(row["id"])
    if not normalized_id and not normalized_url:
        row = conn.execute(
            """
            SELECT id FROM job_variants
            WHERE job_id = ? AND source = ?
              AND (source_job_id IS NULL OR source_job_id = '')
              AND (url IS NULL OR url = '')
            ORDER BY id LIMIT 1
            """,
            (int(job_id), normalized_source),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        # A detail item can lose its identity (for example a skipped target whose
        # URL did not resolve). Bind it to the provider's lone variant rather than
        # inventing a second one; multiple variants stay ambiguous and return None.
        rows = conn.execute(
            "SELECT id FROM job_variants WHERE job_id = ? AND source = ? ORDER BY id",
            (int(job_id), normalized_source),
        ).fetchall()
        if len(rows) == 1:
            return int(rows[0]["id"])
    return None


def ensure_job_variant(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    source: str,
    source_job_id: str | None = None,
    url: str | None = None,
    timestamp: str,
    raw_document_id: int | None = None,
    content_status: ContentStatus | str = ContentStatus.UNKNOWN,
    content_hash: str = "",
    description_chars: int = 0,
) -> int:
    """Find or create one provider variant from explicit identity fields.

    A second provider observing the same canonical role adds/refreshes its own
    variant; the canonical ``jobs`` row and the first provider's identity are
    never overwritten. Existing variant content is only upgraded to ``ok``,
    never downgraded, by a later search card.
    """
    normalized_source = str(source or "").strip()
    normalized_id = str(source_job_id or "").strip() or None
    normalized_url = str(url or "").strip() or None
    normalized_status = str(content_status)
    variant_id = resolve_variant_id(
        conn,
        job_id=int(job_id),
        source=normalized_source,
        source_job_id=normalized_id,
        url=normalized_url,
    )
    if variant_id is not None:
        with conn:
            conn.execute(
                """
                UPDATE job_variants
                SET last_seen_at = ?, is_active = 1,
                    url = COALESCE(url, ?), source_job_id = COALESCE(source_job_id, ?),
                    content_status = CASE WHEN ? = 'ok' THEN ? ELSE content_status END,
                    content_hash = CASE WHEN ? = 'ok' THEN ? ELSE content_hash END,
                    description_chars = CASE WHEN ? = 'ok' THEN ? ELSE description_chars END,
                    raw_document_id = COALESCE(?, raw_document_id)
                WHERE id = ?
                """,
                (
                    timestamp,
                    normalized_url,
                    normalized_id,
                    normalized_status,
                    normalized_status,
                    normalized_status,
                    content_hash,
                    normalized_status,
                    int(description_chars),
                    raw_document_id,
                    variant_id,
                ),
            )
        return variant_id

    review_state, notes = _variant_review_state(
        normalized_source, normalized_id, normalized_url
    )
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO job_variants (
                job_id, source, source_job_id, url, first_seen_at, last_seen_at,
                content_status, content_hash, description_chars, is_active,
                review_state, raw_document_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                int(job_id),
                normalized_source,
                normalized_id,
                normalized_url,
                timestamp,
                timestamp,
                normalized_status,
                content_hash,
                int(description_chars),
                review_state,
                raw_document_id,
                notes,
            ),
        )
    return int(cursor.lastrowid)


def upsert_job_variant(
    conn: sqlite3.Connection,
    job_id: int,
    posting: JobPosting,
    *,
    timestamp: str,
    raw_document_id: int | None = None,
) -> int:
    """Record this posting's provider identity under its canonical job."""
    return ensure_job_variant(
        conn,
        job_id,
        source=str(getattr(posting, "source", "") or ""),
        source_job_id=getattr(posting, "source_job_id", None),
        url=getattr(posting, "url", None),
        timestamp=timestamp,
        raw_document_id=raw_document_id,
        content_status=getattr(posting, "content_status", ContentStatus.UNKNOWN),
        content_hash=str(getattr(posting, "content_hash", "") or ""),
        description_chars=len(getattr(posting, "description", "") or ""),
    )


def job_variants_for_job(
    conn: sqlite3.Connection, job_id: int
) -> list[JobVariant]:
    rows = conn.execute(
        "SELECT * FROM job_variants WHERE job_id = ? ORDER BY id",
        (int(job_id),),
    ).fetchall()
    return [_row_to_variant(row) for row in rows]


def update_job_variant_detail(
    conn: sqlite3.Connection,
    variant_id: int,
    *,
    status: str,
    content_status: ContentStatus | str | None = None,
    content_hash: str | None = None,
    description_chars: int | None = None,
    fetched_at: str | None = None,
    last_seen_at: str | None = None,
) -> None:
    """Record one detail-fetch outcome on a variant.

    ``content_status`` is only written when the caller has richer evidence
    (``ok``/``partial``); a failed attempt records ``detail_status`` alone so it
    cannot downgrade a previously enriched variant.
    """
    assignments = ["detail_status = ?"]
    params: list[Any] = [str(status)]
    if fetched_at is not None:
        assignments.append("detail_fetched_at = COALESCE(?, detail_fetched_at)")
        params.append(fetched_at)
    if content_status is not None:
        assignments.append("content_status = ?")
        params.append(str(content_status))
    if content_hash:
        assignments.append("content_hash = ?")
        params.append(content_hash)
    if description_chars is not None:
        assignments.append("description_chars = ?")
        params.append(int(description_chars))
    if last_seen_at:
        assignments.append("last_seen_at = ?")
        params.append(last_seen_at)
    params.append(int(variant_id))
    with conn:
        conn.execute(
            f"UPDATE job_variants SET {', '.join(assignments)} WHERE id = ?",
            params,
        )


def deactivate_job_variant(conn: sqlite3.Connection, variant_id: int) -> bool:
    """Mark one provider variant inactive, never deleting its history."""
    with conn:
        cursor = conn.execute(
            "UPDATE job_variants SET is_active = 0 WHERE id = ? AND is_active = 1",
            (int(variant_id),),
        )
    return cursor.rowcount > 0


def _sync_job_active(conn: sqlite3.Connection, job_id: int) -> bool:
    """Keep a canonical job active while at least one provider variant is.

    A job with no variant rows (should not happen after the v6 backfill, but
    possible for a hand-built database) is left untouched rather than guessed.
    Returns ``True`` only when the canonical status actually changed.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active "
        "FROM job_variants WHERE job_id = ?",
        (int(job_id),),
    ).fetchone()
    total = int(row["total"] or 0)
    if total == 0:
        return False
    want = 1 if int(row["active"] or 0) > 0 else 0
    with conn:
        cursor = conn.execute(
            "UPDATE jobs SET is_active = ? WHERE id = ? AND is_active != ?",
            (want, int(job_id), want),
        )
    return cursor.rowcount > 0


def store_posting(
    conn: sqlite3.Connection,
    posting: JobPosting,
    *,
    now: str | None = None,
    raw_document_id: int | None = None,
) -> tuple[Job, DedupeDecision]:
    """Persist a posting, deduplicating against existing rows.

    Returns the stored (or matched) job plus the dedupe decision so callers can
    report counts and reasons.
    """
    if not posting.fingerprint:
        raise ValueError("posting must be finalized before storage")
    timestamp = now or utcnow_iso()
    posting.raw_document_id = raw_document_id or posting.raw_document_id

    decision = find_duplicate(conn, posting)
    if decision.is_new:
        job = Job.from_posting(posting, now=timestamp)
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO jobs (
                    fingerprint, dedupe_key, source, source_job_id, url, title, company,
                    location, description, employment_type, seniority, salary, language,
                    posted_at, first_seen_at, last_seen_at, content_status, content_hash,
                    sighting_count, is_active, raw_document_id, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.fingerprint,
                    job.dedupe_key,
                    job.source,
                    job.source_job_id,
                    job.url,
                    job.title,
                    job.company,
                    job.location,
                    job.description,
                    job.employment_type,
                    job.seniority,
                    job.salary,
                    job.language,
                    job.posted_at,
                    job.first_seen_at,
                    job.last_seen_at,
                    str(job.content_status),
                    job.content_hash,
                    job.sighting_count,
                    int(job.is_active),
                    job.raw_document_id,
                    _dumps(job.extra),
                ),
            )
        job.id = int(cursor.lastrowid)
        upsert_job_variant(
            conn,
            job.id,
            posting,
            timestamp=timestamp,
            raw_document_id=posting.raw_document_id,
        )
        return job, decision

    existing_id = int(decision.existing_job_id or 0)
    job = get_job(conn, existing_id)
    if job is None:  # pragma: no cover - defensive, FK integrity guarantees this
        raise RuntimeError(f"duplicate decision pointed at missing job {existing_id}")
    updated = _merge_seen_job(job, posting, timestamp)
    with conn:
        conn.execute(
            """
            UPDATE jobs
            SET last_seen_at = ?, sighting_count = sighting_count + 1, is_active = 1,
                description = ?, content_status = ?, content_hash = ?,
                url = COALESCE(url, ?), location = COALESCE(location, ?),
                salary = COALESCE(salary, ?), posted_at = COALESCE(posted_at, ?),
                raw_document_id = COALESCE(?, raw_document_id)
            WHERE id = ?
            """,
            (
                timestamp,
                updated.description,
                str(updated.content_status),
                updated.content_hash,
                posting.url,
                posting.location,
                posting.salary,
                posting.posted_at,
                raw_document_id,
                job.id,
            ),
        )
    refreshed = get_job(conn, existing_id)
    # Record this provider's own identity even when the canonical row was first
    # seen from another provider: the new variant is what makes the second
    # provider selectable and enrichable.
    upsert_job_variant(
        conn,
        existing_id,
        posting,
        timestamp=timestamp,
        raw_document_id=posting.raw_document_id,
    )
    return (refreshed or job), decision


def _merge_seen_job(job: Job, posting: JobPosting, timestamp: str) -> Job:
    """Return an in-memory view of the merged row (no I/O)."""
    merged = replace(job, extra=dict(job.extra))
    merged.last_seen_at = timestamp
    merged.sighting_count = job.sighting_count + 1
    merged.is_active = True
    # Prefer richer content when the new fetch actually carries more text.
    if posting.content_hash and posting.content_hash != job.content_hash:
        new_length = len(posting.description or "")
        old_length = len(job.description or "")
        if new_length > old_length:
            merged.description = posting.description
            merged.content_hash = posting.content_hash
            merged.content_status = posting.content_status
    return merged


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def count_jobs(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


@dataclass
class JobListItem:
    """A job joined with its latest successful evaluation."""

    job: Job
    score: float | None = None
    recommendation: str | None = None
    confidence: float | None = None
    evaluation_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.to_dict(),
            "score": self.score,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "evaluation_id": self.evaluation_id,
        }


def list_jobs(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    min_score: float | None = None,
    include_inactive: bool = False,
    scored_only: bool = False,
    rank_by_score: bool = False,
    job_ids: Sequence[int] | None = None,
) -> list[JobListItem]:
    """List jobs with their latest successful evaluation attached."""
    clauses: list[str] = []
    params: list[Any] = []
    if not include_inactive:
        clauses.append("j.is_active = 1")
    if min_score is not None:
        clauses.append("e.overall_score IS NOT NULL AND e.overall_score >= ?")
        params.append(float(min_score))
    if scored_only:
        clauses.append("e.id IS NOT NULL")
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        clauses.append(f"j.id IN ({placeholders})")
        params.extend(int(job_id) for job_id in job_ids)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = (
        "ORDER BY (e.overall_score IS NULL), e.overall_score DESC, "
        "COALESCE(e.confidence, 0) DESC, j.last_seen_at DESC"
        if rank_by_score
        else "ORDER BY j.last_seen_at DESC, j.id DESC"
    )
    params.append(int(limit))
    rows = conn.execute(
        f"""
        SELECT j.*,
               e.overall_score AS score,
               e.recommendation AS recommendation,
               e.confidence AS confidence,
               e.id AS evaluation_id
        FROM jobs j
        LEFT JOIN latest_evaluations e ON e.job_id = j.id
        {where}
        {order}
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        JobListItem(
            job=_row_to_job(row),
            score=row["score"],
            recommendation=row["recommendation"],
            confidence=row["confidence"],
            evaluation_id=row["evaluation_id"],
        )
        for row in rows
    ]


def jobs_needing_evaluation(
    conn: sqlite3.Connection,
    *,
    scorer: ScorerName | str,
    limit: int | None = None,
    fingerprint_for: Callable[[Job], str] | None = None,
) -> list[Job]:
    """Active jobs whose current scoring input has no successful evaluation.

    With ``fingerprint_for`` the selection is **input-aware**: a job is returned
    when no successful (``status = 'ok'``) evaluation from ``scorer`` carries the
    job's *current* effective-input fingerprint. That keeps a changed profile,
    model, prompt/policy, or rewritten description from being masked by an older
    success — the older row simply stops matching. Historical rows with an empty
    fingerprint also stop matching, so unknown provenance is re-evaluated rather
    than trusted. ``--rescore`` intentionally bypasses this function.

    Without ``fingerprint_for`` the legacy behaviour is preserved: a job is
    returned when it has no successful evaluation from ``scorer`` at all.
    """
    if limit is not None and int(limit) <= 0:
        return []
    if fingerprint_for is None:
        sql = """
            SELECT j.* FROM jobs j
            WHERE j.is_active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM evaluations e
                  WHERE e.job_id = j.id AND e.scorer = ? AND e.status = 'ok'
              )
            ORDER BY j.last_seen_at DESC, j.id DESC
        """
        params: list[Any] = [str(scorer)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [_row_to_job(row) for row in conn.execute(sql, params).fetchall()]

    candidates = [
        _row_to_job(row)
        for row in conn.execute(
            "SELECT j.* FROM jobs j WHERE j.is_active = 1 "
            "ORDER BY j.last_seen_at DESC, j.id DESC"
        ).fetchall()
    ]
    successful: dict[int, set[str]] = {}
    for row in conn.execute(
        "SELECT job_id, input_fingerprint FROM evaluations "
        "WHERE scorer = ? AND status = 'ok'",
        (str(scorer),),
    ).fetchall():
        successful.setdefault(int(row["job_id"]), set()).add(row["input_fingerprint"] or "")

    selected: list[Job] = []
    for job in candidates:
        expected = fingerprint_for(job)
        if expected in successful.get(int(job.id or 0), set()):
            continue
        selected.append(job)
        if limit is not None and len(selected) >= int(limit):
            break
    return selected


def all_active_jobs(
    conn: sqlite3.Connection, *, limit: int | None = None, source: str | None = None
) -> list[Job]:
    """Active jobs in ``id`` order, optionally narrowed to one provider."""
    clauses = ["is_active = 1"]
    params: list[Any] = []
    if source:
        clauses.append("source = ?")
        params.append(str(source))
    sql = "SELECT * FROM jobs WHERE " + " AND ".join(clauses) + " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_row_to_job(row) for row in conn.execute(sql, params).fetchall()]


#: Failed detail attempts after which an offer may be scored on its card anyway.
#: Without the exception, an offer whose provider keeps refusing would never be
#: scored; a 404/410 is definitive and never counts (it deactivates the offer).
DETAIL_ATTEMPTS_BEFORE_SCORING = 2


def _jobs_needing_details_predicate(prefix: str = "") -> str:
    column = f"{prefix}." if prefix else ""
    return (
        f"({column}content_status != 'ok' OR {column}description IS NULL"
        f" OR TRIM({column}description) = '')"
    )


def jobs_awaiting_details(conn: sqlite3.Connection) -> set[int]:
    """Active offers that still need details and have attempts left to get them.

    ``score`` skips these so the model never pays to score a search card, and the
    dashboard marks them so the reader knows why an offer has no score yet. A
    ``skipped`` detail row means the request was never made and does not count;
    only real failures (``failed``/``partial``) do.
    """
    sql = f"""
        SELECT j.id AS id
        FROM jobs j
        WHERE j.is_active = 1
          AND {_jobs_needing_details_predicate("j")}
          AND (
              SELECT COUNT(*)
              FROM job_detail_fetches f
              WHERE f.job_id = j.id
                AND f.status IN ('failed', 'partial')
                AND COALESCE(f.http_status, 0) NOT IN (404, 410)
          ) < ?
    """
    rows = conn.execute(sql, (DETAIL_ATTEMPTS_BEFORE_SCORING,)).fetchall()
    return {int(row["id"]) for row in rows}


def jobs_needing_details(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    job_ids: Sequence[int] | None = None,
    source: str | None = None,
) -> list[Job]:
    """Active jobs that still lack a successful full description.

    A job counts as enriched when it is ``content_status = 'ok'`` *and* has a
    non-empty description, so search cards (``partial``) are always selected.
    ``source`` optionally restricts the selection to one provider.
    """
    clauses = [
        "is_active = 1",
        _jobs_needing_details_predicate(),
    ]
    params: list[Any] = []
    if source:
        clauses.append("source = ?")
        params.append(str(source))
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        clauses.append(f"id IN ({placeholders})")
        params.extend(int(job_id) for job_id in job_ids)
    sql = (
        "SELECT * FROM jobs WHERE "
        + " AND ".join(clauses)
        + " ORDER BY last_seen_at DESC, id DESC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_row_to_job(row) for row in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# Provider-variant detail selection
# ---------------------------------------------------------------------------
#
# The fetchers need the chosen *variant's* provider id and listing URL, not the
# canonical ``jobs`` row's first-seen identity. These readers return canonical
# ``Job`` objects whose ``source``/``source_job_id``/``url`` are replaced with
# the selected variant's values, so the existing provider plan functions resolve
# the correct URL without a second identity model.

_VARIANT_SELECT_COLUMNS = """
    j.*,
    v.source AS variant_source,
    v.source_job_id AS variant_source_job_id,
    v.url AS variant_url,
    v.id AS variant_id
"""


def _jobs_with_variant_identity(rows: Sequence[sqlite3.Row]) -> list[Job]:
    jobs: list[Job] = []
    for row in rows:
        job = _row_to_job(row)
        jobs.append(
            replace(
                job,
                source=row["variant_source"],
                source_job_id=row["variant_source_job_id"],
                url=row["variant_url"],
            )
        )
    return jobs


def variants_needing_details(
    conn: sqlite3.Connection,
    *,
    source: str,
    limit: int | None = None,
    job_ids: Sequence[int] | None = None,
) -> list[Job]:
    """Active variants for ``source`` whose own detail is still incomplete.

    Variant-scoped on purpose: a second provider's variant is enriched even when
    the canonical job was already enriched from another provider.
    """
    clauses = [
        "j.is_active = 1",
        "v.is_active = 1",
        "v.source = ?",
        "(v.content_status != 'ok' OR v.description_chars <= 0)",
    ]
    params: list[Any] = [str(source)]
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        clauses.append(f"v.job_id IN ({placeholders})")
        params.extend(int(job_id) for job_id in job_ids)
    sql = (
        "SELECT "
        + _VARIANT_SELECT_COLUMNS
        + " FROM job_variants v JOIN jobs j ON j.id = v.job_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY v.last_seen_at DESC, v.id DESC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return _jobs_with_variant_identity(conn.execute(sql, params).fetchall())


def active_variants(
    conn: sqlite3.Connection,
    *,
    source: str,
    limit: int | None = None,
    job_ids: Sequence[int] | None = None,
) -> list[Job]:
    """Every active variant for ``source``, regardless of detail status."""
    clauses = ["j.is_active = 1", "v.is_active = 1", "v.source = ?"]
    params: list[Any] = [str(source)]
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        clauses.append(f"v.job_id IN ({placeholders})")
        params.extend(int(job_id) for job_id in job_ids)
    sql = (
        "SELECT "
        + _VARIANT_SELECT_COLUMNS
        + " FROM job_variants v JOIN jobs j ON j.id = v.job_id WHERE "
        + " AND ".join(clauses)
        # Ascending canonical job id preserves the historical `--all` order of
        # `all_active_jobs`, so a bounded run processes offers deterministically
        # and a later fail-closed refusal cannot pre-empt an earlier success.
        + " ORDER BY j.id ASC, v.id ASC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return _jobs_with_variant_identity(conn.execute(sql, params).fetchall())


def variant_job_for_source(
    conn: sqlite3.Connection, job_id: int, source: str
) -> Job | None:
    """The ``source`` variant of one job, for the explicit ``--job-id`` route.

    Unlike the bulk selectors this ignores ``is_active`` so an explicit request
    keeps the historical ability to target a deactivated offer.
    """
    row = conn.execute(
        "SELECT "
        + _VARIANT_SELECT_COLUMNS
        + " FROM job_variants v JOIN jobs j ON j.id = v.job_id "
        "WHERE v.job_id = ? AND v.source = ? ORDER BY v.id LIMIT 1",
        (int(job_id), str(source)),
    ).fetchone()
    return _jobs_with_variant_identity([row])[0] if row is not None else None


def count_active_variants(conn: sqlite3.Connection, *, source: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM job_variants v JOIN jobs j ON j.id = v.job_id "
            "WHERE j.is_active = 1 AND v.is_active = 1 AND v.source = ?",
            (str(source),),
        ).fetchone()[0]
    )


def count_variants_needing_details(conn: sqlite3.Connection, *, source: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM job_variants v JOIN jobs j ON j.id = v.job_id "
            "WHERE j.is_active = 1 AND v.is_active = 1 AND v.source = ? "
            "AND (v.content_status != 'ok' OR v.description_chars <= 0)",
            (str(source),),
        ).fetchone()[0]
    )


#: Joins a job to its single most recent detail-fetch attempt (highest id,
#: matching :func:`latest_detail_fetch`). A job with no fetch rows never matches,
#: so "refresh" only considers offers that already have fetch history.
_LATEST_DETAIL_FETCH_JOIN = """
    JOIN job_detail_fetches f ON f.id = (
        SELECT latest.id FROM job_detail_fetches latest
        WHERE latest.job_id = j.id
        ORDER BY latest.id DESC
        LIMIT 1
    )
"""


def _jobs_needing_refresh_predicate() -> str:
    """SQL for "the latest detail fetch warrants a refresh".

    Exactly three cases qualify, all evaluated on the latest attempt only:

    1. ``ok``/``partial`` evidence older than the ``?`` cutoff (stale content);
    2. a ``failed`` attempt that never received an HTTP response
       (``http_status IS NULL`` and no stored ``raw_document_id``), which is a
       transient/transport failure worth retrying;
    3. a ``failed`` 404/410 that a later re-sighting refutes
       (``jobs.last_seen_at`` newer than the fetch), which the ingestion pipeline
       would otherwise leave as a permanent death signal.

    Skipped attempts, failed attempts with a recorded HTTP response, and every
    status not listed above are intentionally excluded. The single ``?`` is the
    cutoff timestamp; callers must pass it as the first parameter.
    """
    return (
        "("
        "(f.status IN ('ok', 'partial') "
        "AND COALESCE(f.fetched_at, f.stored_at) < ?) "
        "OR (f.status = 'failed' AND f.http_status IS NULL "
        "AND f.raw_document_id IS NULL) "
        "OR (f.status = 'failed' AND f.http_status IN (404, 410) "
        "AND j.last_seen_at > COALESCE(f.fetched_at, f.stored_at))"
        ")"
    )


def count_jobs_needing_refresh(
    conn: sqlite3.Connection, *, cutoff: str, source: str | None = None
) -> int:
    """Number of active jobs whose latest fetch evidence warrants a refresh.

    ``source`` optionally narrows the count to one provider, so a
    provider-specific refresh command reports its own truncation without
    counting unrelated platforms.
    """
    clauses = ["j.is_active = 1", _jobs_needing_refresh_predicate()]
    params: list[Any] = [cutoff]
    if source:
        clauses.append("j.source = ?")
        params.append(str(source))
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs j "
            + _LATEST_DETAIL_FETCH_JOIN
            + " WHERE "
            + " AND ".join(clauses),
            params,
        ).fetchone()[0]
    )


def jobs_needing_refresh(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
    limit: int | None = None,
    job_ids: Sequence[int] | None = None,
    source: str | None = None,
) -> list[Job]:
    """Active jobs whose latest detail-fetch evidence warrants a refresh.

    Only jobs that already have fetch history are considered; offers never
    fetched are the default ``fetch-details`` selection, not this one. Ordering
    is deterministic and prioritizes the oldest evidence first
    (``COALESCE(fetched_at, stored_at) ASC``), with ``id ASC`` breaking ties, so
    a bounded refresh spends its budget on the stalest offers. ``source``
    optionally restricts the selection to one provider.
    """
    clauses = ["j.is_active = 1", _jobs_needing_refresh_predicate()]
    params: list[Any] = [cutoff]
    if source:
        clauses.append("j.source = ?")
        params.append(str(source))
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        clauses.append(f"j.id IN ({placeholders})")
        params.extend(int(job_id) for job_id in job_ids)
    sql = (
        "SELECT j.* FROM jobs j "
        + _LATEST_DETAIL_FETCH_JOIN
        + " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY COALESCE(f.fetched_at, f.stored_at) ASC, j.id ASC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_row_to_job(row) for row in conn.execute(sql, params).fetchall()]


def variants_needing_refresh(
    conn: sqlite3.Connection,
    *,
    source: str,
    cutoff: str,
    limit: int | None = None,
    job_ids: Sequence[int] | None = None,
) -> list[Job]:
    """Active variants for ``source`` whose latest fetch evidence is stale.

    Reuses the job-level refresh predicate but scopes it to an active variant of
    the requested provider, and returns the variant's own identity so the
    fetcher rebuilds the correct URL.
    """
    clauses = ["j.is_active = 1", "v.is_active = 1", _jobs_needing_refresh_predicate()]
    params: list[Any] = [str(source), cutoff]
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        clauses.append(f"j.id IN ({placeholders})")
        params.extend(int(job_id) for job_id in job_ids)
    sql = (
        "SELECT "
        + _VARIANT_SELECT_COLUMNS
        + " FROM jobs j "
        + _LATEST_DETAIL_FETCH_JOIN
        + " JOIN job_variants v ON v.job_id = j.id AND v.source = ?"
        " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY COALESCE(f.fetched_at, f.stored_at) ASC, j.id ASC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return _jobs_with_variant_identity(conn.execute(sql, params).fetchall())


def count_variants_needing_refresh(
    conn: sqlite3.Connection, *, source: str, cutoff: str
) -> int:
    clauses = ["j.is_active = 1", "v.is_active = 1", _jobs_needing_refresh_predicate()]
    params: list[Any] = [str(source), cutoff]
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs j "
            + _LATEST_DETAIL_FETCH_JOIN
            + " JOIN job_variants v ON v.job_id = j.id AND v.source = ?"
            " WHERE "
            + " AND ".join(clauses),
            params,
        ).fetchone()[0]
    )


def update_job_content(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    description: str | None,
    content_status: ContentStatus | str,
    content_hash: str,
    raw_document_id: int | None = None,
    company: str | None = None,
    location: str | None = None,
    posted_at: str | None = None,
    employment_type: str | None = None,
    salary: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Replace a stored job's description without touching unrelated columns.

    ``raw_document_id`` is only written when provided, so enriching a job keeps
    the provenance of the original search payload intact. ``company``,
    ``location``, ``posted_at``, ``employment_type``, ``salary``, and ``extra``
    are optional detail-derived fields: each is only written when non-empty, so a
    provider fetch can fill a gap in a partial search card without clobbering
    existing values. ``extra`` is merged into the stored ``extra_json`` (never
    replaced wholesale), so unrelated keys survive.
    """
    assignments = [
        "description = ?",
        "content_status = ?",
        "content_hash = ?",
        "raw_document_id = COALESCE(?, raw_document_id)",
    ]
    params: list[Any] = [
        description,
        str(content_status),
        content_hash,
        raw_document_id,
    ]
    for column, value in (
        ("company", company),
        ("location", location),
        ("posted_at", posted_at),
        ("employment_type", employment_type),
        ("salary", salary),
    ):
        if value:
            assignments.append(f"{column} = ?")
            params.append(value)
    if extra:
        merged = dict(extra)
        existing = conn.execute(
            "SELECT extra_json FROM jobs WHERE id = ?", (int(job_id),)
        ).fetchone()
        if existing is not None:
            stored = _loads(existing["extra_json"], {})
            if isinstance(stored, dict):
                merged = {**stored, **extra}
        assignments.append("extra_json = ?")
        params.append(_dumps(merged))
    params.append(int(job_id))
    with conn:
        conn.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
            params,
        )


def deactivate_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Mark one job inactive without deleting it or any of its history.

    A definitive detail failure (HTTP 404/410) means the posting is gone, so the
    row is hidden from the default listings while its evaluations, feedback, and
    fetch provenance stay queryable. Returns ``True`` only when an *active* row
    actually changed, so callers can report a real lifecycle transition instead
    of a repeated no-op. A later re-sighting reactivates the job through
    :func:`store_posting`.
    """
    with conn:
        cursor = conn.execute(
            "UPDATE jobs SET is_active = 0 WHERE id = ? AND is_active = 1",
            (int(job_id),),
        )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Job detail fetches
# ---------------------------------------------------------------------------


def insert_detail_fetch(
    conn: sqlite3.Connection, fetch: JobDetailFetch
) -> JobDetailFetch:
    stored_at = fetch.stored_at or utcnow_iso()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO job_detail_fetches (
                job_id, run_id, source, url, final_url, status, http_status,
                content_status, description_chars, byte_size, sha256, content_hash,
                fetched_at, stored_at, raw_document_id, variant_id, error, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(fetch.job_id),
                fetch.run_id,
                fetch.source,
                fetch.url,
                fetch.final_url,
                str(fetch.status),
                fetch.http_status,
                fetch.content_status,
                int(fetch.description_chars),
                int(fetch.byte_size),
                fetch.sha256,
                fetch.content_hash,
                fetch.fetched_at,
                stored_at,
                fetch.raw_document_id,
                fetch.variant_id,
                fetch.error,
                fetch.notes,
            ),
        )
    fetch.id = int(cursor.lastrowid)
    fetch.stored_at = stored_at
    return fetch


def detail_fetches_for_job(
    conn: sqlite3.Connection, job_id: int
) -> list[JobDetailFetch]:
    rows = conn.execute(
        "SELECT * FROM job_detail_fetches WHERE job_id = ? ORDER BY id DESC",
        (int(job_id),),
    ).fetchall()
    return [_row_to_detail_fetch(row) for row in rows]


def latest_detail_fetch(
    conn: sqlite3.Connection, job_id: int
) -> JobDetailFetch | None:
    row = conn.execute(
        "SELECT * FROM job_detail_fetches WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (int(job_id),),
    ).fetchone()
    return _row_to_detail_fetch(row) if row else None


def latest_successful_detail_fetch_source(
    conn: sqlite3.Connection, job_id: int
) -> str | None:
    """The ``source`` of a job's most recent *successful* detail fetch, or ``None``.

    "Latest" is the highest ``id`` (insertion order), matching
    :func:`latest_detail_fetch`. Only ``status = 'ok'`` rows qualify, so a later
    failed, partial, or skipped attempt never hides the provider that actually
    produced the stored description. A successful row with a NULL ``source`` and
    a job with no successful fetch both return ``None``, so callers can tell
    "provenance unknown" apart from "known to be another provider".
    """
    row = conn.execute(
        "SELECT source FROM job_detail_fetches "
        "WHERE job_id = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
        (int(job_id),),
    ).fetchone()
    if row is None:
        return None
    source = row["source"]
    return str(source) if source else None


def latest_detail_fetches_for_jobs(
    conn: sqlite3.Connection, job_ids: Sequence[int]
) -> dict[int, JobDetailFetch]:
    """Latest detail-fetch attempt per job, in one read-only SELECT.

    The dashboard needs one evidence row per visible offer. Calling
    :func:`latest_detail_fetch` per offer would cost one query per row (N+1);
    this reader instead groups the history in SQL and joins the single highest
    ``id`` per job, the same attempt :func:`latest_detail_fetch` reports.
    Read-only by construction: it runs a single SELECT and never writes.
    Jobs with no fetch rows are simply absent from the mapping, so callers can
    tell "never checked" apart from "checked and failed".
    """
    ids = [int(job_id) for job_id in job_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT f.* FROM job_detail_fetches f
        JOIN (
            SELECT job_id, MAX(id) AS latest_id
            FROM job_detail_fetches
            WHERE job_id IN ({placeholders})
            GROUP BY job_id
        ) latest ON latest.latest_id = f.id
        """,
        ids,
    ).fetchall()
    return {int(row["job_id"]): _row_to_detail_fetch(row) for row in rows}


def job_detail_fetch_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM job_detail_fetches").fetchone()[0])


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def upsert_profile(conn: sqlite3.Connection, snapshot: ProfileSnapshot) -> ProfileSnapshot:
    """Insert a profile snapshot once; identical snapshots are reused."""
    existing = conn.execute(
        "SELECT * FROM profiles WHERE name = ? AND fingerprint = ?",
        (snapshot.name, snapshot.fingerprint),
    ).fetchone()
    if existing is not None:
        stored = _row_to_profile(existing)
        stored.evidence = profile_evidence(conn, int(stored.id or 0))
        return stored

    loaded_at = snapshot.loaded_at or utcnow_iso()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO profiles (name, fingerprint, source_path, loaded_at, raw_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                snapshot.name,
                snapshot.fingerprint,
                snapshot.source_path,
                loaded_at,
                _dumps(snapshot.raw),
            ),
        )
        profile_id = int(cursor.lastrowid)
        for evidence in snapshot.evidence:
            conn.execute(
                """
                INSERT INTO profile_evidence (profile_id, kind, label, value_json, weight, source_text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    evidence.kind,
                    evidence.label,
                    _dumps(evidence.value),
                    float(evidence.weight),
                    evidence.source_text,
                ),
            )
    snapshot.id = profile_id
    snapshot.loaded_at = loaded_at
    for evidence in snapshot.evidence:
        evidence.profile_id = profile_id
    return snapshot


def profile_evidence(conn: sqlite3.Connection, profile_id: int) -> list[ProfileEvidence]:
    rows = conn.execute(
        "SELECT * FROM profile_evidence WHERE profile_id = ? ORDER BY id", (profile_id,)
    ).fetchall()
    return [
        ProfileEvidence(
            id=row["id"],
            profile_id=row["profile_id"],
            kind=row["kind"],
            label=row["label"],
            value=_loads(row["value_json"], None),
            weight=row["weight"],
            source_text=row["source_text"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


class EvaluationContractError(ValueError):
    """Raised when an evaluation violates the canonical successful-evaluation invariant.

    ``status=ok`` means a decided, actionable result, so it must carry a numeric
    ``overall_score`` and an ``apply``/``consider``/``skip`` recommendation. A
    caller that produced an unresolved or scoreless success must record it as
    ``needs_more_info`` instead: this guard makes the misleading state impossible
    to persist rather than silently storing it.
    """


def insert_evaluation(conn: sqlite3.Connection, evaluation: Evaluation) -> Evaluation:
    errors = evaluation_contract_errors(evaluation)
    if errors:
        raise EvaluationContractError(
            "refusing to persist an invalid successful evaluation: " + "; ".join(errors)
        )
    created_at = evaluation.created_at or utcnow_iso()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO evaluations (
                job_id, scorer, model, prompt_version, profile_id, overall_score,
                recommendation, confidence, dimensions_json, blockers_json,
                strengths_json, gaps_json, unknowns_json, salary_estimate_low,
                salary_estimate_mid, salary_estimate_high, employer_tier,
                salary_basis, rationale, status, error, raw_document_id,
                input_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation.job_id,
                str(evaluation.scorer),
                evaluation.model,
                evaluation.prompt_version,
                evaluation.profile_id,
                evaluation.overall_score,
                str(evaluation.recommendation),
                float(evaluation.confidence),
                _dumps([dimension.to_dict() for dimension in evaluation.dimensions]),
                _dumps(list(evaluation.blockers)),
                _dumps(list(evaluation.strengths)),
                _dumps(list(evaluation.gaps)),
                _dumps(list(evaluation.unknowns)),
                evaluation.salary_estimate_low,
                evaluation.salary_estimate_mid,
                evaluation.salary_estimate_high,
                evaluation.employer_tier,
                evaluation.salary_basis,
                evaluation.rationale,
                str(evaluation.status),
                evaluation.error,
                evaluation.raw_document_id,
                evaluation.input_fingerprint,
                created_at,
            ),
        )
    evaluation.id = int(cursor.lastrowid)
    evaluation.created_at = created_at
    return evaluation


def evaluations_for_job(conn: sqlite3.Connection, job_id: int) -> list[Evaluation]:
    rows = conn.execute(
        "SELECT * FROM evaluations WHERE job_id = ? ORDER BY id DESC", (job_id,)
    ).fetchall()
    return [_row_to_evaluation(row) for row in rows]


def has_successful_evaluation_with_fingerprint(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    scorer: ScorerName | str,
    fingerprint: str,
) -> bool:
    """Whether a job already has an ``ok`` evaluation with this exact input."""
    if not fingerprint:
        return False
    row = conn.execute(
        "SELECT 1 FROM evaluations WHERE job_id = ? AND scorer = ? "
        "AND status = 'ok' AND input_fingerprint = ? LIMIT 1",
        (int(job_id), str(scorer), str(fingerprint)),
    ).fetchone()
    return row is not None


def latest_evaluation(
    conn: sqlite3.Connection, job_id: int, *, scorer: ScorerName | str | None = None
) -> Evaluation | None:
    if scorer is None:
        row = conn.execute(
            "SELECT * FROM evaluations WHERE job_id = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM evaluations
            WHERE job_id = ? AND scorer = ? AND status = 'ok'
            ORDER BY id DESC LIMIT 1
            """,
            (job_id, str(scorer)),
        ).fetchone()
    return _row_to_evaluation(row) if row else None


def evaluation_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0])


# ---------------------------------------------------------------------------
# Human feedback
# ---------------------------------------------------------------------------

#: Outcome of one feedback upsert. ``stale`` means the stored row was kept
#: because the incoming entry was not strictly newer (older timestamp, an equal
#: timestamp with different text, or no timestamp against a timestamped row).
FEEDBACK_INSERTED = "inserted"
FEEDBACK_UPDATED = "updated"
FEEDBACK_UNCHANGED = "unchanged"
FEEDBACK_STALE = "stale"


def get_feedback(conn: sqlite3.Connection, job_id: int) -> JobFeedback | None:
    """Return the stored human decision for a job, or ``None``."""
    row = conn.execute(
        "SELECT * FROM job_feedback WHERE job_id = ?", (int(job_id),)
    ).fetchone()
    return _row_to_feedback(row) if row else None


def list_feedback(conn: sqlite3.Connection, *, limit: int | None = None) -> list[JobFeedback]:
    """Every stored human decision, newest decision first."""
    sql = (
        "SELECT * FROM job_feedback "
        "ORDER BY (decision_updated_at IS NULL), decision_updated_at DESC, job_id"
    )
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [_row_to_feedback(row) for row in conn.execute(sql, params).fetchall()]


def feedback_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0])


def upsert_feedback(
    conn: sqlite3.Connection, feedback: JobFeedback
) -> tuple[JobFeedback, str]:
    """Store one human decision, returning ``(stored_row, outcome)``.

    The write only happens when the incoming entry is strictly newer than the
    stored one, which is what makes a re-import idempotent and keeps an older
    export from overwriting a newer decision. Timestamps are compared as the
    normalized UTC ISO strings the storage layer already writes, so the
    comparison is a plain lexicographic one. A stored row without a timestamp is
    always replaceable; an incoming entry without one never is.
    """
    job_id = int(feedback.job_id)
    existing = get_feedback(conn, job_id)
    if existing is None:
        imported_at = feedback.imported_at or utcnow_iso()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO job_feedback (
                    job_id, decision, reason, decision_updated_at, project_id,
                    source, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(feedback.decision),
                    feedback.reason,
                    feedback.decision_updated_at,
                    feedback.project_id,
                    feedback.source,
                    imported_at,
                ),
            )
        feedback.id = int(cursor.lastrowid)
        feedback.imported_at = imported_at
        return feedback, FEEDBACK_INSERTED

    incoming_at = feedback.decision_updated_at
    stored_at = existing.decision_updated_at
    if incoming_at is None or (stored_at is not None and incoming_at <= stored_at):
        same = (
            str(feedback.decision) == str(existing.decision)
            and (feedback.reason or None) == (existing.reason or None)
        )
        return existing, FEEDBACK_UNCHANGED if same else FEEDBACK_STALE

    with conn:
        conn.execute(
            """
            UPDATE job_feedback
            SET decision = ?, reason = ?, decision_updated_at = ?, project_id = ?,
                source = ?, imported_at = ?
            WHERE job_id = ?
            """,
            (
                str(feedback.decision),
                feedback.reason,
                incoming_at,
                feedback.project_id,
                feedback.source,
                feedback.imported_at or utcnow_iso(),
                job_id,
            ),
        )
    updated = get_feedback(conn, job_id)
    return (updated or existing), FEEDBACK_UPDATED
