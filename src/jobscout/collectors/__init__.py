"""Collectors: fixture-backed (offline) and public provider adapters (opt-in)."""

from .base import (
    AccessDeniedError,
    CollectorDisabledError,
    CollectorError,
    CollectorResult,
    NotFoundError,
    RateLimitedError,
    TransientFailureError,
    TransportError,
)
from .fixtures import FixturePayload, load_fixture, postings_from_fixture
from .getmanfred import (
    GetManfredSitemapConfig,
    search_getmanfred,
)
from .infojobs import BLOCKED_REASON as INFOJOBS_BLOCKED_REASON
from .infojobs import search_infojobs
from .ratelimit import (
    BackoffPolicy,
    FailureClass,
    RequestPacer,
    RetryDecision,
    classify_status,
    decide_retry,
)
from .robots import RobotsRules, check_robots
from .tecnoempleo import (
    TecnoempleoSearchConfig,
    search_tecnoempleo,
)
from .tecnoempleo_details import (
    TecnoempleoDetailConfig,
    fetch_tecnoempleo_details,
)

__all__ = [
    "AccessDeniedError",
    "BackoffPolicy",
    "CollectorDisabledError",
    "CollectorError",
    "CollectorResult",
    "FailureClass",
    "FixturePayload",
    "GetManfredSitemapConfig",
    "INFOJOBS_BLOCKED_REASON",
    "NotFoundError",
    "RateLimitedError",
    "RequestPacer",
    "RetryDecision",
    "RobotsRules",
    "TecnoempleoDetailConfig",
    "TecnoempleoSearchConfig",
    "TransientFailureError",
    "TransportError",
    "check_robots",
    "classify_status",
    "decide_retry",
    "fetch_tecnoempleo_details",
    "load_fixture",
    "postings_from_fixture",
    "search_getmanfred",
    "search_infojobs",
    "search_tecnoempleo",
]
