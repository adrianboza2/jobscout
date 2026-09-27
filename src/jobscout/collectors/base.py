"""Collector base utilities: HTTP transport, retry policy, fail-closed errors.

The transport is built on ``urllib.request`` to avoid a third-party dependency.
Retry behaviour is delegated to :mod:`jobscout.collectors.ratelimit`, which is
pure and therefore testable without any network access.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from ..config import is_safe_to_store
from ..models import JobPosting, RawDocument, RawDocumentKind, SearchRunStatus
from ..normalize import html_to_text
from ..timeutil import utcnow_iso
from ..url_policy import is_http_scheme, redirect_target_violation, response_url_violation
from .ratelimit import (
    BackoffPolicy,
    FailureClass,
    RequestPacer,
    RetryDecision,
    classify_status,
    decide_retry,
)


#: Set by :class:`UrllibHttpClient` when a bounded read hit its ceiling.
TRUNCATION_HEADER = "x-jobscout-truncated"


class CollectorError(Exception):
    """Base class for collector failures.

    ``response`` carries the offending :class:`HttpResponse` when one exists, so
    callers can still persist provenance about a refusal before failing closed.
    """

    response: "HttpResponse | None" = None


class CollectorDisabledError(CollectorError):
    """Raised when a network collector is invoked without explicit opt-in."""


class AccessDeniedError(CollectorError):
    """Raised on authentication/authorization/anti-bot responses. Fails closed."""


class RateLimitedError(CollectorError):
    """Raised when the remote endpoint answers with a rate-limit status."""


class TransientFailureError(CollectorError):
    """Raised when retries for a transient failure are exhausted."""


class NotFoundError(CollectorError):
    """Raised for a definitive 404/410."""


class TransportError(CollectorError):
    """Raised by transports for connection-level failures (timeouts, DNS, TLS)."""

    #: Whether :func:`fetch_with_policy` may retry this failure.
    retryable = True


class UnsafeUrlError(TransportError):
    """Raised for a non-HTTP scheme or a refused cross-host redirect.

    This is a transport-level failure, but it represents an *unsafe* target
    rather than a flaky connection, so it is never retried: probing again would
    only repeat the unsafe request.
    """

    retryable = False


@dataclass
class HttpResponse:
    """A single HTTP response, whether successful or not."""

    url: str
    status_code: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    fetched_at: str = ""
    elapsed_ms: int = 0
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = utcnow_iso()

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def classification(self) -> FailureClass:
        return classify_status(self.status_code)


@dataclass
class AttemptLog:
    """One request attempt, kept for provenance and tests."""

    attempt: int
    status_code: int | None
    classification: FailureClass
    delay_seconds: float
    retried: bool
    reason: str


class HttpClient(Protocol):
    """Minimal transport protocol so tests can inject a fake client."""

    def fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:  # pragma: no cover - protocol definition
        ...


@dataclass
class CollectorResult:
    """Outcome of one collection attempt (network, dry run, or refusal).

    Shared by every public provider adapter so the ingestion pipeline, run
    accounting, and fail-closed statuses behave identically regardless of the
    source. ``postings`` are already-normalized :class:`JobPosting` objects with
    the adapter's provider set on ``source``.
    """

    postings: list[JobPosting] = field(default_factory=list)
    raw_documents: list[RawDocument] = field(default_factory=list)
    pages_fetched: int = 0
    items_found: int = 0
    status: SearchRunStatus = SearchRunStatus.COMPLETED
    planned_urls: list[str] = field(default_factory=list)
    notes: str | None = None
    error: str | None = None


def build_redirect_handler():
    """Return the controlled redirect handler used by the urllib transport.

    ``urllib`` follows redirects to any host by default, which would let a
    compromised or misconfigured endpoint bounce the collector to an unrelated
    host or downgrade it to plaintext HTTP. Every hop is vetted by
    :func:`jobscout.url_policy.redirect_target_violation`: HTTPS only, no
    embedded credentials, default HTTPS port, and a host allowed relative to the
    source. A violation raises :class:`UnsafeUrlError`, which is never retried.
    """
    import urllib.request

    class _Handler(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req, fp, code, msg, headers, newurl
        ):  # noqa: ANN001 - urllib signature
            reason = redirect_target_violation(req.full_url, newurl)
            if reason:
                raise UnsafeUrlError(f"refusing unsafe redirect to {newurl!r}: {reason}")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return _Handler()


def fetch_robots(
    transport: HttpClient,
    robots_url: str,
    *,
    policy: BackoffPolicy,
    pacer: RequestPacer | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[bool, str, str]:
    """Fetch ``robots.txt`` and return ``(ok, text, note)``.

    A definitive 404 means the site publishes no policy, which is treated as
    "no restrictions". Every other failure returns ``ok=False`` so callers fail
    closed instead of assuming permission. Shared by every collector that gates
    a live run on ``robots.txt``.
    """
    try:
        response = fetch_with_policy(
            transport, robots_url, policy=policy, pacer=pacer, sleep=sleep
        )
    except NotFoundError:
        return True, "", "robots.txt not found; treating as allow-all"
    except (AccessDeniedError, RateLimitedError) as exc:
        return False, "", f"robots.txt refused access: {exc}"
    except CollectorError as exc:
        return False, "", f"robots.txt could not be read: {exc}"
    return True, response.body or "", "robots.txt read"


def record_detail_response(
    result: Any,
    response: "HttpResponse | None",
    url: str,
    *,
    source: str,
    max_bytes: int,
    item: Any,
    store_body: bool = True,
) -> None:
    """Attach a size-bounded ``job_detail`` raw document and link it to ``item``.

    The single implementation shared by every detail adapter. It hashes exactly
    the body it persists, keeps the response status/content-type/fetched-at, and
    stamps ``stored_at`` now. ``store_body=False`` records provenance metadata
    only, so an already-oversized body is never persisted. ``item`` must expose a
    mutable ``raw_document_index``; it is set to the index of the appended
    document so the ingest pipeline can resolve the exact raw row.
    """
    if response is None:
        return
    body = response.body or ""
    encoded = body.encode("utf-8", errors="replace")
    if not store_body:
        safe, reason = False, "response exceeded the detail download limit"
    elif len(encoded) > max_bytes:
        # Name the size ceiling explicitly so the provenance note keeps the
        # established "download limit" phrasing instead of only the generic
        # ``is_safe_to_store`` size reason.
        safe, reason = False, f"payload exceeds the {max_bytes}-byte download limit"
    else:
        safe, reason = is_safe_to_store(body, max_bytes=max_bytes)
    document = RawDocument(
        kind=RawDocumentKind.JOB_DETAIL,
        source=source,
        url=url,
        status_code=response.status_code,
        content_type=response.headers.get("content-type"),
        sha256=hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest(),
        byte_size=len(body.encode("utf-8", errors="replace")),
        fetched_at=response.fetched_at,
        stored_at=utcnow_iso(),
        truncated=not safe,
        notes=None if safe else f"body not stored: {reason}",
        body=body if safe else None,
    )
    item.raw_document_index = len(result.raw_documents)
    result.raw_documents.append(document)


class UrllibHttpClient:
    """``urllib``-based transport.

    No cookies are stored, redirects are followed only within the same host (or
    LinkedIn parent domain), and the user agent is honest (see
    :class:`jobscout.config.Settings.user_agent`).
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        user_agent: str,
        accept: str = "*/*",
        max_download_bytes: int | None = None,
        opener: object | None = None,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.accept = accept
        # Optional download ceiling. ``None`` keeps the previous unbounded read
        # for callers that already bound what they persist. The ceiling applies
        # to error bodies as well as successful ones.
        self.max_download_bytes = max_download_bytes
        # Test seam: an object exposing ``open(request, timeout=...)``. When not
        # provided, a controlled opener is built per call.
        self._opener = opener

    def _build_opener(self) -> object:
        if self._opener is not None:
            return self._opener
        import urllib.request

        return urllib.request.build_opener(build_redirect_handler())

    def _bounded_read(self, stream: object) -> tuple[bytes, bool]:
        """Read at most ``max_download_bytes`` bytes, reporting truncation.

        One extra byte is requested so an oversized body can be detected without
        buffering it entirely.
        """
        limit = self.max_download_bytes
        if limit is None:
            return stream.read(), False  # type: ignore[attr-defined]
        raw = stream.read(int(limit) + 1)  # type: ignore[attr-defined]
        if len(raw) > limit:
            return raw[: int(limit)], True
        return raw, False

    def fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        import time
        import urllib.error
        import urllib.request

        # Fail before any I/O on a non-HTTP scheme. urllib happily opens
        # ``file://`` and returns the file's bytes with a ``None`` status, which
        # would otherwise be read as an (unbounded) transport response.
        if not is_http_scheme(url):
            scheme = urlsplit(str(url)).scheme or "none"
            raise UnsafeUrlError(
                f"refusing to fetch non-HTTP URL scheme {scheme!r}: {url!r}"
            )

        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": self.accept,
            # Ask for identity encoding so no decompression dependency is needed.
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if headers:
            request_headers.update({str(k): str(v) for k, v in headers.items()})
        request = urllib.request.Request(
            url, data=body, headers=request_headers, method=method.upper()
        )
        opener = self._build_opener()
        started = time.monotonic()
        try:
            with opener.open(request, timeout=self.timeout) as response:  # type: ignore[attr-defined]
                final_url = response.geturl()
                # Validate the URL the response actually came from, not just the
                # redirect-handler hops: an injected opener can return any URL,
                # and a handler is not guaranteed to be in the chain. The policy
                # is the same one a hop gets — HTTPS only, no credentials, a host
                # allowed relative to the request, and the request's own HTTPS
                # port (default for every provider endpoint).
                reason = response_url_violation(url, final_url)
                if reason:
                    raise UnsafeUrlError(
                        f"refusing unsafe response URL {final_url!r}: {reason}"
                    )
                raw, truncated = self._bounded_read(response)
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                if truncated:
                    response_headers[TRUNCATION_HEADER] = "1"
                status = getattr(response, "status", None)
                if status is None:
                    # A response object without an HTTP status is not HTTP.
                    raise UnsafeUrlError(
                        f"refusing response without an HTTP status from {final_url!r}"
                    )
                return HttpResponse(
                    url=final_url,
                    status_code=int(status),
                    body=raw.decode("utf-8", errors="replace"),
                    headers=response_headers,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
        except urllib.error.HTTPError as exc:
            # Error bodies are read through the same ceiling as successful
            # bodies so a large error page cannot be buffered unbounded.
            raw, truncated = self._bounded_read(exc)
            error_headers = dict(exc.headers or {})
            normalized = {k.lower(): v for k, v in error_headers.items()}
            if truncated:
                normalized[TRUNCATION_HEADER] = "1"
            return HttpResponse(
                url=url,
                status_code=int(exc.code),
                body=raw.decode("utf-8", errors="replace"),
                headers=normalized,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"HTTP {exc.code}",
            )
        except urllib.error.URLError as exc:
            raise TransportError(f"connection failure: {exc.reason}") from exc
        except TimeoutError as exc:  # pragma: no cover - platform dependent
            raise TransportError("connection timed out") from exc


def _error_for_classification(response: HttpResponse) -> CollectorError:
    classification = response.classification
    if classification is FailureClass.ACCESS_DENIED:
        error: CollectorError = AccessDeniedError(
            f"access denied (HTTP {response.status_code}) for {response.url}; failing closed"
        )
    elif classification is FailureClass.RATE_LIMITED:
        error = RateLimitedError(
            f"rate limited (HTTP {response.status_code}) for {response.url}; failing closed"
        )
    elif classification is FailureClass.NOT_FOUND:
        error = NotFoundError(f"not found (HTTP {response.status_code}) for {response.url}")
    elif classification is FailureClass.TRANSIENT:
        error = TransientFailureError(
            f"transient failure (HTTP {response.status_code}) for {response.url}"
        )
    else:
        error = AccessDeniedError(
            f"unexpected status HTTP {response.status_code} for {response.url}; failing closed"
        )
    error.response = response
    return error


def fetch_with_policy(
    client: HttpClient,
    url: str,
    *,
    policy: BackoffPolicy,
    pacer: RequestPacer | None = None,
    sleep: Callable[[float], None] | None = None,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    on_attempt: Callable[[AttemptLog], None] | None = None,
    max_attempts: int = 10,
) -> HttpResponse:
    """Perform one request with bounded retries for *transient* failures only.

    Access errors (401/403/429/999/unexpected) and 404s are never retried: the
    collector fails closed instead of probing an endpoint that said no.
    """
    import time as _time

    sleeper = sleep or _time.sleep
    attempts: list[AttemptLog] = []
    attempt = 1
    while True:
        response: HttpResponse | None = None
        failure: Exception | None = None
        try:
            response = client.fetch_once(url, method=method, headers=headers, body=body)
        except TransportError as exc:
            if not getattr(exc, "retryable", True):
                # An unsafe target (non-HTTP scheme, refused cross-host
                # redirect) is not a transient glitch: fail closed immediately
                # instead of repeating the same unsafe request.
                raise
            failure = exc
        except CollectorError:
            raise
        except Exception as exc:  # pragma: no cover - defensive transport guard
            failure = TransportError(str(exc))

        status_code = response.status_code if response is not None else None
        decision: RetryDecision = decide_retry(
            policy, attempt, status_code=status_code, failure=failure
        )
        log = AttemptLog(
            attempt=attempt,
            status_code=status_code,
            classification=decision.classification,
            delay_seconds=decision.delay_seconds,
            retried=decision.should_retry,
            reason=decision.reason,
        )
        attempts.append(log)
        if on_attempt is not None:
            on_attempt(log)

        if not decision.should_retry:
            if response is not None:
                if response.ok:
                    return response
                raise _error_for_classification(response)
            raise TransientFailureError(
                f"request failed after {attempt} attempt(s): {decision.reason}"
            )

        if pacer is not None:
            pacer.wait()
        if decision.delay_seconds > 0:
            sleeper(decision.delay_seconds)
        attempt += 1
        if attempt > max_attempts:  # pragma: no cover - guarded by policy.max_retries
            raise TransientFailureError(f"exceeded {max_attempts} attempts for {url}")


__all__ = [
    "TRUNCATION_HEADER",
    "AccessDeniedError",
    "AttemptLog",
    "CollectorDisabledError",
    "CollectorError",
    "CollectorResult",
    "HttpClient",
    "HttpResponse",
    "NotFoundError",
    "RateLimitedError",
    "TransientFailureError",
    "TransportError",
    "UnsafeUrlError",
    "UrllibHttpClient",
    "build_redirect_handler",
    "fetch_robots",
    "fetch_with_policy",
    "html_to_text",
    "record_detail_response",
]
