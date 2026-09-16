"""Rate-limit policy and backoff decisions.

This module is intentionally free of I/O: no sockets, no real sleeping. The
caller injects ``rng``/``clock``/``sleep`` so every decision is reproducible in
tests.

Policy summary (conservative by default):

* Transient failures (timeouts, connection errors, 5xx, 408, 425) are retried
  with exponential backoff plus jitter, bounded by ``max_retries``.
* Access errors (401, 403, 429, any unexpected status) fail closed immediately.
* 404/410 fail closed immediately.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol


class FailureClass(str, Enum):
    """How a response or transport error should be treated."""

    OK = "ok"
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"
    NOT_FOUND = "not_found"
    UNEXPECTED = "unexpected"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# Statuses that signal "you are not welcome here"; never retried by default.
ACCESS_DENIED_STATUSES = frozenset({401, 403, 407, 451, 999})
RATE_LIMIT_STATUSES = frozenset({429})
NOT_FOUND_STATUSES = frozenset({404, 410})
TRANSIENT_STATUSES = frozenset({408, 425, 500, 502, 503, 504, 507, 509, 598, 599})


def classify_status(status_code: int | None) -> FailureClass:
    """Classify an HTTP status code into a handling bucket."""
    if status_code is None:
        return FailureClass.TRANSIENT
    if 200 <= status_code < 300:
        return FailureClass.OK
    if status_code in RATE_LIMIT_STATUSES:
        return FailureClass.RATE_LIMITED
    if status_code in NOT_FOUND_STATUSES:
        return FailureClass.NOT_FOUND
    if status_code in ACCESS_DENIED_STATUSES:
        return FailureClass.ACCESS_DENIED
    if status_code in TRANSIENT_STATUSES or 500 <= status_code < 600:
        return FailureClass.TRANSIENT
    if 400 <= status_code < 500:
        # Unknown 4xx: treat as a refusal, not as a temporary glitch.
        return FailureClass.ACCESS_DENIED
    return FailureClass.UNEXPECTED


@dataclass(frozen=True)
class BackoffPolicy:
    """Retry/backoff configuration."""

    base_delay_seconds: float = 2.0
    factor: float = 2.0
    max_delay_seconds: float = 30.0
    max_retries: int = 2
    jitter_seconds: float = 0.5
    retry_on_rate_limit: bool = False
    retry_on_access_denied: bool = False

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """Deterministic (unless jitter) delay before the retry that follows ``attempt``."""
        exponent = max(0, attempt - 1)
        base = min(self.base_delay_seconds * (self.factor**exponent), self.max_delay_seconds)
        jitter = 0.0
        if self.jitter_seconds > 0:
            generator = rng or random.Random()
            jitter = generator.uniform(0.0, self.jitter_seconds)
        return round(base + jitter, 3)


@dataclass(frozen=True)
class RetryDecision:
    """Outcome of the retry policy for one attempt."""

    should_retry: bool
    delay_seconds: float
    classification: FailureClass
    reason: str


def decide_retry(
    policy: BackoffPolicy,
    attempt: int,
    *,
    status_code: int | None = None,
    failure: BaseException | None = None,
    rng: random.Random | None = None,
) -> RetryDecision:
    """Decide whether another attempt is allowed and after how long.

    ``attempt`` is 1-based and refers to the attempt that just finished.
    """
    if failure is not None and status_code is None:
        classification = FailureClass.TRANSIENT
        reason = f"transport failure: {type(failure).__name__}: {failure}"
    else:
        classification = classify_status(status_code)
        reason = f"HTTP {status_code}" if status_code is not None else "no status"

    retryable = classification is FailureClass.TRANSIENT
    if classification is FailureClass.RATE_LIMITED and policy.retry_on_rate_limit:
        retryable = True
    if classification is FailureClass.ACCESS_DENIED and policy.retry_on_access_denied:
        retryable = True

    if not retryable:
        if classification is FailureClass.OK:
            return RetryDecision(False, 0.0, classification, f"{reason}: success")
        return RetryDecision(
            False,
            0.0,
            classification,
            f"{reason}: {classification.value} is not retryable; failing closed",
        )

    if attempt > policy.max_retries:
        return RetryDecision(
            False,
            0.0,
            classification,
            f"{reason}: retries exhausted after {attempt} attempt(s)",
        )

    return RetryDecision(
        True,
        policy.delay_for(attempt, rng=rng),
        classification,
        f"{reason}: retry {attempt}/{policy.max_retries}",
    )


class _Sleeper(Protocol):  # pragma: no cover - protocol definition
    def __call__(self, seconds: float) -> None: ...


class RequestPacer:
    """Enforces a minimum interval between outbound requests.

    ``clock`` and ``sleep`` are injectable so tests can assert pacing without
    waiting for real time.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        jitter_seconds: float = 0.0,
        rng: random.Random | None = None,
        clock: Callable[[], float] | None = None,
        sleep: _Sleeper | None = None,
    ) -> None:
        import time as _time

        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.jitter_seconds = max(0.0, float(jitter_seconds))
        self.rng = rng or random.Random()
        self._clock = clock or _time.monotonic
        self._sleep = sleep or _time.sleep
        self._last_request_at: float | None = None
        self.sleep_calls: list[float] = []

    def _target_interval(self) -> float:
        if self.jitter_seconds <= 0:
            return self.min_interval_seconds
        return self.min_interval_seconds + self.rng.uniform(0.0, self.jitter_seconds)

    def wait(self) -> float:
        """Block until the next request may be sent. Returns seconds slept."""
        now = self._clock()
        interval = self._target_interval()
        if self._last_request_at is None:
            self._last_request_at = now
            return 0.0
        elapsed = now - self._last_request_at
        remaining = interval - elapsed
        if remaining > 0:
            rounded = round(remaining, 3)
            self._sleep(rounded)
            self.sleep_calls.append(rounded)
            self._last_request_at = now + rounded
            return rounded
        self._last_request_at = now
        return 0.0
