"""Domain models.

These dataclasses mirror the SQLite schema one-to-one so persistence stays
explicit and boring. They are plain data containers: no I/O happens here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class StrEnum(str, Enum):
    """String enum whose ``str()`` is its value (3.11-compatible)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class ContentStatus(StrEnum):
    """How complete the captured job content is."""

    OK = "ok"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    ERROR = "error"


class SearchRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DRY_RUN = "dry_run"
    COMPLETED = "completed"
    FAILED_CLOSED = "failed_closed"
    FAILED = "failed"


class EvaluationStatus(StrEnum):
    """How complete an evaluation is.

    ``needs_more_info`` is a deliberate, non-success state: the scorer ran and
    produced a score, but could not settle on a public recommendation. It exists
    so an unresolved result is stored honestly instead of being forced into
    ``ok`` (which requires a decided recommendation) or hidden behind a zero
    placeholder. It is never treated as a successful evaluation by
    ``latest_evaluations`` or ``jobs_needing_evaluation``.
    """

    OK = "ok"
    NEEDS_MORE_INFO = "needs_more_info"
    PARSE_ERROR = "parse_error"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"


class Recommendation(StrEnum):
    APPLY = "apply"
    CONSIDER = "consider"
    SKIP = "skip"
    NEED_MORE_INFO = "need_more_info"


#: Recommendations that may accompany a successful (``status=ok``) evaluation.
#: ``need_more_info`` is deliberately absent: it is an unresolved diagnostic, not
#: a public recommendation, so it must be stored with a non-ok status.
PUBLIC_RECOMMENDATIONS: tuple[str, ...] = (
    Recommendation.APPLY.value,
    Recommendation.CONSIDER.value,
    Recommendation.SKIP.value,
)


class FeedbackDecision(StrEnum):
    """The human's own decision about an offer.

    Deliberately a separate vocabulary from :class:`Recommendation`: the model
    recommendation is evidence, the human decision is the verdict, and the two
    are stored apart so they can be compared later.

    The active workflow is binary (:data:`ACTIVE_FEEDBACK_DECISIONS`).
    ``CONSIDER`` is kept as a historical value: rows created before the binary
    switch stay importable, visible and countable, but the dashboard never offers
    it for a new mark.
    """

    APPLY = "apply"
    CONSIDER = "consider"
    REJECT = "reject"


#: Human decisions the active dashboard can produce. Defined once here so the
#: browser UI, the importer, and the calibration report cannot drift apart.
ACTIVE_FEEDBACK_DECISIONS: tuple[str, ...] = (
    FeedbackDecision.APPLY.value,
    FeedbackDecision.REJECT.value,
)

#: Historical human decisions kept for compatibility. They are still accepted by
#: the importer and counted by calibration, but never offered for a new decision.
LEGACY_FEEDBACK_DECISIONS: tuple[str, ...] = (FeedbackDecision.CONSIDER.value,)


class ScorerName(StrEnum):
    HEURISTIC = "heuristic"
    LLM = "llm"
    OPENCODE = "opencode"


class DuplicateKind(StrEnum):
    NEW = "new"
    EXACT_DUPLICATE = "exact_duplicate"
    PROBABLE_DUPLICATE = "probable_duplicate"


class RawDocumentKind(StrEnum):
    SEARCH_RESULT = "search_result"
    JOB_DETAIL = "job_detail"
    FIXTURE = "fixture"
    LLM_RESPONSE = "llm_response"


class PositionKind(StrEnum):
    """How an observed position should be interpreted.

    ``search_rank`` is a real result position inside a keyword search and is
    comparable within that provider/keyword/cycle. ``none`` means the provider
    exposed no ordered search result at all (for example a sitemap inventory),
    so a stored rank must never be read as a quality or relevance signal.
    """

    NONE = "none"
    SEARCH_RANK = "search_rank"


class DetailFetchStatus(StrEnum):
    """Outcome of one job-detail fetch attempt."""

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


@dataclass
class JobPosting:
    """A normalized job as produced by a collector, before persistence."""

    source: str
    title: str
    company: str
    url: str | None = None
    location: str | None = None
    description: str | None = None
    source_job_id: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    salary: str | None = None
    language: str | None = None
    posted_at: str | None = None
    fetched_at: str | None = None
    content_status: ContentStatus = ContentStatus.OK
    raw_document_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    dedupe_key: str = ""
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["content_status"] = str(self.content_status)
        return payload


@dataclass
class Job:
    """A persisted, deduplicated job row."""

    id: int | None
    fingerprint: str
    dedupe_key: str
    source: str
    title: str
    company: str
    url: str | None = None
    location: str | None = None
    description: str | None = None
    source_job_id: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    salary: str | None = None
    language: str | None = None
    posted_at: str | None = None
    first_seen_at: str = ""
    last_seen_at: str = ""
    content_status: ContentStatus = ContentStatus.OK
    content_hash: str = ""
    sighting_count: int = 1
    is_active: bool = True
    raw_document_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_posting(cls, posting: JobPosting, *, now: str) -> "Job":
        return cls(
            id=None,
            fingerprint=posting.fingerprint,
            dedupe_key=posting.dedupe_key,
            source=posting.source,
            title=posting.title,
            company=posting.company,
            url=posting.url,
            location=posting.location,
            description=posting.description,
            source_job_id=posting.source_job_id,
            employment_type=posting.employment_type,
            seniority=posting.seniority,
            salary=posting.salary,
            language=posting.language,
            posted_at=posting.posted_at,
            first_seen_at=now,
            last_seen_at=now,
            content_status=posting.content_status,
            content_hash=posting.content_hash,
            sighting_count=1,
            is_active=True,
            raw_document_id=posting.raw_document_id,
            extra=dict(posting.extra),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["content_status"] = str(self.content_status)
        return payload


@dataclass
class RawDocument:
    """A verbatim fetch kept for provenance and reproducibility."""

    id: int | None = None
    kind: RawDocumentKind = RawDocumentKind.SEARCH_RESULT
    source: str | None = None
    url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    sha256: str = ""
    byte_size: int = 0
    fetched_at: str | None = None
    stored_at: str = ""
    truncated: bool = False
    run_id: int | None = None
    notes: str | None = None
    body: str | None = None

    def to_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = str(self.kind)
        if not include_body:
            payload.pop("body", None)
        return payload


@dataclass
class JobDetailFetch:
    """Provenance for one attempt to enrich a stored job with its detail page.

    One row is written per attempted job, including failures and skips, so the
    reason a description is missing is always auditable.
    """

    id: int | None = None
    job_id: int = 0
    run_id: int | None = None
    source: str | None = "linkedin"
    url: str | None = None
    #: URL actually served after any allowed redirect. Nullable because OL-5
    #: is the slice that captures it; every row predating that stays NULL.
    final_url: str | None = None
    status: DetailFetchStatus = DetailFetchStatus.FAILED
    http_status: int | None = None
    content_status: str | None = None
    description_chars: int = 0
    byte_size: int = 0
    sha256: str | None = None
    content_hash: str | None = None
    fetched_at: str | None = None
    stored_at: str = ""
    raw_document_id: int | None = None
    error: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = str(self.status)
        return payload


@dataclass
class SearchRun:
    """One collector invocation, dry-run or live."""

    id: int | None = None
    provider: str = "linkedin"
    label: str | None = None
    query: str | None = None
    location: str | None = None
    #: Optional grouping id for "comparable search cycles". Runs that share a
    #: cycle id across providers can be compared without collapsing different
    #: platforms or sweep dates into one silently-mixed series. Rows predating
    #: the column (and runs that never set it) stay ``None``.
    cycle_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    status: SearchRunStatus = SearchRunStatus.PENDING
    started_at: str = ""
    finished_at: str | None = None
    pages_fetched: int = 0
    items_found: int = 0
    new_jobs: int = 0
    duplicate_jobs: int = 0
    error: str | None = None
    raw_document_id: int | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = str(self.status)
        return payload


@dataclass
class SearchRunItem:
    """Link between a search run and the job it produced or re-found."""

    run_id: int
    job_id: int
    decision: DuplicateKind = DuplicateKind.NEW
    rank_in_run: int = 0
    #: How :attr:`rank_in_run` must be read. ``None`` is the historical value
    #: written before the column existed, so old rows are never reinterpreted.
    position_kind: PositionKind | None = None
    created_at: str = ""


@dataclass
class DimensionScore:
    """One explainable scoring dimension."""

    name: str
    score: float
    weight: float = 1.0
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "weight": self.weight,
            "rationale": self.rationale,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DimensionScore":
        return cls(
            name=str(data.get("name", "")),
            score=float(data.get("score", 0.0)),
            weight=float(data.get("weight", 1.0)),
            rationale=str(data.get("rationale", "") or ""),
            evidence=[str(item) for item in data.get("evidence", []) or []],
        )


@dataclass
class Evaluation:
    """A persisted, explainable evaluation of one job."""

    job_id: int
    scorer: ScorerName | str
    overall_score: float | None = None
    recommendation: Recommendation | str = Recommendation.NEED_MORE_INFO
    confidence: float = 0.0
    dimensions: list[DimensionScore] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    rationale: str = ""
    status: EvaluationStatus | str = EvaluationStatus.OK
    error: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    profile_id: int | None = None
    raw_document_id: int | None = None
    input_fingerprint: str = ""
    created_at: str = ""
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "scorer": str(self.scorer),
            "overall_score": self.overall_score,
            "recommendation": str(self.recommendation),
            "confidence": self.confidence,
            "dimensions": [dimension.to_dict() for dimension in self.dimensions],
            "blockers": list(self.blockers),
            "strengths": list(self.strengths),
            "gaps": list(self.gaps),
            "unknowns": list(self.unknowns),
            "rationale": self.rationale,
            "status": str(self.status),
            "error": self.error,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "profile_id": self.profile_id,
            "raw_document_id": self.raw_document_id,
            "input_fingerprint": self.input_fingerprint,
            "created_at": self.created_at,
        }


def is_public_recommendation(value: Any) -> bool:
    """Whether ``value`` is a recommendation a successful evaluation may carry."""
    return str(value) in PUBLIC_RECOMMENDATIONS


def evaluation_contract_errors(evaluation: "Evaluation") -> list[str]:
    """Canonical invariant for a persisted successful evaluation.

    This is the single source of truth that the schema adapters, the scoring
    pipeline, and the persistence layer all enforce: a ``status=ok`` evaluation
    must carry a real numeric ``overall_score`` and a decided public
    recommendation (apply/consider/skip). Zero is a legitimate score for a real
    incompatibility, never an "unknown" placeholder, and ``need_more_info`` is
    never a successful public recommendation.

    Non-ok evaluations are always allowed; the check only constrains success.
    """
    if str(evaluation.status) != EvaluationStatus.OK.value:
        return []
    errors: list[str] = []
    if evaluation.overall_score is None:
        errors.append("status=ok requires a numeric overall_score")
    if not is_public_recommendation(evaluation.recommendation):
        errors.append("status=ok requires an apply/consider/skip recommendation")
    return errors


@dataclass
class JobFeedback:
    """A human decision about one job, imported from the dashboard export.

    One row per job (the latest decision wins), stored next to — never instead
    of — the model's evaluation. ``decision_updated_at`` is the timestamp the
    dashboard recorded when the human last changed the entry, so an older
    export can never overwrite a newer stored decision.
    """

    job_id: int
    decision: FeedbackDecision | str
    reason: str | None = None
    decision_updated_at: str | None = None
    project_id: str = ""
    source: str | None = None
    imported_at: str = ""
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "decision": str(self.decision),
            "reason": self.reason,
            "decision_updated_at": self.decision_updated_at,
            "project_id": self.project_id,
            "source": self.source,
            "imported_at": self.imported_at,
        }


@dataclass
class ProfileEvidence:
    """One structured fact extracted from the local candidate profile."""

    kind: str
    label: str
    value: Any = None
    weight: float = 1.0
    source_text: str | None = None
    id: int | None = None
    profile_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile_id": self.profile_id,
            "kind": self.kind,
            "label": self.label,
            "value": self.value,
            "weight": self.weight,
            "source_text": self.source_text,
        }


@dataclass
class ProfileSnapshot:
    """A versioned snapshot of the local profile used for scoring."""

    name: str
    fingerprint: str
    raw: dict[str, Any] = field(default_factory=dict)
    evidence: list[ProfileEvidence] = field(default_factory=list)
    source_path: str | None = None
    loaded_at: str = ""
    id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "fingerprint": self.fingerprint,
            "source_path": self.source_path,
            "loaded_at": self.loaded_at,
            "raw": self.raw,
            "evidence": [item.to_dict() for item in self.evidence],
        }


__all__ = [
    "ContentStatus",
    "DetailFetchStatus",
    "DimensionScore",
    "DuplicateKind",
    "Evaluation",
    "EvaluationStatus",
    "FeedbackDecision",
    "Job",
    "JobDetailFetch",
    "JobFeedback",
    "JobPosting",
    "PositionKind",
    "PUBLIC_RECOMMENDATIONS",
    "ProfileEvidence",
    "ProfileSnapshot",
    "RawDocument",
    "RawDocumentKind",
    "Recommendation",
    "ScorerName",
    "evaluation_contract_errors",
    "is_public_recommendation",
    "SearchRun",
    "SearchRunItem",
    "SearchRunStatus",
    "StrEnum",
    "_dumps",
    "_loads",
]
