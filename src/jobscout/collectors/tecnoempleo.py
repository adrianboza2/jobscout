"""Tecnoempleo public keyword-search collector (opt-in, bounded).

Scope and ethics (enforced by construction, not by documentation alone):

* **Disabled by default.** :func:`search_tecnoempleo` refuses any network call
  unless ``network_enabled=True``, which the CLI only sets when the operator
  passes ``--confirm-network``.
* **Approved public path only.** It uses Tecnoempleo's server-rendered keyword
  search under ``/ofertas-trabajo/``. Its public ``robots.txt`` allows that path
  and disallows private/API/feed paths (``/alertas-empleo-rss.php``,
  ``/profesionales/``, ``/accesoempresa.php``, ...). The adapter re-fetches
  ``robots.txt`` before the search and fails closed if the path is disallowed or
  the robots document cannot be trusted.
* **No login, no cookies, no private RSS, no unauthorized API, no JavaScript.**
* **Search position is provenance.** Records are stored with
  ``position_kind = search_rank``; the rank is where the offer appeared, never a
  quality signal.
* **Bounded and fail-closed.** Page/item caps, request delay, timeout, and the
  raw download ceiling are conservative. 401/403/429 abort the run; a first-page
  200 body with no recognizable offer link is refused rather than recorded as
  "no jobs", because the parser is deliberately conservative about unknown
  markup. Once offers have been collected, a later page with no recognizable
  offer link is a normal end of pagination and stops the run as completed.
* **Live URL validation.** The search URL shape was verified against Tecnoempleo's
  live server-rendered pages: keywords use the ``te`` query parameter and
  pagination uses ``pagina``.

Operators are responsible for complying with the target site's terms of service
and local law.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import quote, urlencode, urljoin, urlsplit

from ..config import (
    DEFAULT_TECNOEMPLEO_OFFER_PATH_MARKER,
    DEFAULT_TECNOEMPLEO_ROBOTS_URL,
    DEFAULT_TECNOEMPLEO_SEARCH_URL,
    HARD_MAX_ITEMS,
    HARD_MAX_PAGES,
    Settings,
    is_safe_to_store,
    validate_bounded_int,
)
from ..keywords import retired_search
from ..models import (
    ContentStatus,
    JobPosting,
    PositionKind,
    RawDocument,
    RawDocumentKind,
    SearchRunStatus,
)
from ..normalize import canonicalize_url, html_to_text, normalize_job_posting, normalize_text
from ..timeutil import to_iso, utcnow_iso
from ..url_policy import is_tecnoempleo_host, validate_tecnoempleo_url
from .base import (
    TRUNCATION_HEADER,
    AccessDeniedError,
    CollectorDisabledError,
    CollectorError,
    CollectorResult,
    HttpClient,
    HttpResponse,
    NotFoundError,
    RateLimitedError,
    UrllibHttpClient,
    fetch_with_policy,
)
from .htmlutil import VOID_TAGS
from .ratelimit import BackoffPolicy, RequestPacer
from .robots import check_robots

PROVIDER = "tecnoempleo"
DEFAULT_PAGE_PARAM = "pagina"
_RF_ID_RE = re.compile(r"(rf-[0-9a-zA-Z]+)")


@dataclass
class TecnoempleoSearchConfig:
    """Bounded search parameters for the public server-rendered search."""

    keywords: str
    location: str | None = None
    search_url: str = DEFAULT_TECNOEMPLEO_SEARCH_URL
    robots_url: str = DEFAULT_TECNOEMPLEO_ROBOTS_URL
    offer_path_marker: str = DEFAULT_TECNOEMPLEO_OFFER_PATH_MARKER
    page_param: str = DEFAULT_PAGE_PARAM
    max_pages: int = 2
    max_items: int = 40

    def __post_init__(self) -> None:
        if not self.keywords or not self.keywords.strip():
            raise ValueError("keywords are required for a Tecnoempleo search")
        retired = retired_search(PROVIDER, self.keywords)
        if retired is not None:
            raise ValueError(
                f"query {self.keywords.strip()!r} is retired for {PROVIDER} on "
                f"{retired.retired_on} ({retired.reason}); the same query stays "
                "available on other providers and its search history is preserved"
            )
        validate_tecnoempleo_url(self.search_url, name="tecnoempleo_search_url")
        validate_tecnoempleo_url(self.robots_url, name="tecnoempleo_robots_url")
        if not self.offer_path_marker.strip():
            raise ValueError("offer_path_marker must be non-empty")
        validate_bounded_int(self.max_pages, name="max_pages", hard_max=HARD_MAX_PAGES)
        validate_bounded_int(self.max_items, name="max_items", hard_max=HARD_MAX_ITEMS)

    @property
    def provider(self) -> str:
        return PROVIDER

    @property
    def position_kind(self) -> PositionKind:
        return PositionKind.SEARCH_RANK

    def to_run_params(self, result: CollectorResult) -> dict[str, Any]:
        return {
            "keywords": self.keywords,
            "location": self.location,
            "search_url": self.search_url,
            "max_pages": self.max_pages,
            "max_items": self.max_items,
            "offer_path_marker": self.offer_path_marker,
            "planned_urls": list(result.planned_urls),
            "network_enabled": result.status is not SearchRunStatus.DRY_RUN,
        }


def build_search_url(config: TecnoempleoSearchConfig, *, page: int = 1) -> str:
    """Build one bounded server-rendered search URL.

    ``https://www.tecnoempleo.com/ofertas-trabajo/<location>/?te=<keyword>``, or
    ``https://www.tecnoempleo.com/ofertas-trabajo/?te=<keyword>`` when no
    location is configured, with ``&<page_param>=N`` appended for pages after
    the first. The location is kept as a path segment (the base keeps its
    trailing slash so ``robots.txt`` prefixes stay meaningful); the keyword is
    URL encoded as the ``te`` query parameter via :func:`urllib.parse.urlencode`.
    """
    base = config.search_url.rstrip("/")
    if config.location and config.location.strip():
        path = f"{base}/{quote(config.location.strip(), safe='')}/"
    else:
        path = f"{base}/"
    params: list[tuple[str, Any]] = [("te", config.keywords.strip())]
    if page > 1:
        params.append((config.page_param, int(page)))
    return f"{path}?{urlencode(params)}"


def plan_search_urls(config: TecnoempleoSearchConfig) -> list[str]:
    """Return the URLs a live run would request (used by ``--dry-run``)."""
    return [build_search_url(config, page=page) for page in range(1, config.max_pages + 1)]


# ---------------------------------------------------------------------------
# HTML parsing (pure, offline-testable)
# ---------------------------------------------------------------------------


def _anchor_text(parts: list[str]) -> str:
    return normalize_text(" ".join(part for part in parts if part) or "")


class _OfferLinkParser(HTMLParser):
    """Collect anchors whose ``href`` looks like a Tecnoempleo offer link."""

    def __init__(self, *, marker: str) -> None:
        super().__init__(convert_charrefs=True)
        self._marker = marker.lower()
        self._active = False
        self._href = ""
        self._parts: list[str] = []
        self.links: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = {key: (value or "") for key, value in attrs}
        href = attributes.get("href", "")
        if self._marker and self._marker in href.lower():
            self._active = True
            self._href = href
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._active:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._active:
            return
        self.links.append({"href": self._href, "text": _anchor_text(self._parts)})
        self._active = False
        self._href = ""
        self._parts = []

    def finalize(self) -> None:
        if self._active:
            self.links.append({"href": self._href, "text": _anchor_text(self._parts)})


def extract_tecnoempleo_job_id(url: str) -> str | None:
    """Extract the stable ``rf-...`` offer id from an offer URL."""
    match = _RF_ID_RE.search(str(url or ""))
    return match.group(1) if match else None


def title_from_offer_url(url: str) -> str:
    """Humanize the leading path slug (the offer title in Tecnoempleo URLs)."""
    path = urlsplit(str(url or "")).path
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""
    slug = segments[0].replace("-", " ")
    slug = re.sub(r"\s+", " ", slug).strip()
    if not slug:
        return ""
    return slug[0].upper() + slug[1:]


def parse_offer_links(
    html_text: str,
    *,
    base_url: str = DEFAULT_TECNOEMPLEO_SEARCH_URL,
    offer_path_marker: str = DEFAULT_TECNOEMPLEO_OFFER_PATH_MARKER,
) -> list[dict[str, str]]:
    """Parse offer links from a server-rendered search page.

    Returns canonical ``{url, source_job_id, title}`` entries. Non-offer links,
    cross-host links, and repeats are dropped. Never raises for malformed HTML:
    unknown markup simply yields fewer links.
    """
    if not html_text:
        return []
    lowered = html_text.lower()
    if offer_path_marker.lower() not in lowered:
        return []
    parser = _OfferLinkParser(marker=offer_path_marker)
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is resilient, guard anyway
        return []
    parser.finalize()

    origin = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in parser.links:
        absolute = canonicalize_url(urljoin(origin, link["href"]))
        if not absolute or absolute in seen:
            continue
        parts = urlsplit(absolute)
        if not is_tecnoempleo_host(parts.hostname or ""):
            continue
        job_id = extract_tecnoempleo_job_id(absolute)
        if not job_id:
            continue
        seen.add(absolute)
        results.append(
            {
                "url": absolute,
                "source_job_id": job_id,
                "title": link["text"] or title_from_offer_url(absolute),
            }
        )
    return results


#: Exact class tokens that identify a real offer card container. Matching is
#: token-based (never substring), so an unrelated class cannot be mistaken for
#: a card. Kept as provenance, not as a mutable, soft selector heuristic.
CARD_CLASS_TOKENS = ("p-3", "border", "rounded")

#: Bound for the optional card snippet, so a card never carries an unbounded
#: description into storage. Only a positively recognized snippet is used.
CARD_DESCRIPTION_MAX_CHARS = 500

_CARD_URL_RE = re.compile(r"location\.href\s*=\s*'([^']+)'", re.IGNORECASE)
_CARD_H3_RE = re.compile(r"<h3\b[^>]*>(.*?)</h3>", re.IGNORECASE | re.DOTALL)
_CARD_ANCHOR_TITLE_RE = re.compile(r"<a\b[^>]*\btitle=[\"']([^\"']+)[\"']", re.IGNORECASE)
_CARD_ANCHOR_TEXT_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_CARD_COMPANY_TITLE_RE = re.compile(
    r"<a\b[^>]*\btitle=[\"']Ofertas de Empleo ([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_CARD_COMPANY_HREF_RE = re.compile(
    r"<a\b[^>]*\bhref=[\"']([^\"']*-trabajo)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_CARD_MODALITY_DATE_RE = re.compile(
    r"<b>([^<]+)</b>\s*[-\u2013\u2014]?\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE
)
_DDMMYYYY_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")


class _BlockCaptureParser(HTMLParser):
    """Capture the full inner HTML of every container matching ``predicate``.

    Nesting is tracked so a container with children (for example a description
    span wrapping technology badges) is captured whole, and a container left
    open by truncated HTML is still finalized.
    """

    def __init__(self, predicate: Callable[[str, dict[str, str]], bool]) -> None:
        super().__init__(convert_charrefs=True)
        self._predicate = predicate
        self.captures: list[str] = []
        self._capturing = False
        self._depth = 0
        self._parts: list[str] = []

    def _finish(self) -> None:
        if self._capturing:
            self.captures.append("".join(self._parts))
        self._capturing = False
        self._depth = 0
        self._parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        if not self._capturing:
            if self._predicate(tag, attributes):
                self._capturing = True
                self._depth = 1
                # Keep the container's own tag: a card's offer URL lives in its
                # ``onclick`` attribute.
                self._parts = [self.get_starttag_text() or f"<{tag}>"]
            return
        raw_tag = self.get_starttag_text() or f"<{tag}>"
        if tag in VOID_TAGS:
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


def _block_captures(
    html_text: str, predicate: Callable[[str, dict[str, str]], bool]
) -> list[str]:
    if not html_text:
        return []
    parser = _BlockCaptureParser(predicate)
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is resilient, guard anyway
        return []
    parser.finalize()
    return parser.captures


def _is_offer_card(tag: str, attributes: dict[str, str]) -> bool:
    return tag == "div" and set(CARD_CLASS_TOKENS) <= set(
        attributes.get("class", "").split()
    )


def _capture_offer_cards(html_text: str) -> list[str]:
    return _block_captures(html_text, _is_offer_card)


def _iso_from_ddmmyyyy(value: str | None) -> str | None:
    """Return an ISO-8601 UTC timestamp for a strict ``dd/mm/yyyy`` date."""
    match = _DDMMYYYY_RE.search(value or "")
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


def _card_offer_url(card_html: str) -> str:
    match = _CARD_URL_RE.search(card_html)
    if match:
        return normalize_text(match.group(1))
    h3 = _CARD_H3_RE.search(card_html)
    if h3:
        anchor = re.search(
            r"<a\b[^>]*\bhref=[\"']([^\"']+)[\"']", h3.group(1), re.IGNORECASE
        )
        if anchor:
            return normalize_text(anchor.group(1))
    return ""


def _card_title(card_html: str) -> str:
    h3 = _CARD_H3_RE.search(card_html)
    if not h3:
        return ""
    inner = h3.group(1)
    titled = _CARD_ANCHOR_TITLE_RE.search(inner)
    if titled:
        value = normalize_text(html_to_text(titled.group(1)))
        if value:
            return value
    text = _CARD_ANCHOR_TEXT_RE.search(inner)
    if text:
        value = normalize_text(html_to_text(text.group(1)))
        if value:
            return value
    return ""


def _card_company(card_html: str) -> str | None:
    match = _CARD_COMPANY_TITLE_RE.search(card_html) or _CARD_COMPANY_HREF_RE.search(
        card_html
    )
    if not match:
        return None
    value = normalize_text(html_to_text(match.group(2)))
    return value or None


def _card_modality_and_date(card_html: str) -> tuple[str | None, str | None]:
    match = _CARD_MODALITY_DATE_RE.search(card_html)
    if not match:
        return None, None
    modality = normalize_text(match.group(1)) or None
    return modality, _iso_from_ddmmyyyy(match.group(2))


def _card_description(card_html: str) -> str | None:
    captures = _block_captures(
        card_html,
        lambda tag, attributes: tag == "span"
        and "hidden-md-down" in attributes.get("class", "").split(),
    )
    if not captures:
        return None
    value = normalize_text(html_to_text(captures[0]))
    if not value:
        return None
    return value[:CARD_DESCRIPTION_MAX_CHARS]


def parse_offer_cards(
    html_text: str,
    *,
    base_url: str = DEFAULT_TECNOEMPLEO_SEARCH_URL,
    offer_path_marker: str = DEFAULT_TECNOEMPLEO_OFFER_PATH_MARKER,
) -> list[dict[str, str | None]]:
    """Parse real offer cards from a server-rendered search page.

    Each recognized card yields the offer URL (from ``onclick``), the ``h3``
    anchor title, the company anchor text, the modality/location before the
    ``dd/mm/yyyy`` date, the ISO ``posted_at``, and an optional bounded
    description snippet. Fields the markup does not positively carry stay
    ``None``; unrecognized markup yields an empty list. The returned entries use
    the same ``url``/``source_job_id``/``title`` contract as
    :func:`parse_offer_links`.
    """
    if not html_text:
        return []
    if offer_path_marker.lower() not in html_text.lower():
        return []
    origin = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"
    results: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for card_html in _capture_offer_cards(html_text):
        href = _card_offer_url(card_html)
        if not href:
            continue
        absolute = canonicalize_url(urljoin(origin, href))
        if not absolute or absolute in seen:
            continue
        parts = urlsplit(absolute)
        if not is_tecnoempleo_host(parts.hostname or ""):
            continue
        job_id = extract_tecnoempleo_job_id(absolute)
        if not job_id:
            continue
        seen.add(absolute)
        location, posted_at = _card_modality_and_date(card_html)
        results.append(
            {
                "url": absolute,
                "source_job_id": job_id,
                "title": _card_title(card_html) or title_from_offer_url(absolute),
                "company": _card_company(card_html),
                "location": location,
                "posted_at": posted_at,
                "description": _card_description(card_html),
            }
        )
    return results


def postings_from_html(
    html_text: str,
    *,
    base_url: str = DEFAULT_TECNOEMPLEO_SEARCH_URL,
    offer_path_marker: str = DEFAULT_TECNOEMPLEO_OFFER_PATH_MARKER,
    fetched_at: str | None = None,
    max_items: int = 40,
) -> list[JobPosting]:
    """Normalize offer links from one search page into partial postings.

    Real offer cards, when positively recognized, also contribute company,
    location, ``posted_at``, and an optional bounded description advance. A page
    with the legacy link-only shape still works through
    :func:`parse_offer_links`, so the URL/title contract is unchanged.
    """
    resolved_at = fetched_at or utcnow_iso()
    entries: list[dict[str, Any]] = [
        dict(card)
        for card in parse_offer_cards(
            html_text, base_url=base_url, offer_path_marker=offer_path_marker
        )
    ]
    if not entries:
        entries = [
            {
                "url": link["url"],
                "source_job_id": link["source_job_id"],
                "title": link["title"],
            }
            for link in parse_offer_links(
                html_text, base_url=base_url, offer_path_marker=offer_path_marker
            )
        ]

    postings: list[JobPosting] = []
    for entry in entries:
        raw = {
            "source": PROVIDER,
            "source_job_id": entry["source_job_id"],
            "url": entry["url"],
            "title": entry["title"],
            "company": entry.get("company") or "",
            "location": entry.get("location"),
            "posted_at": entry.get("posted_at"),
            "description": entry.get("description"),
            "fetched_at": resolved_at,
            "content_status": ContentStatus.PARTIAL.value,
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


def _fetch_robots(
    robots_url: str,
    *,
    transport: HttpClient,
    policy: BackoffPolicy,
    pacer: RequestPacer,
    sleep: Callable[[float], None] | None,
) -> tuple[bool, str, str]:
    """Return ``(ok, robots_text, note)``. A 404 means "no restrictions"."""
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


def search_tecnoempleo(
    config: TecnoempleoSearchConfig,
    *,
    settings: Settings,
    network_enabled: bool = False,
    dry_run: bool = False,
    client: HttpClient | None = None,
    sleep: Callable[[float], None] | None = None,
    on_attempt: Callable[[Any], None] | None = None,
    robots_text: str | None = None,
) -> CollectorResult:
    """Collect one bounded Tecnoempleo keyword search result set.

    Without ``network_enabled=True`` this raises :class:`CollectorDisabledError`;
    with ``dry_run=True`` it returns the planned URLs and performs no I/O. Tests
    inject ``robots_text`` so no robots request is made offline; a live run
    fetches and enforces ``robots.txt`` before the first search request.
    """
    planned = plan_search_urls(config)

    if dry_run:
        return CollectorResult(
            status=SearchRunStatus.DRY_RUN,
            planned_urls=planned,
            notes="dry run: no request was performed",
        )

    if not network_enabled:
        raise CollectorDisabledError(
            "Tecnoempleo collection is disabled; rerun with --confirm-network to allow "
            f"{len(planned)} bounded request(s), or use --dry-run to inspect the plan"
        )

    import time as _time

    transport = client or UrllibHttpClient(
        timeout=settings.http_timeout_seconds,
        user_agent=settings.user_agent,
        accept="text/html,application/xhtml+xml",
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
    robots_note = "robots.txt supplied by caller; not fetched"

    if robots_text is None:
        ok, fetched_robots, robots_note = _fetch_robots(
            config.robots_url,
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
            result.notes = "robots.txt disallows the search path; run aborted"
            return result

    for page_index, url in enumerate(planned):
        pacer.wait()
        try:
            response: HttpResponse = fetch_with_policy(
                transport,
                url,
                policy=policy,
                pacer=pacer,
                sleep=sleep,
                headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.5"},
                on_attempt=on_attempt,
            )
        except (AccessDeniedError, RateLimitedError) as exc:
            result.status = SearchRunStatus.FAILED_CLOSED
            result.error = str(exc)
            result.notes = "remote endpoint refused access; run aborted"
            _record_response(result, exc.response, url, max_bytes=settings.raw_max_bytes)
            return result
        except NotFoundError as exc:
            # A 404 on a later page means the result set ended: keep the offers
            # already collected and finish honestly. A first-page 404 is a real
            # failure, not an empty search.
            _record_response(result, exc.response, url, max_bytes=settings.raw_max_bytes)
            if page_index > 0 and result.postings:
                result.notes = f"pagination ended at page {page_index + 1}: 404"
                break
            result.status = SearchRunStatus.FAILED
            result.error = str(exc)
            result.notes = "search page not found; run failed"
            return result
        except CollectorError as exc:
            result.status = SearchRunStatus.FAILED
            result.error = str(exc)
            _record_response(result, exc.response, url, max_bytes=settings.raw_max_bytes)
            return result

        _record_response(result, response, url, max_bytes=settings.raw_max_bytes)
        result.pages_fetched += 1

        if response.headers.get(TRUNCATION_HEADER) == "1":
            result.notes = (
                f"page {page_index + 1} exceeded the {settings.raw_max_bytes}-byte "
                "download limit; content not parsed"
            )
            break

        page_postings = postings_from_html(
            response.body,
            base_url=config.search_url,
            offer_path_marker=config.offer_path_marker,
            fetched_at=response.fetched_at,
            max_items=config.max_items,
        )
        if not page_postings:
            if "no hemos encontrado ofertas de empleo para" in (response.body or "").lower():
                result.notes = "no offers for this query"
                break
            if page_index == 0 or not result.postings:
                # Unknown markup must not masquerade as an empty result set.
                result.status = SearchRunStatus.FAILED_CLOSED
                result.error = (
                    f"unsupported search response from {url}: no recognizable offer link "
                    f"(marker {config.offer_path_marker!r}); live selectors are unverified"
                )
                result.notes = "unrecognized search markup; run aborted"
                return result
            # A later page with no offers is a normal end of pagination, not a
            # parser failure: keep what we already collected and stop.
            result.notes = (
                f"pagination ended at page {page_index + 1}: no more offers"
            )
            break

        _extend_unique(result.postings, page_postings, limit=config.max_items)
        if len(result.postings) >= config.max_items:
            break

    result.items_found = len(result.postings)
    if result.notes is None and result.status is SearchRunStatus.COMPLETED:
        result.notes = (
            f"{result.items_found} offer(s) parsed from the public search ({robots_note})"
        )
    return result


def _extend_unique(
    target: list[JobPosting], postings: list[JobPosting], *, limit: int
) -> None:
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
    "CARD_CLASS_TOKENS",
    "CARD_DESCRIPTION_MAX_CHARS",
    "DEFAULT_PAGE_PARAM",
    "PROVIDER",
    "TecnoempleoSearchConfig",
    "build_search_url",
    "extract_tecnoempleo_job_id",
    "parse_offer_cards",
    "parse_offer_links",
    "plan_search_urls",
    "postings_from_html",
    "search_tecnoempleo",
    "title_from_offer_url",
]
