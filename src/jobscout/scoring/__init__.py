"""Scoring facade: select a scorer, evaluate a job, persist provenance."""

from __future__ import annotations

from typing import Any, Callable

from ..config import Settings
from ..db import insert_evaluation, upsert_profile
from ..models import (
    Evaluation,
    EvaluationStatus,
    ProfileSnapshot,
    Recommendation,
    ScorerName,
    evaluation_contract_errors,
)
from .currency import currency_blocker
from .heuristic import SCORER_NAME as HEURISTIC_NAME
from .heuristic import input_fingerprint as heuristic_input_fingerprint
from .heuristic import score_job as score_job_heuristic
from .llm import LLMError, LLMNotConfiguredError, LLMRequestError, OpenAICompatibleScorer
from .llm import input_fingerprint as llm_input_fingerprint
from .opencode import (
    OpenCodeError,
    OpenCodeExecutionError,
    OpenCodeNotConfiguredError,
    OpenCodeScorer,
    OpenCodeTimeoutError,
)
from .opencode import input_fingerprint as opencode_input_fingerprint
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


def effective_fingerprint(
    scorer_name: str, job: Any, profile: Profile, settings: Settings
) -> str:
    """Effective-input fingerprint for ``scorer_name`` and this exact input.

    This is the single dispatch point shared by scoring and by input-aware
    selection (``jobs_needing_evaluation``), so the fingerprint a scorer
    persists and the fingerprint the selector compares can never drift apart.
    Each backend contributes its own scorer identity, model, and effective
    prompt/policy version.
    """
    normalized = (scorer_name or "").strip().lower()
    if normalized == ScorerName.HEURISTIC.value:
        return heuristic_input_fingerprint(job, profile)
    if normalized == ScorerName.LLM.value:
        return llm_input_fingerprint(job, profile, settings.llm_model)
    if normalized == ScorerName.OPENCODE.value:
        return opencode_input_fingerprint(job, profile, settings)
    raise ScorerError(f"unknown scorer '{scorer_name}'; expected one of: {', '.join(SCORERS)}")


def fingerprint_resolver(
    scorer_name: str, settings: Settings, profile: Profile
) -> Callable[[Any], str]:
    """Return ``job -> fingerprint`` bound to one scorer/profile/settings.

    Used by the CLI to make normal selection input-aware: an offer is selected
    only when no successful evaluation carries its current effective
    fingerprint. ``--rescore`` deliberately bypasses this.
    """

    def resolve(job: Any) -> str:
        return effective_fingerprint(scorer_name, job, profile, settings)

    return resolve


#: Total attempts allowed while a scorer response is contract-invalid (malformed
#: output, a missing score, or an unresolved recommendation). Transport, access,
#: authentication, and rate-limit failures are never retried as repairs: those
#: include :class:`EvaluationStatus.ERROR` and are reported as technical failures.
MAX_CONTRACT_ATTEMPTS = 2


def _is_contract_repairable(evaluation: Evaluation) -> bool:
    """Whether a scorer response is invalid output worth a bounded repair attempt.

    Two shapes qualify: a reported ``parse_error``, and an ``ok`` evaluation that
    still violates the invariant (defensive: an injected or future scorer might).
    A transport/access failure (``error``) is an operator problem, not a schema
    mishap, and is deliberately not retried.
    """
    if str(evaluation.status) == EvaluationStatus.PARSE_ERROR.value:
        return True
    return bool(evaluation_contract_errors(evaluation))


def _ensure_contract(evaluation: Evaluation) -> Evaluation:
    """Record an unresolved success honestly instead of as an ``ok`` evaluation.

    The persistence layer refuses an ``ok`` evaluation that violates the canonical
    invariant, so a scorer that produced a score but no public recommendation (for
    example the deterministic heuristic on a thin posting) is downgraded to
    ``needs_more_info`` here, preserving the score and the reason.
    """
    errors = evaluation_contract_errors(evaluation)
    if not errors:
        return evaluation
    evaluation.status = EvaluationStatus.NEEDS_MORE_INFO
    detail = "; ".join(errors)
    evaluation.error = f"{evaluation.error}; {detail}" if evaluation.error else detail
    if "incomplete" not in evaluation.rationale.lower():
        evaluation.rationale = (
            evaluation.rationale
            + " [incomplete: the evaluator could not settle on a public recommendation]"
        ).strip()
    return evaluation


def _heuristic_fallback(
    job: Any,
    profile: Profile,
    *,
    primary: Evaluation,
    job_id: int | None,
    profile_id: int | None,
    now: Any,
) -> Evaluation:
    """Recover an invalid scorer response with the deterministic heuristic.

    The fallback keeps the requested scorer as the pipeline identity (so the job
    counts as evaluated for that backend and is not re-run forever), but names the
    heuristic explicitly in ``model`` and in the rationale, and preserves the
    original failure in ``error``. It never invents facts: it only re-scores the
    same stored posting with the offline scorer.
    """
    fallback = score_job_heuristic(
        job, profile, now=now, job_id=job_id, profile_id=profile_id
    )
    reason = primary.error or f"scorer '{primary.scorer}' produced an invalid evaluation"
    fallback.scorer = primary.scorer
    fallback.model = f"{fallback.model}+fallback"
    fallback.raw_document_id = primary.raw_document_id
    fallback.error = reason
    fallback.rationale = (
        f"{fallback.rationale} [fallback: deterministic heuristic used because "
        f"{primary.scorer} could not produce a valid evaluation: {reason}]"
    ).strip()
    return fallback


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
    exact profile version that produced them. A contract-invalid response is
    retried a bounded number of times and then recovered with the offline
    heuristic; a transport/access failure is reported as a technical failure. The
    persisted row therefore never carries an ``ok`` status without a numeric score
    and a decided recommendation.
    """
    snapshot = profile_snapshot or build_snapshot(profile)
    stored_snapshot = upsert_profile(conn, snapshot)

    active_scorer = scorer or get_scorer(scorer_name, settings)
    normalized_name = (scorer_name or "").strip().lower()

    def _score() -> Evaluation:
        return active_scorer.score(
            job,
            profile,
            job_id=getattr(job, "id", None),
            profile_id=stored_snapshot.id,
            conn=conn,
            now=now,
        )

    evaluation = _score()
    attempts = 1
    while (
        _is_contract_repairable(evaluation)
        and normalized_name != ScorerName.HEURISTIC.value
        and attempts < MAX_CONTRACT_ATTEMPTS
    ):
        evaluation = _score()
        attempts += 1

    if (
        _is_contract_repairable(evaluation)
        and normalized_name != ScorerName.HEURISTIC.value
    ):
        evaluation = _heuristic_fallback(
            job,
            profile,
            primary=evaluation,
            job_id=getattr(job, "id", None),
            profile_id=stored_snapshot.id,
            now=now,
        )
        # The fallback keeps the requested scorer identity so the offer counts as
        # evaluated for that backend. Recompute the fingerprint from that same
        # identity, otherwise input-aware selection would never match the
        # heuristic-derived fingerprint and would re-run the offer forever.
        evaluation.input_fingerprint = effective_fingerprint(
            scorer_name, job, profile, settings
        )

    # Defense in depth: the real scorers already reject an unsupported currency
    # before calling a provider, but a scorer that returned a positive verdict
    # anyway must not be able to publish it. An unsupported currency is an
    # absolute policy rejection, never overturned by model text.
    blocker = currency_blocker(getattr(job, "salary", None))
    if (
        blocker is not None
        and str(evaluation.status) == EvaluationStatus.OK.value
        and str(evaluation.recommendation) != Recommendation.SKIP.value
    ):
        evaluation.recommendation = Recommendation.SKIP
        if blocker not in evaluation.blockers:
            evaluation.blockers = list(evaluation.blockers) + [blocker]
        evaluation.rationale = (
            evaluation.rationale.rstrip()
            + " [currency policy: unsupported salary currency forces skip]"
        ).strip()

    evaluation = _ensure_contract(evaluation)
    if evaluation.profile_id is None:
        evaluation.profile_id = stored_snapshot.id
    return insert_evaluation(conn, evaluation)


__all__ = [
    "HEURISTIC_NAME",
    "HeuristicScorer",
    "MAX_CONTRACT_ATTEMPTS",
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
    "effective_fingerprint",
    "evaluate_job",
    "fingerprint_resolver",
    "get_scorer",
    "load_profile",
    "parse_llm_evaluation",
    "score_job_heuristic",
]
