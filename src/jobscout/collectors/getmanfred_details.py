"""Opt-in GetManfred job-detail enrichment from the page's recognized JSON-LD.

The GetManfred sitemap inventory stores offers as ``content_status = partial``
because a sitemap entry only carries the URL slug. The public offer pages do
publish a structured ``JobPosting`` JSON-LD block in their static HTML, so this
adapter enriches those stored offers with the detail the page itself exposes,
reusing the exact same safety model as the Tecnoempleo detail adapter.

Written permission from GetManfred covers automated requests and the storage of
offer descriptions for this project; the robots.txt gate is still enforced at
runtime, so a future policy change stops the run instead of relying on today's
permission alone.

Safety model (enforced by construction, not by documentation alone):

* **Disabled by default.** :func:`fetch_getmanfred_details` refuses any request
  unless ``network_enabled=True``; the CLI only sets it when the operator passes
  ``--confirm-network`` (or injects an offline fixture transport).
* **Robots checked before the first request.** ``robots.txt`` is re-fetched and
  every planned detail URL is checked against it; a disallowed path aborts the
  run closed, exactly like the search collector.
* **No login, no cookies, no private RSS, no unauthorized API, no JavaScript.**
  Only the public detail page already present in the sitemap is fetched.
* **Bounded.** Item count, request delay, timeout, and the detail download
  ceiling are conservative, and the stored raw body is capped by the lower of the
  detail and raw ceilings.
* **Fail closed.** 401/403/429 and unexpected statuses abort the run; only
  transient failures are retried. An oversized or truncated body is never parsed
  nor stored.
* **Recognized JSON-LD only.** Fields are read from a ``JobPosting`` JSON-LD
  object the adapter positively recognizes. A field the page does not expose
  stays ``None`` and is never inferred (the company is never derived from the
  URL slug). Unknown markup is recorded as ``partial`` with an honest error,
  never as ``ok``.
* **Content preservation.** A partial or failed attempt never replaces a
  successful full description; that decision is applied at persist time in
  :func:`jobscout.pipeline.ingest_detail_results`.

Operators are responsible for complying with the target site's terms of service
and local law.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from ..config import (
    DEFAULT_GETMANFRED_OFFER_PATH_PREFIX,
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
from ..normalize import (
    content_hash,
    html_to_text,
    normalize_employment_type,
    normalize_multiline,
    normalize_text,
)
from ..timeutil import parse_datetime, to_iso, utcnow_iso
from ..url_policy import is_getmanfred_host, is_http_scheme
from .base import (
    CLOSED_NOTE,
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
from .getmanfred import PROVIDER, extract_getmanfred_job_id
from .htmlutil import sanitize_redirect
from .ratelimit import BackoffPolicy, RequestPacer
from .robots import check_robots
from .tecnoempleo import _fetch_robots

#: Schema.org ``employmentType`` enum values mapped to the canonical buckets the
#: rest of the database already uses (see ``normalize_employment_type``). A value
#: outside this map (and outside the free-text patterns) is unrecognized and the
#: field stays ``None``.
EMPLOYMENT_TYPE_VALUES: dict[str, str] = {
    "full_time": "full_time",
    "part_time": "part_time",
    "contractor": "contract",
    "contract": "contract",
    "temporary": "temporary",
    "intern": "internship",
    "internship": "internship",
}

#: Minimum characters for extracted text to count as real content.
MIN_DESCRIPTION_CHARS = 10

#: A JSON-LD ``@type`` comparator, case-insensitive and whitespace tolerant.
_JOB_POSTING_TYPE = "jobposting"

#: Next.js page data. A closed offer still answers 200 with its full posting;
#: only ``props.pageProps.offer`` says ``status: CLOSED`` / ``active: false``
#: (live pages: open offers carry ``ACTIVE`` / ``true``).
_NEXT_DATA_RE = re.compile(
    r"""<script[^>]*\bid\s*=\s*["']__NEXT_DATA__["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)


def _as_sequence(value: Any) -> list[Any]:
    """Return ``value`` as a list, preserving ``None`` as a single item.

    JSON-LD fields are frequently a single object, a list, or absent; every
    extractor normalizes through this helper so all three shapes are handled.
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


# ---------------------------------------------------------------------------
# JSON-LD extraction (pure, offline-testable)
# ---------------------------------------------------------------------------


class _JsonLdScriptParser(HTMLParser):
    """Collect the text of every ``<script type="application/ld+json">`` block.

    Deterministic and dependency-free: the parser only tracks the script
    elements whose declared type is JSON-LD and never interprets the JSON
    itself. A script left open by truncated HTML is still finalized, so a
    partial page yields partial JSON instead of silently nothing.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self._capturing = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capturing or tag.lower() != "script":
            return
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if attributes.get("type", "").strip().lower() == "application/ld+json":
            self._capturing = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capturing and tag.lower() == "script":
            self.scripts.append("".join(self._parts))
            self._capturing = False
            self._parts = []

    def finalize(self) -> None:
        if self._capturing:
            self.scripts.append("".join(self._parts))
            self._capturing = False
            self._parts = []


def extract_json_ld_scripts(html_text: str) -> list[str]:
    """Return the raw text of every JSON-LD script block, in document order."""
    if not html_text:
        return []
    parser = _JsonLdScriptParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is resilient, guard anyway
        return []
    parser.finalize()
    return parser.scripts


def _is_job_posting_type(value: Any) -> bool:
    for candidate in _as_sequence(value):
        if isinstance(candidate, str) and candidate.strip().lower() == _JOB_POSTING_TYPE:
            return True
    return False


def _find_job_posting(data: Any) -> dict[str, Any] | None:
    """Recursively locate the first ``@type: JobPosting`` object, ``@graph`` included."""
    for candidate in _as_sequence(data):
        if isinstance(candidate, dict):
            if _is_job_posting_type(candidate.get("@type")):
                return candidate
            graph = _find_job_posting(candidate.get("@graph"))
            if graph is not None:
                return graph
    return None


def parse_json_ld_job_posting(html_text: str) -> dict[str, Any] | None:
    """Return the recognized ``JobPosting`` JSON-LD object, or ``None``.

    Every JSON-LD script is parsed independently; malformed JSON, non-object
    documents, and objects without a ``JobPosting`` ``@type`` are skipped so one
    unrelated schema block on the page cannot hide the offer.
    """
    for raw in extract_json_ld_scripts(html_text):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        node = _find_job_posting(data)
        if node is not None:
            return node
    return None


# ---------------------------------------------------------------------------
# Field extraction (pure, from the recognized JSON-LD only)
# ---------------------------------------------------------------------------


def _extract_description(job: dict[str, Any]) -> str:
    raw = job.get("description")
    if raw is None:
        return ""
    if isinstance(raw, str):
        value = raw
    elif isinstance(raw, dict):
        value = raw.get("text") or raw.get("value")
    elif isinstance(raw, (list, tuple)):
        value = " ".join(str(part) for part in raw if part is not None)
    else:
        value = str(raw)
    if not value:
        return ""
    if "<" in str(value):
        return html_to_text(value)
    return normalize_multiline(value)


def parse_detail_description(html_text: str) -> str:
    """Extract the offer description from JSON-LD, or ``""`` when unrecognized."""
    job = parse_json_ld_job_posting(html_text)
    if job is None:
        return ""
    return _extract_description(job)


def _extract_title(job: dict[str, Any]) -> str | None:
    value = normalize_text(job.get("title")) if isinstance(job.get("title"), str) else ""
    return value[:200] or None


def _extract_company(job: dict[str, Any]) -> str | None:
    for candidate in _as_sequence(job.get("hiringOrganization")):
        if isinstance(candidate, str):
            value = normalize_text(candidate)
        elif isinstance(candidate, dict):
            value = normalize_text(candidate.get("name"))
        else:
            value = ""
        if value:
            return value[:120]
    return None


def parse_detail_company(html_text: str) -> str | None:
    """Extract the company from ``hiringOrganization`` only (never the slug)."""
    job = parse_json_ld_job_posting(html_text)
    if job is None:
        return None
    return _extract_company(job)


def _extract_location(job: dict[str, Any]) -> str | None:
    for place in _as_sequence(job.get("jobLocation")):
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if isinstance(address, dict):
            parts = [
                normalize_text(address.get(key))
                for key in ("addressLocality", "addressRegion", "addressCountry")
            ]
            parts = [part for part in parts if part]
            if parts:
                return ", ".join(parts)[:160]
            street = normalize_text(address.get("streetAddress"))
            if street:
                return street[:160]
        locality = normalize_text(place.get("addressLocality"))
        if locality:
            return locality[:160]
    return None


def parse_detail_location(html_text: str) -> str | None:
    """Extract a location from ``jobLocation``; empty/missing stays ``None``.

    ``jobLocationType`` (for example ``TELECOMMUTE``) is deliberately not turned
    into a location: only an address the page actually exposes is stored, and an
    absent locality is never replaced by an inferred value.
    """
    job = parse_json_ld_job_posting(html_text)
    if job is None:
        return None
    return _extract_location(job)


def parse_detail_posted_at(html_text: str) -> str | None:
    """Normalize ``datePosted`` to a UTC ISO-8601 instant, else ``None``."""
    job = parse_json_ld_job_posting(html_text)
    if job is None:
        return None
    return _extract_posted_at(job)


def _extract_posted_at(job: dict[str, Any]) -> str | None:
    for candidate in _as_sequence(job.get("datePosted")):
        text = normalize_text(candidate)
        if not text:
            continue
        parsed = parse_datetime(text)
        if parsed:
            return to_iso(parsed)
    return None


def _normalize_employment_value(value: Any) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    mapped = EMPLOYMENT_TYPE_VALUES.get(text.lower())
    if mapped:
        return mapped
    bucket = normalize_employment_type(text)
    return None if bucket == "unknown" else bucket


def parse_detail_employment_type(html_text: str) -> str | None:
    """Map a recognized ``employmentType`` to a canonical bucket, else ``None``."""
    job = parse_json_ld_job_posting(html_text)
    if job is None:
        return None
    return _extract_employment_type(job)


def _extract_employment_type(job: dict[str, Any]) -> str | None:
    for candidate in _as_sequence(job.get("employmentType")):
        bucket = _normalize_employment_value(candidate)
        if bucket:
            return bucket
    return None


def _format_number(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    text = normalize_text(value)
    return text or None


def _format_amount(currency: str, minimum: Any, maximum: Any, unit: str) -> str | None:
    low = _format_number(minimum)
    high = _format_number(maximum)
    if low and high and low != high:
        amount = f"{low}-{high}"
    else:
        amount = low or high
    if not amount:
        return None
    return f"{currency}{amount}{f' {unit}' if unit else ''}".strip()


def _format_salary_candidate(value: Any) -> str | None:
    if isinstance(value, str):
        text = normalize_text(value)
        return text or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _format_number(value)
    if not isinstance(value, dict):
        return None
    currency = normalize_text(value.get("currency"))
    for inner in _as_sequence(value.get("value")):
        if isinstance(inner, dict):
            explicit = inner.get("value")
            if isinstance(explicit, str) and normalize_text(explicit):
                return normalize_text(explicit)
            amount = _format_amount(
                currency,
                inner.get("minValue"),
                inner.get("maxValue"),
                normalize_text(inner.get("unitText")),
            )
            if amount:
                return amount
        elif inner is not None:
            text = normalize_text(inner)
            if text:
                return f"{currency}{text}" if currency and currency not in text else text
    amount = normalize_text(value.get("amount"))
    if amount:
        return f"{currency}{amount}" if currency and currency not in amount else amount
    return None


def parse_detail_salary(html_text: str) -> str | None:
    """Extract ``baseSalary`` as a readable string, else ``None``.

    The page's own explicit value string is preferred; otherwise a monetary
    range is rendered from the numbers the page provides. A shape that cannot be
    read stays ``None`` instead of producing a fabricated range.
    """
    job = parse_json_ld_job_posting(html_text)
    if job is None:
        return None
    return _extract_salary(job)


def _extract_salary(job: dict[str, Any]) -> str | None:
    for candidate in _as_sequence(job.get("baseSalary")):
        formatted = _format_salary_candidate(candidate)
        if formatted:
            return formatted[:120]
    return None


def parse_detail_closed(html_text: str) -> bool:
    """True when the page data marks the offer as closed. Never raises.

    Missing or unreadable page data means "not known to be closed": only an
    explicit ``CLOSED`` status or ``active: false`` deactivates an offer.
    """
    match = _NEXT_DATA_RE.search(html_text or "")
    if match is None:
        return False
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return False
    offer: Any = data
    for key in ("props", "pageProps", "offer"):
        offer = offer.get(key) if isinstance(offer, dict) else None
    if not isinstance(offer, dict):
        return False
    status = offer.get("status")
    return (isinstance(status, str) and status.strip().upper() == "CLOSED") or offer.get("active") is False


@dataclass
class GetManfredDetailFields:
    """Structured detail extracted from a recognized GetManfred offer page."""

    description: str = ""
    title: str | None = None
    company: str | None = None
    location: str | None = None
    posted_at: str | None = None
    employment_type: str | None = None
    salary: str | None = None

    @property
    def recognized(self) -> bool:
        return len(self.description) >= MIN_DESCRIPTION_CHARS


def parse_detail_fields(html_text: str) -> GetManfredDetailFields:
    """Parse all recognized fields from one GetManfred detail page.

    Fields are extracted independently: a description with no company markup
    still yields a description, and a page that exposes no salary yields
    ``None`` rather than a guessed value.
    """
    job = parse_json_ld_job_posting(html_text)
    if job is None:
        return GetManfredDetailFields()
    return GetManfredDetailFields(
        description=_extract_description(job),
        title=_extract_title(job),
        company=_extract_company(job),
        location=_extract_location(job),
        posted_at=_extract_posted_at(job),
        employment_type=_extract_employment_type(job),
        salary=_extract_salary(job),
    )


# ---------------------------------------------------------------------------
# URL planning (pure)
# ---------------------------------------------------------------------------


def resolve_detail_url(job: Any) -> str | None:
    """Resolve a fetchable, same-host GetManfred detail URL from a stored job.

    The stored URL is the sitemap entry, already an absolute HTTPS
    ``getmanfred.com`` URL. A URL on another host, a non-HTTPS scheme, or one
    that is not a known ``/ofertas-empleo/{id}/...`` offer path is refused so a
    tampered stored row cannot point the fetcher off-site or at an arbitrary
    page.
    """
    candidate = normalize_text(getattr(job, "url", None))
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if not is_http_scheme(candidate) or parts.scheme.lower() != "https":
        return None
    if "@" in parts.netloc:
        return None
    if not is_getmanfred_host(parts.hostname or ""):
        return None
    if not (parts.path or "").startswith(DEFAULT_GETMANFRED_OFFER_PATH_PREFIX):
        return None
    if not extract_getmanfred_job_id(candidate):
        return None
    return candidate


@dataclass
class GetManfredDetailTarget:
    """A selected job plus the detail URL it resolves to (if any)."""

    job: Any
    source_job_id: str | None
    url: str | None
    reason: str | None = None


def plan_detail_targets(
    jobs: Sequence[Any], *, max_items: int
) -> tuple[list[GetManfredDetailTarget], int]:
    """Return ``(targets, truncated_count)`` for a bounded selection of jobs.

    Jobs without a resolvable GetManfred detail URL are kept as skipped targets
    so the caller can report them without sending a request.
    """
    items = list(jobs)
    selected = items[: max(0, int(max_items))]
    truncated = len(items) - len(selected)
    targets: list[GetManfredDetailTarget] = []
    for job in selected:
        url = resolve_detail_url(job)
        if url is None:
            targets.append(
                GetManfredDetailTarget(
                    job=job,
                    source_job_id=normalize_text(getattr(job, "source_job_id", None))
                    or None,
                    url=None,
                    reason=(
                        "no fetchable GetManfred detail URL could be resolved from "
                        "the stored job"
                    ),
                )
            )
            continue
        targets.append(
            GetManfredDetailTarget(
                job=job,
                source_job_id=extract_getmanfred_job_id(url)
                or normalize_text(getattr(job, "source_job_id", None))
                or None,
                url=url,
            )
        )
    return targets, truncated


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class GetManfredDetailItem:
    """Outcome of one GetManfred detail attempt (ok, partial, failed, skipped)."""

    job_id: int
    source_job_id: str | None = None
    url: str | None = None
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
    raw_document_index: int | None = None
    #: Extracted structured fields, applied to the stored job on persist.
    #: ``title`` only replaces a title the inventory derived from the URL slug.
    title: str | None = None
    company: str | None = None
    location: str | None = None
    posted_at: str | None = None
    employment_type: str | None = None
    salary: str | None = None
    #: The page data says the offer is closed.
    closed: bool = False

    @property
    def is_definitive_missing(self) -> bool:
        """True when a FAILED fetch proved the posting is gone (HTTP 404/410)."""
        return (
            self.status is DetailFetchStatus.FAILED
            and self.http_status in (404, 410)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "source_job_id": self.source_job_id,
            "url": self.url,
            "status": str(self.status),
            "content_status": str(self.content_status),
            "description_chars": len(self.description or ""),
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "posted_at": self.posted_at,
            "employment_type": self.employment_type,
            "salary": self.salary,
            "fetched_at": self.fetched_at,
            "http_status": self.http_status,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "error": self.error,
            "notes": self.notes,
        }


@dataclass
class GetManfredDetailResult:
    """Outcome of one GetManfred detail run (network, dry run, or refusal)."""

    items: list[GetManfredDetailItem] = field(default_factory=list)
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


# ---------------------------------------------------------------------------
# Network path
# ---------------------------------------------------------------------------


def _sanitize_final_url(requested_url: str | None, response_url: str | None) -> str | None:
    """Return the safe final URL of an allowed same-host redirect, else ``None``."""
    return sanitize_redirect(
        requested_url,
        response_url,
        allow_host=is_getmanfred_host,
        require_https=True,
    )


def _record_detail_response(
    result: GetManfredDetailResult,
    response: HttpResponse | None,
    url: str,
    *,
    max_bytes: int,
    item: GetManfredDetailItem,
    store_body: bool = True,
) -> None:
    """Attach a size-bounded raw document for a response."""
    if response is None:
        return
    body = response.body or ""
    if store_body:
        safe, reason = is_safe_to_store(body, max_bytes=max_bytes)
    else:
        safe, reason = False, "response exceeded the detail download limit"
    document = RawDocument(
        kind=RawDocumentKind.JOB_DETAIL,
        source=PROVIDER,
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


def fetch_getmanfred_details(
    jobs: Sequence[Any],
    *,
    settings: Settings,
    network_enabled: bool = False,
    dry_run: bool = False,
    max_items: int | None = None,
    client: HttpClient | None = None,
    sleep: Callable[[float], None] | None = None,
    on_attempt: Callable[[AttemptLog], None] | None = None,
    robots_text: str | None = None,
) -> GetManfredDetailResult:
    """Fetch detail pages for a bounded selection of stored GetManfred jobs.

    Without ``network_enabled=True`` this raises
    :class:`CollectorDisabledError`; with ``dry_run=True`` it returns the planned
    URLs and performs no I/O. ``robots_text`` may be injected so offline tests
    skip the robots request; a live run fetches and enforces ``robots.txt``
    before the first detail request.
    """
    resolved_max_items = (
        max_items if max_items is not None else settings.getmanfred_detail_max_items
    )
    validate_bounded_int(resolved_max_items, name="max_items", hard_max=HARD_MAX_ITEMS)
    targets, truncated = plan_detail_targets(jobs, max_items=resolved_max_items)
    planned = [target.url for target in targets if target.url]
    result = GetManfredDetailResult(
        planned_urls=planned, requested=len(targets), truncated=max(0, truncated)
    )

    if dry_run:
        result.status = SearchRunStatus.DRY_RUN
        result.notes = "dry run: no request was performed"
        return result

    if not network_enabled:
        raise CollectorDisabledError(
            "GetManfred detail fetching is disabled; rerun with --confirm-network to "
            f"allow {len(planned)} bounded request(s), or use --dry-run to inspect the plan"
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

    robots_note = "robots.txt supplied by caller; not fetched"
    if planned:
        if robots_text is None:
            ok, fetched_robots, robots_note = _fetch_robots(
                settings.getmanfred_robots_url,
                transport=transport,
                policy=policy,
                pacer=pacer,
                sleep=sleep,
            )
            if not ok:
                result.status = SearchRunStatus.FAILED_CLOSED
                result.error = robots_note
                result.notes = "could not establish robots.txt permission; run aborted"
                return result
            robots_text = fetched_robots

        for url in planned:
            allowed, reason = check_robots(
                robots_text, url, user_agent=settings.user_agent
            )
            if not allowed:
                result.status = SearchRunStatus.FAILED_CLOSED
                result.error = f"refusing disallowed path for {url}: {reason}"
                result.notes = "robots.txt disallows the detail path; run aborted"
                return result

    for target in targets:
        job = target.job
        job_id = int(getattr(job, "id", 0) or 0)

        if target.url is None:
            result.items.append(
                GetManfredDetailItem(
                    job_id=job_id,
                    source_job_id=target.source_job_id,
                    status=DetailFetchStatus.SKIPPED,
                    error=target.reason,
                )
            )
            continue

        pacer.wait()
        try:
            response = fetch_with_policy(
                transport,
                target.url,
                policy=policy,
                pacer=pacer,
                sleep=sleep,
                headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.5"},
                on_attempt=on_attempt,
            )
        except NotFoundError as exc:
            not_found_response = exc.response
            item = GetManfredDetailItem(
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
            item = GetManfredDetailItem(
                job_id=job_id,
                source_job_id=target.source_job_id,
                url=target.url,
                status=DetailFetchStatus.FAILED,
                error=str(exc),
            )
            _record_detail_response(
                result,
                exc.response,
                target.url,
                max_bytes=settings.raw_max_bytes,
                item=item,
            )
            result.items.append(item)
            result.requests_made += 1
            result.status = SearchRunStatus.FAILED_CLOSED
            result.error = str(exc)
            result.notes = "remote endpoint refused access; run aborted"
            return result
        except CollectorError as exc:
            item = GetManfredDetailItem(
                job_id=job_id,
                source_job_id=target.source_job_id,
                url=target.url,
                status=DetailFetchStatus.FAILED,
                error=str(exc),
            )
            _record_detail_response(
                result,
                exc.response,
                target.url,
                max_bytes=settings.raw_max_bytes,
                item=item,
            )
            result.items.append(item)
            result.requests_made += 1
            result.status = SearchRunStatus.FAILED
            result.error = str(exc)
            result.notes = "detail fetch aborted after a non-retryable failure"
            return result

        result.requests_made += 1
        body = response.body or ""
        item = GetManfredDetailItem(
            job_id=job_id,
            source_job_id=target.source_job_id,
            url=target.url,
            final_url=_sanitize_final_url(target.url, response.url),
            fetched_at=response.fetched_at,
            http_status=response.status_code,
            byte_size=len(body.encode("utf-8", errors="replace")),
        )

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
                f"response exceeded the {settings.detail_max_bytes}-byte download "
                "limit; content not stored"
            )
            result.items.append(item)
            continue

        _record_detail_response(
            result,
            response,
            target.url,
            max_bytes=min(settings.detail_max_bytes, settings.raw_max_bytes),
            item=item,
        )

        item.closed = parse_detail_closed(body)
        fields = parse_detail_fields(body)
        if fields.recognized:
            material = "\n".join(
                [
                    str(getattr(job, "title", "") or ""),
                    fields.company or str(getattr(job, "company", "") or ""),
                    fields.location or str(getattr(job, "location", "") or ""),
                    fields.description,
                ]
            )
            item.status = DetailFetchStatus.OK
            item.description = fields.description
            item.content_status = ContentStatus.OK
            item.content_hash = content_hash(material)
            item.sha256 = hashlib.sha256(fields.description.encode("utf-8")).hexdigest()
            item.title = fields.title
            item.company = fields.company
            item.location = fields.location
            item.posted_at = fields.posted_at
            item.employment_type = fields.employment_type
            item.salary = fields.salary
            item.notes = f"description extracted ({len(fields.description)} chars)"
        else:
            item.status = DetailFetchStatus.PARTIAL
            item.content_status = ContentStatus.PARTIAL
            item.error = "no recognized JobPosting JSON-LD description found in the response"
        if item.closed:
            item.notes = (item.notes + "; " if item.notes else "") + CLOSED_NOTE
        result.items.append(item)

    result.status = SearchRunStatus.COMPLETED
    enriched = sum(1 for item in result.items if item.status is DetailFetchStatus.OK)
    result.notes = (
        f"{enriched} detail(s) enriched out of {result.requested} selected job(s) "
        f"({robots_note})"
    )
    return result


__all__ = [
    "EMPLOYMENT_TYPE_VALUES",
    "MIN_DESCRIPTION_CHARS",
    "GetManfredDetailFields",
    "GetManfredDetailItem",
    "GetManfredDetailResult",
    "GetManfredDetailTarget",
    "extract_json_ld_scripts",
    "fetch_getmanfred_details",
    "parse_detail_closed",
    "parse_detail_company",
    "parse_detail_description",
    "parse_detail_employment_type",
    "parse_detail_fields",
    "parse_detail_location",
    "parse_detail_posted_at",
    "parse_detail_salary",
    "parse_json_ld_job_posting",
    "plan_detail_targets",
    "resolve_detail_url",
]
