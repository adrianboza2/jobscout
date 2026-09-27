"""Currency policy is enforced before any provider call on every scorer route.

Offline only: the fake HTTP client and the fake OpenCode runner raise if invoked,
so these assertions prove the provider was never called for an offer quoted in an
unsupported currency. No test touches the network.
"""

from __future__ import annotations

from dataclasses import replace

from conftest import FIXED_NOW, FakeHttpClient, FakeRunner, no_network

from jobscout.db import store_posting
from jobscout.models import (
    Evaluation,
    EvaluationStatus,
    JobPosting,
    Recommendation,
    ScorerName,
)
from jobscout.normalize import finalize_posting
from jobscout.scoring import evaluate_job
from jobscout.scoring.currency import (
    currency_blocker,
    salary_currencies,
    unsupported_currency_codes,
)
from jobscout.scoring.heuristic import score_job
from jobscout.scoring.llm import OpenAICompatibleScorer
from jobscout.scoring.opencode import OpenCodeScorer


def _job(strong_posting, salary: str):
    return replace(
        strong_posting,
        salary=salary,
        source_job_id=f"currency-{abs(hash(salary))}",
    )


# ---------------------------------------------------------------------------
# Shared policy: $ / US$ / R$ and conservative ambiguity
# ---------------------------------------------------------------------------


def test_salary_currency_signals_for_dollar_prefixes() -> None:
    assert salary_currencies("$ 5000 per month") == ({"USD"}, set())
    assert salary_currencies("US$ 5000 per month") == ({"USD"}, set())
    assert salary_currencies("EUR 4000 per month") == ({"EUR"}, set())
    # R$ is BRL: the bare ``$`` no longer reads as USD behind a currency prefix.
    assert salary_currencies("R$ 5000 per month") == (set(), {"BRL"})
    assert unsupported_currency_codes("R$ 5000 per month") == ("BRL",)
    # Mixed supported/unsupported evidence stays conservative: never hard-reject.
    assert unsupported_currency_codes("R$ 5000 or US$ 1000") == ()
    assert currency_blocker("R$ 5000 or US$ 1000") is None
    # Unknown or missing evidence is not a rejection either.
    assert unsupported_currency_codes("5000 per month") == ()


def test_currency_blocker_phrase() -> None:
    blocker = currency_blocker("GBP 5000 per month")
    assert blocker is not None
    assert "unsupported currency" in blocker
    assert "USD and EUR" in blocker


# ---------------------------------------------------------------------------
# Route 1: heuristic
# ---------------------------------------------------------------------------


def test_heuristic_rejects_brl_r_dollar(profile, strong_posting) -> None:
    evaluation = score_job(
        _job(strong_posting, "R$ 5000 per month"), profile, now=FIXED_NOW
    )
    assert evaluation.recommendation is Recommendation.SKIP
    assert any("unsupported currency" in blocker for blocker in evaluation.blockers)


def test_heuristic_rejects_currency_only_text_without_a_number(
    profile, strong_posting
) -> None:
    # The currency is detected before the amount, so an explicit unsupported
    # currency hard-rejects even when no comparable number can be read.
    evaluation = score_job(_job(strong_posting, "GBP competitive"), profile, now=FIXED_NOW)
    assert evaluation.recommendation is Recommendation.SKIP
    assert any("unsupported currency" in blocker for blocker in evaluation.blockers)


def test_heuristic_keeps_usd_and_eur_cases() -> None:
    # ``$`` means USD; EUR is supported but never FX-converted into the USD floor.
    assert salary_currencies("$ 5000 per month") == ({"USD"}, set())
    assert unsupported_currency_codes("$ 5000 per month") == ()
    assert unsupported_currency_codes("EUR 5000 per month") == ()


def test_heuristic_eur_offer_is_neutral_not_rejected(profile, strong_posting) -> None:
    evaluation = score_job(
        _job(strong_posting, "EUR 5000 per month"), profile, now=FIXED_NOW
    )
    assert evaluation.blockers == []
    assert any("no FX" in unknown for unknown in evaluation.unknowns)


# ---------------------------------------------------------------------------
# Route 2: OpenAI-compatible HTTP scorer
# ---------------------------------------------------------------------------


def test_llm_rejects_unsupported_currency_without_a_provider_call(
    llm_settings, profile, strong_posting, monkeypatch
) -> None:
    no_network(monkeypatch)
    client = FakeHttpClient([])  # any request would raise AssertionError
    scorer = OpenAICompatibleScorer(llm_settings, client=client, sleep=lambda _: None)

    evaluation = scorer.score(_job(strong_posting, "R$ 5000 per month"), profile)

    assert client.call_count == 0
    assert evaluation.status is EvaluationStatus.OK
    assert evaluation.recommendation is Recommendation.SKIP
    assert evaluation.overall_score == 0.0
    assert any("unsupported currency" in blocker for blocker in evaluation.blockers)


# ---------------------------------------------------------------------------
# Route 3: OpenCode CLI scorer
# ---------------------------------------------------------------------------


def test_opencode_rejects_unsupported_currency_without_launching_the_cli(
    opencode_settings, profile, strong_posting, monkeypatch
) -> None:
    no_network(monkeypatch)
    runner = FakeRunner([])  # any invocation would raise AssertionError
    scorer = OpenCodeScorer(opencode_settings, runner=runner)

    evaluation = scorer.score(_job(strong_posting, "GBP 5000 per month"), profile)

    assert runner.calls == []
    assert evaluation.status is EvaluationStatus.OK
    assert evaluation.recommendation is Recommendation.SKIP
    assert any("unsupported currency" in blocker for blocker in evaluation.blockers)


# ---------------------------------------------------------------------------
# Pipeline guard: model text can never overturn the rejection
# ---------------------------------------------------------------------------


def test_evaluate_job_forces_skip_when_a_scorer_tries_to_apply(
    conn, settings, profile
) -> None:
    class _ApplyScorer:
        name = ScorerName.OPENCODE.value

        def score(self, job, profile, **kwargs):  # noqa: ANN001 - test double
            return Evaluation(
                job_id=kwargs.get("job_id") or 0,
                scorer=ScorerName.OPENCODE,
                overall_score=95.0,
                recommendation=Recommendation.APPLY,
                confidence=0.9,
                model="stub",
            )

    posting = finalize_posting(
        JobPosting(
            source="fixture",
            title="BRL Backend Engineer",
            company="Acme Brasil",
            location="Remote",
            url="https://example.test/brl-role",
            source_job_id="brl-role-1",
            description="Python and SQL.",
            salary="R$ 9000 por mes",
        )
    )
    job, _ = store_posting(conn, posting, now="2026-09-15T10:00:00Z")

    evaluation = evaluate_job(
        conn,
        job,
        profile,
        scorer_name=ScorerName.OPENCODE.value,
        settings=settings,
        scorer=_ApplyScorer(),
    )

    assert evaluation.recommendation is Recommendation.SKIP
    assert any("unsupported currency" in blocker for blocker in evaluation.blockers)
