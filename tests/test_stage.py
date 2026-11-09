"""The offer stage: one rule, fixed by a table of cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobscout.db import connect, init_db, insert_detail_fetch, insert_evaluation, store_posting
from jobscout.models import (
    DetailFetchStatus,
    Evaluation,
    EvaluationStatus,
    JobDetailFetch,
    JobPosting,
    Recommendation,
    ScorerName,
)
from jobscout.normalize import finalize_posting
from jobscout.stage import OfferStage, OfferState, derive_stage, offer_states

INACTIVE = OfferState(OfferStage.INACTIVE)
PENDING = OfferState(OfferStage.PENDING_DETAILS)
READY = OfferState(OfferStage.READY_TO_SCORE)
FAILED = OfferState(OfferStage.READY_TO_SCORE, score_failed=True)
SCORED = OfferState(OfferStage.SCORED)
RESCORE = OfferState(OfferStage.NEEDS_RESCORE)


# active, has_score, awaiting_details, needs_rescore, latest_status -> state
CASES = [
    # An inactive offer is inactive whatever else holds.
    (False, True, False, True, "ok", INACTIVE),
    (False, False, True, None, None, INACTIVE),
    # No score yet: details first, then ready to score.
    (True, False, True, False, None, PENDING),
    (True, False, True, None, "error", PENDING),
    (True, False, False, False, None, READY),
    (True, False, False, None, "needs_more_info", READY),
    # A failed latest evaluation is a mark on "ready to score".
    (True, False, False, False, "error", FAILED),
    (True, False, False, False, "parse_error", FAILED),
    (True, False, False, False, "not_configured", FAILED),
    # A shown score wins over late details (a card-only score is kept).
    (True, True, True, False, "ok", SCORED),
    (True, True, False, False, "error", SCORED),
    (True, True, False, True, "ok", RESCORE),
    (True, True, True, True, "ok", RESCORE),
    # Without a profile staleness is unknown: the offer stays scored.
    (True, True, False, None, "ok", SCORED),
]


@pytest.mark.parametrize(
    "is_active, has_score, awaiting, needs_rescore, latest_status, expected", CASES
)
def test_derive_stage_table(
    is_active, has_score, awaiting, needs_rescore, latest_status, expected
) -> None:
    assert (
        derive_stage(
            is_active=is_active,
            has_score=has_score,
            awaiting_details=awaiting,
            needs_rescore=needs_rescore,
            latest_status=latest_status,
        )
        == expected
    )


def _store(conn, serial: str, *, description: str | None = "Python, SQL.") -> int:
    posting = finalize_posting(
        JobPosting(
            source="linkedin",
            source_job_id=serial,
            url=f"https://example.test/jobs/{serial}",
            title=f"Offer {serial}",
            company="Acme",
            description=description,
        )
    )
    job, _ = store_posting(conn, posting, now="2026-11-01T07:00:00Z")
    return int(job.id)


def _evaluate(conn, job_id: int, status: EvaluationStatus) -> None:
    insert_evaluation(
        conn,
        Evaluation(
            job_id=job_id,
            scorer=ScorerName.LLM,
            status=status,
            overall_score=70.0 if status is EvaluationStatus.OK else None,
            recommendation=Recommendation.CONSIDER if status is EvaluationStatus.OK else None,
            confidence=0.7,
            rationale="r",
        ),
    )


def test_offer_states_reads_the_stored_facts(tmp_path: Path) -> None:
    conn = connect(tmp_path / "stage.sqlite3")
    try:
        init_db(conn)
        scored = _store(conn, "s")
        _evaluate(conn, scored, EvaluationStatus.OK)
        stale = _store(conn, "st")
        _evaluate(conn, stale, EvaluationStatus.OK)
        failed = _store(conn, "f")
        _evaluate(conn, failed, EvaluationStatus.ERROR)
        ready = _store(conn, "r")
        pending = _store(conn, "p")
        exhausted = _store(conn, "x")
        inactive = _store(conn, "i")
        conn.execute(
            "UPDATE jobs SET content_status = 'partial', description = NULL "
            "WHERE id IN (?, ?)",
            (pending, exhausted),
        )
        # Two real detail failures release an offer to be scored on its card.
        for http_status in (403, None):
            insert_detail_fetch(
                conn,
                JobDetailFetch(
                    job_id=exhausted,
                    source="linkedin",
                    status=DetailFetchStatus.FAILED,
                    http_status=http_status,
                    stored_at="2026-11-02T07:00:00Z",
                ),
            )
        conn.execute("UPDATE jobs SET is_active = 0 WHERE id = ?", (inactive,))
        conn.commit()

        states = offer_states(conn, stale_job_ids={stale})
        assert states == {
            scored: SCORED,
            stale: RESCORE,
            failed: FAILED,
            ready: READY,
            pending: PENDING,
            exhausted: READY,
            inactive: INACTIVE,
        }
        # A subset by id, and no profile: nothing is marked stale.
        assert offer_states(conn, [stale, pending], stale_job_ids=None) == {
            stale: SCORED,
            pending: PENDING,
        }
    finally:
        conn.close()
