"""Import of the dashboard's local feedback export into SQLite.

The dashboard keeps the human's decision in the browser's ``localStorage`` and
offers a manual ``jobscout.feedback.v1`` JSON export. This module turns that file
into durable rows, so the human verdict stops being trapped in one browser
profile and can later be compared with the model recommendation.

Two signals, two tables
------------------------
The human decision lives in ``job_feedback``; the model recommendation stays in
``evaluations``. Nothing here touches an evaluation, and nothing here ever
touches LinkedIn or the network: importing is a local file read plus local
SQLite writes.

Binary decisions with a legacy value
------------------------------------
The active dashboard offers only ``apply`` and ``reject``. ``consider`` is a
*legacy* value: a pre-binary export that carries it is still accepted and stored
verbatim, so historical feedback is never rejected or silently converted. The
importer validates against the full :data:`DECISIONS` vocabulary; the UI and any
new mark are limited to :data:`ACTIVE_DECISIONS`.

Validation and failure behaviour
--------------------------------
Document-level problems (unreadable file, invalid JSON, foreign schema, missing
or mismatched ``project_id``, a ``feedback`` value that is not an object) raise
:class:`FeedbackError` with a message naming the cause, and nothing is written.

Entry-level problems (unknown job id, invalid decision, malformed timestamp, an
entry that is not an object, a reason that is not a string) reject *that entry*
with a clear reason while the valid entries are still imported. This mirrors the
existing CLI convention for a missing job id (report it, keep going) and keeps
one bad row from discarding a whole export.

Idempotency
-----------
A job has at most one feedback row, and a write only happens when the incoming
entry is strictly newer than the stored one (``decision_updated_at``), so
re-importing the same file is a no-op and an older export can never overwrite a
newer decision. An entry with no ``updated_at`` never replaces a timestamped
row. Decision timestamps are normalized to fixed-width UTC instants with
microsecond precision, so the comparison stays exact even for two edits inside
the same second.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .dashboard import FEEDBACK_SCHEMA
from .db import (
    FEEDBACK_INSERTED,
    FEEDBACK_STALE,
    FEEDBACK_UNCHANGED,
    FEEDBACK_UPDATED,
    get_job,
    upsert_feedback,
)
from .models import (
    ACTIVE_FEEDBACK_DECISIONS,
    LEGACY_FEEDBACK_DECISIONS,
    FeedbackDecision,
    JobFeedback,
)
from .timeutil import parse_datetime, utcnow_iso

#: Every decision the importer accepts, including historical values. The active
#: dashboard is binary (:data:`ACTIVE_DECISIONS`); a legacy ``consider`` export is
#: still valid and is never rejected or rewritten.
DECISIONS: tuple[str, ...] = tuple(str(decision) for decision in FeedbackDecision)

#: Decisions the binary dashboard offers for a new mark.
ACTIVE_DECISIONS: tuple[str, ...] = ACTIVE_FEEDBACK_DECISIONS

#: Decisions kept for historical compatibility: importable and countable, never
#: offered by the active UI.
LEGACY_DECISIONS: tuple[str, ...] = LEGACY_FEEDBACK_DECISIONS

_JOB_ID_RE = re.compile(r"[0-9]+")


class FeedbackError(RuntimeError):
    """Raised when an export cannot be imported as a whole."""


class _EntryRejected(ValueError):
    """Internal: one entry is unusable, the rest of the export is fine."""


@dataclass
class FeedbackRejection:
    """One entry that was not imported, with the reason it was refused."""

    job_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"job_id": self.job_id, "reason": self.reason}


@dataclass
class FeedbackImportOutcome:
    """Accounting for one feedback import."""

    path: str | None = None
    project_id: str = ""
    exported_at: str | None = None
    source: str | None = None
    entries: int = 0
    imported: int = 0
    updated: int = 0
    unchanged: int = 0
    stale: int = 0
    skipped_no_decision: int = 0
    rejected: list[FeedbackRejection] = field(default_factory=list)
    job_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "project_id": self.project_id,
            "exported_at": self.exported_at,
            "source": self.source,
            "entries": self.entries,
            "imported": self.imported,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "stale": self.stale,
            "skipped_no_decision": self.skipped_no_decision,
            "rejected_count": len(self.rejected),
            "rejected": [item.to_dict() for item in self.rejected],
            "job_ids": list(self.job_ids),
        }


# ---------------------------------------------------------------------------
# Reading and validating the export
# ---------------------------------------------------------------------------


def load_feedback_export(path: str | Path) -> Any:
    """Read and JSON-decode an export file, or raise :class:`FeedbackError`."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise FeedbackError(f"the export at {source} could not be read: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise FeedbackError(f"the export at {source} is not valid JSON: {exc}") from exc


def validate_feedback_export(
    document: Any, *, expected_project_id: str = ""
) -> tuple[str, dict[str, Any]]:
    """Validate the export envelope, returning ``(project_id, feedback)``.

    Only the envelope is checked here; entries are validated one by one while
    importing so a single bad entry does not discard the whole file.
    """
    if not isinstance(document, dict):
        raise FeedbackError("the export must be a JSON object")
    schema = document.get("schema")
    if schema != FEEDBACK_SCHEMA:
        raise FeedbackError(
            f"unsupported export schema {schema!r}; expected {FEEDBACK_SCHEMA!r}"
        )
    project_id = document.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise FeedbackError("the export has no project_id")
    if expected_project_id and project_id != expected_project_id:
        raise FeedbackError(
            f"project_id mismatch: the export belongs to project {project_id!r}, "
            f"but this database is {expected_project_id!r}; regenerate the "
            "dashboard from this database and export the feedback again, or pass "
            f"--project-id {project_id!r} to accept the export's project explicitly"
        )
    feedback = document.get("feedback")
    if feedback is None:
        raise FeedbackError("the export has no 'feedback' object")
    if not isinstance(feedback, dict):
        raise FeedbackError("'feedback' must be a JSON object keyed by job id")
    return project_id, feedback


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------


def _canonical_timestamp(value: str) -> str:
    """Normalize an export timestamp to a fixed-width UTC instant.

    The dashboard writes milliseconds (``...T14:12:00.431Z``) but a hand-edited
    or third-party export may not, so every value is rewritten with exactly six
    fractional digits. Fixed width is what lets the storage layer compare two
    decision timestamps as plain strings without losing sub-second ordering: two
    edits inside the same second must stay distinguishable, otherwise the newer
    one could be mistaken for a re-import.
    """
    parsed = parse_datetime(value)
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _normalize_timestamp(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _EntryRejected(f"{field_name} must be a non-empty ISO-8601 string")
    normalized = _canonical_timestamp(value.strip())
    if not normalized:
        raise _EntryRejected(f"{field_name} {value!r} is not a valid ISO-8601 timestamp")
    return normalized


def _parse_job_id(key: Any) -> int:
    text = str(key).strip()
    if _JOB_ID_RE.fullmatch(text) is None or int(text) < 1:
        raise _EntryRejected(f"job id {key!r} is not a positive integer")
    return int(text)


def _parse_entry(
    raw: Any,
    *,
    job_id: int,
    project_id: str,
    source: str | None,
    imported_at: str,
) -> JobFeedback | None:
    """Build the row for one entry, or ``None`` when it carries no decision."""
    if not isinstance(raw, dict):
        raise _EntryRejected("entry must be a JSON object")
    decision = raw.get("decision")
    if decision is None:
        return None
    if not isinstance(decision, str):
        raise _EntryRejected("decision must be a string")
    normalized = decision.strip().lower()
    if not normalized:
        # The dashboard stores an empty decision while the human has not decided.
        return None
    if normalized not in DECISIONS:
        raise _EntryRejected(
            f"unknown decision {decision!r}; expected one of {', '.join(DECISIONS)}"
        )
    reason = raw.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise _EntryRejected("reason must be a string or null")
    if isinstance(reason, str) and not reason.strip():
        reason = None
    updated_at = raw.get("updated_at")
    decision_updated_at = (
        None if updated_at is None else _normalize_timestamp(updated_at, field_name="updated_at")
    )
    return JobFeedback(
        job_id=job_id,
        decision=normalized,
        reason=reason,
        decision_updated_at=decision_updated_at,
        project_id=project_id,
        source=source,
        imported_at=imported_at,
    )


def _sort_key(key: Any) -> tuple[int, int, str]:
    """Numeric ids first in numeric order, then anything else, deterministically."""
    text = str(key)
    if _JOB_ID_RE.fullmatch(text) is not None:
        return (0, int(text), "")
    return (1, 0, text)


def _optional_timestamp(value: Any) -> str | None:
    """Normalize an optional export timestamp, tolerating an unusable one."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _canonical_timestamp(value.strip()) or None


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_feedback_document(
    conn: Any, document: Any, *, expected_project_id: str = ""
) -> FeedbackImportOutcome:
    """Validate and import an already-decoded export document."""
    project_id, entries = validate_feedback_export(
        document, expected_project_id=expected_project_id
    )
    raw_source = document.get("source")
    source = raw_source.strip() if isinstance(raw_source, str) and raw_source.strip() else None
    outcome = FeedbackImportOutcome(
        project_id=project_id,
        exported_at=_optional_timestamp(document.get("exported_at")),
        source=source,
        entries=len(entries),
    )
    imported_at = utcnow_iso()

    for key in sorted(entries, key=_sort_key):
        try:
            job_id = _parse_job_id(key)
        except _EntryRejected as exc:
            outcome.rejected.append(FeedbackRejection(job_id=str(key), reason=str(exc)))
            continue

        try:
            parsed = _parse_entry(
                entries[key],
                job_id=job_id,
                project_id=project_id,
                source=source,
                imported_at=imported_at,
            )
        except _EntryRejected as exc:
            outcome.rejected.append(FeedbackRejection(job_id=str(job_id), reason=str(exc)))
            continue

        if parsed is None:
            outcome.skipped_no_decision += 1
            continue

        if get_job(conn, job_id) is None:
            outcome.rejected.append(
                FeedbackRejection(job_id=str(job_id), reason="job not found in the database")
            )
            continue

        _, status = upsert_feedback(conn, parsed)
        if status == FEEDBACK_INSERTED:
            outcome.imported += 1
            outcome.job_ids.append(job_id)
        elif status == FEEDBACK_UPDATED:
            outcome.updated += 1
            outcome.job_ids.append(job_id)
        elif status == FEEDBACK_UNCHANGED:
            outcome.unchanged += 1
        elif status == FEEDBACK_STALE:
            outcome.stale += 1
        # Any other value would be a bug in ``upsert_feedback``; ignoring it here
        # keeps the accounting honest instead of inventing a bucket.
    return outcome


def import_feedback(
    conn: Any, path: str | Path, *, expected_project_id: str = ""
) -> FeedbackImportOutcome:
    """Read an export file and import it, returning the import accounting."""
    document = load_feedback_export(path)
    outcome = import_feedback_document(
        conn, document, expected_project_id=expected_project_id
    )
    outcome.path = str(path)
    return outcome


__all__ = [
    "ACTIVE_DECISIONS",
    "DECISIONS",
    "LEGACY_DECISIONS",
    "FeedbackError",
    "FeedbackImportOutcome",
    "FeedbackRejection",
    "import_feedback",
    "import_feedback_document",
    "load_feedback_export",
    "validate_feedback_export",
]
