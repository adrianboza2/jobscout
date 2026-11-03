"""Shared effective-input fingerprint across heuristic, LLM, and OpenCode."""

from __future__ import annotations

from dataclasses import replace

import pytest

from jobscout.db import insert_evaluation
from jobscout.models import Evaluation, Recommendation, ScorerName
from jobscout.scoring import (
    ScorerError,
    effective_fingerprint,
    fingerprint_resolver,
    stale_job_ids,
)
from jobscout.scoring.heuristic import input_fingerprint as heuristic_fingerprint
from jobscout.scoring.llm import input_fingerprint as llm_fingerprint
from jobscout.scoring.opencode import input_fingerprint as opencode_fingerprint
from jobscout.scoring.profile import profile_from_dict


def test_each_scorer_has_its_own_fingerprint(strong_posting, profile, settings, opencode_settings) -> None:
    heuristic = heuristic_fingerprint(strong_posting, profile)
    llm = llm_fingerprint(strong_posting, profile, settings.llm_model)
    opencode = opencode_fingerprint(strong_posting, profile, opencode_settings)

    assert len({heuristic, llm, opencode}) == 3


def test_fingerprint_is_stable_for_the_same_input(strong_posting, profile, settings) -> None:
    first = llm_fingerprint(strong_posting, profile, settings.llm_model)
    second = llm_fingerprint(strong_posting, profile, settings.llm_model)
    assert first == second


def test_job_content_change_changes_the_fingerprint(strong_posting, profile, settings) -> None:
    before = llm_fingerprint(strong_posting, profile, settings.llm_model)
    rewritten = replace(strong_posting, description=(strong_posting.description or "") + " extra")
    after = llm_fingerprint(rewritten, profile, settings.llm_model)
    assert before != after


def test_profile_change_changes_the_fingerprint(strong_posting, profile, settings) -> None:
    before = llm_fingerprint(strong_posting, profile, settings.llm_model)
    changed = profile_from_dict({**profile.to_dict(), "summary": "different summary"})
    after = llm_fingerprint(strong_posting, changed, settings.llm_model)
    assert before != after


def test_llm_fingerprint_tracks_the_configured_model(strong_posting, profile) -> None:
    a = llm_fingerprint(strong_posting, profile, "model-a")
    b = llm_fingerprint(strong_posting, profile, "model-b")
    assert a != b


def test_opencode_fingerprint_tracks_the_effective_prompt_version(
    strong_posting, profile, opencode_settings
) -> None:
    minimal = opencode_settings.with_overrides(opencode_variant="minimal")
    maximum = opencode_settings.with_overrides(opencode_variant="max")
    assert opencode_fingerprint(strong_posting, profile, minimal) != opencode_fingerprint(
        strong_posting, profile, maximum
    )

    # A variant change is a prompt-version change, never silently the same.
    assert opencode_fingerprint(strong_posting, profile, minimal) != opencode_fingerprint(
        strong_posting, profile, opencode_settings.with_overrides(opencode_variant="")
    )


def test_facade_dispatches_to_each_backend(strong_posting, profile, settings, opencode_settings) -> None:
    assert (
        effective_fingerprint("heuristic", strong_posting, profile, settings)
        == heuristic_fingerprint(strong_posting, profile)
    )
    assert (
        effective_fingerprint("llm", strong_posting, profile, settings)
        == llm_fingerprint(strong_posting, profile, settings.llm_model)
    )
    assert (
        effective_fingerprint("opencode", strong_posting, profile, opencode_settings)
        == opencode_fingerprint(strong_posting, profile, opencode_settings)
    )


def test_fingerprint_resolver_is_bound_to_scorer_profile_and_settings(
    strong_posting, profile, settings
) -> None:
    resolve = fingerprint_resolver("heuristic", settings, profile)
    assert resolve(strong_posting) == heuristic_fingerprint(strong_posting, profile)


def test_unknown_scorer_is_rejected(strong_posting, profile, settings) -> None:
    with pytest.raises(ScorerError):
        effective_fingerprint("nope", strong_posting, profile, settings)


def _score_with_fingerprint(job, fingerprint: str) -> Evaluation:
    return Evaluation(
        job_id=job.id,
        scorer=ScorerName.HEURISTIC,
        overall_score=50,
        recommendation=Recommendation.CONSIDER,
        input_fingerprint=fingerprint,
    )


def test_stale_job_ids_accepts_a_current_score(conn, stored_job, profile, settings) -> None:
    current = effective_fingerprint("heuristic", stored_job, profile, settings)
    insert_evaluation(conn, _score_with_fingerprint(stored_job, current))

    assert stale_job_ids(conn, [stored_job], profile, settings) == set()


def test_stale_job_ids_flags_changed_input(conn, stored_job, profile, settings) -> None:
    current = effective_fingerprint("heuristic", stored_job, profile, settings)
    insert_evaluation(conn, _score_with_fingerprint(stored_job, current))

    rewritten = replace(
        stored_job, description=(stored_job.description or "") + " extra"
    )
    changed_profile = profile_from_dict(
        {**profile.to_dict(), "summary": "different summary"}
    )

    assert stale_job_ids(conn, [rewritten], profile, settings) == {stored_job.id}
    assert stale_job_ids(conn, [stored_job], changed_profile, settings) == {
        stored_job.id
    }


def test_stale_job_ids_ignores_unscored_offers(conn, stored_job, profile, settings) -> None:
    assert stale_job_ids(conn, [stored_job], profile, settings) == set()


def test_stale_job_ids_flags_an_empty_stored_fingerprint(
    conn, stored_job, profile, settings
) -> None:
    insert_evaluation(conn, _score_with_fingerprint(stored_job, ""))

    assert stale_job_ids(conn, [stored_job], profile, settings) == {stored_job.id}


def test_stale_job_ids_accepts_any_matching_success_of_the_same_scorer(
    conn, stored_job, profile, settings
) -> None:
    current = effective_fingerprint("heuristic", stored_job, profile, settings)
    insert_evaluation(conn, _score_with_fingerprint(stored_job, current))
    insert_evaluation(conn, _score_with_fingerprint(stored_job, "different"))

    # The newest success is what ``list`` shows, but the older success still
    # proves the current input was evaluated, matching selection semantics.
    assert stale_job_ids(conn, [stored_job], profile, settings) == set()
