"""Rate-limit policy, backoff, and HTTP retry tests.

No real network calls and no real sleeping: the transport and the clock are both
injected doubles.
"""

from __future__ import annotations

import random

import pytest

from conftest import FakeHttpClient, response

from jobscout.collectors.base import (
    AccessDeniedError,
    NotFoundError,
    RateLimitedError,
    TransientFailureError,
    TransportError,
    fetch_with_policy,
)
from jobscout.collectors.ratelimit import (
    BackoffPolicy,
    FailureClass,
    RequestPacer,
    classify_status,
    decide_retry,
)


class FakeClock:
    def __init__(self) -> None:
        self.time = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.time += seconds


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, FailureClass.OK),
        (204, FailureClass.OK),
        (301, FailureClass.UNEXPECTED),
        (400, FailureClass.ACCESS_DENIED),
        (401, FailureClass.ACCESS_DENIED),
        (403, FailureClass.ACCESS_DENIED),
        (404, FailureClass.NOT_FOUND),
        (410, FailureClass.NOT_FOUND),
        (418, FailureClass.ACCESS_DENIED),
        (429, FailureClass.RATE_LIMITED),
        (500, FailureClass.TRANSIENT),
        (503, FailureClass.TRANSIENT),
        (999, FailureClass.ACCESS_DENIED),
        (None, FailureClass.TRANSIENT),
    ],
)
def test_classify_status(status, expected) -> None:
    assert classify_status(status) is expected


def test_success_is_not_retried() -> None:
    decision = decide_retry(BackoffPolicy(), 1, status_code=200)
    assert decision.should_retry is False
    assert decision.delay_seconds == 0.0
    assert decision.classification is FailureClass.OK


def test_transient_failure_retries_with_exponential_backoff() -> None:
    policy = BackoffPolicy(base_delay_seconds=2.0, factor=2.0, max_delay_seconds=30.0, max_retries=3, jitter_seconds=0.0)
    first = decide_retry(policy, 1, status_code=503)
    second = decide_retry(policy, 2, status_code=503)
    third = decide_retry(policy, 3, status_code=503)
    exhausted = decide_retry(policy, 4, status_code=503)
    assert (first.should_retry, first.delay_seconds) == (True, 2.0)
    assert (second.should_retry, second.delay_seconds) == (True, 4.0)
    assert (third.should_retry, third.delay_seconds) == (True, 8.0)
    assert exhausted.should_retry is False
    assert exhausted.delay_seconds == 0.0
    assert "exhausted" in exhausted.reason


def test_backoff_is_capped_by_max_delay() -> None:
    policy = BackoffPolicy(base_delay_seconds=10.0, factor=10.0, max_delay_seconds=12.0, max_retries=5, jitter_seconds=0.0)
    assert policy.delay_for(1) == 10.0
    assert policy.delay_for(2) == 12.0
    assert policy.delay_for(9) == 12.0


def test_jitter_is_bounded_and_seed_reproducible() -> None:
    policy = BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=1.0, max_retries=3, jitter_seconds=1.0)
    first = policy.delay_for(1, rng=random.Random(42))
    second = policy.delay_for(1, rng=random.Random(42))
    assert first == second
    assert 1.0 <= first <= 2.0


def test_transport_failure_is_treated_as_transient() -> None:
    decision = decide_retry(BackoffPolicy(), 1, failure=TransportError("connection reset"))
    assert decision.should_retry is True
    assert decision.classification is FailureClass.TRANSIENT
    assert "TransportError" in decision.reason


@pytest.mark.parametrize("status", [401, 403, 429, 999, 404, 410, 418, 301])
def test_access_and_not_found_errors_fail_closed(status: int) -> None:
    decision = decide_retry(BackoffPolicy(max_retries=5), 1, status_code=status)
    assert decision.should_retry is False
    assert decision.delay_seconds == 0.0
    assert "failing closed" in decision.reason


def test_rate_limit_can_be_retried_only_when_explicitly_allowed() -> None:
    default = decide_retry(BackoffPolicy(), 1, status_code=429)
    explicit = decide_retry(BackoffPolicy(retry_on_rate_limit=True), 1, status_code=429)
    assert default.should_retry is False
    assert explicit.should_retry is True


def test_pacer_enforces_minimum_interval_without_real_sleeping() -> None:
    clock = FakeClock()
    pacer = RequestPacer(5.0, clock=clock.clock, sleep=clock.sleep)
    assert pacer.wait() == 0.0  # first request is immediate
    assert pacer.wait() == 5.0
    assert pacer.wait() == 5.0
    assert clock.sleeps == [5.0, 5.0]


def test_pacer_does_not_sleep_when_enough_time_passed() -> None:
    clock = FakeClock()
    pacer = RequestPacer(5.0, clock=clock.clock, sleep=clock.sleep)
    pacer.wait()
    clock.time += 10.0
    assert pacer.wait() == 0.0
    assert clock.sleeps == []


def test_fetch_with_policy_returns_success_without_retry() -> None:
    client = FakeHttpClient([response(200, "ok")])
    result = fetch_with_policy(client, "https://example.test/a", policy=BackoffPolicy(), sleep=lambda _: None)
    assert result.status_code == 200
    assert client.call_count == 1


def test_fetch_with_policy_retries_transient_then_succeeds() -> None:
    client = FakeHttpClient([response(503, "busy"), response(503, "busy"), response(200, "ok")])
    sleeps: list[float] = []
    attempts: list[int] = []
    result = fetch_with_policy(
        client,
        "https://example.test/a",
        policy=BackoffPolicy(base_delay_seconds=1.0, factor=2.0, max_retries=2, jitter_seconds=0.0),
        sleep=sleeps.append,
        on_attempt=lambda log: attempts.append(log.attempt),
    )
    assert result.status_code == 200
    assert client.call_count == 3
    assert sleeps == [1.0, 2.0]
    assert attempts == [1, 2, 3]


def test_fetch_with_policy_fails_closed_on_access_denied() -> None:
    client = FakeHttpClient([response(403, "denied")])
    with pytest.raises(AccessDeniedError) as excinfo:
        fetch_with_policy(
            client,
            "https://example.test/a",
            policy=BackoffPolicy(max_retries=5),
            sleep=lambda _: None,
        )
    assert client.call_count == 1
    assert excinfo.value.response is not None
    assert excinfo.value.response.status_code == 403


def test_fetch_with_policy_fails_closed_on_rate_limit() -> None:
    client = FakeHttpClient([response(429, "slow down")])
    with pytest.raises(RateLimitedError):
        fetch_with_policy(client, "https://example.test/a", policy=BackoffPolicy(), sleep=lambda _: None)
    assert client.call_count == 1


def test_fetch_with_policy_maps_not_found() -> None:
    client = FakeHttpClient([response(404, "missing")])
    with pytest.raises(NotFoundError):
        fetch_with_policy(client, "https://example.test/a", policy=BackoffPolicy(), sleep=lambda _: None)


def test_fetch_with_policy_aborts_unexpected_status() -> None:
    client = FakeHttpClient([response(302, "")])
    with pytest.raises(AccessDeniedError):
        fetch_with_policy(client, "https://example.test/a", policy=BackoffPolicy(), sleep=lambda _: None)
    assert client.call_count == 1


def test_fetch_with_policy_raises_after_exhausting_retries() -> None:
    client = FakeHttpClient([response(500, "boom"), response(500, "boom")])
    with pytest.raises(TransientFailureError):
        fetch_with_policy(
            client,
            "https://example.test/a",
            policy=BackoffPolicy(max_retries=1, jitter_seconds=0.0),
            sleep=lambda _: None,
        )
    assert client.call_count == 2


def test_fetch_with_policy_retries_transport_errors() -> None:
    client = FakeHttpClient([TransportError("timed out"), response(200, "ok")])
    result = fetch_with_policy(
        client,
        "https://example.test/a",
        policy=BackoffPolicy(max_retries=1, jitter_seconds=0.0),
        sleep=lambda _: None,
    )
    assert result.status_code == 200
    assert client.call_count == 2


def test_fetch_with_policy_forwards_headers_and_body() -> None:
    client = FakeHttpClient([response(200, "ok")])
    fetch_with_policy(
        client,
        "https://example.test/a",
        policy=BackoffPolicy(),
        sleep=lambda _: None,
        method="POST",
        headers={"Authorization": "Bearer x"},
        body=b"{}",
    )
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["headers"]["Authorization"] == "Bearer x"
    assert call["body"] == b"{}"
