"""Transport safety tests: scheme policy, redirect control, and read bounds.

Everything here is offline. Redirect handling is exercised through the real
handler with synthetic requests, and the read-bound tests use an injected
``opener`` double, so no socket is ever opened.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from conftest import FakeHttpClient, FakeOpener, FakeRawResponse, no_network, response

from jobscout.collectors.base import (
    TRUNCATION_HEADER,
    TransportError,
    UnsafeUrlError,
    UrllibHttpClient,
    build_redirect_handler,
    fetch_with_policy,
)
from jobscout.collectors.ratelimit import BackoffPolicy


def make_client(**kwargs) -> UrllibHttpClient:
    return UrllibHttpClient(user_agent="jobscout-test", **kwargs)


# ---------------------------------------------------------------------------
# Non-HTTP requests fail closed before any I/O
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    (
        # The prior `file://` probe: urllib would return the local file's bytes.
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
        "data:text/plain,hello",
        "ftp://www.linkedin.com/x",
    ),
)
def test_non_http_requests_are_refused_without_io(url: str, monkeypatch) -> None:
    no_network(monkeypatch)
    with pytest.raises(UnsafeUrlError):
        make_client().fetch_once(url)


def test_unsafe_url_error_is_a_non_retryable_transport_error(monkeypatch) -> None:
    no_network(monkeypatch)
    # The fake transport raises exactly what the real one raises for a file URL.
    client = FakeHttpClient([UnsafeUrlError("refusing non-HTTP URL scheme 'file'")])
    with pytest.raises(UnsafeUrlError):
        fetch_with_policy(
            client,
            "file:///etc/passwd",
            policy=BackoffPolicy(max_retries=2, jitter_seconds=0.0),
            sleep=lambda _seconds: None,
        )
    # An unsafe target is never retried.
    assert client.call_count == 1


def test_unsafe_url_error_is_a_transport_error() -> None:
    assert issubclass(UnsafeUrlError, TransportError)
    assert UnsafeUrlError.retryable is False
    assert TransportError.retryable is True


# ---------------------------------------------------------------------------
# Redirect control
# ---------------------------------------------------------------------------


def _redirect(handler, source_url: str, target_url: str):
    request = urllib.request.Request(source_url)
    # urllib joins relative redirect targets before calling the handler, so an
    # absolute target is the realistic input.
    return handler.redirect_request(
        request, io.BytesIO(b""), 302, "Found", {}, target_url
    )


def test_same_host_redirect_is_followed() -> None:
    handler = build_redirect_handler()
    result = _redirect(
        handler,
        "https://www.linkedin.com/jobs",
        "https://www.linkedin.com/jobs/other",
    )
    assert result is not None
    assert result.full_url == "https://www.linkedin.com/jobs/other"


def test_linkedin_parent_domain_redirect_is_followed() -> None:
    # Real behaviour: the guest endpoint can answer on a locale subdomain.
    handler = build_redirect_handler()
    result = _redirect(
        handler,
        "https://www.linkedin.com/jobs-guest/jobs/api/x",
        "https://es.linkedin.com/jobs-guest/jobs/api/x",
    )
    assert result is not None
    assert result.full_url.startswith("https://es.linkedin.com/")


def test_cross_host_redirect_is_refused() -> None:
    handler = build_redirect_handler()
    with pytest.raises(UnsafeUrlError) as excinfo:
        _redirect(
            handler, "https://www.linkedin.com/jobs", "https://evil.test/steal"
        )
    assert "cross-host" in str(excinfo.value)


def test_lookalike_host_redirect_is_refused() -> None:
    handler = build_redirect_handler()
    with pytest.raises(UnsafeUrlError):
        _redirect(
            handler,
            "https://www.linkedin.com/jobs",
            "https://linkedin.com.evil.test/steal",
        )


def test_non_http_redirect_is_refused() -> None:
    handler = build_redirect_handler()
    with pytest.raises(UnsafeUrlError) as excinfo:
        _redirect(handler, "https://www.linkedin.com/jobs", "file:///etc/passwd")
    assert "non-HTTP" in str(excinfo.value)


def test_llm_redirect_is_same_host_only() -> None:
    handler = build_redirect_handler()
    allowed = _redirect(
        handler, "https://api.example.com/v1", "https://api.example.com/v2"
    )
    assert allowed is not None
    # A different host is refused even though both are unrelated to LinkedIn.
    with pytest.raises(UnsafeUrlError):
        _redirect(handler, "https://api.example.com/v1", "https://evil.test/v2")


# ---------------------------------------------------------------------------
# Bounded error-body reads
# ---------------------------------------------------------------------------


class _RaisingOpener:
    """Opener double that raises a pre-built exception."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls: list[object] = []

    def open(self, request, timeout=None):  # noqa: ANN001 - urllib shape
        self.calls.append(request)
        raise self.exc


def test_error_body_read_is_bounded() -> None:
    limit = 64
    body = b"x" * 5000
    error = urllib.error.HTTPError(
        "https://www.linkedin.com/x", 403, "denied", {}, io.BytesIO(body)
    )
    client = make_client(max_download_bytes=limit, opener=_RaisingOpener(error))
    result = client.fetch_once("https://www.linkedin.com/x")

    assert result.status_code == 403
    assert len(result.body.encode("utf-8")) <= limit
    assert result.headers.get(TRUNCATION_HEADER) == "1"


def test_small_error_body_is_not_marked_truncated() -> None:
    error = urllib.error.HTTPError(
        "https://www.linkedin.com/x", 404, "gone", {}, io.BytesIO(b"gone")
    )
    client = make_client(max_download_bytes=1024, opener=_RaisingOpener(error))
    result = client.fetch_once("https://www.linkedin.com/x")

    assert result.body == "gone"
    assert TRUNCATION_HEADER not in result.headers


def test_unbounded_client_still_reads_the_whole_error_body() -> None:
    # ``max_download_bytes=None`` preserves the previous behaviour for callers
    # that already bound what they persist.
    body = b"y" * 2000
    error = urllib.error.HTTPError(
        "https://www.linkedin.com/x", 500, "boom", {}, io.BytesIO(body)
    )
    client = make_client(opener=_RaisingOpener(error))
    result = client.fetch_once("https://www.linkedin.com/x")

    assert len(result.body) == 2000
    assert TRUNCATION_HEADER not in result.headers


# ---------------------------------------------------------------------------
# Bounded success-body reads
# ---------------------------------------------------------------------------


def test_success_body_read_is_bounded() -> None:
    limit = 64
    opener = FakeOpener(
        FakeRawResponse(b"x" * 5000, "https://www.linkedin.com/jobs")
    )
    client = make_client(max_download_bytes=limit, opener=opener)
    result = client.fetch_once("https://www.linkedin.com/jobs")

    assert result.status_code == 200
    assert len(result.body.encode("utf-8")) <= limit
    assert result.headers.get(TRUNCATION_HEADER) == "1"


def test_small_success_body_is_not_marked_truncated() -> None:
    opener = FakeOpener(FakeRawResponse(b"<html>ok</html>", "https://www.linkedin.com/jobs"))
    client = make_client(max_download_bytes=1024, opener=opener)
    result = client.fetch_once("https://www.linkedin.com/jobs")

    assert result.body == "<html>ok</html>"
    assert TRUNCATION_HEADER not in result.headers


# ---------------------------------------------------------------------------
# A refused redirect surfaces as a fail-closed collector error
# ---------------------------------------------------------------------------


def test_refused_redirect_is_not_retried_end_to_end(monkeypatch) -> None:
    """A cross-host redirect must fail closed on the first attempt."""
    no_network(monkeypatch)
    client = FakeHttpClient([UnsafeUrlError("refusing cross-host redirect")])
    with pytest.raises(UnsafeUrlError):
        fetch_with_policy(
            client,
            "https://www.linkedin.com/jobs",
            policy=BackoffPolicy(max_retries=5, jitter_seconds=0.0),
            sleep=lambda _seconds: None,
        )
    assert client.call_count == 1


def test_access_denied_is_still_not_retried() -> None:
    # Regression guard: the non-retryable branch must not change fail-closed
    # behaviour for ordinary refusals.
    client = FakeHttpClient([response(403, "denied")])
    from jobscout.collectors.base import AccessDeniedError

    with pytest.raises(AccessDeniedError):
        fetch_with_policy(
            client,
            "https://www.linkedin.com/x",
            policy=BackoffPolicy(max_retries=3, jitter_seconds=0.0),
            sleep=lambda _seconds: None,
        )
    assert client.call_count == 1
