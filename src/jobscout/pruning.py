"""Deterministic, offline catalog-pruning plan.

The plan answers two questions with data jobscout already stores, and nothing
else:

* which active, already-scored offers are the worst quarter by
  ``latest_evaluations.overall_score``;
* which keyword *series* (``provider|query``) are the worst quarter within each
  provider by ``avg_score``.

It is a recommendation, never an automatic action. :func:`build_plan` only
reads; :func:`apply_offer_plan` is the single mutation and it reuses the
existing :func:`jobscout.db.deactivate_job` path, so history is kept. No
composite score, schema, or metric is introduced: the keyword context columns
are shown as-is, and a metric with no stored data behind it stays ``None``
("unavailable") instead of being guessed as zero.

Provider identity is part of the keyword series key, so cutting
``query|tecnoempleo`` can never cut ``query|linkedin``. There is no
cycle-count gate: a provider group with fewer than four eligible series is not
over-pruned because ``floor(0.25 * count)`` is zero for it.

The owner rule is the last cut: a candidate is only proposed when it would cost
no *good* offer. The cost of cutting ``(provider, query)`` is the active offers
that **no other query of the same provider** surfaced; a cost offer is *good*
when its existing ``latest_evaluations.overall_score`` is at least
:data:`GOOD_OFFER_SCORE`. A worst-quarter candidate that would cost a good
offer is moved to :attr:`PruningPlan.held_back_keywords` (with its measured
counts and the reason) instead of being proposed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .dashboard import _fetch_keyword_stats
from .db import deactivate_job
from .keywords import retired_search
from .timeutil import utcnow

#: Minimum ``latest_evaluations.overall_score`` for an offer to count as good.
#: Cutting a query that alone surfaced one of these is never proposed.
GOOD_OFFER_SCORE = 45

@dataclass
class PruningPlan:
    """A read-only recommendation for offers and provider-specific keywords."""

    offers: list[dict[str, Any]] = field(default_factory=list)
    offer_scored_active: int = 0
    keyword_candidates: list[dict[str, Any]] = field(default_factory=list)
    held_back_keywords: list[dict[str, Any]] = field(default_factory=list)
    provider_series: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "offer_scored_active": self.offer_scored_active,
            "offer_candidates": len(self.offers),
            "offers": list(self.offers),
            "provider_series": dict(self.provider_series),
            "keyword_candidates": list(self.keyword_candidates),
            "held_back_keywords": list(self.held_back_keywords),
        }

    def to_text(self) -> str:
        lines = [
            (
                f"pruning plan: {len(self.offers)} of {self.offer_scored_active} "
                "scored active offer(s) are the worst quarter; no row was changed"
            )
        ]
        for offer in self.offers:
            lines.append(
                f"  offer #{offer['job_id']} score={offer['score']} "
                f"[{offer['provider']}] {offer['title']}"
            )
        if not self.offers:
            lines.append("  (no offer reaches the cut; nothing to deactivate)")
        lines.append(
            f"keyword candidates per provider: "
            f"{len(self.keyword_candidates)} (report only; the catalog is never mutated)"
        )
        for item in self.keyword_candidates:
            lines.append(
                f"  [{item['provider']}] {item['query']} "
                f"avg={item['avg_score']} top5={item['top5_avg_score']} "
                f"top10={item['top10_avg_score']} dup={item['duplicate_rate']} "
                f"human_apply={item['human_apply_rate']} "
                f"unique={item['unique_offers']} good={item['unique_good_offers']}"
            )
        if self.held_back_keywords:
            lines.append(
                f"held back because retiring them would cost a good offer "
                f"(score >= {GOOD_OFFER_SCORE}): {len(self.held_back_keywords)}"
            )
            for item in self.held_back_keywords:
                lines.append(
                    f"  [{item['provider']}] {item['query']} "
                    f"avg={item['avg_score']} unique={item['unique_offers']} "
                    f"good={item['unique_good_offers']} -- {item['held_back_reason']}"
                )
        lines.append(
            "note: the plan never suppresses a keyword in another provider; "
            "run with --apply to deactivate the selected offers only"
        )
        return "\n".join(lines)



def _quarter(count: int) -> int:
    """Worst ``floor(25%)`` of ``count``, which never exceeds 25 percent."""
    if count <= 0:
        return 0
    return count // 4


def _build_offer_candidates(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], int]:
    """Worst quarter of active, already-scored offers by ascending score.

    Only jobs with a successful latest evaluation carrying a numeric score are
    eligible: an unscored offer, or an ``ok`` evaluation with a NULL score, is
    never selected and does not inflate the denominator. Ties break on
    ascending job id so the same database always yields the same selection and
    the count can never exceed ``floor(25%)``.
    """
    total = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM jobs j
            JOIN latest_evaluations e ON e.job_id = j.id
            WHERE j.is_active = 1 AND e.overall_score IS NOT NULL
            """
        ).fetchone()[0]
    )
    if total == 0:
        return [], 0
    rows = conn.execute(
        """
        SELECT j.id AS id,
               j.title AS title,
               j.source AS provider,
               e.overall_score AS score
        FROM jobs j
        JOIN latest_evaluations e ON e.job_id = j.id
        WHERE j.is_active = 1 AND e.overall_score IS NOT NULL
        ORDER BY e.overall_score ASC, j.id ASC
        LIMIT ?
        """,
        (_quarter(total),),
    ).fetchall()
    offers = [
        {
            "job_id": int(row["id"]),
            "title": str(row["title"] or ""),
            "provider": str(row["provider"] or ""),
            "score": round(float(row["score"]), 2),
        }
        for row in rows
    ]
    return offers, total


def _keyword_unique_cost(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[int, int]]:
    """Unique-offer cost per ``(provider, query)`` series, from stored rows only.

    An active offer is *unique* to a series when no other query of the **same
    provider** surfaced it; the same literal on another provider never counts.
    ``(unique_offers, unique_good_offers)`` is returned per series, where a good
    offer has ``latest_evaluations.overall_score >= GOOD_OFFER_SCORE``. A series
    with no unique offer is absent from the mapping (the caller defaults to
    ``(0, 0)``). The active-job and smoke-cycle rules match ``prune`` and the
    dashboard, so the counted cost is the same set the report ranks.
    """
    rows = conn.execute(
        """
        WITH keyword_jobs AS (
            SELECT DISTINCT r.provider AS provider,
                            r.query AS query,
                            i.job_id AS job_id
            FROM search_run_items i
            JOIN search_runs r ON r.id = i.run_id
            JOIN jobs j ON j.id = i.job_id
            WHERE r.query IS NOT NULL AND TRIM(r.query) <> ''
              AND LOWER(TRIM(COALESCE(r.cycle_id, ''))) NOT LIKE 'smoke%'
              AND j.is_active = 1
        ),
        unique_jobs AS (
            SELECT kj.provider AS provider, kj.query AS query, kj.job_id AS job_id
            FROM keyword_jobs kj
            WHERE NOT EXISTS (
                SELECT 1 FROM keyword_jobs other
                WHERE other.job_id = kj.job_id
                  AND other.provider = kj.provider
                  AND other.query <> kj.query
            )
        )
        SELECT u.provider AS provider,
               u.query AS query,
               COUNT(*) AS unique_offers,
               SUM(CASE WHEN e.overall_score >= ? THEN 1 ELSE 0 END) AS unique_good_offers
        FROM unique_jobs u
        LEFT JOIN latest_evaluations e ON e.job_id = u.job_id
        GROUP BY u.provider, u.query
        """,
        (GOOD_OFFER_SCORE,),
    ).fetchall()
    return {
        (str(row["provider"] or ""), str(row["query"])): (
            int(row["unique_offers"] or 0),
            int(row["unique_good_offers"] or 0),
        )
        for row in rows
    }


def _build_keyword_candidates(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Worst quarter of eligible keyword series, independently per provider.

    Uses the dashboard's existing per-series aggregation, which already keys by
    ``provider|query``, filters synthetic smoke cycles, counts only active
    offers, and leaves an unsupported metric as ``None``. A series is eligible
    only when it has at least one scored active offer (``avg_score`` present).
    A worst-quarter series that alone surfaced a good offer is held back instead
    of proposed, so the owner rule never costs a good offer.
    """
    stats = _fetch_keyword_stats(conn, now=utcnow())
    costs = _keyword_unique_cost(conn)
    eligible_by_provider: dict[str, list[dict[str, Any]]] = {}
    total_by_provider: dict[str, int] = {}
    for item in stats:
        provider = str(item["provider"] or "")
        # A retired pair is no longer searchable, so it must not consume a cut
        # slot or inflate the per-provider denominator. Its analytics history
        # stays in the dashboard; only its candidacy is suppressed.
        if retired_search(provider, str(item["keyword"])) is not None:
            continue
        total_by_provider[provider] = total_by_provider.get(provider, 0) + 1
        if item["avg_score"] is None:
            continue
        eligible_by_provider.setdefault(provider, []).append(item)

    candidates: list[dict[str, Any]] = []
    held_back: list[dict[str, Any]] = []
    for provider in sorted(eligible_by_provider):
        series = eligible_by_provider[provider]
        ranked = sorted(
            series,
            key=lambda item: (float(item["avg_score"]), str(item["keyword"])),
        )
        for item in ranked[: _quarter(len(series))]:
            query = str(item["keyword"])
            unique_offers, unique_good = costs.get((provider, query), (0, 0))
            entry = {
                "provider": provider,
                "query": query,
                "avg_score": item["avg_score"],
                "top5_avg_score": item["top5_avg_score"],
                "top10_avg_score": item["top10_avg_score"],
                "duplicate_rate": item["duplicate_rate"],
                "human_apply_rate": item["human_apply_rate"],
                "unique_offers": unique_offers,
                "unique_good_offers": unique_good,
            }
            if unique_good > 0:
                entry["held_back_reason"] = (
                    f"alone surfaced {unique_good} good offer(s) "
                    f"(score >= {GOOD_OFFER_SCORE}); kept active"
                )
                held_back.append(entry)
            else:
                candidates.append(entry)
    return candidates, held_back, total_by_provider


def build_plan(conn: sqlite3.Connection) -> PruningPlan:
    """Build the read-only pruning recommendation from stored rows only."""
    offers, total = _build_offer_candidates(conn)
    candidates, held_back, provider_series = _build_keyword_candidates(conn)
    return PruningPlan(
        offers=offers,
        offer_scored_active=total,
        keyword_candidates=candidates,
        held_back_keywords=held_back,
        provider_series=provider_series,
    )


def apply_offer_plan(conn: sqlite3.Connection, plan: PruningPlan) -> int:
    """Deactivate exactly the offers the plan selected; return how many changed.

    Delegates to :func:`jobscout.db.deactivate_job`, so a deactivated offer keeps
    every evaluation, feedback row, and fetch record. Only ids carried by the
    plan are touched. Reapplying this same plan object is a no-op because its
    selected rows are already inactive; a fresh CLI plan can select another
    quarter from the remaining active rows.
    """
    changed = 0
    for offer in plan.offers:
        if deactivate_job(conn, int(offer["job_id"])):
            changed += 1
    return changed


__all__ = [
    "GOOD_OFFER_SCORE",
    "PruningPlan",
    "apply_offer_plan",
    "build_plan",
]
