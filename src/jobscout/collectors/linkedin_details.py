"""Opt-in LinkedIn job-detail fetcher.

Search-result cards only carry a summary, so the search collector stores jobs
with ``content_status = partial``. This module enriches those stored jobs with
the description published on the public guest job-detail endpoint.

Safety model (enforced by construction, not by documentation alone):

* **Disabled by default.** :func:`fetch_linkedin_details` refuses to perform any
  request unless ``network_enabled=True``; the CLI only sets it when the operator
  passes ``--confirm-network`` or supplies an offline ``--fixture``.
* **No login automation, no cookies, no CAPTCHA handling, no anti-bot evasion.**
  Only the public guest endpoint is used, with an honest user agent.
* **Bounded.** Item count, request delay, jitter, timeout, and download size all
  have conservative caps (see :class:`jobscout.config.Settings`).
* **Fail closed.** 401/403/429/999 and unexpected statuses abort the run
  immediately; 404/410 is a definitive per-item failure; only transient failures
  are retried.
* **A partial/error result never replaces a successful full description.** That
  decision is applied when persisting, in
  :func:`jobscout.pipeline.ingest_detail_results`.

Operators are responsible for complying with the target site's terms of service
and local law.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Sequence

from ..config import (
    HARD_MAX_ITEMS,
    Settings,
    is_safe_to_store,
    validate_bounded_int,
)
from ..models import (
    ContentStatus,
    DetailFetchStatus,
    RawDocument,
    RawDocumentKind,
    SearchRunStatus,
)
from ..normalize import content_hash, extract_linkedin_job_id, html_to_text, normalize_text
from ..timeutil import utcnow_iso
from .base import (
    TRUNCATION_HEADER,
    AccessDeniedError,
    AttemptLog,
    CollectorDisabledError,
    CollectorError,
    HttpClient,
    HttpResponse,
    NotFoundError,
    RateLimitedError,
    UrllibHttpClient,
    fetch_with_policy,
)
from .htmlutil import VOID_TAGS, sanitize_redirect
from .ratelimit import BackoffPolicy, RequestPacer

#: Class markers that identify the description container in guest detail HTML.
DESCRIPTION_CLASS_MARKERS = (
    "show-more-less-html__markup",
    "description__text",
    "jobs-description__content",
    "job-details-module",
)

#: Descriptions below this length are treated as "not actually a description".
MIN_DESCRIPTION_CHARS = 10

#: HTTP statuses that prove a posting is definitively gone rather than
#: temporarily unavailable. Only these may trigger a lifecycle transition, and
#: they are kept machine-readable on :attr:`DetailItem.http_status`.
DEFINITIVE_MISSING_STATUSES = frozenset({404, 410})

#: A LinkedIn numeric job id: at least 4 ASCII digits and nothing else. This is
#: the same lower bound used by :func:`jobscout.normalize.extract_linkedin_job_id`,
#: kept ASCII-only so a non-ASCII "digit" is never mistaken for a real id.
_NUMERIC_SOURCE_JOB_ID_RE = re.compile(r"[0-9]{4,}")


@dataclass
class DetailFetchConfig:
    """Bounded parameters for one detail-fetch run."""

    max_items: int = 20

    def __post_init__(self) -> None:
        validate_bounded_int(self.max_items, name="max_items", hard_max=HARD_MAX_ITEMS)


@dataclass
class DetailItem:
    """Outcome of one detail fetch attempt (success, partial, failure, skip)."""

    job_id: int
    source_job_id: str | None = None
    url: str | None = None
    #: URL actually served after an allowed redirect, or ``None`` when the
    #: response was not a redirect (or its target was rejected as unsafe). It is
    #: captured from ``response.url`` and never triggers a second request.
    final_url: str | None = None
    status: DetailFetchStatus = DetailFetchStatus.FAILED
    description: str | None = None
    content_status: ContentStatus = ContentStatus.PARTIAL
    content_hash: str = ""
    fetched_at: str | None = None
    http_status: int | None = None
    byte_size: int = 0
    sha256: str | None = None
    raw_document_id: int | None = None
    error: str | None = None
    notes: str | None = None
    #: Index into :attr:`DetailResult.raw_documents`; resolved to an id on persist.
    raw_document_index: int | None = None

    @property
    def is_definitive_missing(self) -> bool:
        """True when a FAILED fetch proved the posting is gone (HTTP 404/410).

        Non-definitive failures (transient, access denied, oversized body) and
        non-failed statuses never report this, so the pipeline cannot mistake a
        temporary glitch for a lifecycle transition.
        """
        return (
            self.status is DetailFetchStatus.FAILED
            and self.http_status in DEFINITIVE_MISSING_STATUSES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_job_id": self.source_job_id,
            "url": self.url,
            "status": str(self.status),
            "content_status": str(self.content_status),
            "description_chars": len(self.description or ""),
            "fetched_at": self.fetched_at,
            "http_status": self.http_status,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "error": self.error,
            "notes": self.notes,
        }


@dataclass
class DetailResult:
    """Outcome of one detail-fetch attempt (network, dry run, or refusal)."""

    items: list[DetailItem] = field(default_factory=list)
    raw_documents: list[RawDocument] = field(default_factory=list)
    planned_urls: list[str] = field(default_factory=list)
    requested: int = 0
    requests_made: int = 0
    truncated: int = 0
    status: SearchRunStatus = SearchRunStatus.COMPLETED
    notes: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "requested": self.requested,
            "requests_made": self.requests_made,
            "truncated": self.truncated,
            "planned_urls": list(self.planned_urls),
            "items": [item.to_dict() for item in self.items],
            "notes": self.notes,
            "error": self.error,
        }


@dataclass
class DetailTarget:
    """A selected job plus the detail URL it resolves to (if any)."""

    job: Any
    source_job_id: str | None
    url: str | None
    reason: str | None = None


# ---------------------------------------------------------------------------
# URL planning (pure)
# ---------------------------------------------------------------------------


def resolve_source_job_id(job: Any) -> str | None:
    """Resolve a LinkedIn job id from a stored job's id or URL.

    Stored jobs normally carry an already-normalized digit-only
    ``source_job_id`` (e.g. ``4457430073``), while their URL uses LinkedIn's
    slug form ``/jobs/view/<title>-at-<company>-<id>`` that
    :func:`extract_linkedin_job_id` intentionally does not match. A numeric id
    is therefore accepted directly; any other value (URN, URL, or junk) goes
    through normal extraction before falling back to the job URL. A malformed
    id is never accepted as-is.

    The offer's ``source`` is checked first: a job whose provider is present and
    is not ``linkedin`` is never resolved, so a GetManfred or Tecnoempleo digit
    id cannot be read inside LinkedIn's numeric id space. Jobs with no ``source``
    attribute or ``source=None`` keep the historical behaviour (test stubs and
    callers that predate the provider column rely on it).
    """
    source = normalize_text(getattr(job, "source", None))
    if source and source.lower() != "linkedin":
        return None
    candidate = normalize_text(getattr(job, "source_job_id", None))
    if candidate and _NUMERIC_SOURCE_JOB_ID_RE.fullmatch(candidate):
        return candidate
    if candidate:
        resolved = extract_linkedin_job_id(candidate)
        if resolved:
            return resolved
    return extract_linkedin_job_id(getattr(job, "url", None) or "")


def build_detail_url(template: str, source_job_id: str) -> str:
    """Build a detail URL from the configured template or bare base URL."""
    if "{job_id}" in template:
        return template.replace("{job_id}", str(source_job_id))
    if "{id}" in template:
        return template.replace("{id}", str(source_job_id))
    return f"{template.rstrip('/')}/{source_job_id}"


def sanitize_final_url(requested_url: str | None, response_url: str | None) -> str | None:
    """Return the safe final URL of an allowed redirect, else ``None``.

    LinkedIn applies no extra host or scheme restriction here: the transport's
    redirect handler already enforced host policy, so only the shape checks
    shared with every detail fetcher run.
    """
    return sanitize_redirect(requested_url, response_url)


def plan_detail_targets(
    jobs: Sequence[Any], *, settings: Settings, max_items: int
) -> tuple[list[DetailTarget], int]:
    """Return ``(targets, truncated_count)`` for a bounded selection of jobs.

    Jobs without a resolvable LinkedIn id are kept as skipped targets so the
    caller can report them without sending a request.
    """
    items = list(jobs)
    selected = items[: max(0, int(max_items))]
    truncated = len(items) - len(selected)
    targets: list[DetailTarget] = []
    for job in selected:
        source_job_id = resolve_source_job_id(job)
        if not source_job_id:
            targets.append(
                DetailTarget(
                    job=job,
                    source_job_id=None,
                    url=None,
                    reason="no LinkedIn job id could be resolved from the stored job",
                )
            )
            continue
        targets.append(
            DetailTarget(
                job=job,
                source_job_id=source_job_id,
                url=build_detail_url(settings.linkedin_detail_url, source_job_id),
            )
        )
    return targets, truncated


# ---------------------------------------------------------------------------
# HTML parsing (pure, offline-testable)
# ---------------------------------------------------------------------------


def _is_description_container(tag: str, classes: str) -> bool:
    """Return True when ``classes`` carries a known description class.

    Matching is token-based rather than substring-based: a class such as
    ``not-a-description__text`` or ``description__textual`` must not be mistaken
    for ``description__text``. A marker also matches its BEM modifier form
    (``description__text description__text--rich``), which LinkedIn emits, but a
    bare ``marker--`` with an empty modifier name is not a real class and must
    not match.
    """
    if tag not in {"div", "section", "article"}:
        return False
    tokens = classes.lower().split()
    return any(
        token == marker
        or (token.startswith(f"{marker}--") and len(token) > len(marker) + 2)
        for token in tokens
        for marker in DESCRIPTION_CLASS_MARKERS
    )


class _DescriptionParser(HTMLParser):
    """Capture the inner HTML of known description containers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.captures: list[str] = []
        self._depth = 0
        self._parts: list[str] = []
        self._capturing = False

    def _finish(self) -> None:
        if self._capturing:
            self.captures.append("".join(self._parts))
        self._capturing = False
        self._depth = 0
        self._parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        if not self._capturing:
            if _is_description_container(tag, attributes.get("class", "")):
                self._capturing = True
                self._depth = 1
                self._parts = []
            return
        raw_tag = self.get_starttag_text() or f"<{tag}>"
        if tag in VOID_TAGS:
            # Void elements never emit an end tag, so they must not change depth.
            self._parts.append(raw_tag)
            return
        self._depth += 1
        self._parts.append(raw_tag)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if self._capturing:
            self._parts.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._capturing:
            return
        self._depth -= 1
        if self._depth <= 0:
            self._finish()
            return
        self._parts.append(f"</{tag}>")

    def finalize(self) -> None:
        """Close a container left open by truncated HTML."""
        if self._capturing:
            self._finish()


def parse_detail_description(html_text: str) -> str:
    """Extract the job description text from a detail fragment.

    Returns an empty string when no known description container is present, so
    an unexpected markup revision or a login wall is recorded as "no content"
    instead of being persisted as a bogus description. Never raises.
    """
    if not html_text:
        return ""
    parser = _DescriptionParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is resilient, guard anyway
        return ""
    parser.finalize()
    candidates = [html_to_text(chunk) for chunk in parser.captures]
    candidates = [
        text for text in candidates if len(text) >= MIN_DESCRIPTION_CHARS
    ]
    if not candidates:
        return ""
    return max(candidates, key=len)


# ---------------------------------------------------------------------------
# Network path
# ---------------------------------------------------------------------------


def _record_detail_response(
    result: DetailResult,
    response: HttpResponse | None,
    url: str,
    *,
    max_bytes: int,
    item: DetailItem,
    store_body: bool = True,
) -> None:
    """Attach a size-bounded raw document for a response.

    ``store_body=False`` records provenance metadata only, so a response that is
    already known to be oversized never has its body persisted.
    """
    if response is None:
        return
    body = response.body or ""
    if store_body:
        safe, reason = is_safe_to_store(body, max_bytes=max_bytes)
    else:
        safe, reason = False, "response exceeded the detail download limit"
    document = RawDocument(
        kind=RawDocumentKind.JOB_DETAIL,
        source="linkedin",
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


def fetch_linkedin_details(
    jobs: Sequence[Any],
    *,
    settings: Settings,
    network_enabled: bool = False,
    dry_run: bool = False,
    max_items: int | None = None,
    client: HttpClient | None = None,
    sleep: Callable[[float], None] | None = None,
    on_attempt: Callable[[AttemptLog], None] | None = None,
) -> DetailResult:
    """Fetch detail pages for a bounded selection of stored jobs.

    Without ``network_enabled=True`` this raises
    :class:`CollectorDisabledError`; with ``dry_run=True`` it returns the planned
    URLs and performs no I/O at all. ``client`` is injectable so tests and the
    offline ``--fixture`` path never touch the network.
    """
    config = DetailFetchConfig(
        max_items=max_items if max_items is not None else settings.detail_max_items
    )
    targets, truncated = plan_detail_targets(
        jobs, settings=settings, max_items=config.max_items
    )
    planned = [target.url for target in targets if target.url]
    result = DetailResult(
        planned_urls=planned, requested=len(targets), truncated=max(0, truncated)
    )

    if dry_run:
        result.status = SearchRunStatus.DRY_RUN
        result.notes = "dry run: no request was performed"
        return result

    if not network_enabled:
        raise CollectorDisabledError(
            "LinkedIn detail fetching is disabled; rerun with --confirm-network to allow "
            f"{len(planned)} bounded request(s), use --fixture for offline responses, "
            "or use --dry-run to inspect the plan"
        )

    import time as _time

    transport = client or UrllibHttpClient(
        timeout=settings.detail_timeout_seconds,
        user_agent=settings.user_agent,
        accept="text/html,application/xhtml+xml",
        max_download_bytes=settings.detail_max_bytes,
    )
    policy = BackoffPolicy(
        base_delay_seconds=max(1.0, settings.request_delay_seconds / 4),
        factor=2.0,
        max_delay_seconds=max(2.0, settings.request_delay_seconds),
        max_retries=max(0, settings.http_max_retries),
        jitter_seconds=min(1.0, settings.request_jitter_seconds),
    )
    pacer = RequestPacer(
        settings.request_delay_seconds,
        jitter_seconds=settings.request_jitter_seconds,
        sleep=sleep or _time.sleep,
    )

    for target in targets:
        job = target.job
        job_id = int(getattr(job, "id", 0) or 0)

        if target.url is None:
            result.items.append(
                DetailItem(
                    job_id=job_id,
                    source_job_id=None,
                    status=DetailFetchStatus.SKIPPED,
                    error=target.reason,
                )
            )
            continue

        # The pacer enforces the minimum delay between outbound requests; its
        # first call is immediate and only records the starting timestamp.
        pacer.wait()
        try:
            response = fetch_with_policy(
                transport,
                target.url,
                policy=policy,
                pacer=pacer,
                sleep=sleep,
                headers={"Accept-Language": "en-US,en;q=0.9"},
                on_attempt=on_attempt,
            )
        except NotFoundError as exc:
            # A definitive per-item answer: record it and keep going. The HTTP
            # status is kept machine-readable so the pipeline can tell a gone
            # posting (404/410) from a transient or access failure.
            not_found_response = exc.response
            item = DetailItem(
                job_id=job_id,
                source_job_id=target.source_job_id,
                url=target.url,
                status=DetailFetchStatus.FAILED,
                http_status=(
                    not_found_response.status_code
                    if not_found_response is not None
                    else None
                ),
                error=str(exc),
            )
            _record_detail_response(
                result,
                not_found_response,
                target.url,
                max_bytes=settings.raw_max_bytes,
                item=item,
            )
            result.items.append(item)
            result.requests_made += 1
            continue
        except (AccessDeniedError, RateLimitedError) as exc:
            item = DetailItem(
                job_id=job_id,
                source_job_id=target.source_job_id,
                url=target.url,
                status=DetailFetchStatus.FAILED,
                error=str(exc),
            )
            _record_detail_response(
                result, exc.response, target.url, max_bytes=settings.raw_max_bytes, item=item
            )
            result.items.append(item)
            result.requests_made += 1
            result.status = SearchRunStatus.FAILED_CLOSED
            result.error = str(exc)
            result.notes = "remote endpoint refused access; run aborted"
            return result
        except CollectorError as exc:
            item = DetailItem(
                job_id=job_id,
                source_job_id=target.source_job_id,
                url=target.url,
                status=DetailFetchStatus.FAILED,
                error=str(exc),
            )
            _record_detail_response(
                result, exc.response, target.url, max_bytes=settings.raw_max_bytes, item=item
            )
            result.items.append(item)
            result.requests_made += 1
            result.status = SearchRunStatus.FAILED
            result.error = str(exc)
            result.notes = "detail fetch aborted after a non-retryable failure"
            return result

        result.requests_made += 1
        body = response.body or ""
        item = DetailItem(
            job_id=job_id,
            source_job_id=target.source_job_id,
            url=target.url,
            # ``response.url`` is the final URL after the transport's redirect
            # handler already vetted the hop. Capturing it here performs no
            # additional request and does not treat a redirect as an expiry.
            final_url=sanitize_final_url(target.url, response.url),
            fetched_at=response.fetched_at,
            http_status=response.status_code,
            byte_size=len(body.encode("utf-8", errors="replace")),
        )

        # Enforce the download ceiling *before* any raw persistence or parsing:
        # an oversized body is recorded as a metadata-only failure and its
        # content is never parsed nor stored verbatim.
        if (
            response.headers.get(TRUNCATION_HEADER) == "1"
            or item.byte_size > settings.detail_max_bytes
        ):
            _record_detail_response(
                result,
                response,
                target.url,
                max_bytes=settings.detail_max_bytes,
                item=item,
                store_body=False,
            )
            item.status = DetailFetchStatus.FAILED
            item.content_status = ContentStatus.ERROR
            item.error = (
                f"response exceeded the {settings.detail_max_bytes}-byte download limit; "
                "content not stored"
            )
            result.items.append(item)
            continue

        _record_detail_response(
            result,
            response,
            target.url,
            # Cap the stored body at the lower of the two ceilings, so a detail
            # response can never be persisted past the raw-payload limit even if
            # the detail limit is configured higher.
            max_bytes=min(settings.detail_max_bytes, settings.raw_max_bytes),
            item=item,
        )

        description = parse_detail_description(body)
        if description:
            material = "\n".join(
                [
                    str(getattr(job, "title", "") or ""),
                    str(getattr(job, "company", "") or ""),
                    str(getattr(job, "location", "") or ""),
                    description,
                ]
            )
            item.status = DetailFetchStatus.OK
            item.description = description
            item.content_status = ContentStatus.OK
            item.content_hash = content_hash(material)
            item.sha256 = hashlib.sha256(description.encode("utf-8")).hexdigest()
            item.notes = f"description extracted ({len(description)} chars)"
        else:
            item.status = DetailFetchStatus.PARTIAL
            item.content_status = ContentStatus.PARTIAL
            item.error = "no description container found in the response"
        result.items.append(item)

    result.status = SearchRunStatus.COMPLETED
    enriched = sum(1 for item in result.items if item.status is DetailFetchStatus.OK)
    result.notes = f"{enriched} detail(s) enriched out of {result.requested} selected job(s)"
    return result


__all__ = [
    "DEFINITIVE_MISSING_STATUSES",
    "DESCRIPTION_CLASS_MARKERS",
    "DetailFetchConfig",
    "DetailItem",
    "DetailResult",
    "DetailTarget",
    "MIN_DESCRIPTION_CHARS",
    "build_detail_url",
    "fetch_linkedin_details",
    "parse_detail_description",
    "plan_detail_targets",
    "resolve_source_job_id",
    "sanitize_final_url",
]
