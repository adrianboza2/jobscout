"""Offline calibration report: model recommendations vs. human decisions.

This module answers one question without changing anything: *when the model
scored an offer and the human later reviewed it, how often did they agree?* It is
an evidence report, never an auto-tuner. It does not modify evaluations,
feedback, prompts, weights, profiles, or scores, and it never touches the
network: reading is a local SQLite ``SELECT`` through a read-only connection.

Two vocabularies, one explicit bridge
-------------------------------------
The model recommends ``apply`` / ``consider`` / ``skip`` / ``need_more_info``;
the human decides ``apply`` / ``consider`` / ``reject``. The bridge is a single
explicit mapping (:data:`MODEL_TO_HUMAN`): ``apply`` maps to ``apply``,
``consider`` to ``consider`` and ``skip`` to ``reject``.

``need_more_info`` is *not* part of that mapping. It is an unresolved category:
it is counted and reported separately, never silently folded into ``skip`` and
never included in the three-way agreement rate. Folding it would invent a human
verdict the model never expressed.

What is compared
----------------
By default, for every stored human decision the report takes the latest *valid*
evaluation (``status = 'ok'``) of that job, regardless of scorer. Passing a
``scorer`` narrows that lookup to evaluations produced by that scorer, which is
the honest way to compare one backend at a time. A ``failed`` evaluation is
never used, so a retry that errored cannot masquerade as the model's answer.

A human decision with no valid evaluation is counted as ``without_evaluation``
and excluded from the agreement math; the model has not spoken yet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

#: Model recommendation -> equivalent human decision. ``skip`` means "reject
#: this offer" for the human, so those two are the same verdict in two
#: vocabularies. This is the *only* place the two vocabularies are bridged.
MODEL_TO_HUMAN: dict[str, str] = {
    "apply": "apply",
    "consider": "consider",
    "skip": "reject",
}

#: Three-way labels, in a fixed order so text and JSON stay stable.
MODEL_LABELS: tuple[str, ...] = ("apply", "consider", "skip")
HUMAN_LABELS: tuple[str, ...] = ("apply", "consider", "reject")

#: Fixed ten-point ``overall_score`` buckets used by the distribution report.
SCORE_BUCKETS: tuple[str, ...] = tuple(
    "90-100" if start >= 90 else f"{start}-{start + 9}"
    for start in range(0, 100, 10)
)

#: Tables the report reads. Their absence means the database is not a jobscout
#: corpus, which is a user-facing error rather than an empty report.
_REQUIRED_TABLES = ("jobs", "evaluations", "job_feedback")


class CalibrationError(RuntimeError):
    """Raised when the report cannot read a jobscout database."""


def equivalent_human_decision(recommendation: Any) -> str | None:
    """Return the human decision a model recommendation stands for.

    ``None`` means the recommendation is unresolved (for example
    ``need_more_info``) and must stay outside the three-way agreement.
    """
    if recommendation is None:
        return None
    return MODEL_TO_HUMAN.get(str(recommendation))


def score_bucket(score: float) -> str:
    """Bucket label for an ``overall_score`` (clamped to ``0-9`` / ``90-100``)."""
    if score < 0:
        return SCORE_BUCKETS[0]
    index = int(score // 10)
    if index >= len(SCORE_BUCKETS):
        index = len(SCORE_BUCKETS) - 1
    return SCORE_BUCKETS[index]


@dataclass
class CalibrationSample:
    """One human decision joined with the model's latest valid evaluation."""

    job_id: int
    human_decision: str
    feedback_updated_at: str | None = None
    evaluation_id: int | None = None
    scorer: str | None = None
    recommendation: str | None = None
    overall_score: float | None = None
    evaluated_at: str | None = None

    @property
    def equivalent(self) -> str | None:
        return equivalent_human_decision(self.recommendation)

    @property
    def resolution(self) -> str:
        """``comparable``, ``unresolved`` or ``without_evaluation``."""
        if self.evaluation_id is None:
            return "without_evaluation"
        return "comparable" if self.equivalent is not None else "unresolved"

    def to_dict(self) -> dict[str, Any]:
        equivalent = self.equivalent
        comparable = self.resolution == "comparable"
        return {
            "job_id": self.job_id,
            "scorer": self.scorer,
            "recommendation": self.recommendation,
            "human_decision": self.human_decision,
            "equivalent": equivalent,
            "agrees": (equivalent == self.human_decision) if comparable else None,
            "resolution": self.resolution,
            "overall_score": self.overall_score,
            "evaluation_id": self.evaluation_id,
            "evaluated_at": self.evaluated_at,
            "feedback_updated_at": self.feedback_updated_at,
        }


def _empty_distribution() -> dict[str, dict[str, Any]]:
    return {
        human: {
            "count": 0,
            "with_score": 0,
            "min": None,
            "max": None,
            "mean": None,
            "buckets": {label: 0 for label in SCORE_BUCKETS},
        }
        for human in HUMAN_LABELS
    }


@dataclass
class CalibrationReport:
    """The full model-vs-human comparison, ready for text or JSON output."""

    scorer: str | None = None
    feedback_total: int = 0
    matched: int = 0
    comparable: int = 0
    exact: int = 0
    unresolved_total: int = 0
    without_evaluation: int = 0
    confusion: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            model: {human: 0 for human in HUMAN_LABELS} for model in MODEL_LABELS
        }
    )
    unresolved: dict[str, int] = field(default_factory=dict)
    unknown_human_decisions: dict[str, int] = field(default_factory=dict)
    score_distribution: dict[str, dict[str, Any]] = field(
        default_factory=_empty_distribution
    )
    #: Samples backing the report; kept private so the public shape stays the
    #: aggregate evidence while ``to_dict`` can still explain each row.
    _samples: list[CalibrationSample] = field(default_factory=list, repr=False)
    _score_values: dict[str, list[float]] = field(default_factory=dict, repr=False)

    @property
    def agreement_rate(self) -> float | None:
        """Exact agreement over comparable samples, or ``None`` when there are none."""
        if self.comparable == 0:
            return None
        return self.exact / self.comparable

    @property
    def samples(self) -> list[CalibrationSample]:
        """Per-job evidence backing the aggregate numbers."""
        return list(self._samples)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer": self.scorer,
            "feedback_total": self.feedback_total,
            "matched": self.matched,
            "without_evaluation": self.without_evaluation,
            "comparable": self.comparable,
            "agreement": {
                "exact": self.exact,
                "comparable": self.comparable,
                "rate": self.agreement_rate,
            },
            "unresolved_total": self.unresolved_total,
            "unresolved": dict(sorted(self.unresolved.items())),
            "unknown_human_decisions": dict(sorted(self.unknown_human_decisions.items())),
            "model_labels": list(MODEL_LABELS),
            "human_labels": list(HUMAN_LABELS),
            "confusion": {
                model: dict(self.confusion[model]) for model in MODEL_LABELS
            },
            "score_distribution": {
                human: dict(self.score_distribution[human]) for human in HUMAN_LABELS
            },
            "samples": [sample.to_dict() for sample in self._samples],
        }

    def _distribution_line(self, human: str) -> str:
        distribution = self.score_distribution[human]
        if distribution["count"] == 0:
            return f"  {human:<8} n=0"
        if distribution["with_score"] == 0:
            return f"  {human:<8} n={distribution['count']} (no overall_score)"
        buckets = ", ".join(
            f"{label}:{count}"
            for label, count in distribution["buckets"].items()
            if count
        )
        return (
            f"  {human:<8} n={distribution['count']} min={distribution['min']:.1f} "
            f"max={distribution['max']:.1f} mean={distribution['mean']:.1f}"
            + (f"  ({buckets})" if buckets else "")
        )

    def to_text(self) -> str:
        label = self.scorer or "all scorers"
        lines = [
            f"calibration [{label}]: {self.feedback_total} feedback row(s), "
            f"{self.comparable} comparable, {self.unresolved_total} unresolved, "
            f"{self.without_evaluation} without a valid evaluation"
        ]
        if self.comparable:
            rate = (self.agreement_rate or 0.0) * 100.0
            lines.append(
                f"agreement: {self.exact}/{self.comparable} exact ({rate:.1f}%)"
            )
        else:
            lines.append("agreement: no comparable samples")

        lines.append("confusion (model -> human):")
        for model in MODEL_LABELS:
            row = self.confusion[model]
            columns = " ".join(f"{human}={row[human]}" for human in HUMAN_LABELS)
            lines.append(f"  {model:<8} -> {columns}")

        lines.append("unresolved (excluded from the three-way agreement):")
        if self.unresolved:
            for name, count in sorted(self.unresolved.items()):
                lines.append(f"  {name}={count}")
        else:
            lines.append("  (none)")

        if self.unknown_human_decisions:
            lines.append("unknown human decisions (excluded):")
            for name, count in sorted(self.unknown_human_decisions.items()):
                lines.append(f"  {name}={count}")

        lines.append("overall_score by human decision:")
        for human in HUMAN_LABELS:
            lines.append(self._distribution_line(human))
        return "\n".join(lines)


def _require_schema(conn: sqlite3.Connection) -> None:
    """Fail clearly when the database has no jobscout schema to read."""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise CalibrationError(
            f"the database could not be read (is it a valid SQLite file?): {exc}"
        ) from exc
    names = {str(row["name"]) for row in rows}
    missing = set(_REQUIRED_TABLES) - names
    if missing:
        raise CalibrationError(
            "the database is not a jobscout database "
            f"(missing: {', '.join(sorted(missing))}); run 'jobscout init-db' first"
        )


def _load_samples(
    conn: sqlite3.Connection, *, scorer: str | None
) -> list[CalibrationSample]:
    """Read every human decision with its latest valid evaluation (no writes)."""
    subquery = (
        "SELECT MAX(x.id) FROM evaluations x "
        "WHERE x.job_id = f.job_id AND x.status = 'ok'"
    )
    params: list[Any] = []
    if scorer is not None:
        subquery += " AND x.scorer = ?"
        params.append(scorer)
    rows = conn.execute(
        f"""
        SELECT f.job_id AS job_id,
               f.decision AS human_decision,
               f.decision_updated_at AS feedback_updated_at,
               e.id AS evaluation_id,
               e.scorer AS scorer,
               e.recommendation AS recommendation,
               e.overall_score AS overall_score,
               e.created_at AS evaluated_at
        FROM job_feedback f
        LEFT JOIN evaluations e ON e.id = ({subquery})
        ORDER BY f.job_id
        """,
        params,
    ).fetchall()
    return [
        CalibrationSample(
            job_id=int(row["job_id"]),
            human_decision=str(row["human_decision"]),
            feedback_updated_at=row["feedback_updated_at"],
            evaluation_id=(
                int(row["evaluation_id"]) if row["evaluation_id"] is not None else None
            ),
            scorer=row["scorer"],
            recommendation=row["recommendation"],
            overall_score=row["overall_score"],
            evaluated_at=row["evaluated_at"],
        )
        for row in rows
    ]


def build_report(
    conn: sqlite3.Connection, *, scorer: Any | None = None
) -> CalibrationReport:
    """Build the calibration report from a read-only connection.

    ``scorer`` narrows the latest-valid-evaluation lookup to one scorer; ``None``
    compares each job's latest valid evaluation regardless of scorer. The
    function only issues ``SELECT`` statements, so it is safe on a read-only
    connection and leaves the corpus untouched.
    """
    _require_schema(conn)
    scorer_value = None if scorer is None else str(scorer)
    report = CalibrationReport(scorer=scorer_value)

    for sample in _load_samples(conn, scorer=scorer_value):
        report._samples.append(sample)
        report.feedback_total += 1
        if sample.evaluation_id is None:
            report.without_evaluation += 1
            continue
        report.matched += 1

        human = sample.human_decision
        known_human = human in HUMAN_LABELS
        if not known_human:
            report.unknown_human_decisions[human] = (
                report.unknown_human_decisions.get(human, 0) + 1
            )

        equivalent = sample.equivalent
        if equivalent is None:
            name = sample.recommendation if sample.recommendation is not None else ""
            report.unresolved[name] = report.unresolved.get(name, 0) + 1
            report.unresolved_total += 1
        elif known_human:
            report.comparable += 1
            model = str(sample.recommendation)
            report.confusion[model][human] += 1
            if equivalent == human:
                report.exact += 1

        # The distribution groups every matched row by its human decision; an
        # unresolved recommendation still carries a score worth seeing.
        if known_human:
            distribution = report.score_distribution[human]
            distribution["count"] += 1
            if sample.overall_score is not None:
                score = float(sample.overall_score)
                distribution["with_score"] += 1
                distribution["buckets"][score_bucket(score)] += 1
                report._score_values.setdefault(human, []).append(score)

    for human, scores in report._score_values.items():
        distribution = report.score_distribution[human]
        distribution["min"] = min(scores)
        distribution["max"] = max(scores)
        distribution["mean"] = sum(scores) / len(scores)

    return report


__all__ = [
    "HUMAN_LABELS",
    "MODEL_LABELS",
    "MODEL_TO_HUMAN",
    "SCORE_BUCKETS",
    "CalibrationError",
    "CalibrationReport",
    "CalibrationSample",
    "build_report",
    "equivalent_human_decision",
    "score_bucket",
]
