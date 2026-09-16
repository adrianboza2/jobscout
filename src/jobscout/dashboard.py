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
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .db import connect_readonly
from .models import _loads
from .timeutil import parse_datetime, utcnow, utcnow_iso

#: Default report location. ``/reports/`` is git-ignored, so a generated report
#: is never published by accident.
DEFAULT_OUTPUT = Path("reports/jobscout-dashboard.html")

#: Default number of ranked offers embedded in the report.
DEFAULT_TOP = 25

#: Hard cap on ``--top``. The whole payload is embedded in one HTML file, so an
#: unbounded value would let a typo build a report large enough to freeze a
#: browser. Raising it is a deliberate, reviewable change.
MAX_TOP = 500

#: Payload schema written into the HTML and used to shape the localStorage key.
DASHBOARD_SCHEMA = "jobscout.dashboard.v1"
FEEDBACK_SCHEMA = "jobscout.feedback.v1"

#: Rationale longer than this is truncated in the report so one verbose model
#: response cannot dominate the page. The stored evaluation keeps the full text.
MAX_RATIONALE_CHARS = 800

#: Recommendation values shown in the UI. ``unscored`` is not a stored value; it
#: is the display bucket for jobs with no successful evaluation.
RECOMMENDATION_ORDER: tuple[str, ...] = ("apply", "consider", "skip", "need_more_info")

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
    missing = {"jobs", "search_runs", "search_run_items", "latest_evaluations"} - names
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


def _validate_top(top: int) -> int:
    """Return ``top`` as an int, or fail with a user-facing error.

    A non-positive value cannot be expressed as a SQL ``LIMIT`` and a huge value
    would produce an unusable report, so both are refused before any file is
    touched.
    """
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


def _age_days(value: str | None, *, now: datetime) -> float | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return round((now - parsed).total_seconds() / 86400.0, 2)


def _concisify(text: str | None) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= MAX_RATIONALE_CHARS:
        return collapsed
    return collapsed[: MAX_RATIONALE_CHARS - 1].rstrip() + "\u2026"


def _fetch_active_jobs(
    conn: sqlite3.Connection, *, top: int
) -> list[sqlite3.Row]:
    """Active jobs ranked by latest successful score, then freshness.

    The ``latest_evaluations`` view is the same source of truth ``rank`` and
    ``list`` use, so the dashboard never disagrees with the CLI.
    """
    return conn.execute(
        """
        SELECT j.id AS id,
               j.title AS title,
               j.company AS company,
               j.location AS location,
               j.url AS url,
               j.source AS source,
               j.posted_at AS posted_at,
               j.first_seen_at AS first_seen_at,
               j.last_seen_at AS last_seen_at,
               j.content_status AS content_status,
               j.sighting_count AS sighting_count,
               j.seniority AS seniority,
               j.employment_type AS employment_type,
               e.overall_score AS score,
               e.recommendation AS recommendation,
               e.confidence AS confidence,
               e.strengths_json AS strengths_json,
               e.gaps_json AS gaps_json,
               e.blockers_json AS blockers_json,
               e.unknowns_json AS unknowns_json,
               e.rationale AS rationale,
               e.scorer AS scorer,
               e.model AS model,
               e.created_at AS evaluated_at
        FROM jobs j
        LEFT JOIN latest_evaluations e ON e.job_id = j.id
        WHERE j.is_active = 1
        ORDER BY (e.overall_score IS NULL),
                 e.overall_score DESC,
                 COALESCE(e.confidence, 0) DESC,
                 j.last_seen_at DESC,
                 j.id DESC
        LIMIT ?
        """,
        (int(top),),
    ).fetchall()


def _fetch_job_keywords(
    conn: sqlite3.Connection, job_ids: Sequence[int]
) -> dict[int, list[dict[str, Any]]]:
    """Keyword provenance per job: which search run observed it, and at what rank.

    LinkedIn result order is *evidence*, not truth: the observed rank is kept so
    the reader can see where a posting surfaced, never as a quality signal.
    """
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        f"""
        SELECT i.job_id AS job_id,
               r.query AS keyword,
               r.provider AS provider,
               r.location AS run_location,
               i.rank_in_run AS rank_in_run,
               i.decision AS decision,
               i.created_at AS seen_at
        FROM search_run_items i
        JOIN search_runs r ON r.id = i.run_id
        WHERE i.job_id IN ({placeholders})
          AND r.query IS NOT NULL
          AND TRIM(r.query) <> ''
        ORDER BY i.job_id, i.id
        """,
        [int(job_id) for job_id in job_ids],
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["job_id"]), []).append(
            {
                "keyword": str(row["keyword"]),
                "rank": int(row["rank_in_run"] or 0),
                "decision": str(row["decision"]),
                "provider": str(row["provider"] or ""),
                "seen_at": row["seen_at"],
            }
        )
    return grouped


def _fetch_keyword_stats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Sightings, yield, and score quality per source keyword.

    ``sightings`` counts observations (a run can observe one job more than once),
    ``unique_jobs`` counts distinct postings, and the score columns come from the
    latest successful evaluation of each distinct job, so a keyword that surfaces
    the same posting twice is not double-counted in the averages.

    Only active jobs count, matching the offers list and the summary. A keyword
    that only ever surfaced deactivated postings must not look productive.
    """
    sighting_rows = conn.execute(
        """
        SELECT r.query AS keyword,
               COUNT(*) AS sightings,
               COUNT(DISTINCT i.job_id) AS unique_jobs,
               SUM(CASE WHEN i.decision <> 'new' THEN 1 ELSE 0 END) AS duplicate_sightings
        FROM search_run_items i
        JOIN search_runs r ON r.id = i.run_id
        JOIN jobs j ON j.id = i.job_id
        WHERE r.query IS NOT NULL AND TRIM(r.query) <> ''
          AND j.is_active = 1
        GROUP BY r.query
        ORDER BY r.query
        """
    ).fetchall()

    score_rows = conn.execute(
        """
        WITH keyword_jobs AS (
            SELECT DISTINCT r.query AS keyword, i.job_id AS job_id
            FROM search_run_items i
            JOIN search_runs r ON r.id = i.run_id
            JOIN jobs j ON j.id = i.job_id
            WHERE r.query IS NOT NULL AND TRIM(r.query) <> ''
              AND j.is_active = 1
        )
        SELECT kj.keyword AS keyword,
               COUNT(*) AS unique_jobs,
               COUNT(e.id) AS scored_jobs,
               AVG(e.overall_score) AS avg_score,
               SUM(CASE WHEN e.recommendation = 'apply' THEN 1 ELSE 0 END) AS apply_count,
               SUM(CASE WHEN e.recommendation = 'consider' THEN 1 ELSE 0 END) AS consider_count
        FROM keyword_jobs kj
        LEFT JOIN latest_evaluations e ON e.job_id = kj.job_id
        GROUP BY kj.keyword
        """
    ).fetchall()
    scores = {str(row["keyword"]): row for row in score_rows}

    stats: list[dict[str, Any]] = []
    for row in sighting_rows:
        keyword = str(row["keyword"])
        score = scores.get(keyword)
        unique_jobs = int(row["unique_jobs"] or 0)
        scored_jobs = int(score["scored_jobs"] or 0) if score is not None else 0
        avg_score = score["avg_score"] if score is not None else None
        stats.append(
            {
                "keyword": keyword,
                "sightings": int(row["sightings"] or 0),
                "unique_jobs": unique_jobs,
                "duplicate_sightings": int(row["duplicate_sightings"] or 0),
                "scored_jobs": scored_jobs,
                "unscored_jobs": max(0, unique_jobs - scored_jobs),
                "avg_score": round(float(avg_score), 2) if avg_score is not None else None,
                "apply_count": int(score["apply_count"] or 0) if score is not None else 0,
                "consider_count": int(score["consider_count"] or 0) if score is not None else 0,
            }
        )
    return stats


def _top_keywords(
    stats: Sequence[dict[str, Any]], *, limit: int = 10
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(top by score quality, top by positive-recommendation yield).

    Both lists only include keywords the data can actually rank: a keyword with
    no scored job is absent from quality, and one with no apply/consider is
    absent from yield.
    """
    quality = sorted(
        (item for item in stats if item["scored_jobs"] > 0 and item["avg_score"] is not None),
        key=lambda item: (-float(item["avg_score"]), -item["unique_jobs"], item["keyword"]),
    )[:limit]
    yield_ranked = sorted(
        (item for item in stats if (item["apply_count"] + item["consider_count"]) > 0),
        key=lambda item: (
            -(item["apply_count"] + item["consider_count"]),
            -(float(item["avg_score"]) if item["avg_score"] is not None else 0.0),
            item["keyword"],
        ),
    )[:limit]
    return quality, yield_ranked


def _summary(
    conn: sqlite3.Connection, offers: Sequence[dict[str, Any]], *, keyword_count: int
) -> dict[str, Any]:
    scored = [offer for offer in offers if offer["score"] is not None]
    counts = {name: 0 for name in RECOMMENDATION_ORDER}
    for offer in scored:
        recommendation = offer["recommendation"]
        if recommendation in counts:
            counts[recommendation] += 1
        else:
            counts["need_more_info"] += 1
    total = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active = 1").fetchone()[0])
    return {
        "active_jobs": total,
        "jobs_shown": len(offers),
        "scored": len(scored),
        "unscored": len(offers) - len(scored),
        "recommendations": counts,
        "keywords": keyword_count,
    }


def _warnings(summary: dict[str, Any], *, keyword_count: int) -> list[str]:
    warnings: list[str] = []
    if summary["active_jobs"] == 0:
        warnings.append(
            "no active jobs are stored yet; ingest a fixture or run a bounded search first"
        )
    if summary["active_jobs"] and summary["scored"] == 0:
        warnings.append(
            "no successful evaluation found; run 'jobscout score --all' "
            "(add --rescore to re-evaluate) before reading scores"
        )
    if summary["unscored"]:
        warnings.append(
            f"{summary['unscored']} selected job(s) have no successful score and are listed last"
        )
    if not keyword_count:
        warnings.append(
            "no keyword provenance recorded; search-run items carry the source keyword "
            "and observed rank, so keyword analytics stay empty until a run is ingested"
        )
    return warnings


def build_payload(
    conn: sqlite3.Connection,
    *,
    top: int = DEFAULT_TOP,
    project_id: str = "",
    database_label: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the serializable dashboard payload from the database.

    Only report-safe fields are included. ``database_label`` is an optional
    human label (a filename, never an absolute path) shown in the header.
    """
    top = _validate_top(top)
    reference = now or utcnow()
    rows = _fetch_active_jobs(conn, top=top)
    keywords_by_job = _fetch_job_keywords(conn, [int(row["id"]) for row in rows])

    offers: list[dict[str, Any]] = []
    for row in rows:
        job_id = int(row["id"])
        posted_at = row["posted_at"]
        first_seen_at = row["first_seen_at"]
        age_days = _age_days(posted_at, now=reference)
        age_source = "posted"
        if age_days is None:
            age_days = _age_days(first_seen_at, now=reference)
            age_source = "first_seen" if age_days is not None else "unknown"
        score = row["score"]
        offers.append(
            {
                "id": job_id,
                "title": _concisify(row["title"]) or "(untitled)",
                "company": _concisify(row["company"]) or "(unknown company)",
                "location": _concisify(row["location"]),
                "url": _safe_url(row["url"]),
                "source": str(row["source"] or ""),
                "posted_at": posted_at,
                "first_seen_at": first_seen_at,
                "last_seen_at": row["last_seen_at"],
                "age_days": age_days,
                "age_source": age_source,
                "sighting_count": int(row["sighting_count"] or 1),
                "seniority": str(row["seniority"] or ""),
                "employment_type": str(row["employment_type"] or ""),
                "content_status": str(row["content_status"] or "unknown"),
                "score": round(float(score), 2) if score is not None else None,
                "recommendation": (
                    str(row["recommendation"])
                    if score is not None and row["recommendation"]
                    else None
                ),
                "confidence": (
                    round(float(row["confidence"]), 3) if row["confidence"] is not None else None
                ),
                "scorer": str(row["scorer"]) if row["scorer"] else "",
                "model": str(row["model"]) if row["model"] else "",
                "evaluated_at": row["evaluated_at"],
                "rationale": _concisify(row["rationale"]),
                "strengths": _string_list(row["strengths_json"]),
                "gaps": _string_list(row["gaps_json"]),
                "blockers": _string_list(row["blockers_json"]),
                "unknowns": _string_list(row["unknowns_json"]),
                "keywords": keywords_by_job.get(job_id, []),
            }
        )

    keyword_stats = _fetch_keyword_stats(conn)
    top_quality, top_yield = _top_keywords(keyword_stats)
    summary = _summary(conn, offers, keyword_count=len(keyword_stats))

    return {
        "schema": DASHBOARD_SCHEMA,
        "feedback_schema": FEEDBACK_SCHEMA,
        "project_id": project_id,
        "database_label": database_label,
        "generated_at": utcnow_iso(),
        "limits": {"top": int(top)},
        "summary": summary,
        "warnings": _warnings(summary, keyword_count=len(keyword_stats)),
        "offers": offers,
        "keywords": keyword_stats,
        "top_quality": top_quality,
        "top_yield": top_yield,
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>__TITLE__</title>
<style>
__STYLE__
</style>
</head>
<body>
<header class="topbar">
  <div>
    <h1>jobscout dashboard</h1>
    <p class="muted" id="report-meta">__META__</p>
  </div>
  <div class="actions">
    <button type="button" id="export-feedback" class="btn primary">Export local feedback (JSON)</button>
    <span class="muted" id="feedback-count">0 reviewed</span>
  </div>
</header>

<section class="notice" id="warnings">__WARNINGS__</section>

<section class="notice privacy">
  <strong>Local only.</strong> Manual feedback below is stored in this browser's
  <code>localStorage</code> under <code>__STORAGE_KEY__</code>. It does not submit an
  application, does not save anything to LinkedIn, and does not write to the SQLite
  database. Use <em>Export local feedback</em> to keep a copy; scores and recommendations
  are produced by the jobscout scorer, not by you.
</section>

<section class="summary" id="summary">__SUMMARY__</section>

<section class="filters" aria-label="Filters">
  <label>Search
    <input type="search" id="filter-text" placeholder="title, company, location">
  </label>
  <label>Recommendation
    <select id="filter-recommendation">
      <option value="">all</option>
      <option value="apply">apply</option>
      <option value="consider">consider</option>
      <option value="skip">skip</option>
      <option value="need_more_info">need_more_info</option>
      <option value="unscored">unscored</option>
    </select>
  </label>
  <label>Minimum score
    <input type="number" id="filter-min-score" min="0" max="100" step="1" placeholder="0">
  </label>
  <label>Keyword
    <select id="filter-keyword">
      <option value="">all</option>
    </select>
  </label>
  <label>Review state
    <select id="filter-review">
      <option value="">all</option>
      <option value="reviewed">reviewed</option>
      <option value="unreviewed">unreviewed</option>
    </select>
  </label>
  <label>Sort
    <select id="filter-sort">
      <option value="score">score (best first)</option>
      <option value="fresh">freshness (newest first)</option>
    </select>
  </label>
  <button type="button" id="reset-filters" class="btn">Reset</button>
</section>

<main>
  <p class="muted" id="offer-count"></p>
  <div id="offers"></div>
  <div id="empty-state" class="empty" hidden>No offer matches the current filters.</div>
</main>

<section class="analytics">
  <h2>Keyword performance</h2>
  <p class="muted">
    Computed from stored search provenance and the latest successful evaluation.
    <strong>Observed rank is provenance, not a quality signal</strong> — LinkedIn
    result order is not treated as ground truth.
  </p>
  <div id="keyword-table"></div>
  <div class="two-col">
    <div>
      <h3>Top 10 by score quality</h3>
      <div id="top-quality"></div>
    </div>
    <div>
      <h3>Top 10 by positive recommendations</h3>
      <div id="top-yield"></div>
    </div>
  </div>
</section>

<footer class="muted">
  Generated by <code>jobscout dashboard</code>. No external assets, no network requests.
</footer>

<script type="application/json" id="jobscout-data">__DATA__</script>
<script>
__SCRIPT__
</script>
<noscript><p class="notice">This dashboard needs JavaScript to filter and render its data.</p></noscript>
</body>
</html>
"""

_STYLE = """
:root { color-scheme: light dark; --bg:#f6f7f9; --fg:#17191c; --muted:#5b6470;
  --card:#ffffff; --line:#d7dbe0; --accent:#1f6feb; --warn:#8a5a00; --warnbg:#fff6e0;
  --ok:#0f7b3f; --bad:#a32121; }
@media (prefers-color-scheme: dark) { :root { --bg:#14161a; --fg:#e6e8eb; --muted:#9aa3ad;
  --card:#1c1f24; --line:#2c3138; --warnbg:#3a2f12; --warn:#f0c26b; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
h1 { font-size:1.35rem; margin:0; } h2 { font-size:1.15rem; margin:0 0 .25rem; } h3 { font-size:1rem; margin:.75rem 0 .35rem; }
code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size:.85em; }
.muted { color:var(--muted); }
.topbar { display:flex; flex-wrap:wrap; gap:1rem; align-items:center; justify-content:space-between;
  padding:1rem 1.25rem; border-bottom:1px solid var(--line); background:var(--card); }
.actions { display:flex; gap:.75rem; align-items:center; }
.notice { margin:.75rem 1.25rem; padding:.7rem .9rem; border:1px solid var(--line); border-radius:8px; background:var(--card); }
.notice.privacy { font-size:.9rem; }
.notice p { margin:.25rem 0; }
.notice.warn { background:var(--warnbg); color:var(--warn); border-color:var(--warn); }
.summary { display:flex; flex-wrap:wrap; gap:.5rem; padding:.75rem 1.25rem; }
.chip { background:var(--card); border:1px solid var(--line); border-radius:999px; padding:.25rem .7rem; font-size:.85rem; }
.filters { display:flex; flex-wrap:wrap; gap:.75rem; align-items:flex-end; padding:.75rem 1.25rem 1rem;
  border-top:1px solid var(--line); border-bottom:1px solid var(--line); background:var(--card); position:sticky; top:0; z-index:5; }
.filters label { display:flex; flex-direction:column; font-size:.8rem; color:var(--muted); gap:.2rem; }
.filters input, .filters select { font:inherit; padding:.35rem .5rem; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); }
main { padding:.5rem 1.25rem 1.5rem; }
.btn { font:inherit; padding:.4rem .8rem; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); cursor:pointer; }
.btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
.btn:disabled { opacity:.5; cursor:not-allowed; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:.9rem 1rem; margin:0 0 .9rem; }
.card.reviewed { border-left:4px solid var(--accent); }
.card-head { display:flex; flex-wrap:wrap; gap:.6rem; align-items:baseline; justify-content:space-between; }
.card h3 { margin:0; font-size:1.05rem; }
.score { font-variant-numeric:tabular-nums; font-weight:700; font-size:1.1rem; }
.badge { font-size:.78rem; padding:.15rem .55rem; border-radius:999px; border:1px solid var(--line); background:var(--bg); }
.badge.apply { border-color:var(--ok); color:var(--ok); }
.badge.consider { border-color:var(--accent); color:var(--accent); }
.badge.skip { border-color:var(--bad); color:var(--bad); }
.meta { display:flex; flex-wrap:wrap; gap:.35rem 1rem; margin:.35rem 0 .5rem; font-size:.85rem; color:var(--muted); }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px,1fr)); gap:.5rem .9rem; margin:.5rem 0; }
.grid h4 { margin:0 0 .2rem; font-size:.82rem; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); }
.grid ul { margin:0; padding-left:1.1rem; font-size:.9rem; }
.rationale { font-size:.92rem; margin:.4rem 0 .2rem; }
.feedback { border-top:1px dashed var(--line); margin-top:.7rem; padding-top:.6rem; display:flex; flex-wrap:wrap; gap:.5rem; align-items:center; }
.feedback textarea { flex:1 1 240px; min-height:2.2rem; font:inherit; padding:.3rem .5rem; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); }
.feedback .btn.selected { background:var(--accent); border-color:var(--accent); color:#fff; }
table { border-collapse:collapse; width:100%; font-size:.88rem; background:var(--card); }
th, td { border:1px solid var(--line); padding:.35rem .5rem; text-align:left; }
th { background:var(--bg); }
.two-col { display:grid; grid-template-columns:repeat(auto-fit, minmax(280px,1fr)); gap:1rem; margin-top:1rem; }
.analytics { padding:1rem 1.25rem 1.5rem; border-top:1px solid var(--line); }
.empty { padding:1.5rem; text-align:center; color:var(--muted); }
footer { padding:1rem 1.25rem 2rem; font-size:.82rem; border-top:1px solid var(--line); }
.toast { position:fixed; bottom:1rem; right:1rem; background:var(--card); border:1px solid var(--line); border-radius:8px; padding:.5rem .8rem; font-size:.85rem; }
"""


# Rendering builds every node with DOM APIs and assigns untrusted values through
# ``textContent`` only. The script never uses ``innerHTML``, so the report is safe
# even if the JSON escaping were somehow bypassed.
_SCRIPT = r"""
(function () {
  "use strict";

  var EXPECTED_SCHEMA = "jobscout.dashboard.v1";

  var dataNode = document.getElementById("jobscout-data");
  var payload;
  try {
    payload = JSON.parse(dataNode.textContent);
  } catch (err) {
    document.getElementById("offer-count").textContent = "Dashboard data could not be parsed.";
    return;
  }
  if (!payload || payload.schema !== EXPECTED_SCHEMA) {
    document.getElementById("offer-count").textContent =
      "This report was generated by an incompatible jobscout version; regenerate it.";
    return;
  }

  // The storage key is derived from the payload schema so the literal is never
  // duplicated: ``jobscout.feedback.v1`` lives only in the payload.
  var STORAGE_KEY = payload.feedback_schema + ":" + payload.project_id;
  var offersById = {};
  payload.offers.forEach(function (offer) { offersById[offer.id] = offer; });

  var feedback = loadFeedback();
  var filters = { text: "", recommendation: "", minScore: null, keyword: "", review: "", sort: "score" };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
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

  function listBlock(title, items) {
    var box = el("div");
    box.appendChild(el("h4", null, title));
    if (!items.length) {
      box.appendChild(el("p", "muted", "\u2014"));
      return box;
    }
    var list = el("ul");
    items.forEach(function (item) { list.appendChild(el("li", null, item)); });
    box.appendChild(list);
    return box;
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
      setToast("Local storage is unavailable; feedback will not persist.");
      return false;
    }
  }

  function setToast(message) {
    var existing = document.querySelector(".toast");
    if (existing) { existing.remove(); }
    var toast = el("div", "toast", message);
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
    document.getElementById("feedback-count").textContent = feedbackCount() + " reviewed";
    document.getElementById("export-feedback").disabled = feedbackCount() === 0;
  }

  function matches(offer) {
    if (filters.recommendation === "unscored") {
      if (offer.score !== null) { return false; }
    } else if (filters.recommendation && offer.recommendation !== filters.recommendation) {
      return false;
    }
    if (filters.minScore !== null && (offer.score === null || offer.score < filters.minScore)) {
      return false;
    }
    if (filters.keyword) {
      var hit = offer.keywords.some(function (entry) { return entry.keyword === filters.keyword; });
      if (!hit) { return false; }
    }
    var reviewed = Boolean(feedback[offer.id] && feedback[offer.id].decision);
    if (filters.review === "reviewed" && !reviewed) { return false; }
    if (filters.review === "unreviewed" && reviewed) { return false; }
    if (filters.text) {
      var needle = filters.text.toLowerCase();
      var haystack = [offer.title, offer.company, offer.location]
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

  function renderKeywords(offer) {
    var box = el("div");
    box.appendChild(el("h4", null, "Source keyword / observed rank"));
    if (!offer.keywords.length) {
      box.appendChild(el("p", "muted", "no stored provenance"));
      return box;
    }
    var list = el("ul");
    offer.keywords.forEach(function (entry) {
      var rank = entry.rank > 0 ? entry.rank : "n/a";
      list.appendChild(el("li", null, entry.keyword + " \u2014 observed rank " + rank +
        " (" + entry.decision + ")"));
    });
    box.appendChild(list);
    return box;
  }

  function renderFeedback(offer) {
    var box = el("div", "feedback");
    var entry = feedback[offer.id] || { decision: "", reason: "" };
    ["apply", "consider", "reject"].forEach(function (decision) {
      var button = el("button", "btn", decision);
      button.type = "button";
      if (entry.decision === decision) { button.classList.add("selected"); }
      button.addEventListener("click", function () {
        entry.decision = (entry.decision === decision) ? "" : decision;
        entry.updated_at = new Date().toISOString();
        feedback[offer.id] = entry;
        saveFeedback();
        syncFeedback(offer.id, { rerender: true });
      });
      box.appendChild(button);
    });
    var reason = el("textarea");
    reason.placeholder = "optional reason (stored in this browser only)";
    reason.value = entry.reason || "";
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
    return box;
  }

  function syncFeedback(jobId, options) {
    refreshFeedbackCount();
    // A decision can move a card in or out of the reviewed/unreviewed filter, so
    // re-filter when one is active. Reason typing must not re-render: it would
    // steal focus from the textarea.
    if (options && options.rerender && filters.review) {
      renderOffers();
      return;
    }
    var entry = feedback[jobId] || {};
    var card = document.querySelector('.card[data-job-id="' + jobId + '"]');
    if (card) {
      card.classList.toggle("reviewed", Boolean(entry.decision));
      var buttons = card.querySelectorAll(".feedback .btn");
      ["apply", "consider", "reject"].forEach(function (decision, index) {
        if (buttons[index]) {
          buttons[index].classList.toggle("selected", entry.decision === decision);
        }
      });
    }
  }

  function renderCard(offer) {
    var card = el("article", "card");
    card.setAttribute("data-job-id", String(offer.id));
    var head = el("div", "card-head");
    head.appendChild(el("h3", null, offer.title));
    var scoreText = (offer.score === null) ? "unscored" : offer.score.toFixed(1);
    var scoreBox = el("div");
    scoreBox.appendChild(el("span", "score", scoreText));
    if (offer.recommendation) {
      scoreBox.appendChild(document.createTextNode(" "));
      scoreBox.appendChild(el("span", "badge " + offer.recommendation, offer.recommendation));
    }
    head.appendChild(scoreBox);
    card.appendChild(head);

    var meta = el("div", "meta");
    meta.appendChild(el("span", null, offer.company));
    if (offer.location) { meta.appendChild(el("span", null, offer.location)); }
    var age = (offer.age_days === null)
      ? "unknown age"
      : (offer.age_days + " day(s) old (" + offer.age_source + ")");
    meta.appendChild(el("span", null, age));
    if (offer.posted_at) { meta.appendChild(el("span", null, "posted " + offer.posted_at)); }
    meta.appendChild(el("span", null, "job #" + offer.id + " \u00b7 " + offer.content_status));
    if (offer.confidence !== null) {
      meta.appendChild(el("span", null, "confidence " + offer.confidence));
    }
    if (offer.scorer) {
      meta.appendChild(el("span", null, "scorer " + offer.scorer + (offer.model ? " (" + offer.model + ")" : "")));
    }
    var href = safeHttpUrl(offer.url);
    if (href) {
      var link = el("a", null, "open on LinkedIn");
      link.href = href;
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      meta.appendChild(link);
    }
    card.appendChild(meta);

    if (offer.rationale) {
      card.appendChild(el("p", "rationale", offer.rationale));
    }

    var grid = el("div", "grid");
    grid.appendChild(listBlock("Strength", offer.strengths));
    grid.appendChild(listBlock("Gap", offer.gaps));
    grid.appendChild(listBlock("Blocker", offer.blockers));
    grid.appendChild(listBlock("Unknown", offer.unknowns));
    grid.appendChild(renderKeywords(offer));
    card.appendChild(grid);
    card.appendChild(renderFeedback(offer));

    var entry = feedback[offer.id] || {};
    if (entry.decision) { card.classList.add("reviewed"); }
    return card;
  }

  function renderOffers() {
    var container = document.getElementById("offers");
    container.textContent = "";
    var matchesList = payload.offers.filter(matches).sort(compare);
    matchesList.forEach(function (offer) { container.appendChild(renderCard(offer)); });
    document.getElementById("empty-state").hidden = matchesList.length !== 0;
    document.getElementById("offer-count").textContent =
      matchesList.length + " of " + payload.offers.length + " offer(s) shown";
  }

  function cell(text) { return el("td", null, text); }

  function renderKeywordTable() {
    var host = document.getElementById("keyword-table");
    if (!payload.keywords.length) {
      host.appendChild(el("p", "muted", "No keyword provenance stored yet."));
      return;
    }
    var table = el("table");
    var thead = el("thead");
    var headRow = el("tr");
    ["Keyword", "Sightings", "Unique jobs", "Duplicate sightings", "Avg score",
     "Scored", "Apply", "Consider"].forEach(function (label) {
      headRow.appendChild(el("th", null, label));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = el("tbody");
    payload.keywords.forEach(function (item) {
      var row = el("tr");
      row.appendChild(cell(item.keyword));
      row.appendChild(cell(item.sightings));
      row.appendChild(cell(item.unique_jobs));
      row.appendChild(cell(item.duplicate_sightings));
      row.appendChild(cell(item.avg_score === null ? "\u2014" : item.avg_score));
      row.appendChild(cell(item.scored_jobs));
      row.appendChild(cell(item.apply_count));
      row.appendChild(cell(item.consider_count));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  function renderTopList(hostId, items) {
    var host = document.getElementById(hostId);
    if (!items.length) {
      host.appendChild(el("p", "muted", "Not enough data yet."));
      return;
    }
    var list = el("ol");
    items.forEach(function (item) {
      var label = item.keyword + " \u2014 avg " + (item.avg_score === null ? "n/a" : item.avg_score) +
        ", " + item.unique_jobs + " job(s), " + (item.apply_count + item.consider_count) +
        " apply/consider";
      list.appendChild(el("li", null, label));
    });
    host.appendChild(list);
  }

  function renderSummary() {
    var host = document.getElementById("summary");
    var summary = payload.summary;
    var chips = {
      "active jobs": summary.active_jobs,
      "shown": summary.jobs_shown,
      "scored": summary.scored,
      "unscored": summary.unscored,
      "apply": summary.recommendations.apply,
      "consider": summary.recommendations.consider,
      "skip": summary.recommendations.skip,
      "need_more_info": summary.recommendations.need_more_info,
      "keywords": summary.keywords
    };
    Object.keys(chips).forEach(function (label) {
      host.appendChild(el("span", "chip", label + ": " + chips[label]));
    });
  }

  function renderWarnings() {
    var host = document.getElementById("warnings");
    if (!payload.warnings.length) { host.hidden = true; return; }
    payload.warnings.forEach(function (warning) {
      host.appendChild(el("p", "warn", warning));
    });
    host.classList.add("warn");
  }

  function populateKeywordFilter() {
    var select = document.getElementById("filter-keyword");
    payload.keywords.forEach(function (item) {
      var option = el("option", null, item.keyword);
      option.value = item.keyword;
      select.appendChild(option);
    });
  }

  function exportFeedback() {
    var document_ = {
      schema: payload.feedback_schema,
      project_id: payload.project_id,
      exported_at: new Date().toISOString(),
      source: "jobscout-dashboard",
      note: "Local manual feedback. Not submitted anywhere.",
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
    setToast("Feedback exported. Keep the file to import it later.");
  }

  function bindFilters() {
    document.getElementById("filter-text").addEventListener("input", function (event) {
      filters.text = event.target.value.trim();
      renderOffers();
    });
    document.getElementById("filter-recommendation").addEventListener("change", function (event) {
      filters.recommendation = event.target.value;
      renderOffers();
    });
    document.getElementById("filter-min-score").addEventListener("input", function (event) {
      var raw = event.target.value.trim();
      var parsed = parseFloat(raw);
      filters.minScore = raw === "" || isNaN(parsed) ? null : parsed;
      renderOffers();
    });
    document.getElementById("filter-keyword").addEventListener("change", function (event) {
      filters.keyword = event.target.value;
      renderOffers();
    });
    document.getElementById("filter-review").addEventListener("change", function (event) {
      filters.review = event.target.value;
      renderOffers();
    });
    document.getElementById("filter-sort").addEventListener("change", function (event) {
      filters.sort = event.target.value;
      renderOffers();
    });
    document.getElementById("reset-filters").addEventListener("click", function () {
      filters = { text: "", recommendation: "", minScore: null, keyword: "", review: "", sort: "score" };
      document.getElementById("filter-text").value = "";
      document.getElementById("filter-recommendation").value = "";
      document.getElementById("filter-min-score").value = "";
      document.getElementById("filter-keyword").value = "";
      document.getElementById("filter-review").value = "";
      document.getElementById("filter-sort").value = "score";
      renderOffers();
    });
    document.getElementById("export-feedback").addEventListener("click", exportFeedback);
  }

  renderSummary();
  renderWarnings();
  populateKeywordFilter();
  renderKeywordTable();
  renderTopList("top-quality", payload.top_quality);
  renderTopList("top-yield", payload.top_yield);
  bindFilters();
  refreshFeedbackCount();
  renderOffers();
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
    meta_parts = [f"generated {generated}"]
    if label:
        meta_parts.append(f"database: {html.escape(str(label))}")
    meta = " &middot; ".join(meta_parts)
    warnings = payload.get("warnings") or []
    warnings_html = (
        "".join(f"<p>{html.escape(str(item))}</p>" for item in warnings)
        if warnings
        else "<p>No warnings.</p>"
    )
    feedback_schema = str(payload.get("feedback_schema") or FEEDBACK_SCHEMA)
    storage_key = html.escape(f"{feedback_schema}:{payload.get('project_id') or ''}")
    title = html.escape(f"jobscout dashboard ({' | '.join(meta_parts)})")
    return _render_template(
        _HTML_TEMPLATE,
        {
            "TITLE": title,
            "STYLE": _STYLE,
            "META": meta,
            "WARNINGS": warnings_html,
            "STORAGE_KEY": storage_key,
            "SUMMARY": "",
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
    top: int = DEFAULT_TOP,
) -> DashboardSummary:
    """Read the database and write the self-contained HTML report.

    Only the output's parent directory is created. The database is never opened
    for writing, so an unreadable or missing database raises
    :class:`DashboardError` before any file is produced. An empty output path, an
    out-of-range ``top``, and an output that resolves onto the database are all
    refused up front, and any filesystem failure while writing becomes a
    :class:`DashboardError` instead of a raw ``OSError``.
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
        keywords=len(payload["keywords"]),
        bytes_written=len(document.encode("utf-8")),
        warnings=list(payload["warnings"]),
    )


__all__ = [
    "DASHBOARD_SCHEMA",
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
