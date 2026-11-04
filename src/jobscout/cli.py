"""Command line interface.

Commands
--------
``init-db``          create or migrate the local SQLite database
``ingest-fixture``   ingest a JSON fixture offline (default development path)
``search-linkedin``  bounded, explicitly opted-in LinkedIn guest search
``fetch-details``    bounded, explicitly opted-in job-detail enrichment
``list``             recent jobs with their latest score
``rank``             jobs ordered by score
``score``            evaluate jobs via OpenCode CLI (default), offline, or an LLM endpoint
``dossier``          research a company with OpenCode and write a Markdown dossier
``keyword-plan``     list the bounded keyword families and the command per query (offline)
``import-feedback``  store a dashboard feedback export in SQLite (offline, local)
``calibrate``        compare the latest valid evaluations with imported human feedback (offline)
``prune``            plan the worst quarter of offers and keyword series (offline, read-only)

Exit codes: ``0`` success, ``1`` error, ``2`` refusal (collector disabled,
access denied, or an unsafe request that was intentionally not performed).

Console encoding: on a legacy console (Windows ``cp1252``) stdout/stderr are
configured to backslash-escape characters the code page cannot encode
(``\\uXXXX``) instead of raising ``UnicodeEncodeError`` and aborting a batch.
``--json`` escapes the non-ASCII range only when the stream cannot carry it, so
the parsed JSON value is always identical and a UTF-8 terminal still receives
raw UTF-8. The stream encoding itself is never changed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__
from .calibration import CalibrationError, build_report
from .collectors.base import CollectorDisabledError, CollectorError
from .collectors.fixtures import FixtureDetailTransport, load_detail_fixture
from .collectors.getmanfred import (
    GETMANFRED_TERMS,
    GetManfredSitemapConfig,
    plan_sitemap_urls as plan_getmanfred_urls,
    search_getmanfred,
)
from .collectors.getmanfred_details import (
    fetch_getmanfred_details,
    plan_detail_targets as plan_getmanfred_detail_targets,
)
from .collectors.linkedin import LinkedInSearchConfig, plan_search_urls, search_linkedin
from .collectors.linkedin_details import (
    DetailFetchConfig,
    fetch_linkedin_details,
    plan_detail_targets,
)
from .collectors.tecnoempleo import (
    TECNOEMPLEO_BROWSE_LABEL,
    TECNOEMPLEO_TEXT_QUERIES,
    TecnoempleoSearchConfig,
    browse_plan,
    plan_search_urls as plan_tecnoempleo_urls,
    search_tecnoempleo,
)
from .collectors.tecnoempleo_details import (
    fetch_tecnoempleo_details,
    plan_detail_targets as plan_tecnoempleo_detail_targets,
)
from .config import ConfigError, HARD_MAX_ITEMS, Settings, validate_bounded_int
from .dashboard import (
    DEFAULT_OUTPUT as DASHBOARD_DEFAULT_OUTPUT,
    DashboardError,
    _fetch_imported_feedback,
    generate_dashboard,
    project_key,
)
from .db import (
    active_variants,
    all_active_jobs,
    connect,
    connect_readonly,
    count_active_variants,
    count_jobs,
    count_variants_needing_details,
    count_variants_needing_refresh,
    evaluation_count,
    feedback_count,
    get_job,
    has_successful_evaluation_with_fingerprint,
    init_db,
    job_detail_fetch_count,
    jobs_awaiting_details,
    jobs_needing_evaluation,
    list_feedback,
    list_jobs,
    list_search_runs,
    variant_job_for_source,
    variants_needing_details,
    variants_needing_refresh,
)
from .dossier import company_slug, dossier_path, generate_dossier
from .feedback import FeedbackError, import_feedback
from .keywords import (
    UnknownKeywordFamily,
    planned_queries,
    resolve_families,
    search_command,
)
from .models import EvaluationStatus, ScorerName, SearchRunStatus
from .notify import (
    DEFAULT_NOTIFY_LIMIT,
    build_report as build_notification_report,
    deliver_report,
    disabled_report,
)
from .pipeline import ingest_collector_result, ingest_detail_results, ingest_fixture
from .preference import PreferenceAssessment, PreferenceOutcome, assess_preference
from .pruning import apply_offer_plan, build_plan
from .seniority import seniority_filter_reason
from .scoring import (
    SCORERS,
    effective_fingerprint,
    evaluate_job,
    fingerprint_resolver,
    load_profile,
    stale_job_ids,
)
from .scoring.profile import ProfileError
from .timeutil import iso_days_ago

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FAIL_CLOSED = 2

#: ``fetch-details --refresh`` age floor when ``--stale-days`` is omitted. ``0``
#: means "no age floor": every already-fetched offer with stale or retryable
#: evidence is a candidate, and an explicit ``--stale-days N`` narrows that.
DEFAULT_REFRESH_STALE_DAYS = 0

SCORER_CHOICES = tuple(SCORERS)

#: ``score`` stops the batch after this many consecutive provider errors.
#: The provider is down, so the next attempts would fail the same way; the
#: remaining offers are left for the next run without an evaluation row.
MAX_CONSECUTIVE_PROVIDER_ERRORS = 3


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

#: Error handler applied to the CLI's standard streams. ``backslashreplace``
#: keeps a batch alive on a legacy console: a character the active code page
#: cannot represent is written as ``\uXXXX`` instead of raising
#: ``UnicodeEncodeError``. The stream *encoding* is never changed, so a UTF-8
#: terminal still receives the original characters unchanged.
STREAM_ERROR_HANDLER = "backslashreplace"


def configure_output_streams(streams: Sequence[Any] | None = None) -> None:
    """Make ``stdout``/``stderr`` tolerate characters the code page lacks.

    On Windows the console encoding is often ``cp1252``, which cannot encode
    characters that routinely appear in model output (U+2011 NON-BREAKING
    HYPHEN, arrows, emoji). A single such character used to abort a whole
    ``score --scorer opencode`` batch while printing one job. Relaxing only the
    error handler preserves the encoding, so valid UTF-8 output is unaffected.
    """
    targets = (sys.stdout, sys.stderr) if streams is None else tuple(streams)
    for stream in targets:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors=STREAM_ERROR_HANDLER)
        except (ValueError, OSError, TypeError):
            # Redirected, detached, or closed streams keep their own policy;
            # :func:`_write_line` still provides a safe boundary below.
            continue


def _stream_encoding(stream: Any) -> str:
    return getattr(stream, "encoding", None) or "utf-8"


def _write_line(text: str = "", *, stream: Any = None) -> None:
    """Write one line, escaping characters the target stream cannot encode.

    This is the CLI's print boundary. It mirrors ``print(text)`` but never lets
    a legacy console encoding abort a command: an unencodable character is
    backslash-escaped for display instead of raising ``UnicodeEncodeError``.
    Streams that :func:`configure_output_streams` could not reconfigure (test
    capture objects, injected dummies, closed wrappers) are still safe.
    """
    target = sys.stdout if stream is None else stream
    rendered = text + "\n"
    try:
        target.write(rendered)
    except UnicodeEncodeError:
        encoding = _stream_encoding(target)
        try:
            escaped = rendered.encode(encoding, errors=STREAM_ERROR_HANDLER).decode(
                encoding, errors="replace"
            )
        except (LookupError, UnicodeError):  # pragma: no cover - unknown codec
            escaped = rendered.encode("ascii", errors=STREAM_ERROR_HANDLER).decode("ascii")
        target.write(escaped)


def _emit(payload: dict[str, Any], *, as_json: bool, text: str) -> None:
    if as_json:
        document = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        try:
            document.encode(_stream_encoding(sys.stdout))
        except (UnicodeEncodeError, LookupError):
            # The stream cannot carry the raw characters, so escape every
            # non-ASCII character. That is still the exact same JSON value, and
            # ``\uXXXX`` escapes are valid JSON where the stream-level
            # ``backslashreplace`` output for astral characters (``\U0001f600``)
            # is not. A UTF-8 stream keeps the original characters and bytes.
            document = json.dumps(payload, ensure_ascii=True, indent=2, default=str)
        _write_line(document)
    else:
        _write_line(text)


def _preference_marker(preference: PreferenceAssessment | None) -> str:
    """A short text marker for a preference verdict, empty when nothing to say.

    A PASS is silent: it is the default and printing it on every line would bury
    the offers that need a human look. FAIL and FLAG are surfaced.
    """
    if preference is None or preference.outcome is PreferenceOutcome.PASS:
        return ""
    return f"  [preference={preference.outcome}]"


def _job_line(
    item: Any,
    preference: PreferenceAssessment | None = None,
    needs_rescore: bool | None = None,
) -> str:
    job = item.job
    score = "  n/a" if item.score is None else f"{item.score:5.1f}"
    recommendation = item.recommendation or "-"
    location = f" ({job.location})" if job.location else ""
    stale = "  [needs re-score]" if needs_rescore else ""
    return (
        f"#{job.id:<4} {score}  {recommendation:<14} {job.title or '(untitled)'}"
        f" @ {job.company or '(unknown company)'}{location}  [{job.content_status}]"
        f"{stale}{_preference_marker(preference)}"
    )


def _job_payload(
    item: Any,
    preference: PreferenceAssessment | None = None,
    needs_rescore: bool | None = None,
) -> dict[str, Any]:
    """One list/rank entry, with the preference verdict attached when computed."""
    payload = item.to_dict()
    payload["needs_rescore"] = needs_rescore
    if preference is not None:
        payload["preference"] = preference.to_dict()
    return payload


def _print_evaluation(evaluation: Any, preference: PreferenceAssessment | None = None) -> None:
    header = (
        f"job #{evaluation.job_id}  scorer={evaluation.scorer}  "
        f"status={evaluation.status}  score={evaluation.overall_score}  "
        f"recommendation={evaluation.recommendation}  confidence={evaluation.confidence}"
    )
    _write_line(header)
    if evaluation.rationale:
        _write_line(f"  rationale: {evaluation.rationale}")
    if preference is not None:
        _write_line(
            f"  preference: {preference.outcome}  {preference.reason}"
        )
        for item in preference.evidence:
            _write_line(f"      evidence: {item}")
    for dimension in evaluation.dimensions:
        _write_line(
            f"  - {dimension.name}: {dimension.score} (weight {dimension.weight}) "
            f"{dimension.rationale}"
        )
        for evidence in dimension.evidence:
            _write_line(f"      evidence: {evidence}")
    for label, values in (
        ("blockers", evaluation.blockers),
        ("strengths", evaluation.strengths),
        ("gaps", evaluation.gaps),
        ("unknowns", evaluation.unknowns),
    ):
        for value in values:
            _write_line(f"  {label}: {value}")
    if evaluation.error:
        _write_line(f"  error: {evaluation.error}")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _add_common_arguments(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    default: Any = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=default,
        help="SQLite database path (default: JOBSCOUT_DB_PATH or data/jobscout.sqlite3)",
    )
    parser.add_argument(
        "--profile",
        metavar="PATH",
        default=default,
        help="candidate profile YAML/JSON (default: JOBSCOUT_PROFILE_PATH)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="emit machine-readable JSON",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobscout",
        description=(
            "Local-first job search collector, deduplicator, and explainable fit scorer. "
            "LinkedIn collection is disabled unless --confirm-network is passed."
        ),
    )
    parser.add_argument("--version", action="version", version=f"jobscout {__version__}")
    _add_common_arguments(parser, suppress_defaults=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        _add_common_arguments(sub, suppress_defaults=True)
        return sub

    init_parser = add_common(subparsers.add_parser("init-db", help="create the SQLite schema"))
    init_parser.set_defaults(handler=cmd_init_db)

    fixture_parser = add_common(
        subparsers.add_parser("ingest-fixture", help="ingest a local JSON fixture (offline)")
    )
    fixture_parser.add_argument("path", help="path to the JSON fixture")
    fixture_parser.add_argument("--source", default="fixture", help="source label (default: fixture)")
    fixture_parser.add_argument("--label", default=None, help="human label for the search run")
    fixture_parser.set_defaults(handler=cmd_ingest_fixture)

    search_parser = add_common(
        subparsers.add_parser(
            "search-linkedin",
            help="bounded LinkedIn guest search (opt-in, disabled by default)",
        )
    )
    search_parser.add_argument("--keywords", required=True, help="search keywords")
    search_parser.add_argument("--location", default=None, help="search location")
    search_parser.add_argument("--distance", type=int, default=None, help="distance in miles")
    search_parser.add_argument("--remote", action="store_true", help="request remote results only")
    search_parser.add_argument("--employment-type", default=None, help="employment type filter")
    search_parser.add_argument("--max-pages", type=int, default=None, help="page cap (bounded)")
    search_parser.add_argument("--max-items", type=int, default=None, help="item cap (bounded)")
    search_parser.add_argument(
        "--cycle-id",
        default=None,
        metavar="ID",
        help="group this run with comparable runs from the same search cycle",
    )
    search_parser.add_argument(
        "--confirm-network",
        action="store_true",
        help="required acknowledgement that real requests will be sent",
    )
    search_parser.add_argument(
        "--dry-run", action="store_true", help="print the request plan without sending anything"
    )
    search_parser.set_defaults(handler=cmd_search_linkedin)

    getmanfred_parser = add_common(
        subparsers.add_parser(
            "search-getmanfred",
            help=(
                "bounded GetManfred sitemap inventory (opt-in, disabled by "
                "default; optional local keyword filter, no fan-out)"
            ),
        )
    )
    getmanfred_parser.add_argument(
        "--keywords",
        default=None,
        help=(
            "optional approved keyword; applied locally to the sitemap "
            "(title/slug match), no per-keyword request"
        ),
    )
    getmanfred_parser.add_argument(
        "--max-documents", type=int, default=None, help="sitemap document cap (bounded)"
    )
    getmanfred_parser.add_argument(
        "--max-items", type=int, default=None, help="item cap (bounded)"
    )
    getmanfred_parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help=(
            "skip offers whose sitemap lastmod is older than this many days "
            "(0 = no cut; default: settings value)"
        ),
    )
    getmanfred_parser.add_argument(
        "--cycle-id",
        default=None,
        metavar="ID",
        help="group this inventory with comparable runs from the same cycle",
    )
    getmanfred_parser.add_argument(
        "--confirm-network",
        action="store_true",
        help="required acknowledgement that real requests will be sent",
    )
    getmanfred_parser.add_argument(
        "--dry-run", action="store_true", help="print the request plan without sending anything"
    )
    getmanfred_parser.add_argument("--label", default=None, help="human label for the run")
    getmanfred_parser.set_defaults(handler=cmd_search_getmanfred)

    tecnoempleo_parser = add_common(
        subparsers.add_parser(
            "search-tecnoempleo",
            help=(
                "bounded Tecnoempleo public keyword search (opt-in, disabled by default)"
            ),
        )
    )
    tecnoempleo_parser.add_argument(
        "--browse",
        action="store_true",
        help=(
            "browse offers by professional function in Madrid; mutually "
            "exclusive with --keywords and --location"
        ),
    )
    tecnoempleo_parser.add_argument(
        "--keywords", default=None, help="search keywords (text mode)"
    )
    tecnoempleo_parser.add_argument("--location", default=None, help="province slug filter")
    tecnoempleo_parser.add_argument(
        "--max-pages", type=int, default=None, help="page cap (bounded)"
    )
    tecnoempleo_parser.add_argument(
        "--max-items", type=int, default=None, help="item cap (bounded)"
    )
    tecnoempleo_parser.add_argument(
        "--cycle-id",
        default=None,
        metavar="ID",
        help="group this run with comparable runs from the same search cycle",
    )
    tecnoempleo_parser.add_argument(
        "--confirm-network",
        action="store_true",
        help="required acknowledgement that real requests will be sent",
    )
    tecnoempleo_parser.add_argument(
        "--dry-run", action="store_true", help="print the request plan without sending anything"
    )
    tecnoempleo_parser.add_argument("--label", default=None, help="human label for the run")
    tecnoempleo_parser.set_defaults(handler=cmd_search_tecnoempleo)

    details_parser = add_common(
        subparsers.add_parser(
            "fetch-details",
            help="enrich stored jobs with full descriptions (opt-in, disabled by default)",
        )
    )
    details_selection = details_parser.add_mutually_exclusive_group()
    details_selection.add_argument(
        "--job-id", type=int, action="append", help="job id (repeatable)"
    )
    details_selection.add_argument(
        "--all", action="store_true", help="select every active job (not just un-enriched ones)"
    )
    details_selection.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "select active offers whose latest fetch evidence warrants a refresh "
            "(stale ok/partial, transient failures without an HTTP response, or "
            "404/410 refuted by a later re-sighting)"
        ),
    )
    details_parser.add_argument(
        "--stale-days",
        type=int,
        default=None,
        metavar="DAYS",
        help=(
            "age floor in days for --refresh; 0 or greater and only valid with "
            f"--refresh (default: {DEFAULT_REFRESH_STALE_DAYS})"
        ),
    )
    details_parser.add_argument(
        "--max-items", type=int, default=None, help="item cap (bounded)"
    )
    details_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request plan without sending anything or writing data",
    )
    details_parser.add_argument(
        "--confirm-network",
        action="store_true",
        help="required acknowledgement that real requests will be sent",
    )
    details_parser.add_argument(
        "--fixture",
        metavar="PATH",
        default=None,
        help="offline detail responses (JSON); never touches the network",
    )
    details_parser.add_argument("--label", default=None, help="human label for the run")
    details_parser.set_defaults(handler=cmd_fetch_details)

    tecno_details_parser = add_common(
        subparsers.add_parser(
            "fetch-tecnoempleo-details",
            help=(
                "enrich stored Tecnoempleo offers with their public detail page "
                "(opt-in, disabled by default)"
            ),
        )
    )
    tecno_details_selection = tecno_details_parser.add_mutually_exclusive_group()
    tecno_details_selection.add_argument(
        "--job-id", type=int, action="append", help="job id (repeatable)"
    )
    tecno_details_selection.add_argument(
        "--all", action="store_true", help="select every active Tecnoempleo offer"
    )
    tecno_details_parser.add_argument(
        "--max-items", type=int, default=None, help="item cap (bounded)"
    )
    tecno_details_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request plan without sending anything or writing data",
    )
    tecno_details_parser.add_argument(
        "--confirm-network",
        action="store_true",
        help="required acknowledgement that real requests will be sent",
    )
    tecno_details_parser.add_argument(
        "--robots",
        metavar="PATH",
        default=None,
        help=(
            "use this local robots.txt text instead of re-fetching it before a "
            "confirmed run (still requires --confirm-network for the detail requests)"
        ),
    )
    tecno_details_parser.add_argument("--label", default=None, help="human label for the run")
    tecno_details_parser.set_defaults(handler=cmd_fetch_tecnoempleo_details)

    getmanfred_details_parser = add_common(
        subparsers.add_parser(
            "fetch-getmanfred-details",
            help=(
                "enrich stored GetManfred offers with their public detail page "
                "(opt-in, disabled by default)"
            ),
        )
    )
    getmanfred_details_selection = (
        getmanfred_details_parser.add_mutually_exclusive_group()
    )
    getmanfred_details_selection.add_argument(
        "--job-id", type=int, action="append", help="job id (repeatable)"
    )
    getmanfred_details_selection.add_argument(
        "--all", action="store_true", help="select every active GetManfred offer"
    )
    getmanfred_details_parser.add_argument(
        "--max-items", type=int, default=None, help="item cap (bounded)"
    )
    getmanfred_details_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request plan without sending anything or writing data",
    )
    getmanfred_details_parser.add_argument(
        "--confirm-network",
        action="store_true",
        help="required acknowledgement that real requests will be sent",
    )
    getmanfred_details_parser.add_argument(
        "--robots",
        metavar="PATH",
        default=None,
        help=(
            "use this local robots.txt text instead of re-fetching it before a "
            "confirmed run (still requires --confirm-network for the detail requests)"
        ),
    )
    getmanfred_details_parser.add_argument(
        "--label", default=None, help="human label for the run"
    )
    getmanfred_details_parser.set_defaults(handler=cmd_fetch_getmanfred_details)

    list_parser = add_common(subparsers.add_parser("list", help="list recent jobs"))
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--min-score", type=float, default=None)
    list_parser.add_argument("--include-inactive", action="store_true")
    list_parser.set_defaults(handler=cmd_list)

    rank_parser = add_common(
        subparsers.add_parser("rank", help="rank jobs that already have a successful score")
    )
    rank_parser.add_argument("--top", type=int, default=20)
    rank_parser.add_argument("--min-score", type=float, default=None)
    rank_parser.set_defaults(handler=cmd_rank)

    score_parser = add_common(subparsers.add_parser("score", help="evaluate jobs"))
    selection = score_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--job-id", type=int, action="append", help="job id (repeatable)")
    selection.add_argument("--all", action="store_true", help="select every active job")
    score_parser.add_argument(
        "--scorer",
        choices=SCORER_CHOICES,
        default=ScorerName.OPENCODE.value,
        help="scorer backend (default: OpenCode with the configured model)",
    )
    score_parser.add_argument(
        "--scorer-model",
        default=None,
        help=(
            "OpenCode model for this run, overrides JOBSCOUT_OPENCODE_MODEL, "
            "e.g. opencode-go/deepseek-v4.1-flash"
        ),
    )
    score_parser.add_argument(
        "--effort",
        default=None,
        help=(
            "OpenCode variant/effort for this run, overrides "
            "JOBSCOUT_OPENCODE_VARIANT, e.g. low/high/max; '' sends no variant; "
            "also sizes the per-offer timeout unless "
            "JOBSCOUT_OPENCODE_TIMEOUT_SECONDS is set"
        ),
    )
    score_parser.add_argument("--limit", type=int, default=None)
    score_parser.add_argument(
        "--rescore",
        action="store_true",
        help="force re-evaluation even when the scoring input is unchanged",
    )
    score_parser.set_defaults(handler=cmd_score)

    dossier_parser = add_common(
        subparsers.add_parser(
            "dossier",
            help="research a company with OpenCode and write a Markdown dossier",
        )
    )
    dossier_parser.add_argument(
        "--job-id",
        type=int,
        action="append",
        default=[],
        help="job id whose company to research (repeatable)",
    )
    dossier_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "research every active job whose imported feedback decision is "
            "'apply'"
        ),
    )
    dossier_parser.add_argument(
        "--refresh",
        action="store_true",
        help="regenerate a dossier even when its file already exists",
    )
    dossier_parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="seconds per company (default: 600; research is slower than scoring)",
    )
    dossier_parser.set_defaults(handler=cmd_dossier)

    runs_parser = add_common(subparsers.add_parser("runs", help="show recent search runs"))
    runs_parser.add_argument("--limit", type=int, default=10)
    runs_parser.set_defaults(handler=cmd_runs)

    keyword_plan_parser = add_common(
        subparsers.add_parser(
            "keyword-plan",
            help=(
                "list the bounded keyword families and the exact search command per "
                "query (offline, no request)"
            ),
        )
    )
    keyword_plan_parser.add_argument(
        "--family",
        action="append",
        default=None,
        metavar="ID",
        help=(
            "keyword family id to include (repeatable; default: "
            "JOBSCOUT_KEYWORD_FAMILIES, or every family when that is unset)"
        ),
    )
    keyword_plan_parser.add_argument(
        "--location",
        default=None,
        help="location to include in the printed preview commands",
    )
    keyword_plan_parser.add_argument(
        "--remote",
        action="store_true",
        help="include the remote-only flag in the printed preview commands",
    )
    keyword_plan_parser.set_defaults(handler=cmd_keyword_plan)

    dashboard_parser = add_common(
        subparsers.add_parser(
            "dashboard",
            help="write a self-contained local HTML report (read-only, offline)",
        )
    )
    dashboard_parser.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=f"output HTML path (default: {DASHBOARD_DEFAULT_OUTPUT})",
    )
    dashboard_parser.add_argument(
        "--top",
        type=int,
        default=None,
        help=(
            "maximum offers to embed, best scored first "
            "(default: every active offer)"
        ),
    )
    dashboard_parser.set_defaults(handler=cmd_dashboard)

    feedback_parser = add_common(
        subparsers.add_parser(
            "import-feedback",
            help="import a dashboard feedback export (jobscout.feedback.v1) into SQLite",
        )
    )
    feedback_parser.add_argument("path", help="path to the exported jobscout-feedback.json")
    feedback_parser.add_argument(
        "--project-id",
        default=None,
        help=(
            "expected project id (default: derived from --db); pass the export's own "
            "id only when the database moved and the dashboard cannot be regenerated"
        ),
    )
    feedback_parser.set_defaults(handler=cmd_import_feedback)

    calibrate_parser = add_common(
        subparsers.add_parser(
            "calibrate",
            help=(
                "compare the latest valid evaluations with imported human feedback "
                "(read-only, offline)"
            ),
        )
    )
    calibrate_parser.add_argument(
        "--scorer",
        choices=SCORER_CHOICES,
        default=None,
        help=(
            "only compare evaluations from this scorer (default: each job's latest "
            "valid evaluation, regardless of scorer)"
        ),
    )
    calibrate_parser.set_defaults(handler=cmd_calibrate)

    notify_parser = add_common(
        subparsers.add_parser(
            "notify",
            help=(
                "preview local notifications for high-value offers "
                "(offline, opt-in, disabled by default)"
            ),
        )
    )
    notify_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "maximum notifications to report, bounded by "
            f"{HARD_MAX_ITEMS} (default: {DEFAULT_NOTIFY_LIMIT})"
        ),
    )
    notify_parser.add_argument(
        "--deliver",
        action="store_true",
        help=(
            "send via the configured local channel after previewing; without "
            "it the command only prints"
        ),
    )
    notify_parser.set_defaults(handler=cmd_notify)

    prune_parser = add_common(
        subparsers.add_parser(
            "prune",
            help=(
                "plan the worst quarter of scored offers and provider-specific "
                "keyword series (offline, read-only preview by default)"
            ),
        )
    )
    prune_parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "deactivate the offers the plan selected; without it the command "
            "only prints the candidates and writes nothing"
        ),
    )
    prune_parser.set_defaults(handler=cmd_prune)

    return parser


def resolve_settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    return settings.with_overrides(
        db_path=Path(args.db) if getattr(args, "db", None) else None,
        profile_path=Path(args.profile) if getattr(args, "profile", None) else None,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init_db(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    try:
        version = init_db(conn)
        total = count_jobs(conn)
    finally:
        conn.close()
    _emit(
        {"db_path": str(settings.db_path), "schema_version": version, "jobs": total},
        as_json=getattr(args, "as_json", False),
        text=f"initialized {settings.db_path} (schema v{version}, {total} job(s) stored)",
    )
    return EXIT_OK


def cmd_ingest_fixture(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        outcome = ingest_fixture(
            conn, args.path, source=args.source, label=args.label, settings=settings
        )
    finally:
        conn.close()
    _emit(
        outcome.to_dict(),
        as_json=getattr(args, "as_json", False),
        text=(
            f"run #{outcome.run_id}: {outcome.items_found} posting(s), "
            f"{outcome.new_jobs} new, {outcome.duplicate_jobs} duplicate(s)"
        ),
    )
    return EXIT_OK


def _run_search(
    args: argparse.Namespace,
    settings: Settings,
    *,
    config: Any,
    plan_fn: Callable[[Any], list[str]],
    refusal_message: str,
    collect_fn: Callable[..., Any],
    label: str | None = None,
) -> int:
    """Shared dry-run/gate/collect/emit skeleton for the search commands.

    ``plan_fn`` returns the planned URLs for ``--dry-run``; ``collect_fn`` is the
    provider collector (called with ``network_enabled=True``); ``label`` is the
    optional run label. The refusal message is provider-specific, so the caller
    builds it where the config metrics are known.
    """
    if args.dry_run:
        planned = plan_fn(config)
        _emit(
            {"dry_run": True, "planned_urls": planned},
            as_json=getattr(args, "as_json", False),
            text="dry run, no request performed. Planned requests:\n  "
            + "\n  ".join(planned),
        )
        return EXIT_OK

    if not args.confirm_network:
        _write_line(refusal_message, stream=sys.stderr)
        return EXIT_FAIL_CLOSED

    conn = connect(settings.db_path)
    try:
        init_db(conn)
        try:
            result = collect_fn(config, settings=settings, network_enabled=True)
        except CollectorDisabledError as exc:  # pragma: no cover - guarded above
            _write_line(f"collector disabled: {exc}", stream=sys.stderr)
            return EXIT_FAIL_CLOSED
        outcome = ingest_collector_result(
            conn,
            result,
            config=config,
            settings=settings,
            cycle_id=getattr(args, "cycle_id", None),
            label=label,
        )
    except CollectorError as exc:
        _write_line(f"collection failed: {exc}", stream=sys.stderr)
        return EXIT_FAIL_CLOSED
    finally:
        conn.close()

    _emit(
        {**outcome.to_dict(), "planned_urls": result.planned_urls},
        as_json=getattr(args, "as_json", False),
        text=(
            f"run #{outcome.run_id} [{outcome.status}]: {outcome.items_found} item(s), "
            f"{outcome.new_jobs} new, {outcome.duplicate_jobs} duplicate(s)"
            + (f"\nnote: {outcome.notes}" if outcome.notes else "")
            + (f"\nerror: {outcome.error}" if outcome.error else "")
        ),
    )
    return _collection_exit_code(outcome.status)


def cmd_search_linkedin(args: argparse.Namespace, settings: Settings) -> int:
    try:
        config = LinkedInSearchConfig(
            keywords=args.keywords,
            location=args.location,
            distance_miles=args.distance,
            remote=args.remote,
            employment_type=args.employment_type,
            max_pages=args.max_pages if args.max_pages is not None else settings.max_pages,
            max_items=args.max_items if args.max_items is not None else settings.max_items,
        )
    except (ConfigError, ValueError) as exc:
        _write_line(f"invalid search options: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    refusal = (
        "refusing to run: LinkedIn collection is disabled by default.\n"
        "Rerun with --confirm-network to send bounded requests "
        f"(max {config.max_pages} page(s), {config.max_items} item(s), "
        f"{settings.request_delay_seconds:g}s delay), or use --dry-run to inspect the plan."
    )
    return _run_search(
        args,
        settings,
        config=config,
        plan_fn=lambda config: plan_search_urls(config, settings=settings),
        refusal_message=refusal,
        collect_fn=search_linkedin,
    )


#: One collector's terminal status -> the CLI's exit code. Shared so every
#: provider reports the same fail-closed/failure/success semantics.
def _collection_exit_code(status: SearchRunStatus) -> int:
    if status == SearchRunStatus.FAILED_CLOSED:
        return EXIT_FAIL_CLOSED
    if status == SearchRunStatus.FAILED:
        return EXIT_ERROR
    return EXIT_OK


def cmd_search_getmanfred(args: argparse.Namespace, settings: Settings) -> int:
    """Run the bounded GetManfred sitemap inventory (optional local keyword filter)."""
    try:
        config = GetManfredSitemapConfig(
            sitemap_url=settings.getmanfred_sitemap_url,
            offer_path_prefix=settings.getmanfred_offer_path_prefix,
            keywords=args.keywords,
            max_documents=(
                args.max_documents
                if args.max_documents is not None
                else settings.getmanfred_max_documents
            ),
            max_items=(
                args.max_items
                if args.max_items is not None
                else settings.getmanfred_max_items
            ),
            max_age_days=(
                args.max_age_days
                if args.max_age_days is not None
                else settings.getmanfred_max_age_days
            ),
        )
    except (ConfigError, ValueError) as exc:
        _write_line(f"invalid GetManfred options: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    refusal = (
        "refusing to run: GetManfred collection is disabled by default.\n"
        "Rerun with --confirm-network to send bounded requests "
        f"(max {config.max_documents} sitemap document(s), {config.max_items} item(s), "
        f"{settings.request_delay_seconds:g}s delay), or use --dry-run to inspect the plan."
    )
    return _run_search(
        args,
        settings,
        config=config,
        plan_fn=plan_getmanfred_urls,
        refusal_message=refusal,
        collect_fn=search_getmanfred,
        label=args.label,
    )


def cmd_search_tecnoempleo(args: argparse.Namespace, settings: Settings) -> int:
    """Run the bounded Tecnoempleo public search (text query or function browse)."""
    if args.browse and (args.keywords is not None or args.location is not None):
        _write_line(
            "usage error: --browse cannot be combined with --keywords or --location",
            stream=sys.stderr,
        )
        return EXIT_ERROR

    try:
        config = TecnoempleoSearchConfig(
            keywords=TECNOEMPLEO_BROWSE_LABEL if args.browse else args.keywords,
            location=None if args.browse else args.location,
            search_url=settings.tecnoempleo_search_url,
            robots_url=settings.tecnoempleo_robots_url,
            offer_path_marker=settings.tecnoempleo_offer_path_marker,
            max_pages=(
                args.max_pages
                if args.max_pages is not None
                else settings.tecnoempleo_max_pages
            ),
            max_items=(
                args.max_items
                if args.max_items is not None
                else settings.tecnoempleo_max_items
            ),
            browse=args.browse,
        )
    except (ConfigError, ValueError) as exc:
        _write_line(f"invalid Tecnoempleo options: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    refusal = (
        "refusing to run: Tecnoempleo collection is disabled by default.\n"
        "Rerun with --confirm-network to send bounded requests "
        f"(max {config.max_pages} page(s), {config.max_items} item(s), "
        f"{settings.request_delay_seconds:g}s delay), or use --dry-run to inspect the plan."
    )
    return _run_search(
        args,
        settings,
        config=config,
        plan_fn=plan_tecnoempleo_urls,
        refusal_message=refusal,
        collect_fn=search_tecnoempleo,
        label=args.label,
    )


def _effective_stale_days(args: argparse.Namespace) -> int:
    """Resolve ``--stale-days`` to its effective value (``None`` -> default)."""
    return DEFAULT_REFRESH_STALE_DAYS if args.stale_days is None else int(args.stale_days)


def _refresh_cutoff(args: argparse.Namespace) -> str:
    """Absolute UTC cutoff a refresh uses for stale ``ok``/``partial`` evidence."""
    return iso_days_ago(_effective_stale_days(args))


def _refresh_output_fields(
    args: argparse.Namespace, cutoff: str | None
) -> dict[str, Any]:
    """Additive JSON fields for a refresh selection (``{}`` otherwise).

    Keeping these out of every other selection means the existing ``--job-id``,
    ``--all`` and default outputs stay byte-for-byte compatible.
    """
    if not getattr(args, "refresh", False) or cutoff is None:
        return {}
    return {
        "selection": "refresh",
        "stale_days": _effective_stale_days(args),
        "cutoff": cutoff,
    }


def _refresh_text_line(args: argparse.Namespace, cutoff: str) -> str:
    days = _effective_stale_days(args)
    label = "day" if days == 1 else "days"
    return (
        f"refresh selection: active offers with stale or retryable evidence "
        f"(stale-days {days} {label}, cutoff {cutoff})"
    )


def _select_detail_jobs(
    conn: Any,
    args: argparse.Namespace,
    *,
    source: str = "linkedin",
    provider_label: str = "LinkedIn",
    limit: int,
    refresh_cutoff: str | None = None,
) -> tuple[list[Any], int]:
    """Resolve the selection for a detail-fetch command.

    Returns ``(jobs, omitted)`` where ``jobs`` is bounded to ``limit`` and
    ``omitted`` is how many selected jobs were dropped by that cap. The count
    lets the CLI report truncation instead of always claiming zero.

    Every route is restricted to ``source`` (default, ``--all``, ``--refresh``,
    and explicit ``--job-id``). Each fetcher builds a URL inside its provider's
    id space, so letting another provider's numeric id into this selection would
    read a different posting's body. ``provider_label`` only names the provider
    in the "not found" message; ``--refresh`` is a no-op for providers whose
    subcommand does not define it.
    """
    if args.job_id:
        selected = []
        found: set[int] = set()
        for job_id in args.job_id:
            job = variant_job_for_source(conn, job_id, source)
            if job is not None:
                selected.append(job)
                found.add(job_id)
        for missing in [job_id for job_id in args.job_id if job_id not in found]:
            _write_line(
                f"job #{missing} not found among {provider_label} offers",
                stream=sys.stderr,
            )
        return selected[:limit], max(0, len(selected) - limit)
    if args.all:
        total = count_active_variants(conn, source=source)
        return (
            active_variants(conn, source=source, limit=limit),
            max(0, total - limit),
        )
    if getattr(args, "refresh", False):
        cutoff = refresh_cutoff or _refresh_cutoff(args)
        total = count_variants_needing_refresh(conn, source=source, cutoff=cutoff)
        return (
            variants_needing_refresh(
                conn, source=source, cutoff=cutoff, limit=limit
            ),
            max(0, total - limit),
        )
    total = count_variants_needing_details(conn, source=source)
    return (
        variants_needing_details(conn, source=source, limit=limit),
        max(0, total - limit),
    )


def _open_existing_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open an existing database for reading only, or return ``None``.

    A dry run must not create a database or write schema/data. This helper only
    opens a database that already exists through the read-only URI in
    :func:`jobscout.db.connect_readonly` (``file:...?mode=ro``, plus
    ``PRAGMA query_only``) so no write can slip through. It intentionally
    bypasses :func:`jobscout.db.connect`, which would create the parent
    directory, a new file, and WAL journals. A file that exists but cannot be
    opened read-only is treated as "nothing to plan" rather than a crash.
    """
    try:
        return connect_readonly(db_path)
    except sqlite3.Error:
        return None


def _run_detail_dry_run(
    args: argparse.Namespace,
    settings: Settings,
    *,
    max_items: int,
    select_fn: Callable[..., tuple[list[Any], int]],
    plan_fn: Callable[..., tuple[list[Any], int]],
    unavailable_label: str,
    refresh_cutoff: str | None = None,
) -> int:
    """Plan a detail-fetch command without creating or writing a database.

    ``select_fn`` scopes the selection to one provider; ``plan_fn(jobs,
    max_items=)`` resolves fetch targets; ``unavailable_label`` names the id or
    URL the dry-run could not resolve. ``_open_existing_readonly`` is intentional:
    a dry run must not create a database or write schema/data.
    """
    jobs: list[Any] = []
    omitted = 0
    note: str | None = None
    conn = _open_existing_readonly(Path(settings.db_path))
    if conn is None:
        note = f"no database at {settings.db_path}; nothing to plan"
    else:
        try:
            jobs, omitted = select_fn(
                conn, args, limit=max_items, refresh_cutoff=refresh_cutoff
            )
        except sqlite3.DatabaseError:
            # Covers a database with no jobscout schema as well as an
            # unreadable/corrupt file: plan nothing instead of crashing.
            note = "database could not be read or is not initialized; nothing to plan"
        finally:
            conn.close()

    targets, _ = plan_fn(jobs, max_items=max_items)
    planned = [target.url for target in targets if target.url]
    skipped = [target.reason for target in targets if target.url is None]
    text = "dry run, no request performed and no data written. Planned requests:\n  " + (
        "\n  ".join(planned) or "(none)"
    )
    if note:
        text += f"\n  {note}"
    if getattr(args, "refresh", False) and refresh_cutoff:
        text += f"\n  {_refresh_text_line(args, refresh_cutoff)}"
    if omitted:
        text += f"\n  {omitted} selected job(s) omitted by --max-items"
    if skipped:
        text += (
            f"\n  {len(skipped)} selected job(s) have no resolvable {unavailable_label}"
        )
    _emit(
        {
            "dry_run": True,
            "selected": len(targets),
            "truncated": omitted,
            "planned_urls": planned,
            "skipped_reasons": skipped,
            "note": note,
            **_refresh_output_fields(args, refresh_cutoff),
        },
        as_json=getattr(args, "as_json", False),
        text=text,
    )
    return EXIT_OK


def _finish_detail_run(
    args: argparse.Namespace,
    outcome: Any,
    result: Any,
    *,
    refresh_cutoff: str | None = None,
) -> int:
    """Emit a detail run's summary and map its status to an exit code."""
    as_json = getattr(args, "as_json", False)
    text = (
        f"detail run #{outcome.run_id} [{outcome.status}]: {outcome.enriched} enriched, "
        f"{outcome.partial} partial, {outcome.skipped} skipped, {outcome.failed} failed "
        f"(of {outcome.requested} selected)"
        + (f"\nnote: {outcome.notes}" if outcome.notes else "")
    )
    if refresh_cutoff:
        text += f"\n  {_refresh_text_line(args, refresh_cutoff)}"
    if result.truncated:
        text += f"\n  {result.truncated} selected job(s) omitted by --max-items"
    _emit(
        {
            **outcome.to_dict(),
            "planned_urls": result.planned_urls,
            "truncated": result.truncated,
            **_refresh_output_fields(args, refresh_cutoff),
        },
        as_json=as_json,
        text=text,
    )
    if not as_json and (outcome.enriched or outcome.partial):
        _write_line("next: score --all --rescore (or score --job-id ID) to score the enriched jobs")
    if outcome.status == SearchRunStatus.FAILED_CLOSED:
        return EXIT_FAIL_CLOSED
    if outcome.status == SearchRunStatus.FAILED:
        return EXIT_ERROR
    return EXIT_OK


def _run_detail_fetch(
    args: argparse.Namespace,
    settings: Settings,
    *,
    max_items: int,
    select_fn: Callable[..., tuple[list[Any], int]],
    collect_fn: Callable[..., Any],
    provider: str | None,
    collect_kwargs: dict[str, Any],
    refresh_cutoff: str | None = None,
) -> int:
    """Run a confirmed detail fetch and emit its summary.

    The caller has already cleared the confirmation gate. ``collect_kwargs``
    carries the provider-specific transport argument (``client`` for LinkedIn,
    ``robots_text`` for the robots-gated providers); ``provider`` is passed to
    :func:`ingest_detail_results` (``None`` keeps LinkedIn's default).
    """
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        jobs, omitted = select_fn(
            conn, args, limit=max_items, refresh_cutoff=refresh_cutoff
        )
        try:
            result = collect_fn(
                jobs,
                settings=settings,
                network_enabled=True,
                max_items=max_items,
                **collect_kwargs,
            )
        except CollectorDisabledError as exc:  # pragma: no cover - guarded above
            _write_line(f"detail fetching disabled: {exc}", stream=sys.stderr)
            return EXIT_FAIL_CLOSED
        # ``jobs`` was already bounded by the selection query, so record the
        # omission count we measured against the database.
        result.truncated = omitted
        outcome = ingest_detail_results(
            conn, result, settings=settings, label=args.label, provider=provider
        )
    except CollectorError as exc:
        _write_line(f"detail fetching failed: {exc}", stream=sys.stderr)
        return EXIT_FAIL_CLOSED
    finally:
        conn.close()

    return _finish_detail_run(args, outcome, result, refresh_cutoff=refresh_cutoff)


def cmd_fetch_details(args: argparse.Namespace, settings: Settings) -> int:
    try:
        config = DetailFetchConfig(
            max_items=args.max_items if args.max_items is not None else settings.detail_max_items
        )
    except ConfigError as exc:
        _write_line(f"invalid detail options: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    # ``--stale-days`` only modifies ``--refresh``; reject it earlier than any
    # database, fixture, or network work so the failure costs nothing.
    stale_days = getattr(args, "stale_days", None)
    if stale_days is not None and not getattr(args, "refresh", False):
        _write_line("--stale-days requires --refresh", stream=sys.stderr)
        return EXIT_ERROR
    if stale_days is not None and int(stale_days) < 0:
        _write_line("--stale-days must be 0 or greater", stream=sys.stderr)
        return EXIT_ERROR
    refresh_cutoff = _refresh_cutoff(args) if getattr(args, "refresh", False) else None

    offline_transport = None
    if args.fixture:
        try:
            fixture = load_detail_fixture(Path(args.fixture))
        except CollectorError as exc:
            _write_line(f"detail fixture error: {exc}", stream=sys.stderr)
            return EXIT_ERROR
        offline_transport = FixtureDetailTransport(fixture.entries)

    if args.dry_run:
        # Match ``search-linkedin --dry-run``: plan only, and never create a
        # database. Read the existing database if (and only if) it is present.
        # ``--refresh --dry-run`` stays read-only and offline through the same
        # path: the refresh selection is a plain SELECT on the read-only URI.
        return _run_detail_dry_run(
            args,
            settings,
            max_items=config.max_items,
            select_fn=_select_detail_jobs,
            plan_fn=lambda jobs, max_items: plan_detail_targets(
                jobs, settings=settings, max_items=max_items
            ),
            unavailable_label="LinkedIn job id",
            refresh_cutoff=refresh_cutoff,
        )

    # Refuse before opening the database: an unconfirmed live run must leave no
    # trace and must not create a database file.
    if offline_transport is None and not args.confirm_network:
        _write_line(
            "refusing to run: LinkedIn detail fetching is disabled by default.\n"
            "Rerun with --confirm-network to send bounded requests "
            f"(max {config.max_items} item(s), {settings.request_delay_seconds:g}s delay), "
            "use --fixture PATH for offline fixture responses, "
            "or use --dry-run to inspect the plan.",
            stream=sys.stderr,
        )
        return EXIT_FAIL_CLOSED

    # The refusal gate above already required --confirm-network or an offline
    # fixture. An injected fixture transport performs no I/O.
    return _run_detail_fetch(
        args,
        settings,
        max_items=config.max_items,
        select_fn=_select_detail_jobs,
        collect_fn=fetch_linkedin_details,
        provider=None,
        collect_kwargs={"client": offline_transport},
        refresh_cutoff=refresh_cutoff,
    )


def _select_tecnoempleo_detail_jobs(
    conn: Any,
    args: argparse.Namespace,
    *,
    limit: int,
    refresh_cutoff: str | None = None,
) -> tuple[list[Any], int]:
    """Resolve the selection for ``fetch-tecnoempleo-details`` (Tecnoempleo-scoped)."""
    return _select_detail_jobs(
        conn,
        args,
        source="tecnoempleo",
        provider_label="Tecnoempleo",
        limit=limit,
        refresh_cutoff=refresh_cutoff,
    )


def cmd_fetch_tecnoempleo_details(args: argparse.Namespace, settings: Settings) -> int:
    """Enrich stored Tecnoempleo offers from their public detail pages.

    Offline-plannable and disabled by default. ``--dry-run`` reads the existing
    database read-only and writes nothing; a live run requires
    ``--confirm-network`` and re-fetches ``robots.txt`` before the first request.
    """
    try:
        max_items = (
            args.max_items
            if args.max_items is not None
            else settings.tecnoempleo_detail_max_items
        )
        validate_bounded_int(max_items, name="max_items", hard_max=HARD_MAX_ITEMS)
    except (ConfigError, ValueError) as exc:
        _write_line(f"invalid Tecnoempleo detail options: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    robots_text: str | None = None
    if args.robots:
        try:
            robots_text = Path(args.robots).read_text(encoding="utf-8")
        except OSError as exc:
            _write_line(f"robots fixture error: {exc}", stream=sys.stderr)
            return EXIT_ERROR

    if args.dry_run:
        return _run_detail_dry_run(
            args,
            settings,
            max_items=max_items,
            select_fn=_select_tecnoempleo_detail_jobs,
            plan_fn=plan_tecnoempleo_detail_targets,
            unavailable_label="Tecnoempleo URL",
        )

    if not args.confirm_network:
        _write_line(
            "refusing to run: Tecnoempleo detail fetching is disabled by default.\n"
            "Rerun with --confirm-network to send bounded requests "
            f"(max {max_items} item(s), {settings.request_delay_seconds:g}s delay), "
            "or use --dry-run to inspect the plan.",
            stream=sys.stderr,
        )
        return EXIT_FAIL_CLOSED

    return _run_detail_fetch(
        args,
        settings,
        max_items=max_items,
        select_fn=_select_tecnoempleo_detail_jobs,
        collect_fn=fetch_tecnoempleo_details,
        provider="tecnoempleo",
        collect_kwargs={"robots_text": robots_text},
    )


def _select_getmanfred_detail_jobs(
    conn: Any,
    args: argparse.Namespace,
    *,
    limit: int,
    refresh_cutoff: str | None = None,
) -> tuple[list[Any], int]:
    """Resolve the selection for ``fetch-getmanfred-details`` (GetManfred-scoped)."""
    return _select_detail_jobs(
        conn,
        args,
        source="getmanfred",
        provider_label="GetManfred",
        limit=limit,
        refresh_cutoff=refresh_cutoff,
    )


def cmd_fetch_getmanfred_details(args: argparse.Namespace, settings: Settings) -> int:
    """Enrich stored GetManfred offers from their public detail pages.

    Offline-plannable and disabled by default. ``--dry-run`` reads the existing
    database read-only and writes nothing; a live run requires
    ``--confirm-network`` and re-fetches ``robots.txt`` before the first request.
    """
    try:
        max_items = (
            args.max_items
            if args.max_items is not None
            else settings.getmanfred_detail_max_items
        )
        validate_bounded_int(max_items, name="max_items", hard_max=HARD_MAX_ITEMS)
    except (ConfigError, ValueError) as exc:
        _write_line(f"invalid GetManfred detail options: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    robots_text: str | None = None
    if args.robots:
        try:
            robots_text = Path(args.robots).read_text(encoding="utf-8")
        except OSError as exc:
            _write_line(f"robots fixture error: {exc}", stream=sys.stderr)
            return EXIT_ERROR

    if args.dry_run:
        return _run_detail_dry_run(
            args,
            settings,
            max_items=max_items,
            select_fn=_select_getmanfred_detail_jobs,
            plan_fn=plan_getmanfred_detail_targets,
            unavailable_label="GetManfred URL",
        )

    if not args.confirm_network:
        _write_line(
            "refusing to run: GetManfred detail fetching is disabled by default.\n"
            "Rerun with --confirm-network to send bounded requests "
            f"(max {max_items} item(s), {settings.request_delay_seconds:g}s delay), "
            "or use --dry-run to inspect the plan.",
            stream=sys.stderr,
        )
        return EXIT_FAIL_CLOSED

    return _run_detail_fetch(
        args,
        settings,
        max_items=max_items,
        select_fn=_select_getmanfred_detail_jobs,
        collect_fn=fetch_getmanfred_details,
        provider="getmanfred",
        collect_kwargs={"robots_text": robots_text},
    )


def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    try:
        profile = load_profile(settings.profile_path)
    except ProfileError:
        # Listing must keep working without a profile; it only skips the flag.
        profile = None
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        items = list_jobs(
            conn,
            limit=args.limit,
            min_score=args.min_score,
            include_inactive=args.include_inactive,
        )
        preferences = {item.job.id: assess_preference(item.job) for item in items}
        stale = (
            stale_job_ids(
                conn, (item.job for item in items), profile, settings
            )
            if profile is not None
            else set()
        )
    finally:
        conn.close()
    rescore = {
        item.job.id: (None if profile is None else item.job.id in stale)
        for item in items
    }
    text = "\n".join(
        _job_line(item, preferences.get(item.job.id), rescore[item.job.id])
        for item in items
    ) or "no jobs stored yet"
    _emit(
        {
            "count": len(items),
            "jobs": [
                _job_payload(
                    item, preferences.get(item.job.id), rescore[item.job.id]
                )
                for item in items
            ],
        },
        as_json=getattr(args, "as_json", False),
        text=text,
    )
    return EXIT_OK


def cmd_rank(args: argparse.Namespace, settings: Settings) -> int:
    try:
        profile = load_profile(settings.profile_path)
    except ProfileError:
        # Ranking must keep working without a profile; it only skips the flag.
        profile = None
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        items = list_jobs(
            conn,
            limit=args.top,
            min_score=args.min_score,
            scored_only=True,
            rank_by_score=True,
        )
        preferences = {item.job.id: assess_preference(item.job) for item in items}
        stale = (
            stale_job_ids(
                conn, (item.job for item in items), profile, settings
            )
            if profile is not None
            else set()
        )
        # An imported human decision outranks the model: the offer leaves the
        # shortlist instead of re-appearing on every run.
        decided = _fetch_imported_feedback(conn, [item.job.id for item in items])
    finally:
        conn.close()
    # shortcut: --top is applied in SQL before hiding, so a head full of
    # decided offers shows fewer than --top; re-query with a larger limit
    # if that ever matters.
    hidden_count = sum(1 for item in items if item.job.id in decided)
    items = [item for item in items if item.job.id not in decided]
    rescore = {
        item.job.id: (None if profile is None else item.job.id in stale)
        for item in items
    }
    text = "\n".join(
        _job_line(item, preferences.get(item.job.id), rescore[item.job.id])
        for item in items
    ) or "no scored jobs yet"
    if hidden_count:
        text += (
            f"\n{hidden_count} offer(s) hidden: already decided "
            "(import-feedback has the final word)"
        )
    _emit(
        {
            "count": len(items),
            "hidden_decided": hidden_count,
            "jobs": [
                _job_payload(
                    item, preferences.get(item.job.id), rescore[item.job.id]
                )
                for item in items
            ],
        },
        as_json=getattr(args, "as_json", False),
        text=text,
    )
    return EXIT_OK


def _display_path(path: Path) -> str:
    """A path for the operator: relative to the cwd when it lives under it."""
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def cmd_dossier(args: argparse.Namespace, settings: Settings) -> int:
    """Write one company dossier per selected offer's company."""
    job_ids = list(getattr(args, "job_id", None) or [])
    if not job_ids and not getattr(args, "apply", False):
        _write_line(
            "error: pass --job-id and/or --apply", stream=sys.stderr
        )
        return EXIT_ERROR

    as_json = getattr(args, "as_json", False)
    today = date.today()
    conn = connect(settings.db_path)
    selected: list[Any] = []
    failed = 0
    try:
        init_db(conn)
        if getattr(args, "apply", False):
            for feedback in list_feedback(conn):
                if str(feedback.decision) != "apply":
                    continue
                job = get_job(conn, int(feedback.job_id))
                if job is not None and job.is_active:
                    selected.append(job)
        for job_id in job_ids:
            job = get_job(conn, job_id)
            if job is None:
                _write_line(f"job #{job_id} not found", stream=sys.stderr)
                failed += 1
            else:
                selected.append(job)
    finally:
        conn.close()

    # One dossier per company: the first selected offer of a company wins.
    chosen: dict[str, Any] = {}
    for job in selected:
        company = (job.company or "").strip()
        if not company:
            _write_line(
                f"warning: skipping job #{job.id}: no company", stream=sys.stderr
            )
            continue
        try:
            slug = company_slug(company)
        except ValueError as exc:
            _write_line(f"warning: skipping job #{job.id}: {exc}", stream=sys.stderr)
            continue
        chosen.setdefault(slug, job)

    records: list[dict[str, Any]] = []
    lines: list[str] = []
    for slug, job in chosen.items():
        path = dossier_path(settings, job.company)
        record: dict[str, Any] = {
            "company": job.company,
            "slug": slug,
            "job_id": job.id,
            "path": str(path),
        }
        if path.exists() and not getattr(args, "refresh", False):
            record["status"] = "skipped"
            records.append(record)
            lines.append(f"skipped {slug} (exists)")
            continue
        try:
            result = generate_dossier(
                settings, job, today=today, timeout=args.timeout
            )
        except Exception as exc:  # one failure must not stop the others
            failed += 1
            error = str(exc) or exc.__class__.__name__
            record.update({"status": "failed", "error": error})
            records.append(record)
            lines.append(f"failed {slug}: {error}")
            continue

        record.update(
            {
                "status": "wrote",
                "path": str(result.path),
                "removed_linkedin_lines": result.removed_linkedin_lines,
            }
        )
        records.append(record)
        lines.append(f"wrote {_display_path(result.path)}")
        if result.removed_linkedin_lines:
            message = (
                f"warning: removed {result.removed_linkedin_lines} linkedin.com "
                f"line(s) from {slug}"
            )
            # A warning must not corrupt a --json document.
            _write_line(message, stream=sys.stderr if as_json else sys.stdout)

    _emit(
        {
            "model": settings.opencode_model,
            "refresh": bool(getattr(args, "refresh", False)),
            "written": sum(1 for item in records if item["status"] == "wrote"),
            "skipped": sum(1 for item in records if item["status"] == "skipped"),
            "failed": failed,
            "dossiers": records,
        },
        as_json=as_json,
        text="\n".join(lines) or "no companies selected",
    )
    return EXIT_ERROR if failed else EXIT_OK


def _split_model_variant(model: str) -> tuple[str, str | None]:
    """Split ``provider/model#variant`` into its model and optional variant."""
    base, separator, variant = model.partition("#")
    return base, (variant or None) if separator else None


def _fallback_settings(settings: Settings, fallback_model: str) -> Settings:
    """Return settings that score with the configured fallback model."""
    model, variant = _split_model_variant(fallback_model)
    return replace(settings, opencode_model=model, opencode_variant=variant)


def cmd_score(args: argparse.Namespace, settings: Settings) -> int:
    if args.scorer != ScorerName.OPENCODE.value and (
        args.scorer_model is not None or args.effort is not None
    ):
        _write_line(
            "usage error: --scorer-model and --effort only apply to the opencode scorer",
            stream=sys.stderr,
        )
        return EXIT_ERROR
    # The overrides must feed both the fingerprint and the evaluation, so they
    # are applied to the settings before either is resolved.
    if args.scorer_model is not None:
        settings = replace(settings, opencode_model=args.scorer_model)
    if args.effort is not None:
        settings = replace(settings, opencode_variant=args.effort or None)

    try:
        profile = load_profile(settings.profile_path)
    except ProfileError as exc:
        _write_line(f"profile error: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    conn = connect(settings.db_path)
    as_json = getattr(args, "as_json", False)
    results: list[dict[str, Any]] = []
    skipped_seniority = 0
    skipped_pending_details = 0
    try:
        init_db(conn)
        if args.job_id:
            jobs = [job for job in (get_job(conn, job_id) for job_id in args.job_id) if job]
            missing = [job_id for job_id in args.job_id if job_id not in {job.id for job in jobs}]
            for job_id in missing:
                _write_line(f"job #{job_id} not found", stream=sys.stderr)
        else:
            if args.rescore:
                candidates = all_active_jobs(conn)
            else:
                # Input-aware selection: only offers whose current effective scoring
                # input has no successful evaluation yet are re-scored, so a changed
                # profile, model, prompt/policy, or description is picked up without
                # re-running unchanged offers. ``--rescore`` still forces every
                # active offer.
                candidates = jobs_needing_evaluation(
                    conn,
                    scorer=args.scorer,
                    fingerprint_for=fingerprint_resolver(args.scorer, settings, profile),
                )
            # Rule 1: no scoring without details. The offer waits for its
            # description instead of paying for a card-only score; the query
            # releases it after enough failed attempts. Like seniority, this
            # runs before ``--limit`` and ``--job-id`` bypasses it.
            awaiting_details = jobs_awaiting_details(conn)
            # The seniority filter runs before ``--limit`` so hidden offers do not
            # consume the Hermes sweep's cap. ``--job-id`` deliberately bypasses it.
            jobs = []
            for job in candidates:
                if int(job.id) in awaiting_details:
                    skipped_pending_details += 1
                    continue
                if seniority_filter_reason(job.title) is not None:
                    skipped_seniority += 1
                    continue
                jobs.append(job)
            if args.limit is not None:
                jobs = jobs[: args.limit] if int(args.limit) > 0 else []

        ok_count = 0
        failed: list[dict[str, Any]] = []
        consecutive_provider_errors = 0
        stopped_early = False
        not_attempted = 0
        fallback_settings: Settings | None = None
        fallback_model: str | None = None
        allow_fallback = (
            args.scorer == ScorerName.OPENCODE.value
            and bool(settings.opencode_fallback_model)
        )
        for index, job in enumerate(jobs):
            active_settings = fallback_settings or settings
            if fallback_settings is not None:
                # Once on the fallback, an offer already scored with the
                # fallback input (an earlier run, or earlier in this one) would
                # just be duplicated. The primary fingerprint is deliberately
                # not consulted here: that selection already happened above.
                fingerprint = effective_fingerprint(
                    args.scorer, job, profile, active_settings
                )
                if has_successful_evaluation_with_fingerprint(
                    conn,
                    int(job.id),
                    scorer=args.scorer,
                    fingerprint=fingerprint,
                ):
                    continue
            evaluation = evaluate_job(
                conn,
                job,
                profile,
                scorer_name=args.scorer,
                settings=active_settings,
            )
            if (
                allow_fallback
                and fallback_settings is None
                and str(evaluation.status) == EvaluationStatus.ERROR.value
            ):
                fallback_model = str(settings.opencode_fallback_model)
                fallback_settings = _fallback_settings(settings, fallback_model)
                message = (
                    f"primary model {settings.opencode_model} failed: "
                    f"{evaluation.error}; using fallback {fallback_model} "
                    "for the rest of this run"
                )
                # A warning must not corrupt a --json document, so it goes to
                # stderr there; in text mode it stays with the run output.
                _write_line(message, stream=sys.stderr if as_json else sys.stdout)

                # Re-evaluate this same offer with the fallback model. The
                # switch is sticky for the rest of the run, so the primary is
                # not retried for every remaining offer.
                evaluation = evaluate_job(
                    conn,
                    job,
                    profile,
                    scorer_name=args.scorer,
                    settings=fallback_settings,
                )
            # Preference is computed from the same stored text and reported
            # beside the evaluation; it never edits the score or the record.
            preference = assess_preference(job)
            result = evaluation.to_dict()
            result["preference"] = preference.to_dict()
            results.append(result)
            if str(evaluation.status) == EvaluationStatus.OK.value:
                ok_count += 1
            else:
                failed.append(
                    {"job_id": job.id, "status": str(evaluation.status), "error": evaluation.error}
                )
            if not as_json:
                _print_evaluation(evaluation, preference)
                _write_line()
            # Rule 3: stop a batch whose provider is clearly down. The counter
            # reads the final status (after the fallback retry), so a rescued
            # offer resets it; ``parse_error`` is the model's output, not the
            # provider, and resets it too. The error rows already written stay.
            if str(evaluation.status) == EvaluationStatus.ERROR.value:
                consecutive_provider_errors += 1
            else:
                consecutive_provider_errors = 0
            if consecutive_provider_errors >= MAX_CONSECUTIVE_PROVIDER_ERRORS:
                # Only a run with selected offers still ahead is "stopped
                # early"; reaching the threshold on the last offer is just a
                # finished run and keeps the usual exit code.
                stopped_early = index + 1 < len(jobs)
                not_attempted = len(jobs) - (index + 1)
                break
    finally:
        conn.close()

    _emit(
        {
            "scorer": args.scorer,
            "profile": str(settings.profile_path),
            "evaluated": len(results),
            "succeeded": ok_count,
            "failed": failed,
            "fallback_model": fallback_model,
            "skipped_seniority": skipped_seniority,
            "skipped_pending_details": skipped_pending_details,
            "stopped_early": stopped_early,
            "not_attempted": not_attempted,
            "evaluations": results,
        },
        as_json=as_json,
        text=(
            f"scored {len(results)} job(s), {ok_count} succeeded, {len(failed)} failed"
            + (
                f"\nskipped {skipped_seniority} offer(s) filtered by seniority"
                if skipped_seniority
                else ""
            )
            + (
                f"\nskipped {skipped_pending_details} offer(s) waiting for details"
                if skipped_pending_details
                else ""
            )
            + (
                f"\nstopped after {MAX_CONSECUTIVE_PROVIDER_ERRORS} consecutive provider "
                f"errors: {len(results)} of {len(jobs)} offer(s) attempted, "
                f"{not_attempted} left for the next run"
                if stopped_early
                else ""
            )
        ),
    )
    if stopped_early:
        # The sweep records a non-zero exit as a failed score run, so a cut
        # batch cannot pass silently even when its first offers succeeded.
        return EXIT_ERROR
    if results and ok_count == 0:
        return EXIT_ERROR
    return EXIT_OK


def cmd_runs(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        runs = list_search_runs(conn, limit=args.limit)
        jobs = count_jobs(conn)
        evaluations = evaluation_count(conn)
        details = job_detail_fetch_count(conn)
    finally:
        conn.close()
    text = "\n".join(
        f"#{run.id:<4} {run.status:<14} {run.provider:<10} {run.query or ''} "
        f"new={run.new_jobs} dup={run.duplicate_jobs} items={run.items_found}"
        for run in runs
    ) or "no search runs yet"
    _emit(
        {
            "jobs": jobs,
            "evaluations": evaluations,
            "detail_fetches": details,
            "runs": [run.to_dict() for run in runs],
        },
        as_json=getattr(args, "as_json", False),
        text=f"{jobs} job(s), {evaluations} evaluation(s), {details} detail fetch(es)\n{text}",
    )
    return EXIT_OK


def cmd_keyword_plan(args: argparse.Namespace, settings: Settings) -> int:
    """Print the bounded keyword plan. Offline: no request, no database access.

    The catalog is the whole search surface, so this command makes the coverage
    reviewable before any live run. Each printed command is a ``--dry-run``
    preview; turning one into a real search means replacing ``--dry-run`` with
    ``--confirm-network``, which stays an explicit human action.
    """
    requested = list(args.family) if args.family else list(settings.keyword_families)
    try:
        families = resolve_families(requested or None)
    except UnknownKeywordFamily as exc:
        _write_line(str(exc), stream=sys.stderr)
        return EXIT_ERROR

    planned = planned_queries(
        [family.id for family in families],
        location=args.location,
        remote=args.remote,
    )
    tecnoempleo = {
        "browse": browse_plan(settings.tecnoempleo_search_url),
        "text_queries": list(TECNOEMPLEO_TEXT_QUERIES),
    }
    getmanfred = {
        "keywords": ",".join(GETMANFRED_TERMS),
        "max_age_days": settings.getmanfred_max_age_days,
    }
    browse = tecnoempleo["browse"]
    lines = [
        f"keyword plan: {len(families)} family(ies), {len(planned)} bounded query(ies); "
        "no request was performed",
        f"tecnoempleo browse: {browse['label']} -> {browse['url']} "
        f"(max {browse['max_pages']} page(s), {browse['max_items']} item(s))",
        f"tecnoempleo text queries: {', '.join(tecnoempleo['text_queries'])}",
        f"getmanfred terms: {', '.join(GETMANFRED_TERMS)} "
        f"(lastmod within {getmanfred['max_age_days']} days)",
    ]
    retired_for_by_query = {item["query"]: item["retired_for"] for item in planned}
    for family in families:
        lines.append(f"[{family.id}] {family.label} ({family.group})")
        lines.append(f"  {family.focus}")
        for query in family.queries:
            retired_for = retired_for_by_query.get(query, [])
            marker = f" (retired for: {', '.join(retired_for)})" if retired_for else ""
            lines.append(f"  - {query}{marker}")
            lines.append(
                f"    {search_command(query, location=args.location, remote=args.remote)}"
            )
    lines.append(
        "note: replace --dry-run with --confirm-network to run one query for real; "
        "queries are never expanded beyond this list"
    )
    _emit(
        {
            "families": [family.to_dict() for family in families],
            "planned": planned,
            "family_count": len(families),
            "query_count": len(planned),
            "tecnoempleo": tecnoempleo,
            "getmanfred": getmanfred,
        },
        as_json=getattr(args, "as_json", False),
        text="\n".join(lines),
    )
    return EXIT_OK


def cmd_dashboard(args: argparse.Namespace, settings: Settings) -> int:
    """Write the self-contained HTML report.

    The database is only ever read (read-only URI plus ``PRAGMA query_only``)
    and the output parent directory is created; no SQLite row is written. A
    missing database is an error, while an empty one still produces a report
    with a visible warning so the operator can see *why* it is empty. An explicit
    ``--output ""`` is rejected rather than silently redirected to the default,
    and an output that resolves to the database is refused.
    """
    # ``None`` means "not passed": the dashboard default is every active offer,
    # and an explicit ``--top`` is preserved as the bounded override.
    output = DASHBOARD_DEFAULT_OUTPUT if args.output is None else args.output
    top = args.top
    try:
        profile = load_profile(settings.profile_path)
    except ProfileError:
        # The report must keep working without a profile; it only skips the flag.
        profile = None
    stale: set[int] | None = None
    if profile is not None:
        conn = connect_readonly(settings.db_path)
        if conn is not None:
            try:
                stale = stale_job_ids(
                    conn, all_active_jobs(conn), profile, settings
                )
            except sqlite3.DatabaseError:
                # A missing or unreadable schema is reported by the generator
                # with its own DashboardError; flagging is only an extra.
                stale = None
            finally:
                conn.close()
    try:
        summary = generate_dashboard(
            settings.db_path, output, top=top, stale_job_ids=stale
        )
    except DashboardError as exc:
        _write_line(f"dashboard error: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    text = (
        f"wrote {summary.output}: {summary.offers} offer(s), {summary.scored} scored, "
        f"{summary.keywords} keyword(s), {summary.bytes_written} byte(s)"
    )
    text += "\n" + "\n".join(f"note: {warning}" for warning in summary.warnings)
    text += (
        "\nmanual feedback stays in your browser (localStorage) until you run "
        "'jobscout import-feedback <export>' to store it in SQLite; it never reaches "
        "LinkedIn"
    )
    _emit(
        summary.to_dict(),
        as_json=getattr(args, "as_json", False),
        text=text,
    )
    return EXIT_OK


def cmd_import_feedback(args: argparse.Namespace, settings: Settings) -> int:
    """Store a dashboard export in SQLite.

    The export's envelope is validated first (schema and project id); a document
    that fails that check writes nothing and exits ``1``. Entry-level problems
    are rejected one by one and reported to stderr while the valid entries are
    imported, following the same convention as ``score --job-id`` for a job that
    does not exist.

    The database must already exist: an import has nothing to attach feedback to
    without a job corpus, so this refuses to create one (unlike ``list``, which
    is happy to create an empty database and report zero rows).
    """
    db_path = Path(settings.db_path)
    if str(db_path) != ":memory:" and not db_path.is_file():
        _write_line(
            f"no database found at {db_path}; run 'jobscout init-db', ingest a "
            "fixture or a search, and score a job before importing feedback",
            stream=sys.stderr,
        )
        return EXIT_ERROR

    expected_project = args.project_id or project_key(db_path)
    conn = connect(db_path)
    try:
        init_db(conn)
        try:
            outcome = import_feedback(
                conn, args.path, expected_project_id=expected_project
            )
        except FeedbackError as exc:
            _write_line(f"feedback import error: {exc}", stream=sys.stderr)
            return EXIT_ERROR
        stored_total = feedback_count(conn)
    finally:
        conn.close()

    for rejection in outcome.rejected:
        _write_line(
            f"feedback entry {rejection.job_id}: rejected: {rejection.reason}",
            stream=sys.stderr,
        )

    as_json = getattr(args, "as_json", False)
    text = (
        f"feedback import [{outcome.project_id}]: {outcome.entries} entr(ies), "
        f"{outcome.imported} imported, {outcome.updated} updated, "
        f"{outcome.unchanged} unchanged, {outcome.stale} stale, "
        f"{outcome.skipped_no_decision} without decision, "
        f"{len(outcome.rejected)} rejected; {stored_total} decision(s) stored"
    )
    _emit(
        {**outcome.to_dict(), "stored_total": stored_total},
        as_json=as_json,
        text=text,
    )
    return EXIT_OK


def cmd_calibrate(args: argparse.Namespace, settings: Settings) -> int:
    """Report model-vs-human agreement without changing anything.

    The database is opened read-only (``mode=ro`` plus ``PRAGMA query_only``) and
    every statement is a ``SELECT``: no evaluation, feedback row, prompt, profile,
    or score is ever written, and no database is created. A missing database is a
    user-facing error (there is nothing to report yet); an initialized but empty
    database still produces a zero report, so "no data" is visible rather than
    mistaken for a failure.
    """
    db_path = Path(settings.db_path)
    missing_message = (
        f"no database found at {db_path}; run 'jobscout init-db', ingest a fixture "
        "or a search, score some jobs, and import feedback before calibrating"
    )
    if str(db_path) != ":memory:" and not db_path.is_file():
        _write_line(missing_message, stream=sys.stderr)
        return EXIT_ERROR

    conn = connect_readonly(db_path)
    if conn is None:
        _write_line(missing_message, stream=sys.stderr)
        return EXIT_ERROR
    try:
        try:
            report = build_report(conn, scorer=getattr(args, "scorer", None))
        except CalibrationError as exc:
            _write_line(f"calibration error: {exc}", stream=sys.stderr)
            return EXIT_ERROR
        except sqlite3.Error as exc:
            _write_line(
                f"calibration error: database at {db_path} could not be read: {exc}",
                stream=sys.stderr,
            )
            return EXIT_ERROR
    finally:
        conn.close()

    _emit(report.to_dict(), as_json=getattr(args, "as_json", False), text=report.to_text())
    return EXIT_OK


def cmd_notify(args: argparse.Namespace, settings: Settings) -> int:
    """Preview or deliver local notifications for high-value offers.

    Disabled by default: when ``JOBSCOUT_NOTIFY_ENABLED`` is false this returns
    success with an honest report and never creates or opens a database. When
    enabled, it opens an *existing* database read-only and selects active offers
    at or above the configured score threshold, optionally matching a configured
    preference outcome, excluding every offer with any imported human feedback.
    ``--deliver`` is the only path that can reach a system notifier; without it
    the command only prints. There is no scheduler, daemon, or automatic run.
    """
    as_json = getattr(args, "as_json", False)
    try:
        limit = validate_bounded_int(
            args.limit if args.limit is not None else DEFAULT_NOTIFY_LIMIT,
            name="limit",
            hard_max=HARD_MAX_ITEMS,
        )
    except ConfigError as exc:
        _write_line(f"invalid notify options: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    if not settings.notify_enabled:
        report = disabled_report(settings, limit=limit)
        _emit(report.to_dict(), as_json=as_json, text=report.to_text())
        return EXIT_OK

    db_path = Path(settings.db_path)
    missing_message = (
        f"no database found at {db_path}; run 'jobscout init-db', ingest a "
        "fixture or a search, and score a job before notifying"
    )
    if str(db_path) != ":memory:" and not db_path.is_file():
        _write_line(missing_message, stream=sys.stderr)
        return EXIT_ERROR
    conn = _open_existing_readonly(db_path)
    if conn is None:
        _write_line(missing_message, stream=sys.stderr)
        return EXIT_ERROR
    try:
        report = build_notification_report(conn, settings, limit=limit)
    except sqlite3.Error as exc:
        _write_line(
            f"notify error: database at {db_path} could not be read: {exc}",
            stream=sys.stderr,
        )
        return EXIT_ERROR
    finally:
        conn.close()

    payload = report.to_dict()
    payload["preview_only"] = not args.deliver
    text = report.to_text()
    if args.deliver:
        result = deliver_report(report, delivery=settings.notify_delivery)
        payload["delivery"] = result.to_dict()
        text += f"\ndelivery: {result.detail}"
    else:
        payload["delivery"] = {
            "requested": settings.notify_delivery,
            "channel": None,
            "delivered": False,
            "detail": "preview only",
        }
    _emit(payload, as_json=as_json, text=text)
    return EXIT_OK


def cmd_prune(args: argparse.Namespace, settings: Settings) -> int:
    """Plan catalog pruning offline, or deactivate only the selected offers.

    The default preview opens an existing database read-only and never creates
    one, so it cannot write a row. ``--apply`` reopens the database through the
    normal writable path but still refuses to create a missing file; it applies
    exactly the offers the plan selected and keeps all history through the
    existing deactivation path. Keyword candidates are a report only: the
    keyword catalog is never mutated, and candidates stay separate per provider.
    """
    db_path = Path(settings.db_path)
    missing_message = (
        f"no database found at {db_path}; run 'jobscout init-db', ingest a "
        "fixture or a search, and score some jobs before pruning"
    )
    as_json = getattr(args, "as_json", False)
    apply = bool(getattr(args, "apply", False))

    if not apply:
        conn = _open_existing_readonly(db_path)
        if conn is None:
            if str(db_path) == ":memory:" or not db_path.is_file():
                _write_line(missing_message, stream=sys.stderr)
            else:
                _write_line(
                    f"database at {db_path} could not be opened read-only",
                    stream=sys.stderr,
                )
            return EXIT_ERROR
        try:
            try:
                plan = build_plan(conn)
            except sqlite3.Error as exc:
                _write_line(
                    f"pruning error: database at {db_path} could not be read: {exc}",
                    stream=sys.stderr,
                )
                return EXIT_ERROR
        finally:
            conn.close()
        payload = plan.to_dict()
        payload["applied"] = False
        payload["changed"] = 0
        _emit(payload, as_json=as_json, text=plan.to_text())
        return EXIT_OK

    if str(db_path) != ":memory:" and not db_path.is_file():
        _write_line(missing_message, stream=sys.stderr)
        return EXIT_ERROR
    conn = connect(db_path)
    try:
        try:
            plan = build_plan(conn)
            changed = apply_offer_plan(conn, plan)
        except sqlite3.Error as exc:
            _write_line(
                f"pruning error: database at {db_path} could not be updated: {exc}",
                stream=sys.stderr,
            )
            return EXIT_ERROR
    finally:
        conn.close()

    payload = plan.to_dict()
    payload["applied"] = True
    payload["changed"] = changed
    text = plan.to_text() + f"\napplied: deactivated {changed} offer(s)"
    _emit(payload, as_json=as_json, text=text)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    # Before anything can print (including argparse usage/errors): let a legacy
    # console escape characters it cannot encode instead of raising
    # UnicodeEncodeError and aborting a whole batch on one model-supplied word.
    configure_output_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = resolve_settings(args)
    except ConfigError as exc:
        _write_line(f"configuration error: {exc}", stream=sys.stderr)
        return EXIT_ERROR
    try:
        return int(args.handler(args, settings))
    except CollectorDisabledError as exc:
        _write_line(f"refusing to run: {exc}", stream=sys.stderr)
        return EXIT_FAIL_CLOSED
    except ProfileError as exc:
        _write_line(f"profile error: {exc}", stream=sys.stderr)
        return EXIT_ERROR
    except CollectorError as exc:
        _write_line(f"collection error: {exc}", stream=sys.stderr)
        return EXIT_FAIL_CLOSED
    except ConfigError as exc:
        _write_line(f"configuration error: {exc}", stream=sys.stderr)
        return EXIT_ERROR
    except DashboardError as exc:
        _write_line(f"dashboard error: {exc}", stream=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        _write_line("interrupted", stream=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
