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
human feedback values (``apply``/``consider``/``reject``) are translated for
display only, so the payload stays round-trippable against the database.
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

#: A posting counts as *fresh* in keyword analytics when it is at most this many
#: days old. jobscout has no verified "expired on LinkedIn" signal, so freshness
#: is the honest, schema-supported proxy for how apply-able a keyword's results
#: still are.
FRESH_WINDOW_DAYS = 21

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

    Every rate is computed from stored rows only. A metric with no data behind it
    (no scored job, no imported human decision, no usable date) stays ``None``
    instead of being guessed as zero.

    ``jobscout`` has no verified "posting expired on LinkedIn" signal, so
    freshness is measured from the stored dates it actually has: a posting counts
    as fresh when it is at most :data:`FRESH_WINDOW_DAYS` days old, using
    ``posted_at`` and falling back to ``first_seen_at``. ``unknown_age_jobs``
    counts jobs where neither date is usable, so a keyword is never credited with
    freshness it cannot prove.
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

    inactive_rows = conn.execute(
        """
        SELECT r.query AS keyword,
               COUNT(DISTINCT i.job_id) AS all_jobs,
               COUNT(DISTINCT CASE WHEN j.is_active = 0 THEN i.job_id END) AS inactive_jobs
        FROM search_run_items i
        JOIN search_runs r ON r.id = i.run_id
        JOIN jobs j ON j.id = i.job_id
        WHERE r.query IS NOT NULL AND TRIM(r.query) <> ''
        GROUP BY r.query
        """
    ).fetchall()
    inactive = {str(row["keyword"]): row for row in inactive_rows}

    observation_rows = conn.execute(
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
               j.posted_at AS posted_at,
               j.first_seen_at AS first_seen_at,
               e.overall_score AS score,
               e.recommendation AS recommendation,
               f.decision AS human_decision
        FROM keyword_jobs kj
        JOIN jobs j ON j.id = kj.job_id
        LEFT JOIN latest_evaluations e ON e.job_id = kj.job_id
        LEFT JOIN job_feedback f ON f.job_id = kj.job_id
        ORDER BY kj.keyword, kj.job_id
        """
    ).fetchall()

    observations: dict[str, list[sqlite3.Row]] = {}
    for row in observation_rows:
        observations.setdefault(str(row["keyword"]), []).append(row)

    stats: list[dict[str, Any]] = []
    for row in sighting_rows:
        keyword = str(row["keyword"])
        rows_for_keyword = observations.get(keyword, [])

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
        count_row = inactive.get(keyword)
        all_jobs = int(count_row["all_jobs"] or 0) if count_row is not None else unique_jobs
        inactive_jobs = int(count_row["inactive_jobs"] or 0) if count_row is not None else 0
        human_reviewed = len(decisions)
        human_apply = decisions.count("apply")
        human_consider = decisions.count("consider")
        human_reject = decisions.count("reject")
        duplicate_sightings = int(row["duplicate_sightings"] or 0)
        sightings = int(row["sightings"] or 0)

        stats.append(
            {
                "keyword": keyword,
                "sightings": sightings,
                "unique_jobs": unique_jobs,
                "duplicate_sightings": duplicate_sightings,
                "duplicate_rate": _rate(duplicate_sightings, sightings),
                "inactive_jobs": inactive_jobs,
                "inactive_rate": _rate(inactive_jobs, all_jobs),
                "scored_jobs": scored_jobs,
                "unscored_jobs": max(0, unique_jobs - scored_jobs),
                "avg_score": _mean(scores),
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


def _top_keywords(
    stats: Sequence[dict[str, Any]], *, limit: int = 10
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(top by model score quality, top by model yield, top by human approval).

    Each list only includes keywords the data can actually rank: a keyword with no
    scored job is absent from quality, one with no apply/consider is absent from
    model yield, and one with no imported human decision is absent from human
    approval. An empty list is the honest answer, never a placeholder row.
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
    human = sorted(
        (
            item
            for item in stats
            if item["human_reviewed"] > 0 and item["human_apply_rate"] is not None
        ),
        key=lambda item: (
            -float(item["human_apply_rate"]),
            -item["human_reviewed"],
            item["keyword"],
        ),
    )[:limit]
    return quality, yield_ranked, human


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
    human_reviewed_jobs = int(conn.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0])
    return {
        "active_jobs": total,
        "jobs_shown": len(offers),
        "scored": len(scored),
        "unscored": len(offers) - len(scored),
        "recommendations": counts,
        "keywords": keyword_count,
        # Imported human decisions, a different signal from the model
        # recommendation and the only source of the human rate columns.
        "human_reviewed_jobs": human_reviewed_jobs,
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
                "title": _concisify(row["title"]) or "(sin título)",
                "company": _concisify(row["company"]) or "(empresa desconocida)",
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

    keyword_stats = _fetch_keyword_stats(conn, now=reference)
    top_quality, top_yield, top_human = _top_keywords(keyword_stats)
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
        "top_human": top_human,
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
<html lang="es">
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
<a class="skip-link" href="#contenido">Saltar al contenido principal</a>

<div class="panel">

<header class="topbar">
  <div class="topbar-id">
    <p class="kicker">Informe local &middot; sin conexión</p>
    <h1>Panel de ofertas &mdash; jobscout</h1>
  </div>
  <nav class="topbar-nav" aria-label="Secciones del informe">
    <a href="#resumen">Resumen</a>
    <a href="#contenido">Ofertas</a>
    <a href="#analitica">Palabras clave</a>
    <a href="#filtros">Filtros</a>
  </nav>
</header>

<div class="content">

<section class="__WARNINGS_CLASS__" id="warnings" aria-labelledby="warnings-title">
  <h2 id="warnings-title" class="notice-title">Avisos</h2>
  __WARNINGS__
</section>

<section class="strip" id="resumen" aria-labelledby="kpis-title">
  <div class="section-head">
    <h2 id="kpis-title">Resumen</h2>
  </div>
  <div class="kpi-grid" id="summary" aria-live="polite"></div>
  <p class="muted" id="summary-note"></p>
</section>

<main id="contenido" aria-labelledby="offers-title">
  <div class="section-head">
    <h2 id="offers-title">Ofertas</h2>
    <p class="muted" id="offer-count" role="status"></p>
  </div>
  <div id="offers"></div>
  <p id="empty-state" class="empty" hidden>
    Ninguna oferta coincide con la búsqueda y los filtros actuales.
  </p>
</main>

<section class="block" id="analitica" aria-labelledby="analytics-title">
  <div class="section-head">
    <h2 id="analytics-title">Desempeño por palabra clave</h2>
  </div>
  <p class="muted">
    Calculado a partir de la procedencia guardada de las búsquedas y de la última
    evaluación exitosa. <strong>La posición observada es procedencia, no una medida de
    calidad</strong>: el orden de resultados de LinkedIn no se trata como verdad.
  </p>
  <details class="method">
    <summary>Metodología de las métricas</summary>
    <p>
      Cada tasa se calcula solo sobre ofertas activas y sobre hechos guardados: una
      métrica sin datos detrás queda como <em>sin dato</em>, nunca como un cero
      inventado. La frescura usa un umbral de 21 días y cae a la fecha de primera
      visualización cuando no hay fecha de publicación; las ofertas sin ninguna fecha
      usable no se cuentan como frescas. La columna de decisión humana proviene del
      feedback importado con <code>jobscout import-feedback</code>, no de sus marcas en
      este navegador.
    </p>
  </details>
  <div class="table-wrap">
    <div id="keyword-table"></div>
  </div>
  <div class="two-col">
    <section aria-labelledby="top-quality-title">
      <h3 id="top-quality-title">Top 10 por calidad del puntaje</h3>
      <div id="top-quality"></div>
    </section>
    <section aria-labelledby="top-yield-title">
      <h3 id="top-yield-title">Top 10 por recomendaciones positivas</h3>
      <div id="top-yield"></div>
    </section>
    <section aria-labelledby="top-human-title">
      <h3 id="top-human-title">Top 10 por aprobación humana</h3>
      <div id="top-human"></div>
    </section>
  </div>
</section>

</div>

<aside class="rail" aria-label="Controles y contexto del informe">

  <section class="rail-block" id="filtros" aria-labelledby="filters-title">
    <h2 id="filters-title" class="rail-title">Buscar y filtrar</h2>
    <p class="filters-summary" id="filters-summary" aria-live="polite">Sin filtros activos</p>
    <div class="filters" role="search" aria-label="Búsqueda y filtros de ofertas">
      <div class="filter-field">
        <label for="filter-text">Buscar</label>
        <input type="search" id="filter-text" autocomplete="off"
          placeholder="título, empresa, ubicación">
      </div>
      <div class="filter-field">
        <label for="filter-recommendation">Recomendación del modelo</label>
        <select id="filter-recommendation">
          <option value="">todas</option>
          <option value="apply">Aplicar</option>
          <option value="consider">Considerar</option>
          <option value="skip">Descartar</option>
          <option value="need_more_info">Requiere más información</option>
          <option value="unscored">Sin puntaje</option>
        </select>
      </div>
      <div class="filter-field">
        <label for="filter-keyword">Palabra clave de origen</label>
        <select id="filter-keyword">
          <option value="">todas</option>
        </select>
      </div>
      <div class="filter-field">
        <label for="filter-min-score">Puntaje mínimo</label>
        <input type="number" id="filter-min-score" min="0" max="100" step="1"
          inputmode="numeric" placeholder="0">
      </div>
      <div class="filter-field">
        <label for="filter-review">Estado de revisión</label>
        <select id="filter-review">
          <option value="">todas</option>
          <option value="reviewed">revisadas</option>
          <option value="unreviewed">sin revisar</option>
        </select>
      </div>
      <div class="filter-field">
        <label for="filter-sort">Ordenar por</label>
        <select id="filter-sort">
          <option value="score">puntaje (mejor primero)</option>
          <option value="fresh">fecha (más recientes primero)</option>
        </select>
      </div>
      <div class="filter-field">
        <button type="button" id="reset-filters" class="btn btn-ghost"
          title="Restablece la búsqueda y todos los filtros">Restablecer filtros</button>
      </div>
    </div>
  </section>

  <section class="rail-block" aria-labelledby="actions-title">
    <h2 id="actions-title" class="rail-title">Acciones</h2>
    <div class="rail-actions">
      <button type="button" id="export-feedback" class="btn btn-primary"
        title="Descarga un archivo JSON con sus decisiones guardadas en este navegador">
        Exportar feedback local (JSON)
      </button>
      <span class="muted" id="feedback-count" aria-live="polite">0 revisadas</span>
      <div id="explain-controls" class="control-row"></div>
    </div>
  </section>

  <section class="rail-block" aria-labelledby="axes-title">
    <h2 id="axes-title" class="rail-title">Dos ejes distintos</h2>
    <dl class="axes">
      <dt class="axis-model">Recomendación del modelo</dt>
      <dd>La calcula el evaluador de jobscout a partir de la oferta y de su perfil. Sus
        marcas no la cambian.</dd>
      <dt class="axis-human">Mi decisión (feedback)</dt>
      <dd>Su marca manual. Se guarda solo en este navegador y no modifica el puntaje, la
        recomendación ni la base de datos.</dd>
    </dl>
  </section>

  <section class="rail-block" aria-labelledby="privacy-title">
    <h2 id="privacy-title" class="rail-title">Privacidad y funcionamiento sin conexión</h2>
    <p class="muted">
      <strong>Todo queda en este navegador.</strong> Las decisiones manuales se guardan
      en <code>localStorage</code> bajo la clave <code>__STORAGE_KEY__</code>. No se envía
      ninguna postulación, no se guarda nada en LinkedIn y no se escribe en la base
      SQLite. Para conservar una copia, use <em>Exportar feedback local (JSON)</em>. Los
      puntajes y las recomendaciones provienen del evaluador de jobscout, no de su decisión.
    </p>
  </section>

</aside>

<footer class="scope">
  <div>
    <h2 class="section-label">Alcance del informe</h2>
    <p class="muted">__META__</p>
  </div>
  <div>
    <h2 class="section-label">Sin red</h2>
    <p class="muted">
      Generado por <code>jobscout dashboard</code>. Un solo archivo HTML: sin recursos
      externos, sin solicitudes de red, sin servidor.
    </p>
  </div>
</footer>

</div>

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
  --canvas:#e7ebee;
  --paper:#ffffff;
  --surface:#f4f6f7;
  /* Two hover washes: one that lifts from paper (rows, buttons) and one that
     lifts from the sunken surface (table rows, inputs). */
  --hover:#f8fafb;
  --hover-strong:#eef1f3;
  --ink:#0c1216;
  --soft-ink:#20303a;
  --muted:#5c6a74;
  --hairline:#ccd4da;
  --rule-strong:#8b98a1;
  --ember:#e7000b;
  --signal:#0f6b4f;
  --space:4px;
  --pad-block:16px;
  --radius:3px;
  /* Two separation steps, and only two. ``--gap-card`` is the canvas showing
     between two offers; ``--gap-block`` is the space inside one offer between its
     bands. Every rule drawn below is a hairline meant to be read, never decoration. */
  --gap-card:24px;
  --gap-block:16px;
  --fs-micro:11px;
  --fs-small:13px;
  --fs-body:15px;
  /* The offer title is the row's name, so it has to outrank body text without
     competing with a section heading. */
  --fs-title:17px;
  --fs-heading:20px;
  --fs-display:28px;
  --panel-max:1440px;
  --rail-width:320px;
  /* Measured height of the sticky top bar, used to keep the sticky rail clear of it
     at every desktop width the shell targets. */
  --topbar-h:78px;
  --font-sans:system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono:ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--canvas); color:var(--soft-ink);
  font-family:var(--font-sans); font-size:var(--fs-body); line-height:1.5;
  -webkit-font-smoothing:antialiased; }
h1, h2, h3, h4 { margin:0; color:var(--ink); font-weight:600; }
h1 { font-size:var(--fs-display); line-height:1.15; letter-spacing:-.02em; }
h2 { font-size:var(--fs-heading); line-height:1.25; letter-spacing:-.01em; }
h3 { font-size:var(--fs-body); line-height:1.35; }
h4 { font-size:var(--fs-micro); line-height:1.3; font-weight:700; text-transform:uppercase;
  letter-spacing:.08em; color:var(--muted); }
p { margin:0; }
code { font-family:var(--font-mono); font-size:.9em; }
.muted { color:var(--muted); }
a { color:var(--ink); text-decoration:underline; text-underline-offset:2px;
  text-decoration-color:var(--rule-strong); }
a:hover { text-decoration-color:var(--ink); }
:focus-visible { outline:3px solid var(--ink); outline-offset:1px; }
.skip-link { position:absolute; left:-9999px; top:0; z-index:100; background:var(--ink);
  color:var(--paper); font-weight:600; font-size:var(--fs-small); text-decoration:none;
  padding:10px 14px; }
.skip-link:focus { left:0; }

/* Desktop-only shell: a fixed two-column grid (content + control rail) with no
   narrow-screen breakpoint. The rail follows the content in the DOM so the reading
   and tab order follow the required section order, and it is placed on the right so
   the visual order matches that DOM order. */
.panel { max-width:var(--panel-max); margin:0 auto; padding:0 20px 28px;
  display:grid; grid-template-columns:minmax(0, 1fr) var(--rail-width);
  gap:18px 20px; align-items:start; }
/* The top bar is the report's one sticky element: the section shortcuts stay
   reachable while the offer list scrolls past. It owns no width of its own, so it
   never covers the column a reader is working in. */
.topbar { grid-column:1 / -1; grid-row:1; position:sticky; top:0; z-index:20;
  background:var(--canvas); min-height:var(--topbar-h); display:flex; flex-wrap:wrap;
  align-items:flex-end; justify-content:space-between; gap:8px 24px;
  padding:14px 0 10px; border-bottom:2px solid var(--ink); }
.topbar-id { display:flex; flex-direction:column; gap:2px; }
.kicker { font-size:var(--fs-micro); font-weight:700; text-transform:uppercase;
  letter-spacing:.1em; color:var(--muted); }
.topbar-nav { display:flex; gap:18px; font-size:var(--fs-micro); font-weight:700;
  text-transform:uppercase; letter-spacing:.07em; }
.topbar-nav a { color:var(--soft-ink); text-decoration:none; padding:4px 0;
  border-bottom:2px solid transparent;
  transition:color .14s ease, border-color .14s ease; }
.topbar-nav a:hover { color:var(--ink); text-decoration:underline;
  border-bottom-color:var(--ink); }
.topbar-nav a:active { color:var(--ink); }

.content { grid-column:1; grid-row:2; display:flex; flex-direction:column; gap:22px;
  min-width:0; }
.content > * { min-width:0; }
footer.scope { grid-column:1 / -1; grid-row:3; display:grid;
  grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px 24px; padding-top:12px;
  border-top:2px solid var(--ink); }
footer.scope p { font-size:var(--fs-small); }

/* Sticky control rail: pinned below the top bar, never over the content column,
   and internally scrollable so a short window still reaches the last block. */
.rail { grid-column:2; grid-row:2; display:flex; flex-direction:column; gap:12px;
  position:sticky; top:calc(var(--topbar-h) + 14px);
  max-height:calc(100vh - var(--topbar-h) - 28px); overflow-y:auto;
  scrollbar-gutter:stable; }
.rail-block { display:flex; flex-direction:column; gap:10px; background:var(--paper);
  border:1px solid var(--rule-strong); border-radius:var(--radius); padding:var(--pad-block); }
.rail-block p { font-size:var(--fs-small); }
.rail-block code { overflow-wrap:anywhere; }
.rail-title { font-size:var(--fs-micro); font-weight:700; text-transform:uppercase;
  letter-spacing:.09em; color:var(--ink); padding-bottom:6px;
  border-bottom:1px solid var(--hairline); }

.filters { display:flex; flex-direction:column; gap:10px; }
.filter-field { display:flex; flex-direction:column; gap:4px; min-width:0; }
.filter-field label { font-size:var(--fs-micro); font-weight:700; text-transform:uppercase;
  letter-spacing:.07em; color:var(--muted); }
/* Element-qualified so it outranks the rail's generic paragraph size. */
p.filters-summary { font-size:var(--fs-micro); font-weight:700; text-transform:uppercase;
  letter-spacing:.07em; color:var(--muted); }
p.filters-summary.active { color:var(--ink); }
.filters input, .filters select, .feedback textarea { font-family:inherit;
  font-size:var(--fs-small); width:100%; padding:8px 10px; color:var(--ink);
  background:var(--surface); border:1px solid var(--hairline);
  border-radius:var(--radius);
  transition:background-color .14s ease, border-color .14s ease; }
.filters input:hover, .filters select:hover, .feedback textarea:hover {
  background:var(--hover-strong); }
.filters input:focus, .filters select:focus, .feedback textarea:focus {
  background:var(--paper); border-color:var(--ink); }

.btn { font-family:inherit; font-size:var(--fs-small); font-weight:600; padding:8px 13px;
  cursor:pointer; color:var(--ink); background:var(--paper);
  border:1px solid var(--rule-strong); border-radius:var(--radius);
  transition:background-color .14s ease, border-color .14s ease, color .14s ease; }
.btn:hover:not(:disabled) { background:var(--surface); border-color:var(--ink); }
.btn:active:not(:disabled) { background:var(--hover-strong); border-color:var(--ink); }
.btn:disabled { opacity:.45; cursor:not-allowed; }
.btn-primary { background:var(--ink); border-color:var(--ink); color:var(--paper); }
.btn-primary:hover:not(:disabled) { background:var(--soft-ink); border-color:var(--soft-ink); }
.btn-ghost { background:transparent; border-color:var(--hairline); }
.btn-ghost:hover:not(:disabled) { background:var(--surface); border-color:var(--rule-strong); }
#export-feedback { width:100%; }
.rail-actions { display:flex; flex-direction:column; gap:8px; }
.control-row { display:flex; gap:8px; }
.control-row .btn { flex:1 1 0; }

.axes { margin:0; display:flex; flex-direction:column; gap:10px; }
.axes dt { display:flex; align-items:center; gap:6px; font-size:var(--fs-micro);
  font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--ink); }
.axes dt::before { content:""; flex:0 0 auto; width:9px; height:9px;
  border:1px solid var(--ink); background:var(--paper); }
/* The accent swatch is the legend for the model axis; the human axis stays ink so a
   reader never learns "the accent means my decision". */
.axis-model::before { background:var(--signal); border-color:var(--signal); }
.axis-human::before { background:var(--ink); border-color:var(--ink); }
.axes dd { margin:4px 0 0; font-size:var(--fs-small); color:var(--muted); }

.notice { display:flex; flex-direction:column; gap:6px; background:var(--paper);
  border:1px solid var(--hairline); border-radius:var(--radius); padding:var(--pad-block); }
.notice-title { font-size:var(--fs-small); }
.notice p { font-size:var(--fs-small); color:var(--muted); }
/* Ember is reserved for real warning/error states, never for decoration. */
.notice.warn { background:#fff5f5; border-color:var(--ember); border-left-width:4px; }
.notice.warn .notice-title { color:var(--ember); }
.notice.warn p { color:var(--soft-ink); }

.strip { display:flex; flex-direction:column; gap:8px; }
.section-label { font-size:var(--fs-micro); font-weight:700; text-transform:uppercase;
  letter-spacing:.09em; color:var(--muted); }
/* Region boundary: every region heading closes with a hard ink rule, so the
   summary strip, the offers list and the analytics panel each read as their own
   section instead of one continuous sheet. */
.section-head { display:flex; align-items:baseline; justify-content:space-between;
  gap:12px; padding-bottom:8px; border-bottom:2px solid var(--ink); }
.section-head h2 { font-size:var(--fs-heading); }
/* Exactly six stat blocks, so the strip is an explicit fixed six-column row. */
.kpi-grid { display:grid; grid-template-columns:repeat(6, minmax(0, 1fr));
  background:var(--paper); border:1px solid var(--rule-strong);
  border-radius:var(--radius); overflow:hidden; }
.kpi { display:flex; flex-direction:column; gap:3px; padding:12px 14px;
  border-left:1px solid var(--hairline); }
.kpi:first-child { border-left:0; }
.kpi-value { font-family:var(--font-mono); font-size:var(--fs-display); line-height:1;
  font-weight:600; letter-spacing:-.02em; color:var(--ink);
  font-variant-numeric:tabular-nums; }
.kpi-label { font-size:var(--fs-micro); text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); }
/* The human-reviewed counter is the one KPI the reader owns, so it is marked with an
   ink rule rather than the model accent. */
.kpi-reviewed { background:var(--surface); box-shadow:inset 3px 0 0 var(--ink); }
#summary-note { font-size:var(--fs-small); }

#contenido { display:flex; flex-direction:column; gap:18px; min-width:0; }
#offer-count { font-size:var(--fs-small); }
/* Each offer is its own paper block sitting on the canvas. The list container
   paints nothing: separation is the vertical gap between blocks plus one
   restrained outline per block, so two neighbouring offers can never merge into
   one continuous list. Elevation is not used as a separator. */
#offers { display:flex; flex-direction:column; gap:var(--gap-card); }
.card { display:flex; flex-direction:column; gap:var(--gap-block);
  padding:24px 26px; background:var(--paper);
  border:1px solid var(--rule-strong); border-radius:var(--radius);
  transition:background-color .14s ease, border-color .14s ease; }
.card:hover { background:var(--hover); border-color:var(--ink); }
/* Reviewed means "you touched this one": an ink marker, not a hue. */
.card.reviewed { background:var(--surface); border-color:var(--ink);
  box-shadow:inset 3px 0 0 var(--ink); }
.card.reviewed:hover { background:var(--hover-strong); }
/* Two fixed header columns instead of a wrapping flex row: the title column takes
   the remaining width and the score/recommendation column keeps its rule, so the
   hairline between them never ends up floating on a line of its own. */
.card-head { display:grid; grid-template-columns:minmax(0, 1fr) auto;
  gap:16px 24px; align-items:start; }
.card-title { min-width:0; display:flex; flex-direction:column; gap:4px; }
.card-title h3 { font-size:var(--fs-title); line-height:1.3; letter-spacing:-.01em;
  overflow-wrap:anywhere; }
.card-sub { display:flex; flex-wrap:wrap; gap:6px; align-items:baseline;
  font-size:var(--fs-small); }
.card-sub .company { color:var(--soft-ink); font-weight:600; }
.card-sub .location { color:var(--muted); }
.card-sub .dot { color:var(--rule-strong); }
/* The right-hand side of a row: the model's score and badge, then the one action
   that leaves the report. A hairline marks where the header's title column ends
   and the score/recommendation column begins. */
.card-side { display:flex; flex-direction:column; align-items:flex-end; gap:12px;
  min-width:0; padding-left:20px; border-left:1px solid var(--hairline); }
.card-score { display:flex; align-items:center; gap:10px; }
.offer-link { display:inline-flex; align-items:center; gap:7px;
  font-size:var(--fs-small); font-weight:600; text-decoration:none; color:var(--ink);
  background:var(--paper); border:1px solid var(--rule-strong);
  border-radius:var(--radius); padding:7px 13px;
  transition:background-color .14s ease, border-color .14s ease, color .14s ease; }
.offer-link::after { content:"\\2197"; font-family:var(--font-mono); }
.offer-link:hover { background:var(--ink); border-color:var(--ink); color:var(--paper); }
.offer-link:active { background:var(--soft-ink); border-color:var(--soft-ink); }
.score { display:inline-flex; align-items:baseline; gap:3px;
  font-family:var(--font-mono); font-size:var(--fs-display); line-height:1;
  font-weight:600; letter-spacing:-.02em; color:var(--ink);
  font-variant-numeric:tabular-nums; }
.score-max { font-family:var(--font-sans); font-size:var(--fs-micro); font-weight:600;
  color:var(--muted); letter-spacing:0; }
/* Model-axis weight only: apply reads loudest, skip and need_more_info recede. */
.card.rec-apply .score { color:var(--signal); }
.card.rec-skip .score, .card.rec-need_more_info .score { color:var(--muted); }

/* The badge belongs to the model axis. The accent marks "apply" and nothing else;
   human feedback never uses it (see .decision-btn below). */
.badge { display:inline-flex; align-items:baseline; font-family:var(--font-sans);
  font-size:var(--fs-micro); font-weight:700; text-transform:uppercase;
  letter-spacing:.06em; white-space:nowrap; padding:4px 8px;
  border:1px solid var(--hairline); border-radius:2px; background:var(--surface);
  color:var(--muted); }
.badge::before { content:"Modelo"; margin-right:5px; padding-right:5px;
  border-right:1px solid currentColor; }
.badge-apply { background:var(--signal); border-color:var(--signal); color:#ffffff; }
.badge-consider { background:var(--paper); border-color:var(--ink); color:var(--ink); }
.badge-skip { background:var(--surface); border-color:var(--hairline); color:var(--muted); }
.badge-need_more_info { background:transparent; border-style:dashed;
  border-color:var(--rule-strong); color:var(--muted); }
.badge-unscored { background:transparent; border-style:dashed;
  border-color:var(--rule-strong); color:var(--muted); }

/* Exactly six facts per offer, so three fixed columns fill two tidy rows and the
   longer values ("27.82 días (fecha de publicación)") stop wrapping mid-phrase.
   The rule above the facts is the boundary between the offer header (title, score,
   recommendation) and the stored facts underneath; the whitespace carries the rest. */
.meta { list-style:none; display:grid;
  grid-template-columns:repeat(3, minmax(0, 1fr)); gap:8px 18px; margin:0;
  padding:14px 0 0; border-top:1px solid var(--hairline);
  font-size:var(--fs-small); }
.meta li { display:flex; gap:6px; align-items:baseline; min-width:0; }
.meta-key { flex:0 0 auto; font-size:var(--fs-micro); text-transform:uppercase;
  letter-spacing:.06em; color:var(--muted); }
.meta-value { color:var(--soft-ink); overflow-wrap:anywhere; }

/* An inset rule block rather than a colour band: the explanation is marked with a
   heavier left rule on a sunken surface, so it reads as a quotation of the model. */
.rationale { font-size:var(--fs-small); color:var(--soft-ink); background:var(--surface);
  border:1px solid var(--hairline); border-left:3px solid var(--rule-strong);
  padding:10px 12px; }
.rationale strong { display:block; font-size:var(--fs-micro); text-transform:uppercase;
  letter-spacing:.06em; color:var(--muted); }

/* Native disclosure: the platform owns the keyboard and screen-reader behaviour.
   The whole row is the hit target, the chevron states open/closed, and hovering or
   focusing it is obvious without a heavy chrome. */
.explain { border-top:1px solid var(--hairline); }
.explain > summary { display:flex; align-items:center; gap:9px; cursor:pointer;
  padding:10px 12px; border-radius:var(--radius); font-size:var(--fs-small);
  font-weight:700; color:var(--ink);
  transition:background-color .14s ease, color .14s ease; }
.explain > summary::-webkit-details-marker { display:none; }
.explain > summary::before { content:""; flex:0 0 auto; width:0; height:0;
  border-top:5px solid transparent; border-bottom:5px solid transparent;
  border-left:7px solid var(--rule-strong);
  transition:transform .16s ease, border-left-color .14s ease; }
.explain > summary:hover { background:var(--hover-strong); }
.explain > summary:hover::before { border-left-color:var(--ink); }
.explain > summary:active { background:var(--hairline); }
.explain[open] > summary::before { transform:rotate(90deg); border-left-color:var(--ink); }
.explain-body { display:flex; flex-direction:column; gap:14px; padding:6px 14px 12px; }

/* Provenance and the four scoring lists are the last band of the explanation. With
   JavaScript off they are direct children of the offer, so they carry the same
   hairline boundary the disclosure draws when it is present. */
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));
  gap:12px; }
.card > .grid { border-top:1px solid var(--hairline); padding-top:14px; }
.grid > div { display:flex; flex-direction:column; gap:6px; background:var(--surface);
  border:1px solid var(--hairline); border-radius:var(--radius); padding:10px 12px; }
.grid ul { margin:0; padding-left:16px; display:flex; flex-direction:column; gap:3px;
  font-size:var(--fs-small); color:var(--soft-ink); }

/* Human axis: weight, shape and an ink rule. No accent colour anywhere here, so a
   reader can never mistake this for the model recommendation. The heading repeats
   the ink square of the "Mi decisión" legend, which is the axis marker. */
.feedback { display:flex; flex-direction:column; gap:12px; border-top:2px solid var(--ink);
  padding-top:16px; }
.feedback h4 { display:flex; align-items:center; gap:7px; color:var(--ink); }
.feedback h4::before { content:""; flex:0 0 auto; width:9px; height:9px;
  border:1px solid var(--ink); background:var(--ink); }
.feedback-hint { font-size:var(--fs-micro); }
.feedback-controls { display:inline-flex; flex-wrap:wrap; }
.decision-btn { border-radius:0; border-left-width:0; background:var(--paper);
  padding:9px 15px; }
.decision-btn:first-child { border-radius:2px 0 0 2px; border-left-width:1px; }
.decision-btn:last-child { border-radius:0 2px 2px 0; }
.decision-btn:hover:not(:disabled) { background:var(--hover-strong); border-color:var(--ink); }
.decision-btn:active:not(:disabled) { background:var(--hairline); }
.decision-btn.selected { background:var(--ink); border-color:var(--ink); color:var(--paper); }
.decision-btn.selected:hover:not(:disabled) { background:var(--soft-ink);
  border-color:var(--soft-ink); }
/* The check mark repeats the pressed state, so selection never rests on colour. */
.decision-btn.selected::before { content:"\\2713"; margin-right:7px; }
.feedback textarea { resize:vertical; min-height:56px; }
.decision-state { display:flex; flex-wrap:wrap; align-items:baseline; gap:4px 10px;
  font-size:var(--fs-small); color:var(--soft-ink); }
/* Transient, per-row confirmation. It is a polite live region, so it is announced
   once and then removed; it never blocks the next mark. */
.decision-saved { font-size:var(--fs-micro); font-weight:700; text-transform:uppercase;
  letter-spacing:.07em; color:var(--ink); }

/* The analytics region is one sunken surface panel, deliberately not a paper
   sheet: the offers list is a run of separated paper blocks, so the two regions
   can never be mistaken for each other. */
.block { display:flex; flex-direction:column; gap:14px; background:var(--surface);
  border:1px solid var(--rule-strong); border-radius:var(--radius);
  padding:18px 20px; }
.block > .muted { font-size:var(--fs-small); }
.method { background:var(--paper); border:1px solid var(--hairline);
  border-radius:var(--radius); padding:0; overflow:hidden; }
.method > summary { display:flex; align-items:center; gap:9px; cursor:pointer;
  padding:10px 12px; font-size:var(--fs-small); font-weight:700; color:var(--ink);
  transition:background-color .14s ease; }
.method > summary::-webkit-details-marker { display:none; }
.method > summary::before { content:""; flex:0 0 auto; width:0; height:0;
  border-top:5px solid transparent; border-bottom:5px solid transparent;
  border-left:7px solid var(--rule-strong);
  transition:transform .16s ease, border-left-color .14s ease; }
.method > summary:hover { background:var(--paper); }
.method > summary:hover::before { border-left-color:var(--ink); }
.method[open] > summary::before { transform:rotate(90deg); border-left-color:var(--ink); }
.method p { margin:0; padding:0 12px 12px; font-size:var(--fs-small);
  color:var(--soft-ink); }

.table-wrap { overflow-x:auto; border:1px solid var(--hairline);
  border-radius:var(--radius); background:var(--paper); }
table { border-collapse:collapse; width:100%; font-size:var(--fs-small); }
caption { caption-side:top; text-align:left; padding:10px 12px;
  font-size:var(--fs-small); color:var(--muted);
  border-bottom:1px solid var(--hairline); }
th, td { padding:7px 10px; text-align:left; white-space:nowrap;
  border-bottom:1px solid var(--hairline); }
thead th { background:var(--surface); color:var(--muted); font-size:var(--fs-micro);
  font-weight:700; text-transform:uppercase; letter-spacing:.05em; }
tbody tr:last-child td { border-bottom:0; }
tbody tr:hover td { background:var(--surface); }
/* Every column after the keyword is numeric, so it aligns, tabulates and uses mono. */
th + th, td + td { text-align:right; font-family:var(--font-mono);
  font-variant-numeric:tabular-nums; }
th:first-child, td:first-child { text-align:left; font-family:var(--font-sans); }
/* Each header cell holds the sort control, so the hit target is the whole label.
   The arrow is a faint "sortable" hint, ink when that column is the active sort,
   and the direction is mirrored in aria-sort so it is never glyph-only. */
.sort-btn { font:inherit; color:inherit; background:none; border:0; padding:5px 0;
  display:inline-flex; align-items:center; gap:6px; cursor:pointer;
  transition:color .14s ease; }
.sort-btn::after { content:"\\2191"; color:var(--rule-strong); font-family:var(--font-sans);
  transition:color .14s ease; }
.sort-btn:hover { color:var(--ink); }
.sort-btn:hover::after { color:var(--ink); }
th[aria-sort="ascending"] .sort-btn::after { content:"\\2191"; color:var(--ink); }
th[aria-sort="descending"] .sort-btn::after { content:"\\2193"; color:var(--ink); }

.two-col { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:10px; }
.two-col > section { display:flex; flex-direction:column; gap:10px;
  background:var(--paper); border:1px solid var(--hairline);
  border-radius:var(--radius); padding:14px; }
.two-col h3 { font-size:var(--fs-micro); font-weight:700; text-transform:uppercase;
  letter-spacing:.07em; color:var(--ink); }
.two-col ol { margin:0; padding-left:18px; display:flex; flex-direction:column; gap:4px;
  font-size:var(--fs-small); color:var(--soft-ink); }

.empty { padding:32px 16px; text-align:center; color:var(--muted);
  background:var(--paper); border:1px dashed var(--rule-strong);
  border-radius:var(--radius); }
.toast { position:fixed; bottom:20px; right:20px; z-index:50;
  max-width:min(420px, calc(100vw - 40px)); background:var(--ink); color:var(--paper);
  border:1px solid var(--ink); border-radius:var(--radius); padding:10px 14px;
  font-size:var(--fs-small); }

/* Motion is limited to short colour transitions and the disclosure chevron that
   answers the reader's own click. Nothing animates on load, nothing loops, and this
   is the complete reduced-motion guard. */
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
  // the payload; only their visible labels are translated for the reader.
  var FEEDBACK_DECISIONS = ["apply", "consider", "reject"];
  var RECOMMENDATION_LABELS = {
    apply: "Aplicar", consider: "Considerar", skip: "Descartar",
    need_more_info: "Requiere más información"
  };
  var FEEDBACK_LABELS = { apply: "Aplicar", consider: "Revisar", reject: "Descartar" };
  var AGE_SOURCE_LABELS = {
    posted: "fecha de publicación", first_seen: "primera vez visto", unknown: "origen desconocido"
  };
  var CONTENT_STATUS_LABELS = {
    ok: "contenido completo", partial: "contenido parcial", empty: "sin contenido",
    unknown: "contenido desconocido", blocked: "contenido bloqueado", error: "error de contenido"
  };
  var DUPLICATE_LABELS = {
    new: "nueva", exact_duplicate: "duplicado exacto", probable_duplicate: "duplicado probable"
  };

  var dataNode = document.getElementById("jobscout-data");
  var payload;
  try {
    payload = JSON.parse(dataNode.textContent);
  } catch (err) {
    document.getElementById("offer-count").textContent = "No se pudieron leer los datos del panel.";
    return;
  }
  if (!payload || payload.schema !== EXPECTED_SCHEMA) {
    document.getElementById("offer-count").textContent =
      "Este informe fue generado por una versión incompatible de jobscout; vuelva a generarlo.";
    return;
  }

  // The storage key is derived from the payload schema so the literal is never
  // duplicated: ``jobscout.feedback.v1`` lives only in the payload.
  var STORAGE_KEY = payload.feedback_schema + ":" + payload.project_id;

  var feedback = loadFeedback();
  var filters = { text: "", recommendation: "", minScore: null, keyword: "", review: "", sort: "score" };

  function labelFrom(map, value, fallback) {
    return Object.prototype.hasOwnProperty.call(map, value) ? map[value] : fallback;
  }

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
      box.appendChild(el("p", "muted", "Sin datos"));
      return box;
    }
    var list = el("ul");
    items.forEach(function (item) { list.appendChild(el("li", null, item)); });
    box.appendChild(list);
    return box;
  }

  function metaItem(key, value) {
    var item = el("li");
    item.appendChild(el("span", "meta-key", key));
    item.appendChild(document.createTextNode(" "));
    item.appendChild(el("span", "meta-value", String(value)));
    return item;
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
    document.getElementById("feedback-count").textContent = count + " revisadas";
    document.getElementById("export-feedback").disabled = count === 0;
    var kpi = document.getElementById("kpi-reviewed-value");
    if (kpi) { kpi.textContent = String(count); }
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
    box.appendChild(el("h4", null, "Origen: palabra clave / posición observada"));
    if (!offer.keywords.length) {
      box.appendChild(el("p", "muted", "Sin procedencia guardada"));
      return box;
    }
    var list = el("ul");
    offer.keywords.forEach(function (entry) {
      var rank = entry.rank > 0 ? entry.rank : "sin dato";
      var decision = labelFrom(DUPLICATE_LABELS, entry.decision, entry.decision);
      list.appendChild(el("li", null, entry.keyword + " \u2014 posición observada " + rank +
        " (" + decision + ")"));
    });
    box.appendChild(list);
    return box;
  }

  function renderFeedback(offer) {
    var box = el("section", "feedback");
    // The accessible names carry the card's own title/company, so a screen-reader
    // user can tell one offer's feedback controls from another. The visible
    // heading and the stored decision values stay untouched.
    var offerLabel = offer.title + " \u2014 " + offer.company;
    box.setAttribute("aria-label", "Mi decisión sobre la oferta: " + offerLabel);
    box.appendChild(el("h4", null, "Mi decisión (feedback)"));
    box.appendChild(el("p", "feedback-hint muted",
      "Se guarda solo en este navegador. No postula ni escribe en la base de datos."));
    var entry = feedback[offer.id] || { decision: "", reason: "" };
    var controls = el("div", "feedback-controls");
    controls.setAttribute("role", "group");
    controls.setAttribute("aria-label", "Marca esta oferta: " + offerLabel);
    FEEDBACK_DECISIONS.forEach(function (decision) {
      var decisionText = labelFrom(FEEDBACK_LABELS, decision, decision);
      var button = el("button", "btn decision-btn", decisionText);
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
        // Confirm after the re-render so the marker lands on the card that survived
        // the reviewed/unreviewed filter, not on a node that is about to be replaced.
        syncFeedback(offer.id, { rerender: true });
        if (stored) { confirmSaved(offer.id); }
      });
      controls.appendChild(button);
    });
    box.appendChild(controls);
    var reason = el("textarea");
    reason.placeholder = "Motivo opcional (se guarda en este navegador)";
    reason.setAttribute("aria-label", "Motivo opcional de la decisión");
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
    var state = el("p", "decision-state");
    // The text lives in its own node so the transient save confirmation can be
    // appended next to it without being wiped by the next state update.
    state.appendChild(el("span", "decision-state-text",
      entry.decision
        ? "Tu decisión: " + labelFrom(FEEDBACK_LABELS, entry.decision, entry.decision)
        : "Sin decisión registrada"));
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
    var state = document.querySelector('.card[data-job-id="' + jobId + '"] .decision-state');
    if (!state) { return; }
    var marker = el("span", "decision-saved", "Guardado en este navegador");
    marker.setAttribute("role", "status");
    state.appendChild(marker);
    if (savedTimer) { window.clearTimeout(savedTimer); }
    savedTimer = window.setTimeout(function () { marker.remove(); }, 2600);
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
    if (!card) { return; }
    card.classList.toggle("reviewed", Boolean(entry.decision));
    var buttons = card.querySelectorAll(".feedback-controls .decision-btn");
    FEEDBACK_DECISIONS.forEach(function (decision, index) {
      if (!buttons[index]) { return; }
      var selected = entry.decision === decision;
      buttons[index].classList.toggle("selected", selected);
      buttons[index].setAttribute("aria-pressed", selected ? "true" : "false");
    });
    var state = card.querySelector(".decision-state-text");
    if (state) {
      state.textContent = entry.decision
        ? "Tu decisión: " + labelFrom(FEEDBACK_LABELS, entry.decision, entry.decision)
        : "Sin decisión registrada";
    }
  }

  function renderCard(offer) {
    var card = el("article", "card");
    card.setAttribute("data-job-id", String(offer.id));
    if (offer.recommendation) { card.classList.add("rec-" + offer.recommendation); }

    var head = el("header", "card-head");
    var titleBox = el("div", "card-title");
    titleBox.appendChild(el("h3", null, offer.title));
    // Company and location live here and only here: the meta grid below carries the
    // scoring facts, so the row does not repeat the same two values twice.
    var sub = el("p", "card-sub");
    sub.appendChild(el("span", "company", offer.company));
    if (offer.location) {
      sub.appendChild(el("span", "dot", "\u00b7"));
      sub.appendChild(el("span", "location", offer.location));
    }
    titleBox.appendChild(sub);
    head.appendChild(titleBox);

    var scoreBox = el("div", "card-score");
    var scoreNode;
    if (offer.score === null) {
      scoreNode = el("span", "score", "Sin puntaje");
    } else {
      scoreNode = el("span", "score", offer.score.toFixed(1));
      scoreNode.appendChild(el("span", "score-max", " / 100"));
      scoreNode.setAttribute("aria-label", "Puntaje " + offer.score.toFixed(1) + " sobre 100");
    }
    scoreBox.appendChild(scoreNode);
    var recommendation = offer.recommendation || "unscored";
    var recommendationText = offer.recommendation
      ? labelFrom(RECOMMENDATION_LABELS, offer.recommendation, offer.recommendation)
      : "Sin evaluar";
    var badge = el("span", "badge badge-" + recommendation, recommendationText);
    badge.title = "Recomendación del modelo: " + recommendationText;
    scoreBox.appendChild(badge);
    // The right-hand column: the model's score and badge, then the one control that
    // leaves the report, as a bordered link rather than underlined prose buried in
    // the facts list.
    var side = el("div", "card-side");
    side.appendChild(scoreBox);
    var href = safeHttpUrl(offer.url);
    if (href) {
      var link = el("a", "offer-link", "Ver la oferta");
      link.href = href;
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      link.title = "Abre la oferta en LinkedIn en una pestaña nueva";
      side.appendChild(link);
    }
    head.appendChild(side);
    card.appendChild(head);

    var meta = el("ul", "meta");
    if (offer.posted_at) {
      meta.appendChild(metaItem("Publicada", displayTimestamp(offer.posted_at)));
    }
    var age = (offer.age_days === null)
      ? "antigüedad desconocida"
      : (offer.age_days + (offer.age_days === 1 ? " día" : " días") +
         " (" + labelFrom(AGE_SOURCE_LABELS, offer.age_source, offer.age_source) + ")");
    meta.appendChild(metaItem("Antigüedad", age));
    meta.appendChild(metaItem("Estado", labelFrom(CONTENT_STATUS_LABELS, offer.content_status, offer.content_status)));
    meta.appendChild(metaItem("Oferta", "#" + offer.id));
    if (offer.confidence !== null) { meta.appendChild(metaItem("Confianza", offer.confidence)); }
    if (offer.scorer) {
      var scorerText = offer.scorer + (offer.model ? " (" + offer.model + ")" : "");
      meta.appendChild(metaItem("Evaluador", scorerText));
    }
    card.appendChild(meta);

    if (offer.rationale) {
      var rationale = el("p", "rationale");
      rationale.appendChild(el("strong", null, "Justificación del modelo: "));
      rationale.appendChild(document.createTextNode(offer.rationale));
      card.appendChild(rationale);
    }

    var grid = el("div", "grid");
    grid.appendChild(listBlock("Puntos fuertes", offer.strengths));
    grid.appendChild(listBlock("Brechas", offer.gaps));
    grid.appendChild(listBlock("Bloqueos", offer.blockers));
    grid.appendChild(listBlock("Incógnitas", offer.unknowns));
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
      "Mostrando " + matchesList.length + " de " + payload.offers.length + " ofertas";
  }

  // Only the five inputs that narrow the list; ordering is already reported by the
  // order control itself.
  function activeFilterCount() {
    var count = 0;
    if (filters.text) { count += 1; }
    if (filters.recommendation) { count += 1; }
    if (filters.minScore !== null) { count += 1; }
    if (filters.keyword) { count += 1; }
    if (filters.review) { count += 1; }
    return count;
  }

  // The rail says how much is narrowing the list, and Restablecer is only offered
  // when it would actually change something.
  function refreshFilterState() {
    var count = activeFilterCount();
    var summary = document.getElementById("filters-summary");
    summary.textContent = (count === 0)
      ? "Sin filtros activos"
      : (count === 1 ? "1 filtro activo" : count + " filtros activos");
    summary.classList.toggle("active", count > 0);
    document.getElementById("reset-filters").disabled =
      (count === 0 && filters.sort === "score");
  }

  // Every control that changes the visible set goes through here, so the list and
  // the filter summary can never disagree.
  function applyFilters() {
    renderOffers();
    refreshFilterState();
  }

  function cell(text) { return el("td", null, text); }

  // A rate the stored data cannot support is rendered as "sin dato", never as 0.
  function pct(value) {
    return (value === null || value === undefined)
      ? "sin dato" : Math.round(value * 100) + "%";
  }

  function score(value) {
    return (value === null || value === undefined) ? "sin dato" : value;
  }

  function renderKeywordTable() {
    var host = document.getElementById("keyword-table");
    host.textContent = "";
    if (!payload.keywords.length) {
      host.appendChild(el("p", "muted", "Todavía no hay procedencia de palabras clave registrada."));
      return;
    }
    var table = el("table");
    table.appendChild(el("caption", null,
      "Métricas por palabra clave de origen, calculadas solo sobre ofertas activas. " +
      "Tasa duplicados = apariciones repetidas sobre apariciones totales; Tasa aplicar = " +
      "decisiones humanas de aplicar sobre ofertas revisadas; Frescura = ofertas de " +
      "21 días o menos entre las que tienen fecha conocida; No activas = proporción de " +
      "ofertas ya desactivadas."));
    var thead = el("thead");
    var headRow = el("tr");
    ["Palabra clave", "Apariciones", "Ofertas únicas", "Tasa duplicados",
     "Puntaje promedio", "Top 5", "Top 10", "Con puntaje", "Aplicar", "Considerar",
     "Revisadas", "Tasa aplicar (humano)", "Frescura", "No activas"].forEach(function (label) {
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
      row.appendChild(cell(pct(item.duplicate_rate)));
      row.appendChild(cell(score(item.avg_score)));
      row.appendChild(cell(score(item.top5_avg_score)));
      row.appendChild(cell(score(item.top10_avg_score)));
      row.appendChild(cell(item.scored_jobs));
      row.appendChild(cell(item.apply_count));
      row.appendChild(cell(item.consider_count));
      row.appendChild(cell(item.human_reviewed));
      row.appendChild(cell(pct(item.human_apply_rate)));
      row.appendChild(cell(pct(item.fresh_share)));
      row.appendChild(cell(pct(item.inactive_rate)));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  function renderTopList(hostId, items, describe) {
    var host = document.getElementById(hostId);
    host.textContent = "";
    if (!items.length) {
      host.appendChild(el("p", "muted", "Todavía no hay datos suficientes."));
      return;
    }
    var list = el("ol");
    items.forEach(function (item) {
      list.appendChild(el("li", null, describe(item)));
    });
    host.appendChild(list);
  }

  function renderSummary() {
    var host = document.getElementById("summary");
    host.textContent = "";
    var summary = payload.summary;
    var recommendations = summary.recommendations || {};
    var cards = [
      ["Ofertas mostradas", summary.jobs_shown, ""],
      ["Con puntaje", summary.scored, ""],
      ["Recomendación: aplicar", recommendations.apply || 0, "apply"],
      ["Recomendación: considerar", recommendations.consider || 0, "consider"],
      ["Recomendación: descartar", recommendations.skip || 0, "skip"],
      ["Revisadas (tu decisión)", feedbackCount(), "reviewed"]
    ];
    cards.forEach(function (item) {
      var card = el("article", "kpi" + (item[2] ? " kpi-" + item[2] : ""));
      var value = el("span", "kpi-value", item[1]);
      if (item[2] === "reviewed") { value.id = "kpi-reviewed-value"; }
      card.appendChild(value);
      card.appendChild(el("span", "kpi-label", item[0]));
      host.appendChild(card);
    });
    document.getElementById("summary-note").textContent =
      "Ofertas activas en la base: " + summary.active_jobs +
      " \u00b7 Sin puntaje entre las mostradas: " + summary.unscored +
      " \u00b7 Requieren más información: " + (recommendations.need_more_info || 0) +
      " \u00b7 Palabras clave analizadas: " + summary.keywords +
      " \u00b7 Feedback humano importado: " + (summary.human_reviewed_jobs || 0);
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

  function bindFilters() {
    document.getElementById("filter-text").addEventListener("input", function (event) {
      filters.text = event.target.value.trim();
      applyFilters();
    });
    document.getElementById("filter-recommendation").addEventListener("change", function (event) {
      filters.recommendation = event.target.value;
      applyFilters();
    });
    document.getElementById("filter-min-score").addEventListener("input", function (event) {
      var raw = event.target.value.trim();
      var parsed = parseFloat(raw);
      filters.minScore = raw === "" || isNaN(parsed) ? null : parsed;
      applyFilters();
    });
    document.getElementById("filter-keyword").addEventListener("change", function (event) {
      filters.keyword = event.target.value;
      applyFilters();
    });
    document.getElementById("filter-review").addEventListener("change", function (event) {
      filters.review = event.target.value;
      applyFilters();
    });
    document.getElementById("filter-sort").addEventListener("change", function (event) {
      filters.sort = event.target.value;
      applyFilters();
    });
    document.getElementById("reset-filters").addEventListener("click", function () {
      filters = { text: "", recommendation: "", minScore: null, keyword: "", review: "", sort: "score" };
      document.getElementById("filter-text").value = "";
      document.getElementById("filter-recommendation").value = "";
      document.getElementById("filter-min-score").value = "";
      document.getElementById("filter-keyword").value = "";
      document.getElementById("filter-review").value = "";
      document.getElementById("filter-sort").value = "score";
      applyFilters();
      document.getElementById("filter-text").focus();
    });
    document.getElementById("export-feedback").addEventListener("click", exportFeedback);
  }

  renderSummary();
  populateKeywordFilter();
  renderKeywordTable();
  renderTopList("top-quality", payload.top_quality, function (item) {
    return item.keyword + " \u2014 promedio " + score(item.avg_score) +
      ", top 5 " + score(item.top5_avg_score) + ", " +
      item.scored_jobs + " de " + item.unique_jobs + " oferta(s) con puntaje";
  });
  renderTopList("top-yield", payload.top_yield, function (item) {
    return item.keyword + " \u2014 " + (item.apply_count + item.consider_count) +
      " con recomendación de aplicar o considerar de " +
      item.scored_jobs + " oferta(s) con puntaje";
  });
  renderTopList("top-human", payload.top_human, function (item) {
    return item.keyword + " \u2014 " + pct(item.human_apply_rate) +
      " de aprobación humana (" + item.human_apply + " aplicar, " +
      item.human_consider + " considerar, " + item.human_reject + " descartar, " +
      item.human_reviewed + " revisada(s))";
  });
  bindFilters();
  refreshFeedbackCount();
  applyFilters();
})();

// ---------------------------------------------------------------------------
// Progressive enhancement appended after the report script above. The offer cards,
// their rationale, and their provenance lists are already complete and readable
// without this block; it only adds a native disclosure so a long offer list stays
// fast to scan. <details>/<summary> is used on purpose: the platform owns the
// keyboard and screen-reader behaviour, so no custom ARIA has to reimplement it.
// The report script itself is untouched.
// ---------------------------------------------------------------------------
(function () {
  "use strict";

  var container = document.getElementById("offers");
  if (!container) { return; }

  function enhance(card) {
    if (card.getAttribute("data-explain") === "ready") { return; }
    var grid = card.querySelector(".grid");
    if (!grid) { return; }
    var rationale = card.querySelector(".rationale");
    var block = document.createElement("details");
    block.className = "explain";
    var summary = document.createElement("summary");
    summary.textContent = "Explicabilidad del puntaje y procedencia";
    var body = document.createElement("div");
    body.className = "explain-body";
    if (rationale) { body.appendChild(rationale); }
    body.appendChild(grid);
    block.appendChild(summary);
    block.appendChild(body);
    card.insertBefore(block, card.querySelector(".feedback"));
    card.setAttribute("data-explain", "ready");
  }

  function enhanceAll() {
    var cards = container.querySelectorAll(".card");
    for (var i = 0; i < cards.length; i++) { enhance(cards[i]); }
  }

  // -------------------------------------------------------------------------
  // Keyword table: reorder the rows already in the DOM by clicking a header. The
  // analytics values are computed in Python and never change here; only the display
  // order of the rendered rows does, so sorting costs a handful of comparisons.
  // -------------------------------------------------------------------------
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
    var caption = table.querySelector("caption");
    if (caption) {
      caption.textContent = caption.textContent +
        " Haga clic en un encabezado para ordenar las filas.";
    }
    for (var index = 0; index < headers.length; index++) {
      makeHeaderSortable(table, headers, index);
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

  var keywordTable = document.querySelector("#keyword-table table");
  if (keywordTable) { makeSortable(keywordTable); }

  function setAll(open) {
    var blocks = container.querySelectorAll("details.explain");
    for (var i = 0; i < blocks.length; i++) { blocks[i].open = open; }
  }

  var controls = document.getElementById("explain-controls");
  if (controls) {
    var expand = document.createElement("button");
    expand.type = "button";
    expand.className = "btn btn-ghost";
    expand.textContent = "Expandir todo";
    expand.title = "Abre la explicabilidad de todas las ofertas listadas";
    expand.addEventListener("click", function () { setAll(true); });
    var collapse = document.createElement("button");
    collapse.type = "button";
    collapse.className = "btn btn-ghost";
    collapse.textContent = "Contraer todo";
    collapse.title = "Cierra la explicabilidad de todas las ofertas listadas";
    collapse.addEventListener("click", function () { setAll(false); });
    controls.appendChild(expand);
    controls.appendChild(collapse);
  }

  enhanceAll();
  // renderOffers() rebuilds every card on each filter change, so re-apply the
  // disclosure to the freshly built nodes instead of holding card state.
  if (window.MutationObserver) {
    new MutationObserver(enhanceAll).observe(container, { childList: true });
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
    warnings_html = (
        "".join(f"<p>{html.escape(str(item))}</p>" for item in warnings)
        if warnings
        else "<p>Sin avisos.</p>"
    )
    feedback_schema = str(payload.get("feedback_schema") or FEEDBACK_SCHEMA)
    storage_key = html.escape(f"{feedback_schema}:{payload.get('project_id') or ''}")
    heading = str(label) if label else "informe local"
    title = html.escape(f"Panel de jobscout \u2014 {heading}")
    return _render_template(
        _HTML_TEMPLATE,
        {
            "TITLE": title,
            "STYLE": _STYLE,
            "META": meta,
            "WARNINGS": warnings_html,
            "WARNINGS_CLASS": "notice warn" if warnings else "notice",
            "STORAGE_KEY": storage_key,
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
