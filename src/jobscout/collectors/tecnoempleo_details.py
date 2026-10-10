"""Opt-in Tecnoempleo job-detail enrichment.

The Tecnoempleo search collector stores offers as ``content_status = partial``
because a server-rendered search card only carries the title and company. Many
offer pages do publish a full public description, so this adapter enriches those
stored offers with the detail page it is already allowed to fetch — reusing the
exact same safety model as the search collector.

Safety model (enforced by construction, not by documentation alone):

* **Disabled by default.** :func:`fetch_tecnoempleo_details` refuses any request
  unless ``network_enabled=True``; the CLI only sets it when the operator passes
  ``--confirm-network`` (or injects an offline fixture transport).
* **Robots checked before the first request.** ``robots.txt`` is re-fetched and
  every planned detail URL is checked against it; a disallowed path aborts the
  run closed, exactly like the search collector.
* **No login, no cookies, no private RSS, no unauthorized API, no JavaScript.**
  Only the public detail page already present in the search results is fetched.
* **Bounded.** Item count, request delay, timeout, and the detail download
  ceiling are conservative, and the stored raw body is capped by the lower of the
  detail and raw ceilings.
* **Fail closed.** 401/403/429 and unexpected statuses abort the run; only
  transient failures are retried. An oversized or truncated body is never parsed
  nor stored.
* **Recognized markup only.** The description, company, location, date, and
  technologies are extracted only from markup the adapter positively recognizes.
  When the detail page cannot be recognized, the attempt is recorded as
  ``partial`` with an honest error, never as ``ok``.
* **Content preservation.** A partial or failed attempt never replaces a
  successful full description; that decision is applied at persist time in
  :func:`jobscout.pipeline.ingest_detail_results`.

Operators are responsible for complying with the target site's terms of service
and local law.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

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
from ..normalize import content_hash, html_to_text, normalize_text
from ..timeutil import parse_datetime, to_iso, utcnow_iso
from ..url_policy import is_http_scheme, is_tecnoempleo_host
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
from .ratelimit import BackoffPolicy, RequestPacer
from .robots import check_robots
from .tecnoempleo import (
    PROVIDER,
    _fetch_robots,
    extract_tecnoempleo_job_id,
)

#: Class/``itemprop`` markers that positively identify the description block in
#: a Tecnoempleo offer page. Matching is token-based, so a lookalike such as
#: ``no-description`` is never accepted. Kept configuration-free on purpose: a
#: marker list is provenance, not a mutable endpoint.
DESCRIPTION_CLASS_MARKERS = (
    "descripcion",
    "descripcion-oferta",
    "oferta-descripcion",
    "detalle-oferta",
    "job-description",
)

#: Schema.org ``itemprop`` values used as a second, structural signal.
DESCRIPTION_ITEMPROPS = ("description",)

#: ``itemprop`` values that identify company, location, and date fields. A bare
#: ``name`` is deliberately not accepted as a company signal: the real offer
#: page carries an ``itemprop="name"`` inside its ``BreadcrumbList`` whose text
#: is the offer title, which must never be mistaken for the company.
COMPANY_ITEMPROPS = ("hiringOrganization",)
LOCATION_ITEMPROPS = ("jobLocation", "addressLocality")
DATE_ITEMPROPS = ("datePosted",)

#: A ``<time>`` element's machine-readable attribute. Preferred over the human
#: text because it is unambiguous and locale-independent.
_TIME_DATETIME_RE = re.compile(
    r"<time\b[^>]*\bdatetime\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)

#: The real offer page's company link: an ``<a>`` whose ``href`` is
#: ``/<slug>/re-<id>`` and whose ``title`` is ``Ofertas de empleo de X``. The
#: breadcrumb never has this shape, so the company survives even though the
#: page has no ``hiringOrganization`` markup.
_COMPANY_ANCHOR_RE = re.compile(
    r"<a\b"
    r"(?=[^>]*\bhref=[\"'][^\"']*/[^/?#\"']+/re-[0-9A-Za-z]+[\"'])"
    r"(?=[^>]*\btitle=[\"']Ofertas de [Ee]mpleo(?: de)? )"
    r"[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)

#: The page's own title and the real title's location pattern
#: (``Oferta de Empleo <title> en <city>, <company> - Tecnoempleo.com``).
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TITLE_LOCATION_RE = re.compile(
    r"Oferta de Empleo\b.+?\ben\s+(.+?),\s*.+?\s*-\s*Tecnoempleo\.com",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_OFFER_RE = re.compile(r"Oferta de Empleo\s+(.+?)\s+en\s+", re.IGNORECASE)
_H1_TITLE_RE = re.compile(
    r"<h1\b[^>]*\bitemprop=[\"']title[\"'][^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL
)

#: Meta tags read for the company fallback and the real publication date.
_TWITTER_DATA1_META_RE = re.compile(
    r"<meta\b[^>]*\bname=[\"']twitter:data1[\"'][^>]*>", re.IGNORECASE
)
_OG_DESCRIPTION_META_RE = re.compile(
    r"<meta\b[^>]*\bproperty=[\"']og:description[\"'][^>]*>", re.IGNORECASE
)
_META_CONTENT_RE = re.compile(r"\bcontent=[\"']([^\"']*)[\"']", re.IGNORECASE)
_DDMMYYYY_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")

#: Class markers for the company and location blocks, used when no ``itemprop``
#: is present. Every match is exact-token, never substring.
COMPANY_CLASS_MARKERS = ("empresa", "nombre-empresa", "company")
LOCATION_CLASS_MARKERS = ("ubicacion", "localidad", "location")
DATE_CLASS_MARKERS = ("fecha-publicacion", "publicado", "fecha")

#: Class markers for a technologies/skills list. Values are read from the text
#: of the matched container only.
TECH_CLASS_MARKERS = ("tecnologias", "skills", "requisitos-tecnicos")

#: Minimum characters for extracted text to count as real content.
MIN_DESCRIPTION_CHARS = 10

#: Absolute floor for "the detail page is recognizable at all": the description
#: is the only field that makes a page worth storing as ``ok``.
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

#: Technology tokens are kept short and alphanumeric-ish; a token with a space or
#: punctuation is not a technology name.
_TECH_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#._-]{0,31}")
_TECH_SPLIT_RE = re.compile(r"[,;/|\u00b7\n]+")


@dataclass
class TecnoempleoDetailItem:
    """Outcome of one Tecnoempleo detail attempt (ok, partial, failed, skipped)."""

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
    company: str | None = None
    location: str | None = None
    posted_at: str | None = None
    technologies: list[str] = field(default_factory=list)

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
            "company": self.company,
            "location": self.location,
            "posted_at": self.posted_at,
            "technologies": list(self.technologies),
            "fetched_at": self.fetched_at,
            "http_status": self.http_status,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "error": self.error,
            "notes": self.notes,
        }


@dataclass
class TecnoempleoDetailResult:
    """Outcome of one Tecnoempleo detail run (network, dry run, or refusal)."""

    items: list[TecnoempleoDetailItem] = field(default_factory=list)
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
class TecnoempleoDetailTarget:
    """A selected job plus the detail URL it resolves to (if any)."""

    job: Any
    source_job_id: str | None
    url: str | None
    reason: str | None = None


# ---------------------------------------------------------------------------
# URL planning (pure)
# ---------------------------------------------------------------------------


def resolve_detail_url(job: Any) -> str | None:
    """Resolve a fetchable, same-host Tecnoempleo detail URL from a stored job.

    The stored URL is the search result link, which is already an absolute
    ``https://*.tecnoempleo.com`` URL carrying the stable ``rf-`` id. A URL on
    another host, a non-HTTP scheme, or one without a recognizable id is refused
    so a tampered stored row cannot point the fetcher off-site.
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
    if not is_tecnoempleo_host(parts.hostname or ""):
        return None
    if not extract_tecnoempleo_job_id(candidate):
        return None
    return candidate


def plan_detail_targets(
    jobs: Sequence[Any], *, max_items: int
) -> tuple[list[TecnoempleoDetailTarget], int]:
    """Return ``(targets, truncated_count)`` for a bounded selection of jobs.

    Jobs without a resolvable Tecnoempleo detail URL are kept as skipped targets
    so the caller can report them without sending a request.
    """
    items = list(jobs)
    selected = items[: max(0, int(max_items))]
    truncated = len(items) - len(selected)
    targets: list[TecnoempleoDetailTarget] = []
    for job in selected:
        url = resolve_detail_url(job)
        if url is None:
            targets.append(
                TecnoempleoDetailTarget(
                    job=job,
                    source_job_id=normalize_text(getattr(job, "source_job_id", None))
                    or None,
                    url=None,
                    reason=(
                        "no fetchable Tecnoempleo detail URL could be resolved from "
                        "the stored job"
                    ),
                )
            )
            continue
        targets.append(
            TecnoempleoDetailTarget(
                job=job,
                source_job_id=extract_tecnoempleo_job_id(url)
                or normalize_text(getattr(job, "source_job_id", None))
                or None,
                url=url,
            )
        )
    return targets, truncated


# ---------------------------------------------------------------------------
# HTML parsing (pure, offline-testable)
# ---------------------------------------------------------------------------


def _has_class(classes: str, markers: Sequence[str]) -> bool:
    """Exact-token class match (never substring), BEM modifiers included."""
    tokens = classes.lower().split()
    return any(
        token == marker
        or (token.startswith(f"{marker}--") and len(token) > len(marker) + 2)
        for token in tokens
        for marker in markers
    )


def _is_description_container(tag: str, attributes: dict[str, str]) -> bool:
    if tag not in {"div", "section", "article", "p", "ul", "ol"}:
        return False
    if _has_class(attributes.get("class", ""), DESCRIPTION_CLASS_MARKERS):
        return True
    return attributes.get("itemprop", "").lower() in DESCRIPTION_ITEMPROPS


class _BlockParser(HTMLParser):
    """Capture the inner HTML of containers matching a predicate.

    Each capture is ``(text, raw_html)``. The parser tracks nesting so a
    container with children is captured whole, and a container left open by
    truncated HTML is still finalized (so a partial page yields partial content
    instead of nothing).
    """

    def __init__(self, predicate: Callable[[str, dict[str, str]], bool]) -> None:
        super().__init__(convert_charrefs=True)
        self._predicate = predicate
        self.captures: list[tuple[str, str]] = []
        self._depth = 0
        self._parts: list[str] = []
        self._capturing = False

    def _finish(self) -> None:
        if self._capturing:
            raw = "".join(self._parts)
            self.captures.append((html_to_text(raw), raw))
        self._capturing = False
        self._depth = 0
        self._parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        if not self._capturing:
            if self._predicate(tag, attributes):
                self._capturing = True
                self._depth = 1
                self._parts = []
            return
        raw_tag = self.get_starttag_text() or f"<{tag}>"
        if tag in _VOID_TAGS:
            self._parts.append(raw_tag)
            return
        self._depth += 1
        self._parts.append(raw_tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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
        if self._capturing:
            self._finish()


def _captures(
    html_text: str, predicate: Callable[[str, dict[str, str]], bool]
) -> list[tuple[str, str]]:
    if not html_text:
        return []
    parser = _BlockParser(predicate)
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is resilient, guard anyway
        return []
    parser.finalize()
    return parser.captures


def parse_detail_description(html_text: str) -> str:
    """Extract the offer description, or ``""`` for unrecognized markup."""
    candidates = [
        text
        for text, _raw in _captures(html_text, _is_description_container)
        if len(text) >= MIN_DESCRIPTION_CHARS
    ]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _extract_field(
    html_text: str,
    *,
    itemprops: Sequence[str],
    class_markers: Sequence[str],
    max_chars: int = 160,
) -> str | None:
    """Extract one short scalar field from ``itemprop`` or class-marked markup."""

    def predicate(tag: str, attributes: dict[str, str]) -> bool:
        if tag in {"span", "div", "p", "a", "li", "strong", "em", "time"}:
            if attributes.get("itemprop", "").lower() in {
                item.lower() for item in itemprops
            }:
                return True
            if _has_class(attributes.get("class", ""), class_markers):
                return True
        return False

    for text, _raw in _captures(html_text, predicate):
        value = normalize_text(text)
        if value:
            return value[:max_chars]
    return None


def _parse_meta_content(html_text: str, meta_re: re.Pattern[str]) -> str | None:
    """Return the first non-empty ``content`` attribute of a matched meta tag."""
    for match in meta_re.finditer(html_text or ""):
        content = _META_CONTENT_RE.search(match.group(0))
        if content:
            value = normalize_text(content.group(1))
            if value:
                return value
    return None


def _parse_company_from_anchor(html_text: str) -> str | None:
    """Read the company from the real ``/<slug>/re-<id>`` company link.

    The logo link and the name link share the same ``href``/``title``; the logo
    link has only an image, so the first anchor with readable text wins.
    """
    for match in _COMPANY_ANCHOR_RE.finditer(html_text or ""):
        value = normalize_text(html_to_text(match.group(1)))
        if value:
            return value[:120]
    return None


def _offer_title_candidates(html_text: str) -> list[str]:
    """Offer-title strings the company must never be equal to."""
    candidates: list[str] = []
    title_match = _TITLE_TAG_RE.search(html_text or "")
    if title_match:
        page_title = normalize_text(html_to_text(title_match.group(1)))
        offer_match = _TITLE_OFFER_RE.search(page_title)
        if offer_match:
            candidates.append(normalize_text(offer_match.group(1)))
    h1_match = _H1_TITLE_RE.search(html_text or "")
    if h1_match:
        candidates.append(normalize_text(html_to_text(h1_match.group(1))))
    return [candidate for candidate in candidates if candidate]


def _company_equals_offer_title(company: str, html_text: str) -> bool:
    target = normalize_text(company).casefold()
    return any(
        candidate.casefold() == target
        for candidate in _offer_title_candidates(html_text)
    )


def parse_detail_company(html_text: str) -> str | None:
    """Extract the company, ignoring the breadcrumb's ``itemprop="name"``.

    Preference order: the real ``/<slug>/re-<id>`` company link, then the
    ``twitter:data1`` meta tag, then positively recognized markup
    (``hiringOrganization`` or a company class). The breadcrumb name is never a
    company signal, and a value equal to the offer title is refused so the
    guardrail catches a markup regression.
    """
    if not html_text:
        return None
    company = _parse_company_from_anchor(html_text)
    if not company:
        company = _parse_meta_content(html_text, _TWITTER_DATA1_META_RE)
    if not company:
        company = _extract_field(
            html_text,
            itemprops=COMPANY_ITEMPROPS,
            class_markers=COMPANY_CLASS_MARKERS,
        )
    if not company:
        return None
    company = normalize_text(company)
    if not company or len(company) > 120:
        return None
    if _company_equals_offer_title(company, html_text):
        return None
    return company


def parse_detail_location(html_text: str) -> str | None:
    """Extract the location from recognized markup, else the real page title.

    Real offer pages carry no ``jobLocation``; the city is only in the title
    (``Oferta de Empleo <title> en <city>, <company> - Tecnoempleo.com``). When
    recognized structural markup is present it still wins, so old fixtures keep
    working. No soft heuristics are added.
    """
    recognized = _extract_field(
        html_text,
        itemprops=LOCATION_ITEMPROPS,
        class_markers=LOCATION_CLASS_MARKERS,
    )
    if recognized:
        return recognized
    title_match = _TITLE_TAG_RE.search(html_text or "")
    if not title_match:
        return None
    page_title = normalize_text(html_to_text(title_match.group(1)))
    match = _TITLE_LOCATION_RE.search(page_title)
    if not match:
        return None
    city = normalize_text(match.group(1))
    return city[:160] if city else None


def _parse_posted_from_og_description(html_text: str) -> str | None:
    """Real publication date: the strict ``dd/mm/yyyy`` in ``og:description``."""
    content = _parse_meta_content(html_text, _OG_DESCRIPTION_META_RE)
    if not content:
        return None
    match = _DDMMYYYY_RE.search(content)
    if not match:
        return None
    try:
        moment = datetime(
            int(match.group(3)), int(match.group(2)), int(match.group(1)),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    return to_iso(moment)


def parse_detail_posted_at(html_text: str) -> str | None:
    """Extract and normalize a publication date, or ``None`` when unrecognized.

    A machine-readable ``<time datetime="...">`` attribute is the preferred
    source, followed by recognized date markup. Real offer pages carry neither,
    so the strict ``dd/mm/yyyy`` in ``og:description`` is the fallback. Relative
    or locale text is never guessed.
    """
    if html_text:
        match = _TIME_DATETIME_RE.search(html_text)
        if match:
            parsed = parse_datetime(match.group(1).strip())
            if parsed:
                return to_iso(parsed)
    raw = _extract_field(
        html_text, itemprops=DATE_ITEMPROPS, class_markers=DATE_CLASS_MARKERS
    )
    if raw:
        # The text fallback must be a real absolute date. ``parse_flexible_date``
        # is deliberately not used here: a locale date such as "10 de septiembre"
        # would match its relative-date ``d`` unit and produce a wrong timestamp.
        parsed = parse_datetime(raw)
        if parsed:
            return to_iso(parsed)
    return _parse_posted_from_og_description(html_text)


def parse_detail_technologies(html_text: str, *, limit: int = 30) -> list[str]:
    """Extract technology tokens from a recognized technologies container only.

    Unknown markup yields an empty list. The tokens are de-duplicated
    case-insensitively and kept in document order.
    """

    def predicate(tag: str, attributes: dict[str, str]) -> bool:
        return tag in {"div", "ul", "ol", "section", "p", "span"} and _has_class(
            attributes.get("class", ""), TECH_CLASS_MARKERS
        )

    technologies: list[str] = []
    seen: set[str] = set()
    for text, _raw in _captures(html_text, predicate):
        for chunk in _TECH_SPLIT_RE.split(text):
            token = normalize_text(chunk)
            if not token:
                continue
            match = _TECH_TOKEN_RE.fullmatch(token)
            if not match:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            technologies.append(token)
            if len(technologies) >= limit:
                return technologies
    return technologies


@dataclass
class TecnoempleoDetailFields:
    """Structured detail extracted from a recognized offer page."""

    description: str = ""
    company: str | None = None
    location: str | None = None
    posted_at: str | None = None
    technologies: list[str] = field(default_factory=list)

    @property
    def recognized(self) -> bool:
        return len(self.description) >= MIN_DESCRIPTION_CHARS


def parse_detail_fields(html_text: str) -> TecnoempleoDetailFields:
    """Parse all recognized fields from a Tecnoempleo detail page in one pass.

    Fields are extracted independently: a description with no company markup
    still yields a description, and unknown markup yields an empty result rather
    than a fabricated one.
    """
    if not html_text:
        return TecnoempleoDetailFields()
    return TecnoempleoDetailFields(
        description=parse_detail_description(html_text),
        company=parse_detail_company(html_text),
        location=parse_detail_location(html_text),
        posted_at=parse_detail_posted_at(html_text),
        technologies=parse_detail_technologies(html_text),
    )


# ---------------------------------------------------------------------------
# Network path
# ---------------------------------------------------------------------------


def _sanitize_final_url(requested_url: str | None, response_url: str | None) -> str | None:
    """Return the safe final URL of an allowed same-host redirect, else ``None``."""
    requested = str(requested_url or "").strip()
    candidate = str(response_url or "").strip()
    if not candidate or candidate == requested:
        return None
    if not is_http_scheme(candidate):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if "@" in parts.netloc:
        return None
    if not is_tecnoempleo_host(parts.hostname or ""):
        return None
    return candidate


def _record_detail_response(
    result: TecnoempleoDetailResult,
    response: HttpResponse | None,
    url: str,
    *,
    max_bytes: int,
    item: TecnoempleoDetailItem,
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


def fetch_tecnoempleo_details(
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
) -> TecnoempleoDetailResult:
    """Fetch detail pages for a bounded selection of stored Tecnoempleo jobs.

    Without ``network_enabled=True`` this raises
    :class:`CollectorDisabledError`; with ``dry_run=True`` it returns the planned
    URLs and performs no I/O. ``robots_text`` may be injected so offline tests
    skip the robots request; a live run fetches and enforces ``robots.txt``
    before the first detail request.
    """
    resolved_max_items = (
        max_items if max_items is not None else settings.tecnoempleo_detail_max_items
    )
    validate_bounded_int(resolved_max_items, name="max_items", hard_max=HARD_MAX_ITEMS)
    targets, truncated = plan_detail_targets(jobs, max_items=resolved_max_items)
    planned = [target.url for target in targets if target.url]
    result = TecnoempleoDetailResult(
        planned_urls=planned, requested=len(targets), truncated=max(0, truncated)
    )

    if dry_run:
        result.status = SearchRunStatus.DRY_RUN
        result.notes = "dry run: no request was performed"
        return result

    if not network_enabled:
        raise CollectorDisabledError(
            "Tecnoempleo detail fetching is disabled; rerun with --confirm-network to "
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
                settings.tecnoempleo_robots_url,
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
                TecnoempleoDetailItem(
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
            item = TecnoempleoDetailItem(
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
            item = TecnoempleoDetailItem(
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
            item = TecnoempleoDetailItem(
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
        item = TecnoempleoDetailItem(
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
            item.company = fields.company
            item.location = fields.location
            item.posted_at = fields.posted_at
            item.technologies = list(fields.technologies)
            item.notes = f"description extracted ({len(fields.description)} chars)"
        else:
            item.status = DetailFetchStatus.PARTIAL
            item.content_status = ContentStatus.PARTIAL
            item.error = "no recognized offer description found in the response"
        result.items.append(item)

    result.status = SearchRunStatus.COMPLETED
    enriched = sum(1 for item in result.items if item.status is DetailFetchStatus.OK)
    result.notes = (
        f"{enriched} detail(s) enriched out of {result.requested} selected job(s) "
        f"({robots_note})"
    )
    return result


__all__ = [
    "COMPANY_CLASS_MARKERS",
    "DATE_CLASS_MARKERS",
    "DESCRIPTION_CLASS_MARKERS",
    "LOCATION_CLASS_MARKERS",
    "MIN_DESCRIPTION_CHARS",
    "TECH_CLASS_MARKERS",
    "TecnoempleoDetailFields",
    "TecnoempleoDetailItem",
    "TecnoempleoDetailResult",
    "TecnoempleoDetailTarget",
    "fetch_tecnoempleo_details",
    "parse_detail_company",
    "parse_detail_description",
    "parse_detail_fields",
    "parse_detail_location",
    "parse_detail_posted_at",
    "parse_detail_technologies",
    "plan_detail_targets",
    "resolve_detail_url",
]
