"""Self-contained local HTML dashboard for scored jobs and keyword performance.

The report is a single HTML file with embedded CSS/JS and no external asset,
CDN, server, or network dependency, so it opens straight from ``file://``.

Two boundaries are enforced here by construction:

* **Read-only database access.** The database file is opened through
  :func:`open_readonly_database`, which refuses to create a missing file and
  opens it through SQLite's read-only URI (``file:...?mode=ro``), with
  ``PRAGMA query_only`` as a second line of defence. Generating a report can
  never insert, update, or delete a row. Only the *output* parent directory is
  created, and an output path that resolves to the database itself is refused.
* **Data minimization.** Only job identity, score provenance, and keyword
  provenance are serialized. Job descriptions, raw payloads, profile data,
  API keys, and absolute local paths are never placed in the HTML. The
  capability that keys browser-local feedback is a hash of the database path,
  not the path itself.

The generated page styles and filters data at read time; it never submits an
application and never contacts LinkedIn.

One display boundary is deliberate: every string a reader sees is
neutral/professional Spanish, while schema keys, JSON field names, JavaScript
identifiers, stored recommendation values, and CLI command names stay English.
Recommendation values (``apply``/``consider``/``skip``/``need_more_info``) and
human feedback values stay English in the payload. Human feedback is now binary
(``apply``/``reject``); a legacy ``consider`` value is still displayed as a
historical decision and can be replaced, but it is never offered as a new
choice. Every value is translated for display only, so the payload stays
round-trippable against the database.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from .db import (
    connect_readonly,
    count_variants_needing_details,
    jobs_awaiting_details,
    latest_detail_fetches_for_jobs,
)
from .dossier import company_slug, load_dossiers
from .keywords import retired_search
from .models import (
    ACTIVE_FEEDBACK_DECISIONS,
    LEGACY_FEEDBACK_DECISIONS,
    DetailFetchStatus,
    JobDetailFetch,
    _loads,
)
from .preference import PreferenceOutcome, assess_text
from .seniority import seniority_filter_reason
from .timeutil import parse_datetime, utcnow, utcnow_iso

#: Default report location. ``/reports/`` is git-ignored, so a generated report
#: is never published by accident.
DEFAULT_OUTPUT = Path("reports/jobscout-dashboard.html")

#: Default number of ranked offers embedded in the report. ``None`` means every
#: active offer: the report is a local, read-only inventory, so the default must
#: not silently hide active offers behind an implicit cap. Deactivated offers
#: stay in SQLite but are excluded by the ``is_active = 1`` query.
DEFAULT_TOP: int | None = None

#: Hard cap on an explicit ``--top``. The whole payload is embedded in one HTML
#: file, so an unbounded *explicit* value would let a typo build a report large
#: enough to freeze a browser. It deliberately does not apply to the default,
#: which includes every active offer. Raising it is a deliberate, reviewable
#: change.
MAX_TOP = 1000

#: Payload schema written into the HTML and used to shape the localStorage key.
DASHBOARD_SCHEMA = "jobscout.dashboard.v1"
FEEDBACK_SCHEMA = "jobscout.feedback.v1"

#: Human feedback decisions the report's active UI offers, and the historical
#: values it only displays. The JavaScript literals below mirror these; a test
#: pins both so a legacy ``consider`` cannot silently return as a new choice.
DASHBOARD_FEEDBACK_DECISIONS: tuple[str, ...] = ACTIVE_FEEDBACK_DECISIONS
DASHBOARD_LEGACY_FEEDBACK_DECISIONS: tuple[str, ...] = LEGACY_FEEDBACK_DECISIONS

#: Title, company, and location longer than this are truncated in the report so
#: one verbose value cannot dominate the row. The stored rationale is shown in
#: full; only its whitespace is collapsed.
MAX_FIELD_CHARS = 800

#: A posting counts as *fresh* in keyword analytics when it is at most this many
#: days old. jobscout has no verified "expired on LinkedIn" signal, so freshness
#: is the honest, schema-supported proxy for how apply-able a keyword's results
#: still are.
FRESH_WINDOW_DAYS = 21

#: A job counts as "high score" when its score reaches this value; it matches the
#: JS ``s-hi`` band (``score >= 70``) so the table and the offer chips agree.
HIGH_SCORE_THRESHOLD = 70

#: A detail-fetch evidence row counts as *current* when it is at most this many
#: days old. It matches :data:`FRESH_WINDOW_DAYS` but stays a separate, named
#: boundary because it answers a different question: whether the last link check
#: is still recent, not whether the posting itself is young.
DETAIL_EVIDENCE_WINDOW_DAYS = 21

#: Honest link-evidence states. Each one is a fact about the single latest
#: detail-fetch attempt that is actually stored, never a guess:
#:
#: ``unchecked``          no attempt has ever been recorded;
#: ``skipped``            the latest attempt was deliberately not performed;
#: ``ok``                 the latest success (ok/partial) is within the window;
#: ``stale``              the latest success is older than the window;
#: ``definitive_missing`` the latest attempt returned HTTP 404/410;
#: ``retryable_failure``  the latest failure never received an HTTP response;
#: ``failed``             the latest failure recorded any other HTTP response.
DETAIL_STATE_UNCHECKED = "unchecked"
DETAIL_STATE_SKIPPED = "skipped"
DETAIL_STATE_OK = "ok"
DETAIL_STATE_STALE = "stale"
DETAIL_STATE_DEFINITIVE_MISSING = "definitive_missing"
DETAIL_STATE_RETRYABLE_FAILURE = "retryable_failure"
DETAIL_STATE_FAILED = "failed"

DETAIL_STATES: tuple[str, ...] = (
    DETAIL_STATE_UNCHECKED,
    DETAIL_STATE_SKIPPED,
    DETAIL_STATE_OK,
    DETAIL_STATE_STALE,
    DETAIL_STATE_DEFINITIVE_MISSING,
    DETAIL_STATE_RETRYABLE_FAILURE,
    DETAIL_STATE_FAILED,
)

#: HTTP statuses that prove a posting is gone. Mirrors
#: ``collectors.linkedin_details.DEFINITIVE_MISSING_STATUSES``; the dashboard
#: deliberately does not import the network collector, so a contract test pins
#: the two constants together and a drift fails loudly.
DEFINITIVE_MISSING_HTTP_STATUSES = frozenset({404, 410})

#: Recommendation values shown in the UI. ``unscored`` is not a stored value; it
#: is the display bucket for jobs with no successful evaluation.
RECOMMENDATION_ORDER: tuple[str, ...] = ("apply", "consider", "skip", "need_more_info")

#: Preference verdicts, in display order. A separate axis from the model
#: recommendation: it never changes the score and is recomputed from stored text.
PREFERENCE_ORDER: tuple[str, ...] = ("pass", "flag", "fail")

_ALLOWED_URL_SCHEMES = ("http://", "https://")


class DashboardError(RuntimeError):
    """Raised when a dashboard cannot be generated (missing/unreadable DB)."""


@dataclass
class DashboardSummary:
    """Accounting for one generated report, returned to the CLI."""

    output: Path
    offers: int = 0
    scored: int = 0
    keywords: int = 0
    bytes_written: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output),
            "offers": self.offers,
            "scored": self.scored,
            "keywords": self.keywords,
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Read-only database access
# ---------------------------------------------------------------------------


def open_readonly_database(db_path: str | Path) -> sqlite3.Connection:
    """Open an existing database for reading, or raise :class:`DashboardError`.

    Delegates to :func:`jobscout.db.connect_readonly`, so the dashboard shares
    the exact read-only guarantee used by the CLI dry runs: no parent directory
    or database file is created and no write can slip through. A missing file
    and an unreadable file both become a clear, actionable error.
    """
    path = Path(db_path)
    if str(path) == ":memory:":
        raise DashboardError(
            "the dashboard needs an on-disk SQLite database; ':memory:' has no report to read"
        )
    try:
        conn = connect_readonly(path)
    except sqlite3.Error as exc:  # pragma: no cover - os/filesystem dependent
        raise DashboardError(f"database at {path} could not be opened: {exc}") from exc
    if conn is None:
        raise DashboardError(
            f"no database found at {path}; run 'jobscout init-db', "
            "ingest a fixture or search, then score a job before generating the dashboard"
        )
    return conn


def _require_schema(conn: sqlite3.Connection, db_path: str | Path) -> None:
    """Fail with a useful message when the file has no jobscout schema."""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise DashboardError(
            f"database at {db_path} could not be read (is it a valid SQLite file?): {exc}"
        ) from exc
    names = {str(row["name"]) for row in rows}
    missing = {
        "jobs",
        "search_runs",
        "search_run_items",
        "latest_evaluations",
        "job_feedback",
    } - names
    if missing:
        raise DashboardError(
            f"database at {db_path} is not a jobscout database "
            f"(missing: {', '.join(sorted(missing))}); run 'jobscout init-db' first"
        )


def project_key(db_path: str | Path) -> str:
    """Stable, non-reversible identifier for the browser-local feedback store.

    Hashing keeps the absolute local path out of the report while still scoping
    feedback to one database, so two projects never share feedback entries.
    """
    resolved = str(Path(db_path).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def _validate_top(top: int | None) -> int | None:
    """Return ``top`` as a bounded int, or ``None`` for "every active offer".

    An explicit non-positive value cannot be expressed as a SQL ``LIMIT`` and a
    huge one would produce an unusable report, so both are refused before any
    file is touched. ``None`` is the all-offers default and stays unbounded.
    """
    if top is None:
        return None
    try:
        value = int(top)
    except (TypeError, ValueError) as exc:
        raise DashboardError(f"--top must be an integer, got {top!r}") from exc
    if value < 1:
        raise DashboardError(f"--top must be >= 1, got {value}")
    if value > MAX_TOP:
        raise DashboardError(f"--top must be <= {MAX_TOP}, got {value}")
    return value


def _canonical_path(path: str | Path) -> str | None:
    """Absolute, case-normalized path used to compare output against the DB.

    ``realpath`` normalizes ``..`` segments and symlinks (even when the final
    file does not exist yet) and ``normcase`` folds Windows drive/path case, so
    ``data\\Jobscout.sqlite3`` and ``DATA/jobscout.SQLITE3`` compare equal.
    """
    try:
        return os.path.normcase(os.path.realpath(os.fspath(path)))
    except (OSError, ValueError):  # pragma: no cover - malformed path
        return None


def _database_sidecars(db_path: str | Path) -> tuple[Path, ...]:
    """The database file plus the journal/WAL files SQLite may keep beside it."""
    db = Path(db_path)
    return (db, Path(f"{db}-wal"), Path(f"{db}-shm"), Path(f"{db}-journal"))


def _reject_output_over_database(db_path: str | Path, output: str | Path) -> None:
    """Refuse to write the report over the database or one of its journals."""
    target = _canonical_path(output)
    if target is None:
        return
    for candidate in _database_sidecars(db_path):
        resolved = _canonical_path(candidate)
        if resolved is not None and resolved == target:
            raise DashboardError(
                f"the output path resolves to the SQLite database ({candidate}); "
                "refusing to overwrite the database with the report"
            )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _string_list(value: Any) -> list[str]:
    loaded = _loads(value, [])
    if not isinstance(loaded, list):
        return []
    return [text for item in loaded if (text := str(item).strip())]


def _salary(value: Any) -> str | None:
    """The stated salary with collapsed whitespace, or ``None`` when empty."""
    if value is None:
        return None
    collapsed = " ".join(str(value).split())
    return collapsed or None


def _salary_est(row: sqlite3.Row) -> list[Any] | None:
    """The compact ``[low, mid, high, tier, basis]`` estimate, or ``None``.

    The triple is stored complete or entirely ``NULL``; the check keeps the
    client payload in its existing compact style and never invents a range.
    """
    low = row["salary_estimate_low"]
    mid = row["salary_estimate_mid"]
    high = row["salary_estimate_high"]
    if low is None or mid is None or high is None:
        return None
    tier = row["employer_tier"]
    basis = row["salary_basis"]
    return [
        int(low),
        int(mid),
        int(high),
        str(tier) if tier else None,
        str(basis) if basis else None,
    ]


def _dimensions(value: Any) -> list[dict[str, Any]]:
    """Parse ``dimensions_json`` without inventing a score for a broken entry.

    ``DimensionScore.from_dict`` defaults a missing score to ``0.0``, which would
    turn malformed stored data into a fake dimension. Here every element that is
    not a usable dict is dropped, so absent evidence stays absent.
    """
    loaded = _loads(value, [])
    if not isinstance(loaded, list):
        return []
    dimensions: list[dict[str, Any]] = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        weight = item.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            weight = 1.0
        dimensions.append(
            {
                "name": name.strip(),
                "score": float(score),
                "weight": float(weight),
                "rationale": str(item.get("rationale") or ""),
                "evidence": _string_list(item.get("evidence")),
            }
        )
    return dimensions


def _age_days_exact(value: str | None, *, now: datetime) -> float | None:
    """Unrounded age in days, for threshold comparisons that must stay exact."""
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 86400.0


def _age_days(value: str | None, *, now: datetime) -> float | None:
    """Display age in days, rounded; ``None`` when the timestamp is unusable."""
    exact = _age_days_exact(value, now=now)
    return None if exact is None else round(exact, 2)


def _collapse_whitespace(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(str(text).split())


def _concisify(text: str | None) -> str:
    collapsed = _collapse_whitespace(text)
    if len(collapsed) <= MAX_FIELD_CHARS:
        return collapsed
    return collapsed[: MAX_FIELD_CHARS - 1].rstrip() + "\u2026"


def _fetch_active_jobs(
    conn: sqlite3.Connection, *, top: int | None
) -> list[sqlite3.Row]:
    """Active jobs ranked by latest successful score, then freshness.

    The ``latest_evaluations`` view is the same source of truth ``rank`` and
    ``list`` use, so the dashboard never disagrees with the CLI. ``top`` is
    ``None`` for the all-active-offers default; SQLite treats a negative
    ``LIMIT`` as "no upper bound", so the same statement serves both cases.
    """
    limit = -1 if top is None else int(top)
    return conn.execute(
        """
        SELECT j.id AS id,
               j.title AS title,
               j.company AS company,
               j.location AS location,
               j.url AS url,
               j.description AS description,
               j.salary AS salary,
               j.posted_at AS posted_at,
               j.first_seen_at AS first_seen_at,
               j.last_seen_at AS last_seen_at,
               j.seniority AS seniority,
               j.employment_type AS employment_type,
               e.overall_score AS score,
               e.recommendation AS recommendation,
               e.confidence AS confidence,
               e.dimensions_json AS dimensions_json,
               e.strengths_json AS strengths_json,
               e.gaps_json AS gaps_json,
               e.blockers_json AS blockers_json,
               e.unknowns_json AS unknowns_json,
               e.salary_estimate_low AS salary_estimate_low,
               e.salary_estimate_mid AS salary_estimate_mid,
               e.salary_estimate_high AS salary_estimate_high,
               e.employer_tier AS employer_tier,
               e.salary_basis AS salary_basis,
               e.rationale AS rationale,
               e.model AS model,
               e.created_at AS evaluated_at,
               latest_e.status AS evaluation_status
        FROM jobs j
        LEFT JOIN latest_evaluations e ON e.job_id = j.id
        LEFT JOIN evaluations latest_e ON latest_e.id = (
            SELECT x.id FROM evaluations x
            WHERE x.job_id = j.id
            ORDER BY x.id DESC
            LIMIT 1
        )
        WHERE j.is_active = 1
        ORDER BY (e.overall_score IS NULL),
                 e.overall_score DESC,
                 COALESCE(e.confidence, 0) DESC,
                 j.last_seen_at DESC,
                 j.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _last_sweep(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """The newest non-smoke cycle and the start of its first run.

    The sweep writes a dated ``YYYY-MM-DD-sweep`` cycle id, so the cycle is used
    only to group runs and no cycle is compared across platforms. Synthetic
    ``smoke*`` cycles are excluded exactly as in :func:`_fetch_keyword_stats`.
    """
    rows = conn.execute(
        """
        SELECT cycle_id, started_at
        FROM search_runs
        WHERE cycle_id IS NOT NULL
          AND TRIM(cycle_id) <> ''
          AND LOWER(TRIM(cycle_id)) NOT LIKE 'smoke%'
        """
    ).fetchall()
    runs_by_cycle: dict[str, list[tuple[datetime, str]]] = {}
    for row in rows:
        started = parse_datetime(row["started_at"])
        if started is None:
            continue
        cycle = str(row["cycle_id"]).strip()
        runs_by_cycle.setdefault(cycle, []).append((started, str(row["started_at"])))
    if not runs_by_cycle:
        return None, None
    latest_cycle = max(
        runs_by_cycle, key=lambda cycle: max(started for started, _ in runs_by_cycle[cycle])
    )
    first_started = min(runs_by_cycle[latest_cycle], key=lambda item: item[0])[1]
    return latest_cycle, first_started


def _is_new(first_seen_at: Any, last_sweep_at: str | None) -> bool:
    """Whether an offer appeared at or after the latest sweep start.

    Both values are parsed, never compared as strings; an unusable timestamp is
    not evidence of a new offer, so it returns ``False``.
    """
    if last_sweep_at is None:
        return False
    first_seen = parse_datetime(first_seen_at)
    sweep_started = parse_datetime(last_sweep_at)
    if first_seen is None or sweep_started is None:
        return False
    return first_seen >= sweep_started


def _build_status(
    conn: sqlite3.Connection, *, last_sweep_cycle: str | None, last_sweep_at: str | None
) -> dict[str, Any]:
    """Header status-line counts derived from stored rows only.

    New offers are counted over every active job, not only the ``top`` slice, so
    the header never reports a total bounded by the embedded offer list.
    """
    rows = conn.execute(
        """
        SELECT j.first_seen_at AS first_seen_at,
               e.overall_score AS score
        FROM jobs j
        LEFT JOIN latest_evaluations e ON e.job_id = j.id
        WHERE j.is_active = 1
        """
    ).fetchall()
    new_jobs = 0
    new_scored = 0
    for row in rows:
        if not _is_new(row["first_seen_at"], last_sweep_at):
            continue
        new_jobs += 1
        if row["score"] is not None:
            new_scored += 1

    sources = conn.execute(
        """
        SELECT DISTINCT v.source AS source
        FROM job_variants v
        JOIN jobs j ON j.id = v.job_id
        WHERE j.is_active = 1 AND v.is_active = 1
        ORDER BY v.source
        """
    ).fetchall()
    pending_details = {
        str(row["source"]): count_variants_needing_details(conn, source=str(row["source"]))
        for row in sources
    }

    failed_searches = 0
    failed_details = 0
    sweep_started = parse_datetime(last_sweep_at)
    if last_sweep_at is not None and sweep_started is not None:
        failed_rows = conn.execute(
            """
            SELECT provider, started_at
            FROM search_runs
            WHERE status IN ('failed', 'failed_closed')
              AND LOWER(TRIM(COALESCE(cycle_id, ''))) NOT LIKE 'smoke%'
            """
        ).fetchall()
        for row in failed_rows:
            started = parse_datetime(row["started_at"])
            if started is None or started < sweep_started:
                continue
            if str(row["provider"] or "").endswith("-details"):
                failed_details += 1
            else:
                failed_searches += 1

    return {
        "last_sweep_cycle": last_sweep_cycle,
        "last_sweep_at": last_sweep_at,
        "new_jobs": new_jobs,
        "new_scored": new_scored,
        "pending_details": pending_details,
        "failed_runs": {"searches": failed_searches, "details": failed_details},
    }


def _source_key(provider: Any, keyword: Any) -> str:
    """Composite identity of a keyword analytics series.

    Two platforms that used the same literal keyword are different series, but a
    search cycle is provenance rather than identity: the same provider/keyword
    observed in several cycles is one series, so the key is ``provider|keyword``
    (empty parts included).
    """
    return f"{provider or ''}|{keyword or ''}"


def _keyword_source_label(keyword: str, provider: str) -> str:
    """Display label that keeps the provider visible beside the keyword."""
    suffix = provider or ""
    return f"{keyword} \u00b7 {suffix}" if suffix else keyword


def _fetch_job_keyword_keys(
    conn: sqlite3.Connection, job_ids: Sequence[int]
) -> dict[int, list[str]]:
    """The distinct ``provider|keyword`` series keys that surfaced each job.

    Only the keys ship: the keyword filter is their one reader, and one entry
    per sighting was most of the page weight. The full provenance (rank, cycle,
    position kind) stays in SQLite. Keys keep the order of first sighting.
    """
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        f"""
        SELECT i.job_id AS job_id,
               r.query AS keyword,
               r.provider AS provider
        FROM search_run_items i
        JOIN search_runs r ON r.id = i.run_id
        WHERE i.job_id IN ({placeholders})
          AND r.query IS NOT NULL
          AND TRIM(r.query) <> ''
        ORDER BY i.job_id, i.id
        """,
        [int(job_id) for job_id in job_ids],
    ).fetchall()
    grouped: dict[int, dict[str, None]] = {}
    for row in rows:
        key = _source_key(str(row["provider"] or ""), str(row["keyword"]))
        grouped.setdefault(int(row["job_id"]), {})[key] = None
    return {job_id: list(keys) for job_id, keys in grouped.items()}


def _fetch_imported_feedback(
    conn: sqlite3.Connection, job_ids: Sequence[int]
) -> dict[int, str]:
    """Batched read-only read of the imported human decision per shown job id.

    Returns only the ids that actually have a ``job_feedback`` row, so an offer
    with no imported decision is *absent* from the mapping rather than defaulted
    to a value. Only the stored ``decision`` is read: the free-text reason stays
    in SQLite and is never serialized into the report.

    ``search_runs.cycle_id`` is not consulted here: the imported decision is
    keyed only by job, so the absence of a ``job_feedback`` row is the only
    isolation predicate. The sweep cycle is used elsewhere to date the latest
    sweep (see :func:`_last_sweep`); cycle ids from different platforms are not
    compared.
    """
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        f"SELECT job_id, decision FROM job_feedback WHERE job_id IN ({placeholders})",
        [int(job_id) for job_id in job_ids],
    ).fetchall()
    decisions: dict[int, str] = {}
    for row in rows:
        decision = row["decision"]
        if decision:
            decisions[int(row["job_id"])] = str(decision)
    return decisions


def _imported_review(decisions: dict[int, str], job_id: int) -> dict[str, Any]:
    """Serializable imported-decision state for one offer (never the reason).

    ``reviewed`` is true only when a ``job_feedback`` row exists, so "not
    imported" is never confused with a stored value. ``decision`` carries the
    stored value (including a legacy ``consider``) and stays ``None`` when no row
    exists; the reason is intentionally omitted.
    """
    decision = decisions.get(job_id)
    return {"reviewed": bool(decision), "decision": decision or None}


def _rate(numerator: int, denominator: int) -> float | None:
    """A 0..1 ratio rounded for display, or ``None`` when it is undefined.

    A zero denominator has no honest value, so it returns ``None`` instead of an
    invented ``0.0``. Callers render that as "sin dato".
    """
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _mean(values: Sequence[float]) -> float | None:
    """Rounded mean of ``values``, or ``None`` when there is nothing to average."""
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _fetch_keyword_stats(
    conn: sqlite3.Connection, *, now: datetime
) -> list[dict[str, Any]]:
    """Per-keyword provenance, quality, human verdict, and freshness metrics.

    Only active jobs feed sightings, quality, and freshness, matching the offers
    list and the summary: a keyword that only ever surfaced deactivated postings
    must not look productive. ``inactive_jobs``/``inactive_rate`` look at the
    deactivated postings separately, because that share is exactly what makes a
    keyword's active numbers look better than they are.

    ``seen_jobs`` counts every distinct posting the search ever surfaced, inactive
    ones included, and ``high_score_jobs`` counts the scored ones at or above
    :data:`HIGH_SCORE_THRESHOLD`.

    Every rate is computed from stored rows only. A metric with no data behind it
    (no scored job, no imported human decision, no usable date) stays ``None``
    instead of being guessed as zero.

    ``jobscout`` has no verified "posting expired on LinkedIn" signal, so
    freshness is measured from the stored dates it actually has: a posting counts
    as fresh when it is at most :data:`FRESH_WINDOW_DAYS` days old, using
    ``posted_at`` and falling back to ``first_seen_at``. ``unknown_age_jobs``
    counts jobs where neither date is usable, so a keyword is never credited with
    freshness it cannot prove.

    All three queries ignore synthetic smoke cycles: a run whose trimmed,
    case-folded ``cycle_id`` starts with ``smoke`` is test data, not a search
    result, and must not shape the metrics. Runs with a null or regular cycle ID
    stay in. The exclusion is applied per query, not by deleting rows.
    """
    sighting_rows = conn.execute(
        """
        SELECT r.provider AS provider,
               r.query AS keyword,
               COUNT(*) AS sightings,
               COUNT(DISTINCT i.job_id) AS unique_jobs,
               SUM(CASE WHEN i.decision <> 'new' THEN 1 ELSE 0 END) AS duplicate_sightings
        FROM search_run_items i
        JOIN search_runs r ON r.id = i.run_id
        JOIN jobs j ON j.id = i.job_id
        WHERE r.query IS NOT NULL AND TRIM(r.query) <> ''
          AND LOWER(TRIM(COALESCE(r.cycle_id, ''))) NOT LIKE 'smoke%'
          AND j.is_active = 1
        GROUP BY r.provider, r.query
        ORDER BY r.provider, r.query
        """
    ).fetchall()

    inactive_rows = conn.execute(
        """
        SELECT r.provider AS provider,
               r.query AS keyword,
               COUNT(DISTINCT i.job_id) AS all_jobs,
               COUNT(DISTINCT CASE WHEN j.is_active = 0 THEN i.job_id END) AS inactive_jobs
        FROM search_run_items i
        JOIN search_runs r ON r.id = i.run_id
        JOIN jobs j ON j.id = i.job_id
        WHERE r.query IS NOT NULL AND TRIM(r.query) <> ''
          AND LOWER(TRIM(COALESCE(r.cycle_id, ''))) NOT LIKE 'smoke%'
        GROUP BY r.provider, r.query
        """
    ).fetchall()
    inactive = {
        _source_key(row["provider"], row["keyword"]): row for row in inactive_rows
    }

    observation_rows = conn.execute(
        """
        WITH keyword_jobs AS (
            SELECT DISTINCT r.provider AS provider,
                            r.query AS keyword,
                            i.job_id AS job_id
            FROM search_run_items i
            JOIN search_runs r ON r.id = i.run_id
            JOIN jobs j ON j.id = i.job_id
            WHERE r.query IS NOT NULL AND TRIM(r.query) <> ''
              AND LOWER(TRIM(COALESCE(r.cycle_id, ''))) NOT LIKE 'smoke%'
              AND j.is_active = 1
        )
        SELECT kj.provider AS provider,
               kj.keyword AS keyword,
               j.posted_at AS posted_at,
               j.first_seen_at AS first_seen_at,
               e.overall_score AS score,
               e.recommendation AS recommendation,
               f.decision AS human_decision
        FROM keyword_jobs kj
        JOIN jobs j ON j.id = kj.job_id
        LEFT JOIN latest_evaluations e ON e.job_id = kj.job_id
        LEFT JOIN job_feedback f ON f.job_id = kj.job_id
        ORDER BY kj.provider, kj.keyword, kj.job_id
        """
    ).fetchall()

    observations: dict[str, list[sqlite3.Row]] = {}
    for row in observation_rows:
        observations.setdefault(
            _source_key(row["provider"], row["keyword"]), []
        ).append(row)

    stats: list[dict[str, Any]] = []
    for row in sighting_rows:
        provider = str(row["provider"] or "")
        keyword = str(row["keyword"])
        source_key = _source_key(provider, keyword)
        rows_for_keyword = observations.get(source_key, [])

        scores = sorted(
            (float(item["score"]) for item in rows_for_keyword if item["score"] is not None),
            reverse=True,
        )
        recommendations = [
            str(item["recommendation"])
            for item in rows_for_keyword
            if item["score"] is not None and item["recommendation"]
        ]
        decisions = [
            str(item["human_decision"])
            for item in rows_for_keyword
            if item["human_decision"]
        ]

        ages: list[float] = []
        unknown_age_jobs = 0
        for item in rows_for_keyword:
            age = _age_days(item["posted_at"], now=now)
            if age is None:
                age = _age_days(item["first_seen_at"], now=now)
            if age is None:
                unknown_age_jobs += 1
            else:
                ages.append(age)
        fresh_jobs = sum(1 for age in ages if age <= FRESH_WINDOW_DAYS)

        unique_jobs = int(row["unique_jobs"] or 0)
        scored_jobs = len(scores)
        count_row = inactive.get(source_key)
        all_jobs = int(count_row["all_jobs"] or 0) if count_row is not None else unique_jobs
        inactive_jobs = int(count_row["inactive_jobs"] or 0) if count_row is not None else 0
        human_reviewed = len(decisions)
        human_apply = decisions.count("apply")
        human_consider = decisions.count("consider")
        human_reject = decisions.count("reject")
        duplicate_sightings = int(row["duplicate_sightings"] or 0)
        sightings = int(row["sightings"] or 0)
        # A retired pair is still stored history, but it is no longer an active
        # search. The flag lets the payload and the renderer keep active and
        # retired series apart without deleting the analytics row.
        retirement = retired_search(provider, keyword)

        stats.append(
            {
                "keyword": keyword,
                "provider": provider,
                "source_key": source_key,
                "label": _keyword_source_label(keyword, provider),
                "retired": retirement is not None,
                "sightings": sightings,
                "seen_jobs": all_jobs,
                "unique_jobs": unique_jobs,
                "duplicate_sightings": duplicate_sightings,
                "duplicate_rate": _rate(duplicate_sightings, sightings),
                "inactive_jobs": inactive_jobs,
                "inactive_rate": _rate(inactive_jobs, all_jobs),
                "scored_jobs": scored_jobs,
                "unscored_jobs": max(0, unique_jobs - scored_jobs),
                "avg_score": _mean(scores),
                "high_score_jobs": sum(
                    1 for value in scores if value >= HIGH_SCORE_THRESHOLD
                ),
                "top5_avg_score": _mean(scores[:5]),
                "top10_avg_score": _mean(scores[:10]),
                "apply_count": recommendations.count("apply"),
                "consider_count": recommendations.count("consider"),
                "human_reviewed": human_reviewed,
                "human_apply": human_apply,
                "human_consider": human_consider,
                "human_reject": human_reject,
                "human_apply_rate": _rate(human_apply, human_reviewed),
                "human_positive_yield": _rate(
                    human_apply + human_consider, human_reviewed
                ),
                "fresh_jobs": fresh_jobs,
                "unknown_age_jobs": unknown_age_jobs,
                "fresh_share": _rate(fresh_jobs, len(ages)),
            }
        )
    return stats


def _summary(
    conn: sqlite3.Connection,
    offers: Sequence[dict[str, Any]],
    *,
    keyword_count: int,
    retired_keyword_count: int = 0,
) -> dict[str, Any]:
    scored = [offer for offer in offers if offer["score"] is not None]
    counts = {name: 0 for name in RECOMMENDATION_ORDER}
    for offer in scored:
        recommendation = offer["recommendation"]
        if recommendation in counts:
            counts[recommendation] += 1
        else:
            counts["need_more_info"] += 1
    preference_counts = {name: 0 for name in PREFERENCE_ORDER}
    for offer in offers:
        outcome = (offer.get("preference") or {}).get("outcome")
        if outcome in preference_counts:
            preference_counts[outcome] += 1
    # Every scoreless offer is classified by the status of its latest evaluation,
    # so "never evaluated", "evaluated but unresolved" and "evaluated and failed"
    # are never collapsed into one ambiguous "sin puntaje" count.
    evaluation_states: dict[str, int] = {}
    for offer in offers:
        state = offer.get("evaluation_status") or "none"
        evaluation_states[state] = evaluation_states.get(state, 0) + 1
    # The title filter is read-time only, so the count comes from the offers
    # themselves and never from a stored column.
    seniority_filtered = sum(1 for offer in offers if offer.get("seniority_filter"))
    # Only scoreless offers are counted: an offer with a card-only score keeps it
    # and is displayed like any other scored offer, even if its details are late.
    pending_details = sum(
        1 for offer in offers if offer["score"] is None and offer.get("pending_details")
    )
    total = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active = 1").fetchone()[0])
    human_reviewed_jobs = int(conn.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0])
    return {
        "active_jobs": total,
        "jobs_shown": len(offers),
        "scored": len(scored),
        "unscored": len(offers) - len(scored),
        "evaluation_states": evaluation_states,
        "recommendations": counts,
        # Offers hidden by the read-time title filter; the payload still carries
        # every active offer, so ``jobs_shown`` stays the full count.
        "seniority_filtered": seniority_filtered,
        # Unscored offers already waiting for a description are broken out of the
        # general unscored count, because their reason is expected and temporary.
        "pending_details_unscored": pending_details,
        # Preference verdicts are recomputed from the stored posting text and are
        # deliberately separate from the model recommendation above.
        "preferences": preference_counts,
        "keywords": keyword_count,
        # Imported human decisions, a different signal from the model
        # recommendation and the only source of the human rate columns.
        "human_reviewed_jobs": human_reviewed_jobs,
        # ``keywords`` counts every analyzed series; this split lets the UI
        # explain how many of them are retired and excluded from the table.
        "retired_keywords": retired_keyword_count,
    }


def _warnings(summary: dict[str, Any], *, keyword_count: int) -> list[str]:
    warnings: list[str] = []
    if summary["active_jobs"] == 0:
        warnings.append(
            "todavía no hay ofertas activas almacenadas; ingiera un fixture "
            "o ejecute una búsqueda acotada primero"
        )
    if summary["active_jobs"] and summary["scored"] == 0:
        warnings.append(
            "no se encontró ninguna evaluación exitosa; ejecute 'jobscout score --all' "
            "(agregue --rescore para reevaluar) antes de leer los puntajes"
        )
    if summary["unscored"]:
        pending = summary["pending_details_unscored"]
        if pending:
            warnings.append(
                f"{summary['unscored']} oferta(s) seleccionada(s) no tienen puntaje "
                f"exitoso ({summary['unscored'] - pending} sin puntuar, "
                f"{pending} pendientes de detalles) y se listan al final"
            )
        else:
            warnings.append(
                f"{summary['unscored']} oferta(s) seleccionada(s) no tienen puntaje exitoso "
                "y se listan al final"
            )
    if not keyword_count:
        warnings.append(
            "no hay procedencia de palabras clave registrada; los ítems de búsqueda "
            "guardan la palabra clave de origen y la posición observada, así que la "
            "analítica de palabras clave queda vacía hasta que se ingiera una ejecución"
        )
    if keyword_count and not summary["human_reviewed_jobs"]:
        warnings.append(
            "no hay feedback humano importado; las columnas de decisión humana "
            "(revisadas, tasa aplicar) quedan 'sin dato' hasta ejecutar "
            "'jobscout import-feedback' con una exportación del tablero"
        )
    return warnings


def build_payload(
    conn: sqlite3.Connection,
    *,
    top: int | None = DEFAULT_TOP,
    project_id: str = "",
    database_label: str | None = None,
    db_path: str | Path | None = None,
    stale_job_ids: Collection[int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the serializable dashboard payload from the database.

    ``top=None`` (the default) embeds every active offer; an explicit positive
    integer bounds the embedded offers. Only report-safe fields are included.
    ``database_label`` is an optional human label (a filename, never an absolute
    path) shown in the header. ``db_path`` is the database the connection reads
    from; when given, each offer also carries its company's stored dossier
    Markdown (``None`` when there is none), so the report stays self-contained.
    ``stale_job_ids`` marks offers whose shown score no longer matches their
    current scoring input; ``None`` means the profile was unavailable.
    """
    top = _validate_top(top)
    reference = now or utcnow()
    dossiers = load_dossiers(db_path) if db_path is not None else {}
    rows = _fetch_active_jobs(conn, top=top)
    job_ids = [int(row["id"]) for row in rows]
    keyword_keys_by_job = _fetch_job_keyword_keys(conn, job_ids)
    # One batched SELECT for every shown offer instead of one query per offer.
    detail_by_job = latest_detail_fetches_for_jobs(conn, job_ids)
    # The imported decision read does not consult ``cycle_id``; the latest sweep
    # is dated from it separately, so the two concerns stay independent.
    imported_decisions = _fetch_imported_feedback(conn, job_ids)
    # Same helper as ``score``, so the report and the scorer never disagree on
    # which offers are still waiting for a description.
    awaiting_details = jobs_awaiting_details(conn)
    stale = set(stale_job_ids or ())
    last_sweep_cycle, last_sweep_at = _last_sweep(conn)

    offers: list[dict[str, Any]] = []
    for row in rows:
        job_id = int(row["id"])
        posted_at = row["posted_at"]
        first_seen_at = row["first_seen_at"]
        age_days = _age_days(posted_at, now=reference)
        if age_days is None:
            age_days = _age_days(first_seen_at, now=reference)
        score = row["score"]
        # Preference is a second, independent axis. It is recomputed from the
        # stored posting text on every payload build, never persisted alongside
        # (or instead of) the evaluation, so the model score and its history are
        # untouched. The description is read for detection but never serialized.
        preference = assess_text(
            row["title"],
            row["description"],
            row["employment_type"],
            row["seniority"],
            row["location"],
            row["company"],
        )
        # Computed from the title at read time; never persisted, so a rule change
        # needs no migration and old rows are reclassified on the next build.
        seniority_filter = seniority_filter_reason(row["title"])
        offers.append(
            {
                "id": job_id,
                "title": _concisify(row["title"]) or "(sin título)",
                "company": _concisify(row["company"]) or "(empresa desconocida)",
                "location": _concisify(row["location"]),
                "url": _safe_url(row["url"]),
                "is_new": _is_new(first_seen_at, last_sweep_at),
                "age_days": age_days,
                "salary": _salary(row["salary"]),
                "salary_est": _salary_est(row),
                "score": round(float(score), 2) if score is not None else None,
                # The shown score is stale when it exists but no successful
                # evaluation of its scorer carries the current input.
                "needs_rescore": bool(score is not None and job_id in stale),
                "recommendation": (
                    str(row["recommendation"])
                    if score is not None and row["recommendation"]
                    else None
                ),
                # The status of the single latest evaluation of any kind. It lets
                # the report tell "never evaluated" apart from "evaluated and
                # failed" / "evaluated but unresolved", so a scoreless offer is
                # rendered with the real reason instead of a blank score. Only the
                # status is serialized: the stored error text is never embedded.
                "evaluation_status": (
                    str(row["evaluation_status"]) if row["evaluation_status"] else None
                ),
                "confidence": (
                    round(float(row["confidence"]), 3) if row["confidence"] is not None else None
                ),
                "model": str(row["model"]) if row["model"] else "",
                "evaluated_at": row["evaluated_at"],
                "rationale": _collapse_whitespace(row["rationale"]),
                "dimensions": _dimensions(row["dimensions_json"]),
                "strengths": _string_list(row["strengths_json"]),
                "gaps": _string_list(row["gaps_json"]),
                "blockers": _string_list(row["blockers_json"]),
                "unknowns": _string_list(row["unknowns_json"]),
                "keyword_keys": keyword_keys_by_job.get(job_id, []),
                # ``None`` when the title is acceptable; a reason string (for
                # example ``title: senior``) when the client hides it by default.
                "seniority_filter": seniority_filter,
                # The scorer skips these offers until their description arrives
                # (or their detail attempts run out), so the row shows a reason
                # instead of a score. Only an unscored offer is rendered with it.
                "pending_details": job_id in awaiting_details,
                # Only what the row and the text filter read; the schema tag and
                # rule ids stay in ``PreferenceAssessment.to_dict``.
                "preference": {
                    "outcome": str(preference.outcome),
                    "reason": preference.reason,
                    "evidence": preference.evidence,
                },
                # Durable imported human decision: the absence of a row is how
                # "not imported" stays distinct from a stored value. It is
                # displayed in the expanded row, never used as a filter.
                "imported_review": _imported_review(imported_decisions, job_id),
                "detail": _detail_evidence(
                    detail_by_job.get(job_id),
                    now=reference,
                    last_seen_at=row["last_seen_at"],
                ),
                # The company dossier Markdown, or ``None``. Embedded in the
                # payload so the report stays offline and self-contained; the
                # client renders it with ``textContent``, never as HTML.
                "dossier": _dossier_text(dossiers, row["company"]),
            }
        )

    keyword_stats = _fetch_keyword_stats(conn, now=reference)
    # Retired series stay in the payload, marked, as history; both collections
    # are rendered so the table can grey them instead of hiding them.
    active_keyword_stats = [item for item in keyword_stats if not item["retired"]]
    retired_keyword_stats = [item for item in keyword_stats if item["retired"]]
    summary = _summary(
        conn,
        offers,
        keyword_count=len(keyword_stats),
        retired_keyword_count=len(retired_keyword_stats),
    )
    status = _build_status(
        conn,
        last_sweep_cycle=last_sweep_cycle,
        last_sweep_at=last_sweep_at,
    )

    return {
        "schema": DASHBOARD_SCHEMA,
        "feedback_schema": FEEDBACK_SCHEMA,
        "project_id": project_id,
        "database_label": database_label,
        "generated_at": utcnow_iso(),
        "limits": {"top": top},
        "summary": summary,
        "status": status,
        "warnings": _warnings(summary, keyword_count=len(keyword_stats)),
        "offers": offers,
        "keywords": active_keyword_stats,
        "retired_keywords": retired_keyword_stats,
    }


def _safe_url(value: Any) -> str | None:
    """Only absolute http(s) URLs reach an ``href``; anything else is dropped."""
    if value is None:
        return None
    text = str(value).strip()
    lowered = text.lower()
    if not any(lowered.startswith(scheme) for scheme in _ALLOWED_URL_SCHEMES):
        return None
    return text


def _dossier_text(dossiers: dict[str, str], company: str | None) -> str | None:
    """The stored dossier Markdown for ``company``, or ``None``.

    A company name that cannot produce a slug simply has no dossier; that is
    not an error in a read-only report build.
    """
    if not company:
        return None
    try:
        return dossiers.get(company_slug(company))
    except ValueError:
        return None


def _detail_checked_at(fetch: JobDetailFetch) -> str | None:
    """When the attempt's evidence was produced, or ``None`` when unusable."""
    return fetch.fetched_at or fetch.stored_at or None


def _classify_detail_state(
    fetch: JobDetailFetch | None,
    *,
    now: datetime,
    window_days: int = DETAIL_EVIDENCE_WINDOW_DAYS,
) -> str:
    """Classify the single latest detail-fetch attempt into an honest state.

    Only the stored evidence is consulted, so the result can never claim a link
    is broken because of a redirect or a guess. A success whose age cannot be
    computed is reported as ``stale`` rather than ``ok``: freshness the data
    cannot prove is never credited.
    """
    if fetch is None:
        return DETAIL_STATE_UNCHECKED
    status = str(fetch.status)
    if status == DetailFetchStatus.SKIPPED:
        return DETAIL_STATE_SKIPPED
    if status in (DetailFetchStatus.OK, DetailFetchStatus.PARTIAL):
        # The exact age is compared, not the rounded display value, so "exactly
        # the window" stays current and one second past it does not.
        age = _age_days_exact(_detail_checked_at(fetch), now=now)
        if age is not None and age <= window_days:
            return DETAIL_STATE_OK
        return DETAIL_STATE_STALE
    # Any other stored attempt is a failure; classify it by what was recorded.
    if fetch.http_status in DEFINITIVE_MISSING_HTTP_STATUSES:
        return DETAIL_STATE_DEFINITIVE_MISSING
    if fetch.http_status is None and fetch.raw_document_id is None:
        return DETAIL_STATE_RETRYABLE_FAILURE
    return DETAIL_STATE_FAILED


def _redirect_evidence(fetch: JobDetailFetch, *, is_success: bool) -> str | None:
    """A redirect's final URL only as safe evidence, never as a broken link.

    The value is exposed only for a successful attempt, only when it is a safe
    http(s) URL, and only when it actually differs from the requested URL: a
    stored echo of the requested URL is not a redirect, and a failed attempt's
    URL is never presented as a destination.
    """
    if not is_success:
        return None
    final = _safe_url(fetch.final_url)
    if final is None:
        return None
    requested = _safe_url(fetch.url)
    if requested is not None and final == requested:
        return None
    return final


def _detail_evidence(
    fetch: JobDetailFetch | None,
    *,
    now: datetime,
    last_seen_at: str | None = None,
    window_days: int = DETAIL_EVIDENCE_WINDOW_DAYS,
) -> dict[str, Any]:
    """Serializable link evidence for one offer.

    ``state`` is :func:`_classify_detail_state`. ``refuted`` is True only for a
    404/410 that a later re-sighting contradicts (``last_seen_at`` newer than
    the attempt), which is exactly the case where a visible offer's death
    evidence was already disproved by the pipeline. A redirect's ``final_url``
    is attached only as safe evidence of a successful redirect.
    """
    state = _classify_detail_state(fetch, now=now, window_days=window_days)
    if fetch is None:
        return {
            "state": state,
            "age_days": None,
            "window_days": int(window_days),
            "refuted": False,
            "final_url": None,
        }
    checked_at = _detail_checked_at(fetch)
    refuted = bool(
        state == DETAIL_STATE_DEFINITIVE_MISSING
        and last_seen_at
        and checked_at
        and str(last_seen_at) > str(checked_at)
    )
    is_success = state in (DETAIL_STATE_OK, DETAIL_STATE_STALE)
    return {
        "state": state,
        "age_days": _age_days(checked_at, now=now),
        "window_days": int(window_days),
        "refuted": refuted,
        "final_url": _redirect_evidence(fetch, is_success=is_success),
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def embed_json(payload: Any) -> str:
    """Serialize a payload for a ``<script type="application/json">`` block.

    ``</script>`` cannot close the block and ``<!--`` cannot open a comment once
    every ``<``, ``>``, ``&``, and line/paragraph separator is escaped. The
    result is still exactly the same JSON value after ``JSON.parse``.
    """
    document = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return document.translate(
        str.maketrans(
            {
                "<": "\\u003c",
                ">": "\\u003e",
                "&": "\\u0026",
                "\u2028": "\\u2028",
                "\u2029": "\\u2029",
            }
        )
    )


#: Placeholders look like ``__NAME__``. Only uppercase names are tokens, so user
#: text such as ``__script__`` is never mistaken for one.
_TEMPLATE_TOKEN = re.compile(r"__([A-Z][A-Z0-9_]*)__")


#: The embedded families, their files, and the weight range each file covers.
#: Charis and DM Mono ship static faces, Inter a variable font. DM Mono stops at
#: 500, so that face also answers the 600 the figures ask for instead of a faux bold.
_FONT_FACES: tuple[tuple[str, str, str], ...] = (
    ("Charis SIL", "charis-sil-latin-400.woff2", "400"),
    ("Charis SIL", "charis-sil-latin-700.woff2", "700"),
    ("Inter", "inter-latin-wght.woff2", "100 900"),
    ("DM Mono", "dm-mono-latin-400.woff2", "400"),
    ("DM Mono", "dm-mono-latin-500.woff2", "500 900"),
)


@lru_cache(maxsize=1)
def _font_faces_css() -> str:
    """Build the ``@font-face`` block from the packaged woff2 files.

    Embedding the bytes as ``data:`` URIs keeps the report a single file that
    renders identically from ``file://`` with no network and no installed font.
    The files are read once per process; the result is cached.
    """
    font_dir = Path(__file__).parent / "fonts"
    rules: list[str] = []
    for family, filename, weight in _FONT_FACES:
        encoded = base64.b64encode((font_dir / filename).read_bytes()).decode("ascii")
        rules.append(
            "@font-face {\n"
            f'  font-family:"{family}";\n'
            "  font-style:normal;\n"
            f"  font-weight:{weight};\n"
            f'  src:url(data:font/woff2;base64,{encoded}) format("woff2");\n'
            "  font-display:swap;\n"
            "}"
        )
    return "\n".join(rules)


def _render_template(template: str, values: dict[str, str]) -> str:
    """Substitute every ``__TOKEN__`` in a single left-to-right pass.

    The single pass is the security property, not a style choice. A chained
    ``str.replace`` sequence re-scans text it already inserted, so a literal job
    value or database filename equal to ``__DATA__`` or ``__SCRIPT__`` could be
    swapped for the payload or the script block, corrupting the report. Here a
    replacement is written once and never rescanned, so such a value survives as
    literal data.
    """

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise DashboardError(f"template placeholder __{key}__ has no value")
        return values[key]

    return _TEMPLATE_TOKEN.sub(substitute, template)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>__TITLE__</title>
<style>
__FONT_FACES__
__STYLE__
</style>
</head>
<body>
<a class="skip-link" href="#contenido">Saltar al contenido principal</a>

<header class="masthead">
  <div class="masthead-wrap">
  <div class="mast">
    <h1>jobscout</h1>
    <p class="status-line" id="status-line" aria-label="Estado del último barrido"></p>
  </div>
  <nav class="tabs" role="tablist" aria-label="Vistas del informe">
    <button type="button" role="tab" id="tab-offers" aria-selected="true"
      aria-controls="panel-offers" data-view="offers">Ofertas <span class="tab-count" id="tab-offers-count">0</span></button>
    <button type="button" role="tab" id="tab-keywords" aria-selected="false"
      aria-controls="panel-keywords" data-view="keywords">Palabras clave <span class="tab-count" id="tab-keywords-count">0</span></button>
    <span class="tab-spacer"></span>
    <button type="button" id="export-feedback" class="export"
      title="Exportar feedback local (JSON): solo aparece si hay decisiones en este navegador"
      hidden>Exportar <span id="feedback-count" aria-live="polite">0 decisiones</span></button>
  </nav>
  </div>
</header>

<div class="panel">

<div class="content">

<main id="contenido">
  <section id="panel-offers" role="tabpanel" aria-labelledby="tab-offers">
    <div class="toolbar" role="search" aria-label="Búsqueda y filtros de ofertas">
      <input type="search" id="filter-text" autocomplete="off"
        aria-label="Buscar" placeholder="título, empresa, ubicación">
      <div class="seg" id="filter-recommendation" role="group"
        aria-label="Recomendación del modelo">
        <button type="button" data-value="" aria-pressed="true">Todas</button>
        <button type="button" data-value="apply" aria-pressed="false">Aplicar</button>
        <button type="button" data-value="consider" aria-pressed="false">Considerar</button>
        <button type="button" data-value="skip" aria-pressed="false">Descartar</button>
      </div>
      <div class="seg">
        <button type="button" id="filter-new" aria-pressed="false"
          title="Muestra solo las ofertas nuevas del último barrido">Solo nuevas</button>
      </div>
      <div class="seg">
        <button type="button" id="filter-show-seniority" aria-pressed="false"
          title="Las ofertas con título claramente senior se ocultan por defecto">Mostrar 0 ocultas por seniority</button>
      </div>
      <select id="filter-min-score" aria-label="Puntaje mínimo">
        <option value="0">Score ≥ 0</option>
        <option value="50">≥ 50</option>
        <option value="70">≥ 70</option>
      </select>
      <select id="filter-keyword" aria-label="Palabra clave de origen">
        <option value="">Todas las keywords</option>
      </select>
      <select id="filter-sort" aria-label="Ordenar por">
        <option value="score">Orden: score</option>
        <option value="fresh">Orden: más recientes</option>
      </select>
      <span id="result-count" aria-live="polite">0 ofertas</span>
    </div>
    <ul class="list" id="offers"></ul>
    <button type="button" id="show-more" class="mini" hidden>Mostrar 50 más</button>
    <p id="empty-state" class="empty" hidden>
      Ninguna oferta con estos filtros · <button type="button" id="empty-reset" class="mini">limpiar</button>
    </p>
    <p class="report-meta" id="report-meta">__META__</p>
    __WARNINGS__
  </section>

<section id="panel-keywords" role="tabpanel" aria-labelledby="tab-keywords" hidden>
<table id="keyword-table">
  <thead><tr><th>Keyword</th><th>Proveedor</th><th class="num" title="Ofertas distintas vistas por esta búsqueda, inactivas incluidas">Vistas</th><th class="num">Activas</th><th class="num" data-initial-sort="descending">Score medio</th><th class="num" title="Ofertas activas con score de 70 o más">≥ 70</th><th class="num" title="Decisiones «aplicar» importadas con jobscout import-feedback">Aplicadas</th></tr></thead>
  <tbody></tbody>
</table>
<p class="note" id="keyword-empty" hidden>Todavía no hay procedencia de palabras clave registrada.</p>
</section>
</main>

</div>

</div>

<button type="button" class="to-top" id="back-to-top" aria-label="Volver arriba"
  title="Volver arriba" hidden>↑</button>
<script type="application/json" id="jobscout-data">__DATA__</script>
<script>
__SCRIPT__
</script>
<noscript>
  <p class="notice">Este panel necesita JavaScript para filtrar y mostrar sus datos.</p>
</noscript>
</body>
</html>
"""

_STYLE = """
:root {
  color-scheme: light;
  --paper:#f3f0e8;
  --sheet:#fbfaf6;
  --ink:#17160f;
  --ink-2:#55524a;
  --ink-3:#7a766c;
  --rule:#d8d3c6;
  --rule-2:#e8e4d9;
  --signal:#bd3319;
  --signal-wash:#f6e3dc;
  --mid:#74651d;
  --mid-wash:#f0ead2;
  --low:#7a766c;
  --money:#2f6b3a;
  --money-wash:#e3ede2;
  --serif:"Charis SIL","Iowan Old Style","Charter","Georgia",serif;
  --sans:"Inter",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"DM Mono",ui-monospace,"SF Mono","Cascadia Mono",monospace;
  --fs-11:11px;
  --fs-12:12px;
  --fs-13:13px;
  --fs-14:14px;
  --fs-15:15px;
  --fs-16:16px;
  --fs-22:22px;
  --fs-26:26px;
  --sp-2:2px;
  --sp-4:4px;
  --sp-5:5px;
  --sp-6:6px;
  --sp-7:7px;
  --sp-8:8px;
  --sp-9:9px;
  --sp-10:10px;
  --sp-12:12px;
  --sp-14:14px;
  --sp-16:16px;
  --sp-18:18px;
  --sp-24:24px;
  --sp-32:32px;
  --sp-36:36px;
  --sp-48:48px;
  --sp-90:90px;
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--paper); color:var(--ink);
  font-family:var(--sans); font-size:var(--fs-14); line-height:1.5;
  -webkit-font-smoothing:antialiased; }
h1, h2, h3, h4 { margin:0; color:var(--ink); font-weight:600; }
h1 { font-family:var(--serif); font-size:var(--fs-22); font-weight:700; line-height:1; letter-spacing:-.01em; }
h2 { font-size:var(--fs-16); line-height:1.25; letter-spacing:-.01em; }
h3 { font-size:var(--fs-14); line-height:1.35; }
h4 { font-family:var(--mono); font-size:var(--fs-11); line-height:1.3;
  text-transform:uppercase; letter-spacing:.06em; color:var(--ink-3); }
p { margin:0; }
code { font-family:var(--mono); font-size:var(--fs-12); }
.muted { color:var(--ink-3); }
a { color:var(--ink); text-decoration:underline; text-underline-offset:2px;
  text-decoration-color:var(--rule); }
a:hover, a:focus-visible { text-decoration-color:var(--ink); }
:focus-visible { outline:2px solid var(--ink); outline-offset:2px; }
[hidden] { display:none !important; }
.skip-link { position:absolute; left:-9999px; top:0; z-index:100; background:var(--ink);
  color:var(--paper); font-weight:600; font-size:var(--fs-13); text-decoration:none;
  padding:var(--sp-8) var(--sp-12); }
.skip-link:focus { left:0; }

.panel { max-width:1280px; margin:0 auto; padding:0 var(--sp-32) var(--sp-48); }
/* The masthead sits outside the column so its ink rule spans the whole window,
   while its content keeps the same width as the list below. */
.masthead-wrap { max-width:1280px; margin:0 auto; padding:0 var(--sp-32); }
/* The masthead is the report's only sticky element: status, tabs, decisions and
   export stay reachable while the offer list scrolls. Its opaque paper keeps rows
   from showing through, and it stays in the normal flow so it never covers content
   at rest. */
header.masthead { position:sticky; top:0; z-index:20; background:var(--paper);
  border-bottom:1px solid var(--ink); }
.mast { display:flex; align-items:baseline; gap:var(--sp-24);
  padding:var(--sp-18) 0 var(--sp-10); }
.mast h1 { font-size:var(--fs-22); }
.mast .status-line { margin-left:auto; }
.status-line { margin:0; display:flex; flex-wrap:wrap; gap:var(--sp-16);
  font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-2); }
.status-line .bad { color:var(--signal); }
.status-line .calm { color:var(--ink-3); }
/* Floating in the corner keeps the masthead for status and tabs only; it shows up
   after one screen of scrolling, which is when the way back starts to cost. */
.to-top { position:fixed; right:var(--sp-24); bottom:var(--sp-24); z-index:30;
  width:40px; height:40px; cursor:pointer; background:var(--paper); color:var(--ink);
  border:1px solid var(--ink); font-family:var(--sans); font-size:var(--fs-16); line-height:1; }
.to-top:hover, .to-top:focus-visible { background:var(--ink); color:var(--paper); }
.tabs { display:flex; align-items:flex-end; }
.tabs [role="tab"] { cursor:pointer; background:none; color:var(--ink-2);
  border:0; border-bottom:2px solid transparent; font-family:var(--sans);
  font-size:var(--fs-13); font-weight:500; padding:var(--sp-8) var(--sp-14) var(--sp-9); }
.tabs [role="tab"]:hover, .tabs [role="tab"]:focus-visible { color:var(--ink); }
.tabs [role="tab"][aria-selected="true"] { color:var(--ink); border-bottom-color:var(--ink); }
.tab-count { margin-left:var(--sp-4); font-family:var(--mono); font-size:var(--fs-12);
  color:var(--ink-3); }
.tab-spacer { flex:1; }
.export { cursor:pointer; background:var(--paper); color:var(--ink);
  border:1px solid var(--ink); font-family:var(--mono); font-size:var(--fs-12);
  padding:var(--sp-6) var(--sp-10); margin:0 0 var(--sp-6); }
.export:hover:not(:disabled), .export:focus-visible:not(:disabled) {
  background:var(--ink); color:var(--paper); }

.content { display:flex; flex-direction:column; gap:var(--sp-24); min-width:0; }
.content > * { min-width:0; }

.toolbar { display:flex; flex-wrap:wrap; align-items:center; gap:var(--sp-10);
  padding:var(--sp-14) 0; border-bottom:1px solid var(--rule); }
/* The search field keeps a usable width and lets the other controls wrap, so the
   toolbar never overflows a narrow window. */
.toolbar input[type="search"] { flex:1 1 220px; min-width:180px; max-width:360px;
  font-family:var(--sans); font-size:var(--fs-13); padding:var(--sp-7) var(--sp-10);
  color:var(--ink); background:var(--sheet); border:1px solid var(--rule); }
.toolbar select { font-family:var(--sans); font-size:var(--fs-12);
  padding:var(--sp-6) var(--sp-8); color:var(--ink); background:var(--sheet);
  border:1px solid var(--rule); white-space:nowrap; }
/* A select grows to its longest option; capping it keeps the toolbar on one line. */
#filter-keyword { max-width:200px; text-overflow:ellipsis; }
.seg { display:flex; border:1px solid var(--rule); }
.seg button { cursor:pointer; background:var(--sheet); color:var(--ink-2);
  border:0; border-right:1px solid var(--rule); font-family:var(--sans);
  font-size:var(--fs-12); padding:var(--sp-6) var(--sp-10); white-space:nowrap; }
.seg button:last-child { border-right:0; }
.seg button:hover, .seg button:focus-visible { color:var(--ink); }
.seg button[aria-pressed="true"] { background:var(--ink); color:var(--paper); }
#result-count { margin-left:auto; font-family:var(--mono); font-size:var(--fs-12);
  color:var(--ink-3); white-space:nowrap; }

.section-label { font-size:var(--fs-11); font-weight:600; text-transform:uppercase;
  letter-spacing:.06em; color:var(--ink-3); }
/* Region boundary: every region heading closes with a hairline ink rule. */
.section-head { display:flex; align-items:baseline; justify-content:space-between;
  gap:var(--sp-12); padding-bottom:var(--sp-8); border-bottom:1px solid var(--ink); }
.section-head h2 { font-size:var(--fs-16); }
#panel-offers { display:flex; flex-direction:column; min-width:0; }
.report-meta { margin:var(--sp-24) 0 0; font-family:var(--mono);
  font-size:var(--fs-12); color:var(--ink-3); }

#contenido { display:flex; flex-direction:column; gap:var(--sp-24); min-width:0; }
.mast .status-line { margin-left:auto; }
.list { margin:0; padding:0; list-style:none; }
.row { border-bottom:1px solid var(--rule); }
.row > summary { list-style:none; cursor:pointer; display:grid;
  grid-template-columns:64px 1fr 190px 120px 80px 28px; gap:var(--sp-18);
  align-items:center; padding:var(--sp-14) var(--sp-8); }
.row > summary::-webkit-details-marker { display:none; }
.row > summary:hover, .row > summary:focus-visible { background:var(--sheet); }
.row > summary:focus-visible { outline:2px solid var(--ink); outline-offset:-2px; }
.score { font-family:var(--mono); font-size:var(--fs-26); font-weight:600; line-height:1;
  font-variant-numeric:tabular-nums; letter-spacing:-.03em; }
.score small { display:block; height:3px; margin-top:var(--sp-6); background:var(--rule-2); }
.score small i { display:block; height:100%; background:currentColor; }
.score.score-state { font-size:var(--fs-11); font-weight:400; letter-spacing:0; color:var(--ink-3); }
.score.stale { opacity:.55; text-decoration:underline dashed; text-underline-offset:var(--sp-4); }
.score .stale { display:block; margin-top:var(--sp-4); font-family:var(--sans);
  font-size:var(--fs-11); font-weight:400; letter-spacing:0; text-decoration:none; color:var(--ink-3); }
.s-hi { color:var(--signal); } .s-mid { color:var(--mid); } .s-lo { color:var(--low); }
.t { font-family:var(--sans); font-size:var(--fs-15); font-weight:600; line-height:1.3; }
.c { color:var(--ink-2); font-size:var(--fs-13); margin-top:var(--sp-2); }
.chips { display:flex; gap:var(--sp-6); flex-wrap:wrap; }
.chip { font-family:var(--mono); font-size:var(--fs-11); line-height:1; padding:var(--sp-4) var(--sp-6);
  border:1px solid var(--rule); color:var(--ink-2); text-transform:uppercase; letter-spacing:.04em; }
.chip.new { border-color:var(--signal); color:var(--signal); }
.chip.veto { border-color:var(--signal); color:var(--signal); }
.chip.rec-apply { background:var(--signal-wash); border-color:transparent; color:var(--signal); }
.chip.rec-consider { background:var(--mid-wash); border-color:transparent; color:var(--mid); }
.chip.flag { border-color:var(--mid); color:var(--mid); }
.sal { font-family:var(--mono); font-size:var(--fs-13); font-variant-numeric:tabular-nums;
  font-weight:600; color:var(--money); text-align:right; }
.sal em { display:block; font-family:var(--mono); font-size:var(--fs-11); font-style:normal;
  font-weight:400; color:var(--ink-3); }
.sal.unk { color:var(--ink-3); font-weight:400; }
/* The OpenCode estimate stays neutral and is marked with "≈" plus the
   "estimado" label, never mixed with a stated salary. */
.sal.est { color:var(--ink-3); font-weight:400; }
.sal.est::before { content:"\\2248 "; }
.salbox { display:grid; grid-template-columns:auto 1fr; gap:var(--sp-4) var(--sp-16); font-size:var(--fs-13); margin:0; }
.salbox dt { font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-3); }
.salbox dd { margin:0; }
.salbox dd.sal-stated { color:var(--money); font-weight:600; }
.salbox dd.sal-est .tier, .salbox dd.sal-est .basis { color:var(--ink-3); font-size:var(--fs-12); margin-top:var(--sp-2); }
.age { font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-3); text-align:right; font-variant-numeric:tabular-nums; }
.chev { color:var(--ink-3); transition:transform .15s; }
.row[open] .chev { transform:rotate(90deg); }
.row[open] > summary { background:var(--sheet); }
.body { background:var(--sheet); padding:var(--sp-4) var(--sp-8) var(--sp-24) var(--sp-90);
  display:grid; grid-template-columns:1.1fr 1fr; gap:var(--sp-36); }
.body.nodims { grid-template-columns:1fr; }
.body h3 { font-family:var(--mono); font-size:var(--fs-11); font-weight:600; line-height:1;
  text-transform:uppercase; letter-spacing:.08em; color:var(--ink-3); margin:var(--sp-18) 0 var(--sp-10); }
.why { font-family:var(--serif); font-size:var(--fs-16); line-height:1.55; margin:0; max-width:62ch; }
.nodims-note { font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-3); margin-top:var(--sp-8); }
.dims { display:grid; gap:var(--sp-12); }
.dim { display:grid; grid-template-columns:110px 1fr 34px; gap:var(--sp-10); align-items:start; }
.dim .k { font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-2); }
.dim .k em { display:block; font-style:normal; color:var(--ink-3); font-size:var(--fs-11); }
.bar { height:6px; background:var(--rule-2); margin-top:var(--sp-5); }
.bar i { display:block; height:100%; background:var(--ink); }
.dim .v { font-family:var(--mono); font-size:var(--fs-12); text-align:right; font-variant-numeric:tabular-nums; }
.dim p { grid-column:2/4; margin:var(--sp-2) 0 0; font-size:var(--fs-13); color:var(--ink-2); }
.dim q { display:block; font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-3); quotes:none;
  border-left:2px solid var(--rule); padding-left:var(--sp-8); margin-top:var(--sp-4); }
.lists { grid-column:1/-1; display:grid; grid-template-columns:repeat(4,1fr); gap:var(--sp-24);
  border-top:1px solid var(--rule); padding-top:var(--sp-4); }
.lists ul { margin:0; padding-left:var(--sp-16); font-size:var(--fs-13); color:var(--ink-2); }
.lists li { margin-bottom:var(--sp-4); }
.lists .blk h3 { color:var(--signal); }
.dossier { grid-column:1/-1; border-top:1px solid var(--rule); padding-top:var(--sp-8); }
.dossier > summary { list-style:none; cursor:pointer; font-family:var(--mono);
  font-size:var(--fs-12); color:var(--ink-2); padding:var(--sp-4) 0; }
.dossier > summary::-webkit-details-marker { display:none; }
.dossier > summary:hover, .dossier > summary:focus-visible { color:var(--ink); }
.dossier-text { white-space:pre-wrap; overflow-wrap:anywhere; margin:var(--sp-8) 0 0;
  font-family:var(--mono); font-size:var(--fs-12); line-height:1.5; color:var(--ink-2); }
.foot { grid-column:1/-1; display:flex; gap:var(--sp-16); align-items:center; flex-wrap:wrap;
  font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-3);
  border-top:1px solid var(--rule); padding-top:var(--sp-12); }
.foot a { color:var(--ink); margin-left:auto; }
.feedback { display:flex; flex-wrap:wrap; align-items:center; gap:var(--sp-8); }
.feedback-controls { display:inline-flex; gap:var(--sp-16); }
.mini { cursor:pointer; background:transparent; border:1px solid var(--rule); padding:var(--sp-4) var(--sp-8);
  font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-2); }
.mini:hover, .mini:focus-visible { border-color:var(--ink); color:var(--ink); }
.mini[aria-pressed="true"] { background:var(--ink); border-color:var(--ink); color:var(--paper); }
.foot textarea { flex-basis:100%; font-family:var(--sans); font-size:var(--fs-13); background:var(--paper);
  color:var(--ink); border:1px solid var(--rule); padding:var(--sp-8); min-height:56px; }
.decision-saved { color:var(--ink-2); margin-left:var(--sp-8); transition:opacity .15s; }
.decision-saved.fading { opacity:0; }
.decision-state { display:flex; flex-wrap:wrap; align-items:baseline;
  gap:var(--sp-4) var(--sp-8); font-size:var(--fs-13); color:var(--ink-2); }
/* A legacy ``consider`` decision is marked as historical with weight, not hue. */
.decision-state.legacy { font-style:italic; border-left:2px solid var(--rule);
  padding-left:var(--sp-8); }
#show-more { display:block; margin:var(--sp-16) auto 0 var(--sp-8); }
.report-warnings p { font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-3); }

/* Keyword view: the mockup's table, on tokens. */
table { width:100%; border-collapse:collapse; margin-top:var(--sp-8); }
th { font-family:var(--mono); font-size:var(--fs-11); font-weight:600;
  text-transform:uppercase; letter-spacing:.06em; color:var(--ink-3); text-align:left;
  padding:var(--sp-10) var(--sp-8); border-bottom:1px solid var(--ink); cursor:pointer; }
td { padding:var(--sp-10) var(--sp-8); border-bottom:1px solid var(--rule); font-variant-numeric:tabular-nums; }
td.num, th.num { text-align:right; font-family:var(--mono); width:1%; white-space:nowrap;
  padding-left:var(--sp-24); }
th { white-space:nowrap; }
tr.retired td { color:var(--ink-3); }
tr.retired td:first-child::after { content:" · retirada"; font-family:var(--mono);
  font-size:var(--fs-11); }
.note { font-family:var(--mono); font-size:var(--fs-12); color:var(--ink-3);
  padding:var(--sp-24) 0; }
/* The header label is the sort control; only the active column shows its arrow,
   and aria-sort carries the direction so it never rests on the glyph alone. */
.sort-btn { font:inherit; color:inherit; background:none; border:0; padding:0;
  cursor:pointer; text-transform:inherit; letter-spacing:inherit; }
.sort-btn:hover, .sort-btn:focus-visible { color:var(--ink); }
th[aria-sort="ascending"] .sort-btn::after { content:" \\2191"; }
th[aria-sort="descending"] .sort-btn::after { content:" \\2193"; }

.empty { padding:var(--sp-24) 0; font-family:var(--mono); font-size:var(--fs-12);
  color:var(--ink-3); }
.toast { position:fixed; bottom:calc(var(--sp-24) + 40px + var(--sp-12)); right:var(--sp-24); z-index:50;
  max-width:min(420px, calc(100vw - var(--sp-48))); background:var(--ink);
  color:var(--paper); border:1px solid var(--ink); border-radius:2px;
  padding:var(--sp-8) var(--sp-12); font-size:var(--fs-13); }

/* The complete reduced-motion guard: no transition or animation survives it. */
@media (prefers-reduced-motion:reduce) {
  * { transition:none !important; animation:none !important; }
}
"""


# Rendering builds every node with DOM APIs and assigns untrusted values through
# ``textContent`` only. The script never uses ``innerHTML``, so the report is safe
# even if the JSON escaping were somehow bypassed.
_SCRIPT = r"""
(function () {
  "use strict";

  var EXPECTED_SCHEMA = "jobscout.dashboard.v1";
  // Feedback decisions and recommendation values stay English in storage and in
  // the payload; only their visible labels are translated for the reader. The
  // active human decision is binary; ``consider`` is a legacy value that may
  // still be present in localStorage from before this workflow.
  var FEEDBACK_DECISIONS = ["apply", "reject"];
  // The reason is the page's only free-text input, so it is the only one capped.
  var MAX_REASON_CHARS = 500;
  var RECOMMENDATION_LABELS = {
    apply: "Aplicar", consider: "Considerar", skip: "Descartar",
    need_more_info: "Requiere más información",
    // Display-only buckets for an offer without a successful public evaluation.
    failed: "Evaluación fallida", unscored: "Sin evaluar"
  };
  // Labels for the active decision buttons, plus a display-only label for the
  // legacy value. The legacy value is never a button.
  var FEEDBACK_LABELS = { apply: "Aplicar", reject: "Descartar" };
  var LEGACY_FEEDBACK_LABELS = { consider: "Considerar (histórico)" };
  var dataNode = document.getElementById("jobscout-data");
  var payload;
  try {
    payload = JSON.parse(dataNode.textContent);
  } catch (err) {
    document.getElementById("result-count").textContent = "No se pudieron leer los datos del panel.";
    return;
  }
  if (!payload || payload.schema !== EXPECTED_SCHEMA) {
    document.getElementById("result-count").textContent =
      "Este informe fue generado por una versión incompatible de jobscout; vuelva a generarlo.";
    return;
  }

  // The storage key is derived from the payload schema so the literal is never
  // duplicated: ``jobscout.feedback.v1`` lives only in the payload.
  var STORAGE_KEY = payload.feedback_schema + ":" + payload.project_id;

  var feedback = loadFeedback();
  var filters = { text: "", recommendation: "", isNew: false, showSeniority: false, minScore: null, keyword: "", sort: "score" };

  function labelFrom(map, value, fallback) {
    return Object.prototype.hasOwnProperty.call(map, value) ? map[value] : fallback;
  }

  // Active decisions have a normal label; a legacy ``consider`` gets the
  // historical label so the reader can tell it apart from a current choice.
  function feedbackLabel(decision) {
    var active = labelFrom(FEEDBACK_LABELS, decision, null);
    if (active !== null) { return active; }
    return labelFrom(LEGACY_FEEDBACK_LABELS, decision, decision);
  }

  function isLegacyFeedback(decision) {
    return labelFrom(FEEDBACK_LABELS, decision, null) === null && Boolean(decision);
  }

  function decisionStateText(decision) {
    // The pressed button already shows an active decision; only a legacy value,
    // which has no button of its own, needs words.
    if (isLegacyFeedback(decision)) {
      return "Decisión histórica (legacy): " + feedbackLabel(decision) +
        ". Puede reemplazarla con Aplicar o Descartar.";
    }
    return "";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  // Salary estimates are informational only. The row keeps the compact
  // "22–28k" style, while the expanded body spells whole euros with Spanish
  // thousands dots.
  var EMPLOYER_TIER_LABELS = {
    consultora_pequena: "consultora pequeña / pyme",
    consultora_grande: "gran consultora / corporación",
    startup: "startup de producto",
    scaleup_nacional: "scale-up nacional",
    internacional: "internacional / remoto global",
    big_tech: "big tech"
  };

  function salaryThousands(value) {
    return String(Math.round(Number(value))).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }

  function salaryRangeK(estimate) {
    function compact(value) {
      return String(Math.round(Number(value) / 100) / 10).replace(".", ",");
    }
    return compact(estimate[0]) + "\u2013" + compact(estimate[2]) + "k";
  }

  function salaryEstimateText(estimate) {
    return salaryThousands(estimate[0]) + " \u2013 " + salaryThousands(estimate[2]) +
      " \u20ac \u00b7 probable " + salaryThousands(estimate[1]) + " \u20ac";
  }

  // Defence in depth: the Python side already drops non-http(s) URLs, but never
  // assign an unvalidated value to href. Only absolute http(s) survives.
  function safeHttpUrl(value) {
    if (typeof value !== "string" || !/^https?:\/\//i.test(value)) { return null; }
    try {
      var parsed = new URL(value);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") {
        return parsed.href;
      }
    } catch (err) {
      return null;
    }
    return null;
  }

  // Display-only shortening of the stored ISO timestamp: ``2026-09-08T00:00:00Z``
  // becomes ``2026-09-08 00:00 UTC`` so the value stops wrapping mid-token. The
  // payload keeps the original string, and anything the pattern does not fully
  // match (another precision, an offset) is shown verbatim rather than guessed at.
  function displayTimestamp(value) {
    if (typeof value !== "string") { return String(value); }
    var match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2})?(?:\.\d+)?Z?$/
      .exec(value.trim());
    return match ? (match[1] + " " + match[2] + " UTC") : value;
  }

  function loadFeedback() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) { return {}; }
      var parsed = JSON.parse(raw);
      return (parsed && typeof parsed === "object" && !Array.isArray(parsed)) ? parsed : {};
    } catch (err) {
      return {};
    }
  }

  function saveFeedback() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(feedback));
      return true;
    } catch (err) {
      setToast("El almacenamiento local no está disponible; el feedback no se guardará.");
      return false;
    }
  }

  function setToast(message) {
    var existing = document.querySelector(".toast");
    if (existing) { existing.remove(); }
    var toast = el("div", "toast", message);
    toast.setAttribute("role", "status");
    document.body.appendChild(toast);
    window.setTimeout(function () { toast.remove(); }, 4000);
  }

  function feedbackCount() {
    return Object.keys(feedback).filter(function (key) {
      var entry = feedback[key];
      return entry && entry.decision;
    }).length;
  }

  function refreshFeedbackCount() {
    var count = feedbackCount();
    document.getElementById("feedback-count").textContent =
      count + (count === 1 ? " decisión" : " decisiones");
    // Export appears only when this browser holds a decision to hand over.
    document.getElementById("export-feedback").hidden = count === 0;
  }

  // The effective model-axis value for one offer. A stored successful
  // recommendation wins; otherwise the latest evaluation's status explains why
  // there is no score, so an incomplete or failed offer is not shown as merely
  // "unscored". Display-only buckets never reach the database.
  function effectiveRecommendation(offer) {
    if (offer.recommendation) { return offer.recommendation; }
    if (offer.evaluation_status === "needs_more_info") { return "need_more_info"; }
    if (offer.evaluation_status === "parse_error" || offer.evaluation_status === "error" ||
        offer.evaluation_status === "not_configured") { return "failed"; }
    return "unscored";
  }

  function matches(offer) {
    // Filtered offers stay in the payload but are hidden until the rail toggle
    // asks for them; a shown filtered offer still displays its reason.
    if (offer.seniority_filter && !filters.showSeniority) { return false; }
    if (filters.recommendation && effectiveRecommendation(offer) !== filters.recommendation) {
      return false;
    }
    if (filters.isNew && !offer.is_new) { return false; }
    if (filters.minScore !== null && (offer.score === null || offer.score < filters.minScore)) {
      return false;
    }
    if (filters.keyword) {
      // The filter value is the provider|keyword series key, so the same
      // literal keyword from two platforms is never selected together, while
      // every cycle of one platform's keyword matches.
      if (offer.keyword_keys.indexOf(filters.keyword) === -1) { return false; }
    }
    if (filters.text) {
      var needle = filters.text.toLowerCase();
      var pref = offer.preference || {};
      var haystack = [offer.title, offer.company, offer.location]
        .concat(pref.evidence || [])
        .concat([pref.reason || ""])
        .join(" ").toLowerCase();
      if (haystack.indexOf(needle) === -1) { return false; }
    }
    return true;
  }

  function compare(a, b) {
    if (filters.sort === "fresh") {
      var ageA = (a.age_days === null) ? Infinity : a.age_days;
      var ageB = (b.age_days === null) ? Infinity : b.age_days;
      if (ageA !== ageB) { return ageA - ageB; }
    }
    var scoreA = (a.score === null) ? -Infinity : a.score;
    var scoreB = (b.score === null) ? -Infinity : b.score;
    if (scoreA !== scoreB) { return scoreB - scoreA; }
    return a.id - b.id;
  }

  // The header status is one line built only from payload.status. Each part is a
  // span so the signal colour marks exactly the two failure states, never the rest.
  function renderStatusLine() {
    var host = document.getElementById("status-line");
    if (!host) { return; }
    host.textContent = "";
    var status = payload.status || {};
    var failed = status.failed_runs || {};
    var totalFailed = (Number(failed.searches) || 0) + (Number(failed.details) || 0);
    var newJobs = Number(status.new_jobs) || 0;
    var newScored = Number(status.new_scored) || 0;
    var pending = status.pending_details || {};
    var pendingKeys = Object.keys(pending).filter(function (source) {
      return (Number(pending[source]) || 0) > 0;
    });
    var parts = [];
    parts.push([status.last_sweep_at
      ? "Último barrido " + displayTimestamp(status.last_sweep_at)
      : "Sin barridos", ""]);
    parts.push([newJobs + " nuevas", ""]);
    if (newScored < newJobs) {
      parts.push([(newJobs - newScored) + " sin evaluar", "bad"]);
    }
    if (pendingKeys.length) {
      parts.push(["Detalles pendientes: " + pendingKeys.map(function (source) {
        return source + " " + pending[source];
      }).join(" · "), ""]);
    }
    parts.push([totalFailed + " fallos", totalFailed > 0 ? "bad" : "calm"]);
    parts.forEach(function (part, index) {
      if (index > 0) { host.appendChild(document.createTextNode(" · ")); }
      host.appendChild(el("span", part[1] || null, part[0]));
    });
  }

  function bindTabs() {
    var tabs = document.querySelectorAll('[role="tab"]');
    var panels = document.querySelectorAll('[role="tabpanel"]');
    function select(tab) {
      for (var i = 0; i < tabs.length; i++) {
        tabs[i].setAttribute("aria-selected", tabs[i] === tab ? "true" : "false");
      }
      for (var j = 0; j < panels.length; j++) {
        panels[j].hidden = panels[j].id !== tab.getAttribute("aria-controls");
      }
    }
    for (var k = 0; k < tabs.length; k++) {
      tabs[k].addEventListener("click", function () { select(this); });
    }
  }

  function bindBackToTop() {
    var button = document.getElementById("back-to-top");
    if (!button) { return; }
    function sync() { button.hidden = window.scrollY <= window.innerHeight; }
    button.addEventListener("click", function () { window.scrollTo(0, 0); });
    window.addEventListener("scroll", sync, { passive: true });
    sync();
  }

  function renderFeedback(offer) {
    var box = el("span", "feedback");
    // The accessible names carry the row's own title/company, so a screen-reader
    // user can tell one offer's feedback controls from another.
    var offerLabel = offer.title + " \u2014 " + offer.company;
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "Mi decisión sobre la oferta: " + offerLabel);
    box.title = "Se guarda solo en este navegador; no postula ni escribe en la base de datos.";
    var entry = feedback[offer.id] || { decision: "", reason: "" };
    var controls = el("div", "feedback-controls");
    controls.setAttribute("role", "group");
    controls.setAttribute("aria-label", "Marca esta oferta: " + offerLabel);
    FEEDBACK_DECISIONS.forEach(function (decision) {
      var decisionText = labelFrom(FEEDBACK_LABELS, decision, decision);
      var visibleText = decision === "apply" ? "\u2713 aplicar" : "\u2715 descartar";
      var button = el("button", "mini decision-btn", visibleText);
      button.type = "button";
      button.setAttribute("data-decision", decision);
      button.title = "Marca esta oferta como: " + decisionText;
      button.setAttribute("aria-pressed", entry.decision === decision ? "true" : "false");
      if (entry.decision === decision) { button.classList.add("selected"); }
      button.addEventListener("click", function () {
        entry.decision = (entry.decision === decision) ? "" : decision;
        entry.updated_at = new Date().toISOString();
        feedback[offer.id] = entry;
        var stored = saveFeedback();
        // Confirm after the marker lands on the row that is still rendered.
        syncFeedback(offer.id);
        if (stored) { confirmSaved(offer.id); }
      });
      controls.appendChild(button);
    });
    box.appendChild(controls);
    var reason = el("textarea");
    reason.placeholder = "Motivo opcional, máx. " + MAX_REASON_CHARS +
      " caracteres (se guarda en este navegador)";
    reason.maxLength = MAX_REASON_CHARS;
    reason.setAttribute("aria-label", "Motivo opcional de la decisión");
    reason.value = entry.reason || "";
    reason.hidden = !entry.decision;
    var timer = null;
    reason.addEventListener("input", function () {
      if (timer) { window.clearTimeout(timer); }
      timer = window.setTimeout(function () {
        entry.reason = reason.value;
        entry.updated_at = new Date().toISOString();
        feedback[offer.id] = entry;
        saveFeedback();
        syncFeedback(offer.id);
      }, 300);
    });
    box.appendChild(reason);
    var state = el("span", "decision-state");
    if (isLegacyFeedback(entry.decision)) { state.classList.add("legacy"); }
    // The text lives in its own node so the transient save confirmation can be
    // appended next to it without being wiped by the next state update.
    state.appendChild(el("span", "decision-state-text", decisionStateText(entry.decision)));
    box.appendChild(state);
    return box;
  }

  // One short, self-removing confirmation per mark. It is a polite live region, so
  // it is announced once; clicking another offer replaces it instead of stacking
  // toasts, and nothing is shown when storage already refused the write.
  var savedTimer = null;
  function confirmSaved(jobId) {
    var existing = document.querySelectorAll(".decision-saved");
    for (var i = 0; i < existing.length; i++) { existing[i].remove(); }
    var state = document.querySelector('.row[data-job-id="' + jobId + '"] .decision-state');
    if (!state) { return; }
    var marker = el("span", "decision-saved", "Guardado en este navegador");
    marker.setAttribute("role", "status");
    state.appendChild(marker);
    if (savedTimer) { window.clearTimeout(savedTimer); }
    // Fade just before removal; the 150 ms opacity transition ends as it goes.
    window.setTimeout(function () { marker.classList.add("fading"); }, 2450);
    savedTimer = window.setTimeout(function () { marker.remove(); }, 2600);
  }

  function syncFeedback(jobId) {
    refreshFeedbackCount();
    var entry = feedback[jobId] || {};
    var row = document.querySelector('.row[data-job-id="' + jobId + '"]');
    if (!row) { return; }
    row.classList.toggle("reviewed", Boolean(entry.decision));
    var buttons = row.querySelectorAll(".feedback-controls .decision-btn");
    FEEDBACK_DECISIONS.forEach(function (decision, index) {
      if (!buttons[index]) { return; }
      var selected = entry.decision === decision;
      buttons[index].classList.toggle("selected", selected);
      buttons[index].setAttribute("aria-pressed", selected ? "true" : "false");
    });
    var state = row.querySelector(".decision-state-text");
    if (state) {
      state.textContent = decisionStateText(entry.decision);
    }
    var stateBox = row.querySelector(".decision-state");
    if (stateBox) { stateBox.classList.toggle("legacy", isLegacyFeedback(entry.decision)); }
    var reason = row.querySelector(".foot textarea");
    if (reason) { reason.hidden = !entry.decision; }
  }

  function isReviewed(offer) {
    return Boolean(feedback[offer.id] && feedback[offer.id].decision);
  }

  function scoreStateText(status) {
    if (status === "error" || status === "parse_error") { return "sin evaluar (error)"; }
    if (status === "needs_more_info") { return "sin evaluar (falta información)"; }
    if (status === "not_configured") { return "sin evaluar (sin evaluador)"; }
    return "sin evaluar";
  }

  function scoreBand(score) {
    if (score >= 70) { return "s-hi"; }
    if (score >= 50) { return "s-mid"; }
    return "s-lo";
  }

  // The preference is a separate axis: only a real FAIL/FLAG earns a line, and
  // it is prepended so it cannot be missed among the model's own findings.
  function preferenceFirst(offer, outcome) {
    var preference = offer.preference || {};
    if (preference.outcome !== outcome) { return null; }
    var text = "Preferencia: " + (preference.reason || "");
    if (preference.evidence && preference.evidence.length) {
      text += " (" + preference.evidence.join(", ") + ")";
    }
    return text;
  }

  function renderList(title, items, className, firstItem) {
    var box = el("div", className);
    box.appendChild(el("h3", null, title));
    var list = el("ul");
    if (firstItem) { list.appendChild(el("li", null, firstItem)); }
    var values = Array.isArray(items) ? items : [];
    if (values.length) {
      values.forEach(function (item) { list.appendChild(el("li", null, item)); });
    } else if (!firstItem) {
      list.appendChild(el("li", null, "\u2014"));
    }
    box.appendChild(list);
    return box;
  }

  var MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic"];

  function evaluatedDate(value) {
    if (typeof value !== "string") { return null; }
    var match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
    if (!match) { return null; }
    var month = parseInt(match[2], 10) - 1;
    if (month < 0 || month > 11) { return null; }
    return parseInt(match[3], 10) + " " + MONTHS_ES[month];
  }

  function provenanceText(offer) {
    var parts = [];
    if (offer.model) {
      var segments = String(offer.model).split("/");
      var model = segments[segments.length - 1];
      if (model) { parts.push(model); }
    }
    if (offer.confidence !== null && offer.confidence !== undefined) {
      parts.push("confianza " + String(offer.confidence).replace(".", ","));
    }
    var date = evaluatedDate(offer.evaluated_at);
    if (date) { parts.push("evaluada " + date); }
    return parts.join(" \u00b7 ");
  }

  function renderRow(offer) {
    var item = el("li");
    var details = el("details", "row");
    details.setAttribute("data-job-id", String(offer.id));
    if (isReviewed(offer)) { details.classList.add("reviewed"); }

    var summary = el("summary");
    var scoreNode;
    if (offer.score !== null) {
      var rounded = Math.round(offer.score);
      var staleClass = offer.needs_rescore ? " stale" : "";
      scoreNode = el("div", "score " + scoreBand(offer.score) + staleClass, rounded);
      scoreNode.setAttribute(
        "aria-label",
        "Puntaje " + rounded + " sobre 100"
          + (offer.needs_rescore ? ", re-score pendiente" : "")
      );
      var track = el("small");
      var fill = el("i");
      fill.style.width = String(offer.score) + "%";
      track.appendChild(fill);
      scoreNode.appendChild(track);
      if (offer.needs_rescore) {
        var stale = el("span", "stale", "Re-score pendiente");
        stale.setAttribute("title", "El modelo, el prompt, el perfil o el texto de la oferta cambiaron desde este puntaje");
        scoreNode.appendChild(stale);
      }
    } else {
      // A scoreless offer waiting for its description says so instead of the
      // generic "sin evaluar": the state will change on its own after the next
      // detail batch, and no score was ever skipped by mistake.
      scoreNode = el("div", "score score-state", offer.pending_details
        ? "Pendiente de detalles"
        : scoreStateText(offer.evaluation_status));
    }
    summary.appendChild(scoreNode);

    var titleBox = el("div");
    titleBox.appendChild(el("div", "t", offer.title));
    var subtitle = offer.company;
    if (offer.location) { subtitle += " \u00b7 " + offer.location; }
    titleBox.appendChild(el("div", "c", subtitle));
    summary.appendChild(titleBox);

    var chips = el("div", "chips");
    var recommendation = effectiveRecommendation(offer);
    if (recommendation === "apply") {
      chips.appendChild(el("span", "chip rec-apply", "aplicar"));
    } else if (recommendation === "consider") {
      chips.appendChild(el("span", "chip rec-consider", "considerar"));
    } else if (recommendation === "skip") {
      chips.appendChild(el("span", "chip", "descartar"));
    }
    if (offer.is_new) { chips.appendChild(el("span", "chip new", "nueva")); }
    var preference = offer.preference || {};
    if (preference.outcome === "flag") {
      var flag = el("span", "chip flag", "\u26a0 flag");
      flag.title = preference.reason || "";
      chips.appendChild(flag);
    } else if (preference.outcome === "fail") {
      var veto = el("span", "chip veto", "veto");
      veto.title = preference.reason || "";
      chips.appendChild(veto);
    }
    // Shown only when the rail toggle reveals filtered offers; the reason is
    // part of the row so a revealed offer explains why it is normally hidden.
    if (offer.seniority_filter) {
      var seniorityChip = el("span", "chip seniority", offer.seniority_filter);
      seniorityChip.title = "Oculta por defecto: " + offer.seniority_filter;
      chips.appendChild(seniorityChip);
    }
    summary.appendChild(chips);

    var salaryNode;
    if (offer.salary) {
      salaryNode = el("div", "sal", offer.salary);
      salaryNode.appendChild(el("em", null, "indicado"));
    } else if (offer.salary_est) {
      salaryNode = el("div", "sal est", salaryRangeK(offer.salary_est));
      salaryNode.appendChild(el("em", null, "estimado"));
    } else {
      salaryNode = el("div", "sal unk", "\u2014");
      salaryNode.appendChild(el("em", null, "sin dato"));
    }
    summary.appendChild(salaryNode);

    var age = (offer.age_days === null)
      ? "\u2014" : "hace " + Math.round(offer.age_days) + " d";
    summary.appendChild(el("div", "age", age));

    var chev = el("div", "chev", "\u203a");
    chev.setAttribute("aria-hidden", "true");
    summary.appendChild(chev);

    details.appendChild(summary);
    var bodyBuilt = false;
    details.addEventListener("toggle", function () {
      if (!details.open || bodyBuilt) { return; }
      details.appendChild(renderBody(offer));
      bodyBuilt = true;
    });
    item.appendChild(details);
    return item;
  }

  function renderBody(offer) {
    var dims = Array.isArray(offer.dimensions) ? offer.dimensions : [];
    var body = el("div", dims.length ? "body" : "body nodims");

    var left = el("div");
    left.appendChild(el("h3", null, "Justificación del modelo"));
    if (offer.rationale) {
      left.appendChild(el("p", "why", offer.rationale));
    } else {
      left.appendChild(el("p", "why muted", "sin justificación"));
    }
    if (offer.salary || offer.salary_est) {
      left.appendChild(el("h3", null, "Salario · bruto anual"));
      var salaryBox = el("dl", "salbox");
      if (offer.salary) {
        salaryBox.appendChild(el("dt", null, "indicado"));
        salaryBox.appendChild(el("dd", "sal-stated", offer.salary));
      }
      if (offer.salary_est) {
        salaryBox.appendChild(el("dt", null, "estimado"));
        var estimateNode = el("dd", "sal-est", salaryEstimateText(offer.salary_est));
        var tier = offer.salary_est[3];
        var basis = offer.salary_est[4];
        if (tier) {
          estimateNode.appendChild(el("div", "tier",
            EMPLOYER_TIER_LABELS[tier] || tier));
        }
        if (basis) {
          estimateNode.appendChild(el("div", "basis", basis));
        }
        salaryBox.appendChild(estimateNode);
      }
      left.appendChild(salaryBox);
    }
    if (!dims.length) {
      left.appendChild(el("p", "nodims-note", "sin desglose en esta evaluación"));
    }
    body.appendChild(left);

    if (dims.length) {
      var right = el("div");
      right.appendChild(el("h3", null, "Desglose por dimensiones"));
      var dimsBox = el("div", "dims");
      var weightSum = 0;
      dims.forEach(function (dim) { weightSum += Number(dim.weight) || 0; });
      dims.forEach(function (dim) {
        var row = el("div", "dim");
        var key = el("div", "k", dim.name);
        if (weightSum !== 0) {
          key.appendChild(el("em", null,
            "peso " + Math.round(dim.weight / weightSum * 100) + "%"));
        }
        row.appendChild(key);
        var bar = el("div", "bar");
        var fill = el("i");
        fill.style.width = String(dim.score) + "%";
        bar.appendChild(fill);
        row.appendChild(bar);
        row.appendChild(el("div", "v", Math.round(dim.score)));
        if (dim.rationale) { row.appendChild(el("p", null, dim.rationale)); }
        (dim.evidence || []).forEach(function (evidence) {
          row.appendChild(el("q", null, evidence));
        });
        dimsBox.appendChild(row);
      });
      right.appendChild(dimsBox);
      body.appendChild(right);
    }

    var hasLists = ["strengths", "gaps", "blockers", "unknowns"].some(function (key) {
      return Array.isArray(offer[key]) && offer[key].length > 0;
    });
    var blockerFirst = preferenceFirst(offer, "fail");
    var unknownFirst = preferenceFirst(offer, "flag");
    // Most stored evaluations carry no lists; four columns of dashes would only
    // add height, so the strip appears when there is something to read.
    if (hasLists || blockerFirst || unknownFirst) {
      var lists = el("div", "lists");
      lists.appendChild(renderList("Puntos fuertes", offer.strengths, null, null));
      lists.appendChild(renderList("Carencias", offer.gaps, null, null));
      lists.appendChild(renderList("Bloqueos", offer.blockers, "blk", blockerFirst));
      lists.appendChild(renderList("Incógnitas", offer.unknowns, null, unknownFirst));
      body.appendChild(lists);
    }

    // Company dossiers are written by the optional ``dossier`` command. The
    // Markdown arrives in the payload and is set with ``textContent`` (via
    // ``el``), so the report stays self-contained and safe over file://.
    if (offer.dossier) {
      var dossier = el("details", "dossier");
      dossier.appendChild(el("summary", null, "Dossier de la empresa"));
      dossier.appendChild(el("pre", "dossier-text", offer.dossier));
      body.appendChild(dossier);
    }

    var foot = el("div", "foot");
    foot.appendChild(el("span", null, provenanceText(offer)));
    if (offer.imported_review && offer.imported_review.reviewed) {
      foot.appendChild(el("span", null,
        "decisión importada: " + feedbackLabel(offer.imported_review.decision)));
    }
    var feedbackBox = renderFeedback(offer);
    foot.appendChild(feedbackBox);
    var href = safeHttpUrl(offer.url);
    if (href) {
      var link = el("a", null, "Ver la oferta \u2197");
      link.href = href;
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      foot.appendChild(link);
    }
    // The reason opens under the whole footer line, not squeezed between buttons.
    var reasonBox = feedbackBox.querySelector("textarea");
    if (reasonBox) { foot.appendChild(reasonBox); }
    body.appendChild(foot);
    return body;
  }

  var PAGE_SIZE = 50;
  var matchedOffers = [];
  var shownOffers = 0;

  function renderBatch() {
    var container = document.getElementById("offers");
    var end = Math.min(shownOffers + PAGE_SIZE, matchedOffers.length);
    for (var i = shownOffers; i < end; i++) {
      container.appendChild(renderRow(matchedOffers[i]));
    }
    shownOffers = end;
    document.getElementById("show-more").hidden = shownOffers >= matchedOffers.length;
  }

  function renderOffers() {
    var container = document.getElementById("offers");
    container.textContent = "";
    matchedOffers = payload.offers.filter(matches).sort(compare);
    shownOffers = 0;
    renderBatch();
    document.getElementById("empty-state").hidden = matchedOffers.length !== 0;
    var resultCount = document.getElementById("result-count");
    resultCount.textContent = (matchedOffers.length === payload.offers.length)
      ? matchedOffers.length + " ofertas"
      : matchedOffers.length + " de " + payload.offers.length + " ofertas";
    var offersTab = document.getElementById("tab-offers-count");
    if (offersTab) { offersTab.textContent = String(payload.offers.length); }
  }

  // The rail shows the hidden count even while the offers are hidden, so the
  // reader knows the list is filtered and can reveal it from the keyboard.
  function refreshSeniorityFilterState() {
    var button = document.getElementById("filter-show-seniority");
    var count = Number((payload.summary && payload.summary.seniority_filtered) || 0);
    button.setAttribute("aria-pressed", filters.showSeniority ? "true" : "false");
    button.textContent =
      (filters.showSeniority ? "Ocultar " : "Mostrar ") + count + " ocultas por seniority";
  }

  // The toolbar mirrors every control's state, so a filter is never active
  // without the control showing it.
  function refreshFilterState() {
    document.getElementById("filter-new").setAttribute(
      "aria-pressed", filters.isNew ? "true" : "false");
    var buttons = document.getElementById("filter-recommendation").querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) {
      var selected = buttons[i].getAttribute("data-value") === filters.recommendation;
      buttons[i].setAttribute("aria-pressed", selected ? "true" : "false");
    }
    refreshSeniorityFilterState();
  }

  // Every control that changes the visible set goes through here, so the list and
  // the filter summary can never disagree.
  function applyFilters() {
    renderOffers();
    refreshFilterState();
  }

  // GetManfred stores its whole filter list as one comma-separated "keyword", up to
  // ~300 characters, which would blow up the table column and the filter select.
  // Show the first two terms plus a count; the full list stays in the tooltip.
  var providerLabels = {
    linkedin: "LinkedIn", tecnoempleo: "Tecnoempleo", getmanfred: "GetManfred"
  };

  function shortKeyword(keyword) {
    var parts = String(keyword).split(",").map(function (part) { return part.trim(); })
      .filter(function (part) { return part; });
    if (parts.length <= 2) { return parts.join(", "); }
    return parts.slice(0, 2).join(", ") + " +" + (parts.length - 2);
  }

  function renderKeywordTable() {
    var rows = payload.keywords.concat(payload.retired_keywords || []);
    document.getElementById("tab-keywords-count").textContent = String(rows.length);
    var table = document.getElementById("keyword-table");
    var empty = document.getElementById("keyword-empty");
    if (!rows.length) {
      table.hidden = true;
      empty.hidden = false;
      return;
    }
    var body = table.querySelector("tbody");
    body.textContent = "";
    rows.forEach(function (item) {
      var row = el("tr");
      if (item.retired) { row.className = "retired"; }
      var keywordCell = el("td", "kw", shortKeyword(item.keyword));
      keywordCell.title = item.keyword;
      row.appendChild(keywordCell);
      row.appendChild(el("td", null, providerLabels[item.provider] || item.provider));
      row.appendChild(el("td", "num", String(item.seen_jobs)));
      row.appendChild(el("td", "num", String(item.unique_jobs)));
      // A missing average is stated as "sin dato", never rounded to a number.
      row.appendChild(el("td", "num", (item.avg_score === null || item.avg_score === undefined)
        ? "sin dato" : String(Math.round(item.avg_score))));
      row.appendChild(el("td", "num", String(item.high_score_jobs)));
      row.appendChild(el("td", "num", String(item.human_apply)));
      body.appendChild(row);
    });
  }

  function populateKeywordFilter() {
    var select = document.getElementById("filter-keyword");
    payload.keywords.forEach(function (item) {
      // The option text keeps the platform visible; its value is the
      // provider|keyword series key used by the filter predicate.
      var provider = providerLabels[item.provider] || item.provider;
      var option = el("option", null, shortKeyword(item.keyword) + " \u00b7 " + provider);
      option.title = item.keyword;
      option.value = item.source_key || item.keyword;
      select.appendChild(option);
    });
  }

  function exportFeedback() {
    var document_ = {
      schema: payload.feedback_schema,
      project_id: payload.project_id,
      exported_at: new Date().toISOString(),
      source: "jobscout-dashboard",
      note: "Feedback manual local. No se envía a ningún servicio ni a LinkedIn.",
      feedback: feedback
    };
    var blob = new Blob([JSON.stringify(document_, null, 2)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = el("a");
    link.href = url;
    link.download = "jobscout-feedback.json";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    setToast("Feedback exportado. Conserve el archivo como respaldo.");
  }

  function resetFilters() {
    filters = { text: "", recommendation: "", isNew: false, showSeniority: false, minScore: null, keyword: "", sort: "score" };
    document.getElementById("filter-text").value = "";
    document.getElementById("filter-min-score").value = "0";
    document.getElementById("filter-keyword").value = "";
    document.getElementById("filter-sort").value = "score";
    applyFilters();
    document.getElementById("filter-text").focus();
  }

  function bindFilters() {
    document.getElementById("filter-text").addEventListener("input", function (event) {
      filters.text = event.target.value.trim();
      applyFilters();
    });
    document.getElementById("filter-recommendation").addEventListener("click", function (event) {
      var button = event.target.closest("button");
      if (!button) { return; }
      filters.recommendation = button.getAttribute("data-value") || "";
      applyFilters();
    });
    document.getElementById("filter-new").addEventListener("click", function () {
      filters.isNew = !filters.isNew;
      applyFilters();
    });
    document.getElementById("filter-show-seniority").addEventListener("click", function () {
      filters.showSeniority = !filters.showSeniority;
      applyFilters();
    });
    document.getElementById("filter-min-score").addEventListener("change", function (event) {
      var parsed = parseInt(event.target.value, 10);
      // The zero option means "no threshold", so it must not narrow the list.
      filters.minScore = parsed > 0 ? parsed : null;
      applyFilters();
    });
    document.getElementById("filter-keyword").addEventListener("change", function (event) {
      filters.keyword = event.target.value;
      applyFilters();
    });
    document.getElementById("filter-sort").addEventListener("change", function (event) {
      filters.sort = event.target.value;
      applyFilters();
    });
    document.getElementById("empty-reset").addEventListener("click", resetFilters);
    document.getElementById("show-more").addEventListener("click", renderBatch);
    document.getElementById("export-feedback").addEventListener("click", exportFeedback);
  }

  renderStatusLine();
  bindTabs();
  bindBackToTop();
  populateKeywordFilter();
  renderKeywordTable();
  bindFilters();
  refreshFeedbackCount();
  applyFilters();
})();

// ---------------------------------------------------------------------------
// Keyword table: reorder the rows already in the DOM by clicking a header. The
// analytics values are computed in Python and never change here; only the display
// order of the rendered rows does, so sorting costs a handful of comparisons.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  function sortCell(text) {
    var cleaned = text.replace("%", "").trim();
    var parsed = parseFloat(cleaned);
    var numeric = (cleaned !== "" && cleaned !== "sin dato" && !isNaN(parsed));
    return { number: numeric ? parsed : null, text: cleaned };
  }

  // A value the stored data cannot support ("sin dato") sorts last in both
  // directions instead of pretending to be a low number.
  function compareCells(left, right, descending) {
    var a = left.number;
    var b = right.number;
    if (a !== null && b !== null) {
      return descending ? (b - a) : (a - b);
    }
    if (a === null && b === null) {
      var text = left.text.localeCompare(right.text, "es");
      return descending ? -text : text;
    }
    return (a === null) ? 1 : -1;
  }

  function sortRows(table, columnIndex, direction) {
    var body = table.querySelector("tbody");
    if (!body) { return; }
    var rows = Array.prototype.slice.call(body.querySelectorAll("tr"));
    var descending = direction === "descending";
    rows.sort(function (left, right) {
      return compareCells(
        sortCell(left.cells[columnIndex].textContent),
        sortCell(right.cells[columnIndex].textContent),
        descending
      );
    });
    rows.forEach(function (row) { body.appendChild(row); });
  }

  function makeSortable(table) {
    var headers = table.querySelectorAll("thead th");
    for (var index = 0; index < headers.length; index++) {
      makeHeaderSortable(table, headers, index);
    }
    // Open with the best-scoring searches first; aria-sort carries that order so
    // the arrow is never the only sign of which column is sorted.
    for (var i = 0; i < headers.length; i++) {
      var initial = headers[i].getAttribute("data-initial-sort");
      if (initial) {
        headers[i].setAttribute("aria-sort", initial);
        sortRows(table, i, initial);
      }
    }
  }

  function makeHeaderSortable(table, headers, columnIndex) {
    var header = headers[columnIndex];
    var label = header.textContent;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "sort-btn";
    button.textContent = label;
    button.title = "Ordenar por " + label;
    header.textContent = "";
    // aria-sort is mirrored on the header cell, so the direction never rests on the
    // arrow glyph alone. "none" is the honest state for a column that is not sorted.
    header.setAttribute("aria-sort", "none");
    header.appendChild(button);
    button.addEventListener("click", function () {
      var descending = header.getAttribute("aria-sort") === "ascending";
      for (var i = 0; i < headers.length; i++) {
        headers[i].setAttribute("aria-sort", "none");
      }
      header.setAttribute("aria-sort", descending ? "descending" : "ascending");
      sortRows(table, columnIndex, descending ? "descending" : "ascending");
    });
  }

  var keywordTable = document.getElementById("keyword-table");
  if (keywordTable) {
    var keywordBody = keywordTable.querySelector("tbody");
    if (keywordBody && keywordBody.querySelector("tr")) {
      makeSortable(keywordTable);
    }
  }
})();
"""


def render_dashboard_html(payload: dict[str, Any]) -> str:
    """Render the self-contained HTML report for a payload.

    Every value interpolated outside the JSON block is HTML-escaped, and the JSON
    block itself escapes ``<``, ``>``, and ``&`` so it cannot close its script
    tag early. Untrusted values reach the DOM only through ``textContent``.

    Substitution is a single pass (see :func:`_render_template`), so a literal
    value equal to ``__DATA__`` or ``__SCRIPT__`` is written once as data and can
    never be swapped for the payload or the script block.
    """
    generated = html.escape(str(payload.get("generated_at") or ""))
    label = payload.get("database_label")
    summary = payload.get("summary") or {}
    limits = payload.get("limits") or {}
    meta_parts = [f"Generado: {generated}"]
    if label:
        meta_parts.append(f"Base de datos: {html.escape(str(label))}")
    scope = (
        f"Alcance: {int(summary.get('jobs_shown') or 0)} de "
        f"{int(summary.get('active_jobs') or 0)} ofertas activas"
    )
    if limits.get("top") is not None:
        scope += f" (límite top {int(limits['top'])})"
    meta_parts.append(scope)
    meta = " &middot; ".join(meta_parts)
    warnings = payload.get("warnings") or []
    warnings_html = ""
    if warnings:
        paragraphs = "".join(f"<p>{html.escape(str(item))}</p>" for item in warnings)
        warnings_html = f'<div class="report-warnings">{paragraphs}</div>'
    heading = str(label) if label else "informe local"
    title = html.escape(f"Panel de jobscout \u2014 {heading}")
    return _render_template(
        _HTML_TEMPLATE,
        {
            "TITLE": title,
            "FONT_FACES": _font_faces_css(),
            "STYLE": _STYLE,
            "META": meta,
            "WARNINGS": warnings_html,
            "DATA": embed_json(payload),
            "SCRIPT": _SCRIPT,
        },
    )


# ---------------------------------------------------------------------------
# Entry point used by the CLI
# ---------------------------------------------------------------------------


def generate_dashboard(
    db_path: str | Path,
    output: str | Path = DEFAULT_OUTPUT,
    *,
    top: int | None = DEFAULT_TOP,
    stale_job_ids: Collection[int] | None = None,
) -> DashboardSummary:
    """Read the database and write the self-contained HTML report.

    ``top=None`` (the default) embeds every active offer. Only the output's
    parent directory is created. The database is never opened for writing, so an
    unreadable or missing database raises :class:`DashboardError` before any file
    is produced. An empty output path, an out-of-range explicit ``top``, and an
    output that resolves onto the database are all refused up front, and any
    filesystem failure while writing becomes a :class:`DashboardError` instead
    of a raw ``OSError``.
    """
    if output is None or not str(output).strip():
        raise DashboardError("the output path must not be empty")
    top = _validate_top(top)
    target = Path(output)
    _reject_output_over_database(db_path, target)

    conn = open_readonly_database(db_path)
    try:
        _require_schema(conn, db_path)
        payload = build_payload(
            conn,
            top=top,
            project_id=project_key(db_path),
            database_label=Path(db_path).name,
            db_path=db_path,
            stale_job_ids=stale_job_ids,
        )
    except sqlite3.DatabaseError as exc:
        raise DashboardError(f"database at {db_path} could not be read: {exc}") from exc
    finally:
        conn.close()

    document = render_dashboard_html(payload)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Force LF so the report is byte-identical across platforms and the
        # reported size matches the encoded document exactly.
        target.write_text(document, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise DashboardError(f"the report at {target} could not be written: {exc}") from exc

    return DashboardSummary(
        output=target,
        offers=len(payload["offers"]),
        scored=int(payload["summary"]["scored"]),
        # The CLI reports every analyzed series (retired included), matching
        # ``payload['summary']['keywords']``; the table itself shows active ones.
        keywords=int(payload["summary"]["keywords"]),
        bytes_written=len(document.encode("utf-8")),
        warnings=list(payload["warnings"]),
    )


__all__ = [
    "DASHBOARD_SCHEMA",
    "DASHBOARD_FEEDBACK_DECISIONS",
    "DASHBOARD_LEGACY_FEEDBACK_DECISIONS",
    "DEFAULT_OUTPUT",
    "DEFAULT_TOP",
    "DashboardError",
    "DashboardSummary",
    "FEEDBACK_SCHEMA",
    "MAX_TOP",
    "build_payload",
    "embed_json",
    "generate_dashboard",
    "open_readonly_database",
    "project_key",
    "render_dashboard_html",
]
