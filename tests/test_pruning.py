"""Catalog-pruning tests: deterministic offer/keyword quarter plans.

Everything runs offline against synthetic SQLite fixtures. The plan is a
read-only recommendation: preview must never mutate a row, and ``--apply`` may
only deactivate the offers the plan selected, keeping their history. Keyword
series stay separate by ``(provider, query)`` and use only stored metrics, so a
missing human approval rate is unavailable rather than an invented zero.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import no_network

from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.dashboard import _fetch_keyword_stats
from jobscout.keywords import retired_search
from jobscout.db import (
    connect,
    create_search_run,
    deactivate_job,
    init_db,
    insert_evaluation,
    record_run_item,
    store_posting,
)
from jobscout.models import (
    DuplicateKind,
    Evaluation,
    JobPosting,
    Recommendation,
    ScorerName,
    SearchRun,
    SearchRunItem,
    SearchRunStatus,
)
from jobscout.normalize import finalize_posting
from jobscout.pruning import build_plan
from jobscout.timeutil import utcnow


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _posting(**overrides) -> JobPosting:
    payload = {
        "source": "linkedin",
        "title": "Senior Backend Engineer",
        "company": "Acme Data Inc.",
        "location": "Remote, Argentina",
        "url": "https://www.linkedin.com/jobs/view/3901234567/",
        "source_job_id": "3901234567",
        "description": "Python, SQL, PostgreSQL.",
    }
    payload.update(overrides)
    return finalize_posting(JobPosting(**payload))


def _score(job_id: int, score: float, recommendation: str) -> Evaluation:
    return Evaluation(
        job_id=job_id,
        scorer=ScorerName.LLM,
        overall_score=score,
        recommendation=recommendation,
        confidence=0.7,
        model="deepseek-v4.1-flash",
    )


def _insert_ok_without_score(conn: sqlite3.Connection, job_id: int) -> None:
    """Persist the malformed legacy state directly: ``status=ok``, score NULL.

    ``insert_evaluation`` refuses this shape, but a database written before that
    guard existed (or by direct SQL) can hold it, and ``latest_evaluations``
    only filters on ``status='ok'``. The plan must tolerate it.
    """
    conn.execute(
        """
        INSERT INTO evaluations (
            job_id, scorer, overall_score, recommendation, status, created_at
        ) VALUES (?, 'heuristic', NULL, 'skip', 'ok', '2026-09-15T10:00:00Z')
        """,
        (job_id,),
    )
    conn.commit()


def _run(
    conn: sqlite3.Connection,
    keyword: str,
    *,
    provider: str = "linkedin",
    cycle_id: str | None = None,
) -> SearchRun:
    return create_search_run(
        conn,
        SearchRun(
            provider=provider,
            label=f"{provider}:{keyword}",
            query=keyword,
            location="Buenos Aires",
            cycle_id=cycle_id,
            status=SearchRunStatus.COMPLETED,
            started_at="2026-09-15T10:00:00Z",
        ),
    )


def _observe(
    conn: sqlite3.Connection,
    run_id: int,
    job_id: int,
    *,
    rank: int,
    decision: DuplicateKind = DuplicateKind.NEW,
) -> None:
    record_run_item(
        conn,
        SearchRunItem(
            run_id=run_id,
            job_id=job_id,
            decision=decision,
            rank_in_run=rank,
            created_at="2026-09-15T10:00:00Z",
        ),
    )


def _store(conn: sqlite3.Connection, *, source_job_id: str, source: str = "linkedin", **kwargs):
    posting = _posting(
        source=source,
        source_job_id=source_job_id,
        url=f"https://example.test/jobs/{source_job_id}",
        title=f"Role {source_job_id}",
        **kwargs,
    )
    job, _ = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    return job


def _seed_offers(path, scores: dict[float, int]) -> dict[int, float]:
    """Seed ``count`` scored active offers at each score in ``scores``.

    Returns a map of every job id to its score. Offers are unique fingerprints.
    """
    conn = connect(path)
    ids: dict[int, float] = {}
    try:
        init_db(conn)
        counter = 0
        # Ascending score order so a higher score lands on a later id.
        for score in sorted(scores):
            for _ in range(scores[score]):
                counter += 1
                job = _store(conn, source_job_id=f"score-{score}-{counter}")
                insert_evaluation(conn, _score(job.id, score, Recommendation.APPLY))
                ids[int(job.id)] = float(score)
    finally:
        conn.close()
    return ids


# ---------------------------------------------------------------------------
# Offer plan
# ---------------------------------------------------------------------------


def test_offer_quarter_count_uses_floor(tmp_path):
    path = tmp_path / "prune.sqlite3"
    # 10 scored active offers -> floor(0.25 * 10) == 2.
    _seed_offers(path, {10.0: 5, 90.0: 5})
    conn = connect(path)
    try:
        plan = build_plan(conn)
    finally:
        conn.close()
    assert plan.offer_scored_active == 10
    assert len(plan.offers) == 2
    chosen = [offer["score"] for offer in plan.offers]
    assert chosen == [10.0, 10.0]


def test_offer_quarter_never_exceeds_25_percent(tmp_path):
    path = tmp_path / "prune.sqlite3"
    # 7 scored active offers -> floor(0.25 * 7) == 1, strictly below 25%.
    _seed_offers(path, {10.0: 1, 20.0: 3, 90.0: 3})
    conn = connect(path)
    try:
        plan = build_plan(conn)
    finally:
        conn.close()
    assert plan.offer_scored_active == 7
    assert len(plan.offers) == 1


def test_offer_plan_ranks_ascending_and_ties_by_id(tmp_path):
    path = tmp_path / "prune.sqlite3"
    ids = _seed_offers(path, {50.0: 8})
    conn = connect(path)
    try:
        plan = build_plan(conn)
    finally:
        conn.close()
    # 8 scored -> floor(2.0) == 2; identical scores -> lowest ids first.
    assert [offer["job_id"] for offer in plan.offers] == sorted(ids)[:2]


def test_offer_plan_picks_lowest_scores(tmp_path):
    path = tmp_path / "prune.sqlite3"
    _seed_offers(path, {5.0: 2, 95.0: 6})
    conn = connect(path)
    try:
        plan = build_plan(conn)
    finally:
        conn.close()
    # 8 scored -> 2 selected; both must be the two worst.
    assert [offer["score"] for offer in plan.offers] == [5.0, 5.0]


def test_unscored_offers_are_excluded(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        _store(conn, source_job_id="scored-1")
        unscored = _store(conn, source_job_id="unscored-1")
        scored = _store(conn, source_job_id="scored-2")
        insert_evaluation(conn, _score(scored.id, 30.0, Recommendation.SKIP))
        plan = build_plan(conn)
    finally:
        conn.close()
    # Only the single scored active offer is countable; floor(0.25 * 1) == 0.
    assert plan.offer_scored_active == 1
    assert plan.offers == []
    assert unscored.id not in {offer["job_id"] for offer in plan.offers}


def test_ok_evaluation_without_score_is_excluded(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # 8 scored active offers -> eligible denominator is 8, cut floor(2.0) == 2.
        for index, score in enumerate([5.0, 5.0, 90.0, 90.0, 90.0, 90.0, 90.0, 90.0]):
            job = _store(conn, source_job_id=f"scored-{index}")
            insert_evaluation(conn, _score(job.id, score, Recommendation.APPLY))
        # An ok evaluation with no score: neither counted nor ever selected.
        hollow = _store(conn, source_job_id="hollow-1")
        _insert_ok_without_score(conn, int(hollow.id))
        plan = build_plan(conn)
    finally:
        conn.close()
    assert plan.offer_scored_active == 8
    assert len(plan.offers) == 2
    assert [offer["score"] for offer in plan.offers] == [5.0, 5.0]
    assert hollow.id not in {offer["job_id"] for offer in plan.offers}


def test_inactive_offers_are_excluded(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        active = _store(conn, source_job_id="active-1")
        inactive = _store(conn, source_job_id="inactive-1")
        insert_evaluation(conn, _score(active.id, 40.0, Recommendation.SKIP))
        insert_evaluation(conn, _score(inactive.id, 5.0, Recommendation.SKIP))
        deactivate_job(conn, int(inactive.id))
        plan = build_plan(conn)
    finally:
        conn.close()
    assert plan.offer_scored_active == 1
    assert {offer["job_id"] for offer in plan.offers} == set()


def test_empty_database_plan_is_empty(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        plan = build_plan(conn)
    finally:
        conn.close()
    assert plan.offer_scored_active == 0
    assert plan.offers == []
    assert plan.keyword_candidates == []


# ---------------------------------------------------------------------------
# Keyword plan
# ---------------------------------------------------------------------------


def _seed_keyword_series(
    conn: sqlite3.Connection,
    *,
    provider: str,
    query: str,
    scores: list[float],
    duplicate_first: bool = False,
) -> list[int]:
    """Store one series: one active job per score, all observed by one run."""
    run = _run(conn, query, provider=provider)
    job_ids: list[int] = []
    for index, score in enumerate(scores):
        job = _store(conn, source_job_id=f"{provider}-{query}-{index}")
        insert_evaluation(conn, _score(job.id, score, Recommendation.CONSIDER))
        decision = (
            DuplicateKind.PROBABLE_DUPLICATE
            if duplicate_first and index == 0
            else DuplicateKind.NEW
        )
        _observe(conn, run.id, job.id, rank=index + 1, decision=decision)
        job_ids.append(int(job.id))
    return job_ids


def test_keyword_quarter_count_uses_floor_per_provider(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # 8 eligible series -> floor(0.25 * 8) == 2.
        for index in range(8):
            _seed_keyword_series(
                conn, provider="tecnoempleo", query=f"tecno-{index}", scores=[float(index)]
            )
        plan = build_plan(conn)
    finally:
        conn.close()
    assert len(plan.keyword_candidates) == 2


def test_keyword_plan_ranks_by_ascending_avg_score(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # 8 eligible series -> floor(0.25 * 8) == 2, the two worst by avg_score.
        for index, query in enumerate(
            ["worst", "bad", "mid-1", "mid-2", "mid-3", "good-1", "good-2", "best"]
        ):
            _seed_keyword_series(
                conn,
                provider="tecnoempleo",
                query=query,
                scores=[float(5 + index * 12)],
            )
        plan = build_plan(conn)
    finally:
        conn.close()
    assert [item["query"] for item in plan.keyword_candidates] == ["worst", "bad"]


def test_keyword_series_stay_separate_by_provider(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # Four distinct series per provider; the worst literal term is the same on
        # both providers and each provider cuts only its own copy.
        for provider in ("tecnoempleo", "linkedin"):
            for score, query in (
                (10.0, "same term"),
                (40.0, "second"),
                (60.0, "third"),
                (80.0, "fourth"),
            ):
                _seed_keyword_series(
                    conn,
                    provider=provider,
                    query=query,
                    scores=[score],
                )
        plan = build_plan(conn)
    finally:
        conn.close()
    # Same literal query on both providers -> each provider prunes its own one.
    providers = {item["provider"] for item in plan.keyword_candidates}
    assert providers == {"tecnoempleo", "linkedin"}
    assert all(item["query"] == "same term" for item in plan.keyword_candidates)


#: The Tecnoempleo pairs the owner retired (seven-pair decision plus CP-8).
RETIRED_TECNO_QUERIES = (
    "Platform Engineer",
    "Azure Security Engineer",
    "IAM Engineer",
    "Junior Cloud Security Engineer",
    "Security Infrastructure Engineer",
)


def test_prune_omits_retired_tecno_pairs_but_keeps_history(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # The retired pairs are the worst by avg_score: without the retirement
        # they would be selected first and consume the cut slots.
        for index, query in enumerate(RETIRED_TECNO_QUERIES, start=1):
            _seed_keyword_series(
                conn, provider="tecnoempleo", query=query, scores=[float(index)]
            )
        # Five non-retired series: floor(0.25 * 5) == 1 after the omission.
        for index, score in enumerate([10.0, 40.0, 60.0, 80.0, 90.0]):
            _seed_keyword_series(
                conn, provider="tecnoempleo", query=f"keep-{index}", scores=[score]
            )
        plan = build_plan(conn)
        # The dashboard analytics still carry the retired pairs as history.
        dashboard_keywords = {item["keyword"] for item in _fetch_keyword_stats(conn, now=utcnow())}
        run_queries = {
            str(row[0]) for row in conn.execute("SELECT DISTINCT query FROM search_runs")
        }
    finally:
        conn.close()

    retired = set(RETIRED_TECNO_QUERIES)
    assert {item["query"] for item in plan.keyword_candidates}.isdisjoint(retired)
    # Omission also removes them from the per-provider denominator.
    assert [item["query"] for item in plan.keyword_candidates] == ["keep-0"]
    for query in retired:
        assert query in dashboard_keywords
        assert query in run_queries


def test_prune_keeps_the_same_literal_for_unlisted_provider(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # Four LinkedIn series -> floor(1) == 1; "Azure Security Engineer" is
        # retired on Tecnoempleo only, so it must still be prunable on LinkedIn.
        for index, score in enumerate([1.0, 40.0, 60.0, 80.0]):
            _seed_keyword_series(
                conn,
                provider="linkedin",
                query="Azure Security Engineer" if index == 0 else f"linkedin-{index}",
                scores=[score],
            )
        plan = build_plan(conn)
    finally:
        conn.close()
    assert [item["query"] for item in plan.keyword_candidates] == ["Azure Security Engineer"]
    assert plan.keyword_candidates[0]["provider"] == "linkedin"


LINKEDIN_RETIRED_QUERIES = (
    "Identity and Access Management Engineer",
    "IAM Engineer",
)


def test_prune_omits_retired_linkedin_pairs_but_keeps_history(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # The two retired LinkedIn pairs are the worst by avg_score: without the
        # retirement they would be selected first and consume the cut slots.
        for score, query in enumerate(LINKEDIN_RETIRED_QUERIES, start=1):
            _seed_keyword_series(
                conn, provider="linkedin", query=query, scores=[float(score)]
            )
        # Five non-retired series: floor(0.25 * 5) == 1 after the omission.
        for index, score in enumerate([10.0, 40.0, 60.0, 80.0, 90.0]):
            _seed_keyword_series(
                conn, provider="linkedin", query=f"keep-{index}", scores=[score]
            )
        plan = build_plan(conn)
        # The dashboard analytics still carry the retired pairs as history.
        dashboard_keywords = {
            item["keyword"] for item in _fetch_keyword_stats(conn, now=utcnow())
        }
        run_queries = {
            str(row[0]) for row in conn.execute("SELECT DISTINCT query FROM search_runs")
        }
    finally:
        conn.close()

    retired = set(LINKEDIN_RETIRED_QUERIES)
    assert {item["query"] for item in plan.keyword_candidates}.isdisjoint(retired)
    assert [item["query"] for item in plan.keyword_candidates] == ["keep-0"]
    for query in retired:
        assert query in dashboard_keywords
        assert query in run_queries


def test_restored_linkedin_cloud_security_engineer_is_a_prune_candidate(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # CP-8 restored the LinkedIn pair because it costs zero unique offers.
        assert retired_search("linkedin", "Cloud Security Engineer") is None
        for index, score in enumerate([1.0, 40.0, 60.0, 80.0]):
            _seed_keyword_series(
                conn,
                provider="linkedin",
                query="Cloud Security Engineer" if index == 0 else f"linkedin-{index}",
                scores=[score],
            )
        plan = build_plan(conn)
    finally:
        conn.close()
    assert [item["query"] for item in plan.keyword_candidates] == ["Cloud Security Engineer"]
    assert plan.keyword_candidates[0]["provider"] == "linkedin"


def test_prune_keeps_held_tecnoempleo_literal_when_no_good_offer(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # "Identity and Access Management Engineer" is still active on
        # Tecnoempleo (the function browse did not retire it): when its unique
        # offers are all low-scoring it stays a candidate.
        assert retired_search("tecnoempleo", "Identity and Access Management Engineer") is None
        for index, score in enumerate([1.0, 40.0, 60.0, 80.0]):
            _seed_keyword_series(
                conn,
                provider="tecnoempleo",
                query=(
                    "Identity and Access Management Engineer"
                    if index == 0
                    else f"tecno-{index}"
                ),
                scores=[score],
            )
        plan = build_plan(conn)
    finally:
        conn.close()
    assert [item["query"] for item in plan.keyword_candidates] == [
        "Identity and Access Management Engineer"
    ]
    assert plan.keyword_candidates[0]["provider"] == "tecnoempleo"


def test_keyword_plan_keeps_provider_identity_and_metrics(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # 4 eligible series -> floor(1.0) == 1; the worst has one duplicate
        # sighting out of two, so its duplicate rate is exactly 0.5.
        _seed_keyword_series(
            conn,
            provider="tecnoempleo",
            query="term-0",
            scores=[10.0, 10.0],
            duplicate_first=True,
        )
        for index in range(1, 4):
            _seed_keyword_series(
                conn,
                provider="tecnoempleo",
                query=f"term-{index}",
                scores=[float(10 + index * 20)],
            )
        plan = build_plan(conn)
    finally:
        conn.close()
    assert len(plan.keyword_candidates) == 1
    item = plan.keyword_candidates[0]
    assert item["provider"] == "tecnoempleo"
    assert item["query"] == "term-0"
    assert item["avg_score"] == 10.0
    assert item["top5_avg_score"] == 10.0
    assert item["top10_avg_score"] == 10.0
    assert item["duplicate_rate"] == 0.5
    # No imported human feedback -> the rate stays unavailable, not zero.
    assert item["human_apply_rate"] is None


def test_missing_human_apply_rate_is_none_not_zero(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        for index in range(4):
            _seed_keyword_series(
                conn,
                provider="linkedin",
                query=f"term-{index}",
                scores=[float(10 + index * 20)],
            )
        plan = build_plan(conn)
    finally:
        conn.close()
    assert plan.keyword_candidates
    assert all(
        item["human_apply_rate"] is None for item in plan.keyword_candidates
    )


def test_series_without_score_is_not_eligible(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # Four scored series + one unscored series.
        for index in range(4):
            _seed_keyword_series(
                conn, provider="linkedin", query=f"scored-{index}", scores=[float(10 + index * 20)]
            )
        run = _run(conn, "unscored", provider="linkedin")
        job = _store(conn, source_job_id="unscored-only")
        _observe(conn, run.id, job.id, rank=1)
        plan = build_plan(conn)
    finally:
        conn.close()
    queries = [item["query"] for item in plan.keyword_candidates]
    assert "unscored" not in queries
    assert len(plan.keyword_candidates) == 1


def test_small_provider_group_is_not_over_pruned(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        # 3 eligible series -> floor(0.75) == 0, so nothing is cut.
        for index in range(3):
            _seed_keyword_series(
                conn, provider="linkedin", query=f"term-{index}", scores=[float(index)]
            )
        plan = build_plan(conn)
    finally:
        conn.close()
    assert plan.keyword_candidates == []


# ---------------------------------------------------------------------------
# Unique-offer cost (owner rule: never cost a good offer)
# ---------------------------------------------------------------------------


def _seed_unique_cost_fixture(conn: sqlite3.Connection) -> None:
    """Eight Tecnoempleo series whose worst quarter is a good-cost and a free one.

    ``shells`` averages 18.33 (five 10s and one 60) and alone surfaced the 60, a
    good offer, so it must be held back. ``shared-low`` averages 12 but has no
    unique offer: its two postings were also surfaced by ``shared-high``. Five
    neutral series round the group to eight, so ``floor(25%) == 2``.
    """
    shells_run = _run(conn, "shells", provider="tecnoempleo")
    for index, score in enumerate([10.0, 10.0, 10.0, 10.0, 10.0, 60.0]):
        job = _store(conn, source_job_id=f"shells-{index}", source="tecnoempleo")
        insert_evaluation(conn, _score(job.id, score, Recommendation.CONSIDER))
        _observe(conn, shells_run.id, job.id, rank=index + 1)

    low_run = _run(conn, "shared-low", provider="tecnoempleo")
    high_run = _run(conn, "shared-high", provider="tecnoempleo")
    for index, score in enumerate([12.0, 12.0]):
        job = _store(conn, source_job_id=f"shared-{index}", source="tecnoempleo")
        insert_evaluation(conn, _score(job.id, score, Recommendation.CONSIDER))
        _observe(conn, low_run.id, job.id, rank=index + 1)
        _observe(conn, high_run.id, job.id, rank=index + 1)
    for index, score in enumerate([90.0, 90.0]):
        job = _store(conn, source_job_id=f"high-{index}", source="tecnoempleo")
        insert_evaluation(conn, _score(job.id, score, Recommendation.CONSIDER))
        _observe(conn, high_run.id, job.id, rank=index + 3)

    for index, score in enumerate([30.0, 40.0, 50.0, 70.0, 80.0]):
        _seed_keyword_series(
            conn, provider="tecnoempleo", query=f"neutral-{index}", scores=[score]
        )


def test_prune_holds_back_a_candidate_that_costs_a_good_offer(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        _seed_unique_cost_fixture(conn)
        plan = build_plan(conn)
    finally:
        conn.close()

    assert [item["query"] for item in plan.keyword_candidates] == ["shared-low"]
    assert [item["query"] for item in plan.held_back_keywords] == ["shells"]

    kept = plan.keyword_candidates[0]
    assert kept["unique_offers"] == 0
    assert kept["unique_good_offers"] == 0

    held = plan.held_back_keywords[0]
    assert held["unique_offers"] == 6
    assert held["unique_good_offers"] == 1
    assert "held back" in plan.to_text()


def test_prune_json_reports_held_back_candidates(tmp_path, capsys):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        _seed_unique_cost_fixture(conn)
    finally:
        conn.close()

    assert main(["--db", str(path), "prune", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert [item["query"] for item in payload["held_back_keywords"]] == ["shells"]
    assert payload["held_back_keywords"][0]["unique_good_offers"] == 1
    assert [item["query"] for item in payload["keyword_candidates"]] == ["shared-low"]
    assert payload["keyword_candidates"][0]["unique_offers"] == 0


# ---------------------------------------------------------------------------
# Preview is non-mutating
# ---------------------------------------------------------------------------


def _snapshot(path) -> tuple[bytes, tuple[str, ...]]:
    import os

    parent = path.parent
    return (
        path.read_bytes(),
        tuple(sorted(os.listdir(parent))),
    )


def test_preview_does_not_mutate_database(tmp_path):
    path = tmp_path / "prune.sqlite3"
    _seed_offers(path, {10.0: 5, 90.0: 5})
    before = _snapshot(path)
    assert main(["--db", str(path), "prune"]) == EXIT_OK
    after = _snapshot(path)
    assert before == after


def test_preview_json_is_read_only(tmp_path, capsys):
    path = tmp_path / "prune.sqlite3"
    _seed_offers(path, {10.0: 5, 90.0: 5})
    before = _snapshot(path)
    assert main(["--db", str(path), "prune", "--json"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "offers" in out
    assert _snapshot(path) == before


def test_missing_database_is_a_clear_error(tmp_path, capsys):
    path = tmp_path / "does-not-exist.sqlite3"
    assert main(["--db", str(path), "prune"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "no database found" in err
    assert not path.exists()


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def test_apply_deactivates_only_selected_offers(tmp_path):
    path = tmp_path / "prune.sqlite3"
    ids = _seed_offers(path, {10.0: 5, 90.0: 5})
    assert main(["--db", str(path), "prune", "--apply"]) == EXIT_OK

    conn = connect(path)
    try:
        plan = build_plan(conn)
        inactive = {
            int(row["id"])
            for row in conn.execute("SELECT id FROM jobs WHERE is_active = 0")
        }
    finally:
        conn.close()
    # floor(0.25 * 10) == 2 offers deactivated, the two worst.
    assert len(inactive) == 2
    assert all(ids[job_id] == 10.0 for job_id in inactive)
    # A fresh plan no longer counts them as active.
    assert plan.offer_scored_active == 8


def test_apply_preserves_offer_history(tmp_path):
    path = tmp_path / "prune.sqlite3"
    ids = _seed_offers(path, {10.0: 5, 90.0: 5})
    assert main(["--db", str(path), "prune", "--apply"]) == EXIT_OK

    conn = connect(path)
    try:
        for job_id in ids:
            assert conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 10
    finally:
        conn.close()


def test_apply_with_empty_plan_changes_nothing(tmp_path):
    path = tmp_path / "prune.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
        job = _store(conn, source_job_id="only-1")
        insert_evaluation(conn, _score(job.id, 30.0, Recommendation.SKIP))
    finally:
        conn.close()
    before = _snapshot(path)
    assert main(["--db", str(path), "prune", "--apply"]) == EXIT_OK
    assert _snapshot(path) == before


def test_apply_never_creates_a_database(tmp_path):
    path = tmp_path / "absent.sqlite3"
    assert main(["--db", str(path), "prune", "--apply"]) == EXIT_ERROR
    assert not path.exists()


def test_apply_is_deterministic(tmp_path):
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    _seed_offers(first, {10.0: 5, 90.0: 5})
    _seed_offers(second, {10.0: 5, 90.0: 5})
    assert main(["--db", str(first), "prune", "--apply"]) == EXIT_OK
    assert main(["--db", str(second), "prune", "--apply"]) == EXIT_OK

    def _inactive(path):
        conn = connect(path)
        try:
            return sorted(
                int(row["id"])
                for row in conn.execute("SELECT id FROM jobs WHERE is_active = 0")
            )
        finally:
            conn.close()

    assert _inactive(first) == _inactive(second)


def test_apply_uses_no_network(tmp_path, monkeypatch):
    path = tmp_path / "prune.sqlite3"
    _seed_offers(path, {10.0: 5, 90.0: 5})
    no_network(monkeypatch)
    assert main(["--db", str(path), "prune", "--apply"]) == EXIT_OK


def test_apply_is_off_by_default(tmp_path, capsys):
    path = tmp_path / "prune.sqlite3"
    _seed_offers(path, {10.0: 5, 90.0: 5})
    assert main(["--db", str(path), "prune"]) == EXIT_OK
    conn = connect(path)
    try:
        active = conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active = 1").fetchone()[0]
    finally:
        conn.close()
    assert active == 10
