"""Where an offer stands in the pipeline, derived in one place.

The stage is computed from rows that already exist (``jobs``, ``evaluations``,
``job_detail_fetches``), never stored: a stored stage drifts from the data it
summarises, and "needs re-score" depends on the current profile and model, so
it cannot be persisted at all. The dashboard and ``list``/``rank`` read it from
here instead of re-deriving "pending details" or "needs re-score" themselves.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .db import jobs_awaiting_details


class OfferStage(StrEnum):
    """inactive → pending_details → ready_to_score → scored → needs_rescore."""

    INACTIVE = "inactive"
    PENDING_DETAILS = "pending_details"
    READY_TO_SCORE = "ready_to_score"
    SCORED = "scored"
    NEEDS_RESCORE = "needs_rescore"


#: Latest-evaluation statuses that mean the scorer was tried and failed. Such an
#: offer is still ready to score (``score`` selects it again); the failure is a
#: mark on that stage, not a stage of its own. ``needs_more_info`` is a result,
#: not a failure, so it is not here.
FAILED_EVALUATION_STATUSES = frozenset({"parse_error", "error", "not_configured"})


@dataclass(frozen=True)
class OfferState:
    stage: OfferStage
    #: The latest evaluation failed; only ever set on ``READY_TO_SCORE``.
    score_failed: bool = False


def derive_stage(
    *,
    is_active: bool,
    has_score: bool,
    awaiting_details: bool,
    needs_rescore: bool | None,
    latest_status: str | None,
) -> OfferState:
    """The single rule that turns an offer's facts into its stage.

    A shown score wins over missing details: an offer scored on its card keeps
    that score until it is re-scored. ``needs_rescore=None`` means the profile
    was unavailable, so staleness is unknown and the offer stays ``scored``.
    ``awaiting_details`` already excludes offers whose detail attempts ran out
    (see :func:`jobscout.db.jobs_awaiting_details`), which are ready to score.
    """
    if not is_active:
        return OfferState(OfferStage.INACTIVE)
    if has_score:
        return OfferState(OfferStage.NEEDS_RESCORE if needs_rescore else OfferStage.SCORED)
    if awaiting_details:
        return OfferState(OfferStage.PENDING_DETAILS)
    return OfferState(
        OfferStage.READY_TO_SCORE,
        score_failed=(latest_status or "") in FAILED_EVALUATION_STATUSES,
    )


#: Stay below SQLite's usual 999 bound-parameter limit when querying by ids.
_MAX_SQL_VARIABLES = 900

_FACTS_SQL = """
    SELECT j.id AS id,
           j.is_active AS is_active,
           e.overall_score IS NOT NULL AS has_score,
           (
               SELECT x.status FROM evaluations x
               WHERE x.job_id = j.id
               ORDER BY x.id DESC
               LIMIT 1
           ) AS latest_status
    FROM jobs j
    LEFT JOIN latest_evaluations e ON e.job_id = j.id
"""


def offer_states(
    conn: sqlite3.Connection,
    job_ids: Sequence[int] | None = None,
    *,
    stale_job_ids: Collection[int] | None,
) -> dict[int, OfferState]:
    """Stage of each offer in ``job_ids`` (every stored offer when ``None``).

    ``stale_job_ids`` comes from :func:`jobscout.scoring.stale_job_ids`; ``None``
    means the profile was unavailable and no score is marked as stale.
    """
    awaiting = jobs_awaiting_details(conn)
    stale = None if stale_job_ids is None else set(stale_job_ids)

    if job_ids is None:
        rows = conn.execute(_FACTS_SQL).fetchall()
    else:
        ids = [int(job_id) for job_id in job_ids]
        rows = []
        for start in range(0, len(ids), _MAX_SQL_VARIABLES):
            chunk = ids[start : start + _MAX_SQL_VARIABLES]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                conn.execute(
                    _FACTS_SQL + f" WHERE j.id IN ({placeholders})", chunk
                ).fetchall()
            )

    states: dict[int, OfferState] = {}
    for row in rows:
        job_id = int(row["id"])
        states[job_id] = derive_stage(
            is_active=bool(row["is_active"]),
            has_score=bool(row["has_score"]),
            awaiting_details=job_id in awaiting,
            needs_rescore=None if stale is None else job_id in stale,
            latest_status=row["latest_status"],
        )
    return states
