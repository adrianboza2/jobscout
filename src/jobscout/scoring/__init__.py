"""Scoring facade: select a scorer, evaluate a job, persist provenance."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..db import insert_evaluation, upsert_profile
from ..models import Evaluation, ProfileSnapshot, ScorerName
from .heuristic import SCORER_NAME as HEURISTIC_NAME
from .heuristic import score_job as score_job_heuristic
from .llm import LLMError, LLMNotConfiguredError, LLMRequestError, OpenAICompatibleScorer
from .opencode import (
    OpenCodeError,
    OpenCodeExecutionError,
    OpenCodeNotConfiguredError,
    OpenCodeScorer,
    OpenCodeTimeoutError,
)
from .profile import Profile, build_snapshot, load_profile
from .schema import PROMPT_VERSION, ParseOutcome, parse_llm_evaluation

SCORERS = (
    ScorerName.HEURISTIC.value,
    ScorerName.LLM.value,
    ScorerName.OPENCODE.value,
)


class ScorerError(ValueError):
    """Raised when an unknown scorer name is requested."""


class HeuristicScorer:
    """Adapter giving the heuristic scorer the same interface as the LLM one."""

    name = HEURISTIC_NAME

    def __init__(self, settings: Settings | None = None, **_: Any) -> None:
        self.settings = settings

    def score(
        self,
        job: Any,
        profile: Profile,
        *,
        job_id: int | None = None,
        profile_id: int | None = None,
        conn: Any = None,
        now: Any = None,
    ) -> Evaluation:
        return score_job_heuristic(job, profile, now=now, job_id=job_id, profile_id=profile_id)


def get_scorer(name: str, settings: Settings, **kwargs: Any) -> Any:
    """Return a scorer instance implementing ``score(job, profile, ...)``."""
    normalized = (name or "").strip().lower()
    if normalized == ScorerName.HEURISTIC.value:
        return HeuristicScorer(settings)
    if normalized == ScorerName.LLM.value:
        return OpenAICompatibleScorer(settings, **kwargs)
    if normalized == ScorerName.OPENCODE.value:
        return OpenCodeScorer(settings, **kwargs)
    raise ScorerError(f"unknown scorer '{name}'; expected one of: {', '.join(SCORERS)}")


def evaluate_job(
    conn: Any,
    job: Any,
    profile: Profile,
    *,
    scorer_name: str,
    settings: Settings,
    profile_snapshot: ProfileSnapshot | None = None,
    scorer: Any = None,
    now: Any = None,
) -> Evaluation:
    """Evaluate one job and persist the evaluation with full provenance.

    The profile snapshot is upserted first so evaluations always reference the
    exact profile version that produced them.
    """
    snapshot = profile_snapshot or build_snapshot(profile)
    stored_snapshot = upsert_profile(conn, snapshot)

    active_scorer = scorer or get_scorer(scorer_name, settings)
    evaluation = active_scorer.score(
        job,
        profile,
        job_id=getattr(job, "id", None),
        profile_id=stored_snapshot.id,
        conn=conn,
        now=now,
    )
    if evaluation.profile_id is None:
        evaluation.profile_id = stored_snapshot.id
    return insert_evaluation(conn, evaluation)


__all__ = [
    "HEURISTIC_NAME",
    "HeuristicScorer",
    "LLMError",
    "LLMNotConfiguredError",
    "LLMRequestError",
    "OpenCodeError",
    "OpenCodeExecutionError",
    "OpenCodeNotConfiguredError",
    "OpenCodeScorer",
    "OpenCodeTimeoutError",
    "PROMPT_VERSION",
    "ParseOutcome",
    "Profile",
    "SCORERS",
    "ScorerError",
    "build_snapshot",
    "evaluate_job",
    "get_scorer",
    "load_profile",
    "parse_llm_evaluation",
    "score_job_heuristic",
]
