"""GetManfred public sitemap inventory collector (opt-in, no keyword fan-out).

Scope and ethics (enforced by construction, not by documentation alone):

* **Disabled by default.** :func:`search_getmanfred` refuses any network call
  unless ``network_enabled=True``, which the CLI only sets when the operator
  passes ``--confirm-network``.
* **Public discovery only.** The adapter reads the offers sitemap that
  GetManfred advertises in its public ``robots.txt`` (``Allow: /``). It never
  logs in, never sends cookies, never runs JavaScript, and never fetches a job
  detail page.
* **Allow-list.** The configured sitemap URL (and every child sitemap of a
  sitemap index) must be an HTTPS ``getmanfred.com``/``*.getmanfred.com`` host;
  a cross-host child is skipped, never fetched.
* **Inventory, not search.** There is no keyword fan-out and no search rank:
  every observed position is recorded as ``position_kind = none`` with rank 0.
  The offer title is derived from the public URL slug and the company is left
  unknown, because the sitemap carries no company field. This is a discovery
  inventory (``content_status = partial``), not a full posting.
* **Bounded.** Documents, items, request delay, timeout, and the raw download
  ceiling are all conservative.
* **Fail closed.** 401/403/429 and unexpected statuses abort the run; only
  transient failures are retried. A body that is not a recognized sitemap is
  refused instead of producing an empty (but "successful") inventory.

Operators are responsible for complying with the target site's terms of service
and local law.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from ..config import (
    DEFAULT_GETMANFRED_OFFER_PATH_PREFIX,
    DEFAULT_GETMANFRED_SITEMAP_URL,
    HARD_MAX_ITEMS,
    HARD_MAX_PAGES,
    Settings,
    is_safe_to_store,
    validate_bounded_int,
)
from ..models import (
    ContentStatus,
    JobPosting,
    PositionKind,
    RawDocument,
    RawDocumentKind,
    SearchRunStatus,
)
from ..normalize import normalize_job_posting
from ..timeutil import utcnow_iso
from ..url_policy import is_getmanfred_host, validate_getmanfred_url
from .base import (
    TRUNCATION_HEADER,
    AccessDeniedError,
    CollectorDisabledError,
    CollectorError,
    CollectorResult,
    HttpClient,
    HttpResponse,
    RateLimitedError,
    UrllibHttpClient,
    fetch_with_policy,
)
from .ratelimit import BackoffPolicy, RequestPacer

PROVIDER = "getmanfred"
SITEMAP_KIND_URLSET = "urlset"
SITEMAP_KIND_INDEX = "sitemapindex"
SITEMAP_KIND_UNKNOWN = "unknown"

_OFFER_ID_RE = re.compile(r"/ofertas-empleo/(\d+)(?:/|$)")


@dataclass
class GetManfredSitemapConfig:
    """Bounded inventory parameters for the public offers sitemap."""

    sitemap_url: str = DEFAULT_GETMANFRED_SITEMAP_URL
    offer_path_prefix: str = DEFAULT_GETMANFRED_OFFER_PATH_PREFIX
    max_documents: int = 1
    max_items: int = 40

    def __post_init__(self) -> None:
        validate_getmanfred_url(self.sitemap_url, name="getmanfred_sitemap_url")
        if not self.offer_path_prefix.startswith("/"):
            raise ValueError("offer_path_prefix must start with '/'")
        validate_bounded_int(
            self.max_documents, name="max_documents", hard_max=HARD_MAX_PAGES
        )
        validate_bounded_int(self.max_items, name="max_items", hard_max=HARD_MAX_ITEMS)

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def position_kind(self) -> PositionKind:
        """Inventory has no ordered search result, so rank is meaningless."""
        return PositionKind.NONE

    def to_run_params(self, result: CollectorResult) -> dict[str, Any]:
        return {
            "sitemap_url": self.sitemap_url,
            "offer_path_prefix": self.offer_path_prefix,
            "max_documents": self.max_documents,
            "max_items": self.max_items,
            "planned_urls": list(result.planned_urls),
            "network_enabled": result.status is not SearchRunStatus.DRY_RUN,
        }


# ---------------------------------------------------------------------------
# Sitemap parsing (pure, offline-testable)
# ---------------------------------------------------------------------------


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def classify_sitemap(xml_text: str) -> str:
    """Return ``urlset``, ``sitemapindex``, or ``unknown`` for ``xml_text``."""
    if not xml_text or not xml_text.strip():
        return SITEMAP_KIND_UNKNOWN
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return SITEMAP_KIND_UNKNOWN
    name = _local_name(root.tag)
    if name == "urlset":
        return SITEMAP_KIND_URLSET
    if name == "sitemapindex":
        return SITEMAP_KIND_INDEX
    return SITEMAP_KIND_UNKNOWN


def _child_text(element: Any, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def parse_url_entries(xml_text: str) -> list[dict[str, str | None]]:
    """Parse a ``<urlset>`` into ``{loc, lastmod}`` entries, in document order."""
    if not xml_text:
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    if _local_name(root.tag) != "urlset":
        return []
    entries: list[dict[str, str | None]] = []
    for node in root:
        if _local_name(node.tag) != "url":
            continue
        loc = _child_text(node, "loc")
        if loc:
            entries.append({"loc": loc, "lastmod": _child_text(node, "lastmod")})
    return entries


def parse_index_entries(xml_text: str) -> list[str]:
    """Parse a ``<sitemapindex>`` into child sitemap URLs, in document order."""
    if not xml_text:
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    if _local_name(root.tag) != "sitemapindex":
        return []
    urls: list[str] = []
    for node in root:
        if _local_name(node.tag) != "sitemap":
            continue
        loc = _child_text(node, "loc")
        if loc:
            urls.append(loc)
    return urls


def extract_getmanfred_job_id(url: str) -> str | None:
    """Extract the numeric offer id from a ``/ofertas-empleo/<id>/<slug>`` URL."""
    match = _OFFER_ID_RE.search(str(url or ""))
    return match.group(1) if match else None


def title_from_offer_slug(url: str) -> str:
    """Humanize the public URL slug into a readable title.

    The sitemap exposes ``/ofertas-empleo/<id>/<company>-<role>-<month><yy>``
    but no structured title field, so this is deliberately a display-only
    best-effort derived from public URL text. The company is never guessed.
    """
    path = urlsplit(str(url or "")).path
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""
    slug = unquote(segments[-1])
    slug = re.sub(r"[-_]+", " ", slug)
    slug = re.sub(r"\s+", " ", slug).strip()
    if not slug:
        return ""
    return slug[0].upper() + slug[1:]


def is_offer_url(url: str, *, offer_path_prefix: str) -> bool:
    """Whether ``url`` is a same-host offer detail URL under the allowed path."""
    parts = urlsplit(str(url or ""))
    if not is_getmanfred_host(parts.hostname or ""):
        return False
    return (parts.path or "").startswith(offer_path_prefix) and bool(
        extract_getmanfred_job_id(url)
    )


def postings_from_sitemap(
    xml_text: str,
    *,
    offer_path_prefix: str,
    fetched_at: str | None = None,
    max_items: int = 40,
) -> list[JobPosting]:
    """Normalize offer entries from a sitemap into partial postings.

    Non-offer URLs (home pages, static pages) are ignored. Each posting keeps
    the sitemap ``lastmod`` in ``extra`` for provenance, never as ``posted_at``
    because a modification date is not a publication date.
    """
    resolved_at = fetched_at or utcnow_iso()
    postings: list[JobPosting] = []
    for entry in parse_url_entries(xml_text):
        loc = str(entry.get("loc") or "")
        if not is_offer_url(loc, offer_path_prefix=offer_path_prefix):
            continue
        job_id = extract_getmanfred_job_id(loc)
        raw = {
            "source": PROVIDER,
            "source_job_id": job_id,
            "url": loc,
            "title": title_from_offer_slug(loc),
            "company": "",
            "fetched_at": resolved_at,
            "content_status": ContentStatus.PARTIAL.value,
            "sitemap_lastmod": entry.get("lastmod"),
            "inventory": "sitemap",
        }
        posting = normalize_job_posting(raw, source=PROVIDER)
        if posting.content_status is ContentStatus.EMPTY:
            continue
        postings.append(posting)
        if len(postings) >= max_items:
            break
    return postings


# ---------------------------------------------------------------------------
# Network path
# ---------------------------------------------------------------------------


def plan_sitemap_urls(config: GetManfredSitemapConfig) -> list[str]:
    """Return the sitemap URL a live run would request (used by ``--dry-run``)."""
    return [config.sitemap_url]


def search_getmanfred(
    config: GetManfredSitemapConfig,
    *,
    settings: Settings,
    network_enabled: bool = False,
    dry_run: bool = False,
    client: HttpClient | None = None,
    sleep: Callable[[float], None] | None = None,
    on_attempt: Callable[[Any], None] | None = None,
) -> CollectorResult:
    """Collect a bounded GetManfred inventory from the public sitemap.

    Without ``network_enabled=True`` this raises :class:`CollectorDisabledError`;
    with ``dry_run=True`` it returns the planned URL and performs no I/O.
    """
    planned = plan_sitemap_urls(config)

    if dry_run:
        return CollectorResult(
            status=SearchRunStatus.DRY_RUN,
            planned_urls=planned,
            notes="dry run: no request was performed",
        )

    if not network_enabled:
        raise CollectorDisabledError(
            "GetManfred collection is disabled; rerun with --confirm-network to allow "
            f"{len(planned)} bounded request(s), or use --dry-run to inspect the plan"
        )

    import time as _time

    transport = client or UrllibHttpClient(
        timeout=settings.http_timeout_seconds,
        user_agent=settings.user_agent,
        accept="application/xml,text/xml,*/*",
        max_download_bytes=settings.raw_max_bytes,
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

    result = CollectorResult(planned_urls=planned)
    queue = list(planned)
    seen_documents: set[str] = set()
    documents_fetched = 0

    while queue and documents_fetched < config.max_documents:
        url = queue.pop(0)
        if url in seen_documents:
            continue
        seen_documents.add(url)
        pacer.wait()
        try:
            response: HttpResponse = fetch_with_policy(
                transport,
                url,
                policy=policy,
                pacer=pacer,
                sleep=sleep,
                on_attempt=on_attempt,
            )
        except (AccessDeniedError, RateLimitedError) as exc:
            result.status = SearchRunStatus.FAILED_CLOSED
            result.error = str(exc)
            result.notes = "remote endpoint refused access; run aborted"
            _record_response(result, exc.response, url, max_bytes=settings.raw_max_bytes)
            return result
        except CollectorError as exc:
            result.status = SearchRunStatus.FAILED
            result.error = str(exc)
            _record_response(result, exc.response, url, max_bytes=settings.raw_max_bytes)
            return result

        _record_response(result, response, url, max_bytes=settings.raw_max_bytes)
        result.pages_fetched += 1
        documents_fetched += 1

        if response.headers.get(TRUNCATION_HEADER) == "1":
            result.notes = (
                f"{url} exceeded the {settings.raw_max_bytes}-byte download limit; "
                "content not parsed"
            )
            break

        kind = classify_sitemap(response.body)
        if kind == SITEMAP_KIND_URLSET:
            postings = postings_from_sitemap(
                response.body,
                offer_path_prefix=config.offer_path_prefix,
                fetched_at=response.fetched_at,
                max_items=config.max_items,
            )
            _extend_unique(result.postings, postings, limit=config.max_items)
        elif kind == SITEMAP_KIND_INDEX:
            if not _enqueue_child_sitemaps(queue, response.body):
                result.notes = f"{url} is a sitemap index with no allowed child sitemap"
        else:
            # A 200 that is not a recognized sitemap is an unsupported response:
            # fail closed instead of recording a silent empty inventory.
            result.status = SearchRunStatus.FAILED_CLOSED
            result.error = f"unsupported sitemap response from {url} (not an XML urlset or sitemapindex)"
            result.notes = "unrecognized sitemap body; run aborted"
            return result

        if len(result.postings) >= config.max_items:
            break

    result.items_found = len(result.postings)
    if result.notes is None and result.status is SearchRunStatus.COMPLETED:
        result.notes = f"{result.items_found} offer(s) discovered from the public sitemap"
    return result


def _enqueue_child_sitemaps(queue: list[str], xml_text: str) -> bool:
    """Append allow-listed child sitemaps from an index; return whether any were added."""
    added = False
    for child in parse_index_entries(xml_text):
        if not is_getmanfred_host(urlsplit(child).hostname or ""):
            continue
        queue.append(child)
        added = True
    return added


def _extend_unique(
    target: list[JobPosting], postings: list[JobPosting], *, limit: int
) -> None:
    """Append postings, dropping duplicates already collected in this run."""
    seen = {posting.fingerprint for posting in target}
    for posting in postings:
        if len(target) >= limit:
            return
        if posting.fingerprint in seen:
            continue
        seen.add(posting.fingerprint)
        target.append(posting)


def _record_response(
    result: CollectorResult,
    response: HttpResponse | None,
    url: str,
    *,
    max_bytes: int,
) -> None:
    """Attach a bounded raw document for provenance, mirroring the LinkedIn path."""
    if response is None:
        return
    body = response.body or ""
    if response.headers.get(TRUNCATION_HEADER) == "1":
        safe, reason = False, "response exceeded the download limit"
    else:
        safe, reason = is_safe_to_store(body, max_bytes=max_bytes)
    result.raw_documents.append(
        RawDocument(
            kind=RawDocumentKind.SEARCH_RESULT,
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
    )


__all__ = [
    "GetManfredSitemapConfig",
    "PROVIDER",
    "SITEMAP_KIND_INDEX",
    "SITEMAP_KIND_UNKNOWN",
    "SITEMAP_KIND_URLSET",
    "classify_sitemap",
    "extract_getmanfred_job_id",
    "is_offer_url",
    "parse_index_entries",
    "parse_url_entries",
    "plan_sitemap_urls",
    "postings_from_sitemap",
    "search_getmanfred",
    "title_from_offer_slug",
]
