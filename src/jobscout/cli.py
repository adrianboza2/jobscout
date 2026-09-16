"""Command line interface.

Commands
--------
``init-db``          create or migrate the local SQLite database
``ingest-fixture``   ingest a JSON fixture offline (default development path)
``search-linkedin``  bounded, explicitly opted-in LinkedIn guest search
``fetch-details``    bounded, explicitly opted-in job-detail enrichment
``list``             recent jobs with their latest score
``rank``             jobs ordered by score
``score``            evaluate jobs offline, via an LLM endpoint, or via OpenCode CLI

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
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .collectors.base import CollectorDisabledError, CollectorError
from .collectors.fixtures import FixtureDetailTransport, load_detail_fixture
from .collectors.linkedin import LinkedInSearchConfig, plan_search_urls, search_linkedin
from .collectors.linkedin_details import (
    DetailFetchConfig,
    fetch_linkedin_details,
    plan_detail_targets,
)
from .config import ConfigError, Settings
from .db import (
    all_active_jobs,
    connect,
    count_active_jobs,
    count_jobs,
    count_jobs_needing_details,
    evaluation_count,
    get_job,
    init_db,
    job_detail_fetch_count,
    jobs_needing_details,
    jobs_needing_evaluation,
    list_jobs,
    list_search_runs,
)
from .models import EvaluationStatus, ScorerName, SearchRunStatus
from .pipeline import ingest_collector_result, ingest_detail_results, ingest_fixture
from .scoring import SCORERS, evaluate_job, load_profile
from .scoring.profile import ProfileError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FAIL_CLOSED = 2

SCORER_CHOICES = tuple(SCORERS)


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


def _job_line(item: Any) -> str:
    job = item.job
    score = "  n/a" if item.score is None else f"{item.score:5.1f}"
    recommendation = item.recommendation or "-"
    location = f" ({job.location})" if job.location else ""
    return (
        f"#{job.id:<4} {score}  {recommendation:<14} {job.title or '(untitled)'}"
        f" @ {job.company or '(unknown company)'}{location}  [{job.content_status}]"
    )


def _print_evaluation(evaluation: Any) -> None:
    header = (
        f"job #{evaluation.job_id}  scorer={evaluation.scorer}  "
        f"status={evaluation.status}  score={evaluation.overall_score}  "
        f"recommendation={evaluation.recommendation}  confidence={evaluation.confidence}"
    )
    _write_line(header)
    if evaluation.rationale:
        _write_line(f"  rationale: {evaluation.rationale}")
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
        "--confirm-network",
        action="store_true",
        help="required acknowledgement that real requests will be sent",
    )
    search_parser.add_argument(
        "--dry-run", action="store_true", help="print the request plan without sending anything"
    )
    search_parser.set_defaults(handler=cmd_search_linkedin)

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
    score_parser.add_argument("--scorer", choices=SCORER_CHOICES, default=ScorerName.HEURISTIC.value)
    score_parser.add_argument("--limit", type=int, default=None)
    score_parser.add_argument(
        "--rescore", action="store_true", help="re-evaluate jobs that already have a score"
    )
    score_parser.set_defaults(handler=cmd_score)

    runs_parser = add_common(subparsers.add_parser("runs", help="show recent search runs"))
    runs_parser.add_argument("--limit", type=int, default=10)
    runs_parser.set_defaults(handler=cmd_runs)

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
    except ConfigError as exc:
        _write_line(f"invalid search options: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    if args.dry_run:
        planned = plan_search_urls(config, settings=settings)
        _emit(
            {"dry_run": True, "planned_urls": planned},
            as_json=getattr(args, "as_json", False),
            text="dry run, no request performed. Planned requests:\n  "
            + "\n  ".join(planned),
        )
        return EXIT_OK

    if not args.confirm_network:
        _write_line(
            "refusing to run: LinkedIn collection is disabled by default.\n"
            "Rerun with --confirm-network to send bounded requests "
            f"(max {config.max_pages} page(s), {config.max_items} item(s), "
            f"{settings.request_delay_seconds:g}s delay), or use --dry-run to inspect the plan.",
            stream=sys.stderr,
        )
        return EXIT_FAIL_CLOSED

    conn = connect(settings.db_path)
    try:
        init_db(conn)
        try:
            result = search_linkedin(config, settings=settings, network_enabled=True)
        except CollectorDisabledError as exc:  # pragma: no cover - guarded above
            _write_line(f"collector disabled: {exc}", stream=sys.stderr)
            return EXIT_FAIL_CLOSED
        outcome = ingest_collector_result(conn, result, config=config, settings=settings)
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
    if outcome.status == SearchRunStatus.FAILED_CLOSED:
        return EXIT_FAIL_CLOSED
    if outcome.status == SearchRunStatus.FAILED:
        return EXIT_ERROR
    return EXIT_OK


def _select_detail_jobs(
    conn: Any, args: argparse.Namespace, *, limit: int
) -> tuple[list[Any], int]:
    """Resolve the selection for ``fetch-details``.

    Returns ``(jobs, omitted)`` where ``jobs`` is bounded to ``limit`` and
    ``omitted`` is how many selected jobs were dropped by that cap. The count
    lets the CLI report truncation instead of always claiming zero.
    """
    if args.job_id:
        selected = [job for job in (get_job(conn, job_id) for job_id in args.job_id) if job]
        found = {job.id for job in selected}
        for missing in [job_id for job_id in args.job_id if job_id not in found]:
            _write_line(f"job #{missing} not found", stream=sys.stderr)
        return selected[:limit], max(0, len(selected) - limit)
    if args.all:
        total = count_active_jobs(conn)
        return all_active_jobs(conn, limit=limit), max(0, total - limit)
    total = count_jobs_needing_details(conn)
    return jobs_needing_details(conn, limit=limit), max(0, total - limit)


def _open_existing_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open an existing database for reading only, or return ``None``.

    A dry run must not create a database or write schema/data. This helper only
    opens a database that already exists and pins it to ``PRAGMA query_only`` so
    no write can slip through. It intentionally bypasses
    :func:`jobscout.db.connect`, which would create the parent directory, a new
    file, and WAL journals.
    """
    if str(db_path) == ":memory:":
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _run_detail_dry_run(
    args: argparse.Namespace, settings: Settings, config: DetailFetchConfig
) -> int:
    """Plan ``fetch-details`` without creating or writing a database."""
    jobs: list[Any] = []
    omitted = 0
    note: str | None = None
    conn = _open_existing_readonly(Path(settings.db_path))
    if conn is None:
        note = f"no database at {settings.db_path}; nothing to plan"
    else:
        try:
            jobs, omitted = _select_detail_jobs(conn, args, limit=config.max_items)
        except sqlite3.DatabaseError:
            # Covers a database with no jobscout schema as well as an
            # unreadable/corrupt file: plan nothing instead of crashing.
            note = "database could not be read or is not initialized; nothing to plan"
        finally:
            conn.close()

    targets, _ = plan_detail_targets(jobs, settings=settings, max_items=config.max_items)
    planned = [target.url for target in targets if target.url]
    skipped = [target.reason for target in targets if target.url is None]
    text = "dry run, no request performed and no data written. Planned requests:\n  " + (
        "\n  ".join(planned) or "(none)"
    )
    if note:
        text += f"\n  {note}"
    if omitted:
        text += f"\n  {omitted} selected job(s) omitted by --max-items"
    if skipped:
        text += f"\n  {len(skipped)} selected job(s) have no resolvable LinkedIn job id"
    _emit(
        {
            "dry_run": True,
            "selected": len(targets),
            "truncated": omitted,
            "planned_urls": planned,
            "skipped_reasons": skipped,
            "note": note,
        },
        as_json=getattr(args, "as_json", False),
        text=text,
    )
    return EXIT_OK


def cmd_fetch_details(args: argparse.Namespace, settings: Settings) -> int:
    try:
        config = DetailFetchConfig(
            max_items=args.max_items if args.max_items is not None else settings.detail_max_items
        )
    except ConfigError as exc:
        _write_line(f"invalid detail options: {exc}", stream=sys.stderr)
        return EXIT_ERROR

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
        return _run_detail_dry_run(args, settings, config)

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

    conn = connect(settings.db_path)
    try:
        init_db(conn)
        jobs, omitted = _select_detail_jobs(conn, args, limit=config.max_items)

        try:
            # The refusal gate above already required --confirm-network or an
            # offline fixture. An injected fixture transport performs no I/O.
            result = fetch_linkedin_details(
                jobs,
                settings=settings,
                network_enabled=True,
                max_items=config.max_items,
                client=offline_transport,
            )
        except CollectorDisabledError as exc:  # pragma: no cover - guarded above
            _write_line(f"detail fetching disabled: {exc}", stream=sys.stderr)
            return EXIT_FAIL_CLOSED
        # ``jobs`` was already bounded by the selection query, so record the
        # omission count we measured against the database.
        result.truncated = omitted
        outcome = ingest_detail_results(conn, result, settings=settings, label=args.label)
    except CollectorError as exc:
        _write_line(f"detail fetching failed: {exc}", stream=sys.stderr)
        return EXIT_FAIL_CLOSED
    finally:
        conn.close()

    as_json = getattr(args, "as_json", False)
    text = (
        f"detail run #{outcome.run_id} [{outcome.status}]: {outcome.enriched} enriched, "
        f"{outcome.partial} partial, {outcome.skipped} skipped, {outcome.failed} failed "
        f"(of {outcome.requested} selected)"
        + (f"\nnote: {outcome.notes}" if outcome.notes else "")
    )
    if result.truncated:
        text += f"\n  {result.truncated} selected job(s) omitted by --max-items"
    _emit(
        {
            **outcome.to_dict(),
            "planned_urls": result.planned_urls,
            "truncated": result.truncated,
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


def cmd_list(args: argparse.Namespace, settings: Settings) -> int:
    conn = connect(settings.db_path)
    try:
        init_db(conn)
        items = list_jobs(
            conn,
            limit=args.limit,
            min_score=args.min_score,
            include_inactive=args.include_inactive,
        )
    finally:
        conn.close()
    text = "\n".join(_job_line(item) for item in items) or "no jobs stored yet"
    _emit(
        {"count": len(items), "jobs": [item.to_dict() for item in items]},
        as_json=getattr(args, "as_json", False),
        text=text,
    )
    return EXIT_OK


def cmd_rank(args: argparse.Namespace, settings: Settings) -> int:
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
    finally:
        conn.close()
    text = "\n".join(_job_line(item) for item in items) or "no scored jobs yet"
    _emit(
        {"count": len(items), "jobs": [item.to_dict() for item in items]},
        as_json=getattr(args, "as_json", False),
        text=text,
    )
    return EXIT_OK


def cmd_score(args: argparse.Namespace, settings: Settings) -> int:
    try:
        profile = load_profile(settings.profile_path)
    except ProfileError as exc:
        _write_line(f"profile error: {exc}", stream=sys.stderr)
        return EXIT_ERROR

    conn = connect(settings.db_path)
    as_json = getattr(args, "as_json", False)
    results: list[dict[str, Any]] = []
    try:
        init_db(conn)
        if args.job_id:
            jobs = [job for job in (get_job(conn, job_id) for job_id in args.job_id) if job]
            missing = [job_id for job_id in args.job_id if job_id not in {job.id for job in jobs}]
            for job_id in missing:
                _write_line(f"job #{job_id} not found", stream=sys.stderr)
        elif args.rescore:
            jobs = all_active_jobs(conn, limit=args.limit)
        else:
            jobs = jobs_needing_evaluation(conn, scorer=args.scorer, limit=args.limit)

        ok_count = 0
        failed: list[dict[str, Any]] = []
        for job in jobs:
            evaluation = evaluate_job(
                conn,
                job,
                profile,
                scorer_name=args.scorer,
                settings=settings,
            )
            results.append(evaluation.to_dict())
            if str(evaluation.status) == EvaluationStatus.OK.value:
                ok_count += 1
            else:
                failed.append(
                    {"job_id": job.id, "status": str(evaluation.status), "error": evaluation.error}
                )
            if not as_json:
                _print_evaluation(evaluation)
                _write_line()
    finally:
        conn.close()

    _emit(
        {
            "scorer": args.scorer,
            "profile": str(settings.profile_path),
            "evaluated": len(results),
            "succeeded": ok_count,
            "failed": failed,
            "evaluations": results,
        },
        as_json=as_json,
        text=f"scored {len(results)} job(s), {ok_count} succeeded, {len(failed)} failed",
    )
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
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        _write_line("interrupted", stream=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
