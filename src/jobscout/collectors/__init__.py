"""Collectors: fixture-backed (offline) and LinkedIn guest search (opt-in)."""

from .base import (
    AccessDeniedError,
    CollectorDisabledError,
    CollectorError,
    NotFoundError,
    RateLimitedError,
    TransientFailureError,
    TransportError,
)
from .fixtures import FixturePayload, load_fixture, postings_from_fixture
from .ratelimit import (
    BackoffPolicy,
    FailureClass,
    RequestPacer,
    RetryDecision,
    classify_status,
    decide_retry,
)

__all__ = [
    "AccessDeniedError",
    "BackoffPolicy",
    "CollectorDisabledError",
    "CollectorError",
    "FailureClass",
    "FixturePayload",
    "NotFoundError",
    "RateLimitedError",
    "RequestPacer",
    "RetryDecision",
    "TransientFailureError",
    "TransportError",
    "classify_status",
    "decide_retry",
    "load_fixture",
    "postings_from_fixture",
]
