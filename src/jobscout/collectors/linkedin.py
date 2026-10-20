"""LinkedIn public search collector.

Scope and ethics (enforced by construction, not by documentation alone):

* **Disabled by default.** :func:`search_linkedin` refuses to perform any
  network call unless ``network_enabled=True``, which the CLI only sets when the
  operator passes ``--confirm-network``.
* **No login automation, no cookies, no CAPTCHA handling, no anti-bot evasion.**
  Only the public guest search endpoint is used, with an honest user agent.
* **Bounded.** Page count, item count, request delay, and timeout all have
  conservative caps.
* **Fail closed.** 401/403/429/999 and unexpected statuses abort the run
  immediately; only transient failures are retried, with exponential backoff.
* **No detail fetching.** Search cards are summaries
  (``content_status = partial``); no job-detail page is fetched in this work
  unit.

Operators are responsible for complying with the target site's terms of
service and local law.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode, urljoin

from ..config import (
    HARD_MAX_ITEMS,
    HARD_MAX_PAGES,
    Settings,
    is_safe_to_store,
    validate_bounded_int,
)
from ..keywords import retired_search
from ..models import (
    ContentStatus,
    PositionKind,
    RawDocument,
    RawDocumentKind,
    SearchRunStatus,
)
from ..normalize import (
    canonicalize_url,
    extract_linkedin_job_id,
    normalize_job_posting,
    normalize_text,
)
from ..timeutil import utcnow_iso
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
from .htmlutil import VOID_TAGS
from .ratelimit import BackoffPolicy, RequestPacer

#: The guest endpoint serves 10 cards per page (all 266 stored live pages, captured
#: 2026-09-28..10-19). Stepping by 25 silently skipped results 10-24 of every search.
DEFAULT_PAGE_SIZE = 10
DEFAULT_BASE_URL = "https://www.linkedin.com"


@dataclass
class LinkedInSearchConfig:
    """Bounded search parameters for the public guest endpoint."""

    keywords: str
    location: str | None = None
    distance_miles: int | None = None
    remote: bool = False
    employment_type: str | None = None
    max_pages: int = 2
    max_items: int = 40
    page_size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        if not self.keywords or not self.keywords.strip():
            raise ValueError("keywords are required for a LinkedIn search")
        retired = retired_search("linkedin", self.keywords)
        if retired is not None:
            raise ValueError(
                f"query {self.keywords.strip()!r} is retired for linkedin on "
                f"{retired.retired_on} ({retired.reason}); the same query stays "
                "available on other providers and its search history is preserved"
            )
        # Absolute hard caps: enforced here (library), in ``Settings`` (config),
        # and surfaced to the CLI, so no caller can exceed the ceiling.
        validate_bounded_int(self.max_pages, name="max_pages", hard_max=HARD_MAX_PAGES)
        validate_bounded_int(self.max_items, name="max_items", hard_max=HARD_MAX_ITEMS)
        if self.page_size < 1:
            raise ValueError("page_size must be >= 1")

    @property
    def position_kind(self) -> PositionKind:
        """LinkedIn search results are an ordered list, so rank is meaningful."""
        return PositionKind.SEARCH_RANK

    def to_run_params(self, result: CollectorResult) -> dict[str, Any]:
        """Exact historical run provenance for a LinkedIn search."""
        return {
            "keywords": self.keywords,
            "location": self.location,
            "max_pages": self.max_pages,
            "max_items": self.max_items,
            "page_size": self.page_size,
            "planned_urls": list(result.planned_urls),
            "network_enabled": result.status is not SearchRunStatus.DRY_RUN,
        }


def build_search_url(base_url: str, config: LinkedInSearchConfig, *, start: int = 0) -> str:
    """Build a bounded guest-search URL for one page."""
    params: list[tuple[str, Any]] = [("keywords", config.keywords.strip())]
    if config.location:
        params.append(("location", config.location.strip()))
    if config.distance_miles is not None:
        params.append(("distance", int(config.distance_miles)))
    if config.remote:
        params.append(("f_WT", "2"))
    if config.employment_type:
        params.append(("f_JT", config.employment_type))
    params.append(("start", int(start)))
    return f"{base_url}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# HTML parsing (pure, offline-testable)
# ---------------------------------------------------------------------------


def normalize_spaces(text: str) -> str:
    """Collapse all whitespace runs into single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def _collapse_duplicate_text(text: str) -> str:
    """Drop the duplicated half produced by screen-reader-only spans."""
    compact = text.strip()
    if len(compact) >= 4 and len(compact) % 2 == 0:
        half = len(compact) // 2
        if compact[:half] == compact[half:]:
            return compact[:half].strip()
    return compact


def _is_supported_card_root(tag: str, attributes: dict[str, str], classes: str) -> bool:
    """Return whether ``tag`` opens one job card.

    Two markup generations are supported:

    * Historical/synthetic fixtures put the identifying attributes on the outer
      ``<li>`` (``data-entity-urn``, ``result-card`` or
      ``jobs-search-results__list-item``).
    * Current guest-search responses carry a bare outer ``<li>`` and move the
      data onto a nested ``<div class="... base-search-card ... job-search-card"
      data-entity-urn="urn:li:jobPosting:...">``.
    """
    if tag == "li":
        return bool(
            attributes.get("data-entity-urn")
            or "result-card" in classes
            or "jobs-search-results__list-item" in classes
        )
    if tag == "div":
        # ``data-entity-urn`` is the card's data-bearing marker; the card
        # classes guard against unrelated divs that merely carry an entity urn.
        if not attributes.get("data-entity-urn"):
            return False
        tokens = set(classes.split())
        return "base-search-card" in tokens and "job-search-card" in tokens
    return False


class _JobCardParser(HTMLParser):
    """Extract job cards from a guest-search HTML fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, Any]] = []
        self._card: dict[str, Any] | None = None
        self._field: str | None = None
        self._field_tag: str | None = None
        self._field_depth = 0
        self._parts: list[str] = []

    # -- card lifecycle ---------------------------------------------------
    def _start_card(self, attrs: dict[str, str]) -> None:
        self._card = {"fields": {}, "urn": attrs.get("data-entity-urn"), "depth": 0}

    def _finish_card(self) -> None:
        self._close_field()
        card = self._card
        self._card = None
        if not card:
            return
        fields = card.get("fields", {})
        if not fields.get("title") and not card.get("href"):
            return
        card["fields"] = fields
        self.cards.append(card)

    def finalize(self) -> None:
        """Close a card left open by truncated HTML."""
        if self._card is not None:
            self._finish_card()

    # -- field capture ----------------------------------------------------
    def _begin_field(self, name: str, tag: str) -> None:
        self._field = name
        self._field_tag = tag
        self._field_depth = 0
        self._parts = []

    def _close_field(self) -> None:
        if self._card is not None and self._field is not None:
            text = _collapse_duplicate_text(normalize_spaces("".join(self._parts)))
            if text:
                existing = self._card["fields"].get(self._field, "")
                if len(text) > len(existing):
                    self._card["fields"][self._field] = text
        self._field = None
        self._field_tag = None
        self._field_depth = 0
        self._parts = []

    # -- HTMLParser hooks --------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        classes = attributes.get("class", "")

        if _is_supported_card_root(tag, attributes, classes):
            # A new card root while one is still open means the previous card's
            # wrapper never closed (markup drift or truncated HTML). Finalize it
            # so one malformed card cannot swallow the rest of the response.
            if self._card is not None:
                self._finish_card()
            self._start_card(attributes)
            return
        if self._card is None:
            return

        if tag not in VOID_TAGS:
            self._card["depth"] = self._card.get("depth", 0) + 1

        if tag == "a":
            href = attributes.get("href")
            if href and ("base-card__full-link" in classes or not self._card.get("href")):
                self._card["href"] = href
            if attributes.get("title") and not self._card.get("link_title"):
                self._card["link_title"] = normalize_text(attributes["title"])

        if tag == "time" and attributes.get("datetime"):
            self._card["datetime"] = attributes["datetime"]

        if self._field is not None:
            self._field_depth += 1
            return

        if tag == "h3" and "base-search-card__title" in classes:
            self._begin_field("title", tag)
        elif tag == "h4" and "base-search-card__subtitle" in classes:
            self._begin_field("company", tag)
        elif tag == "span" and "job-search-card__location" in classes:
            self._begin_field("location", tag)
        elif tag == "span" and "job-search-card__listdate" in classes:
            self._begin_field("posted_text", tag)

    def handle_data(self, data: str) -> None:
        if self._card is not None and self._field is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        if self._field is not None:
            if self._field_depth > 0:
                self._field_depth -= 1
            else:
                # The field's own closing tag, or malformed nesting: close it
                # either way and keep depth bookkeeping honest.
                self._close_field()
        if tag in VOID_TAGS:
            return
        depth = int(self._card.get("depth", 0))
        if tag == "li" and depth <= 0:
            self._finish_card()
        else:
            self._card["depth"] = max(0, depth - 1)


def parse_job_cards(
    html_text: str,
    *,
    fetched_at: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> list[dict[str, Any]]:
    """Parse guest-search HTML into raw posting dicts.

    Never raises for malformed HTML: unknown markup simply yields fewer cards.
    """
    if not html_text:
        return []
    lowered = html_text.lower()
    # Cheap fast path: skip bodies with no possible card root. Either markup
    # generation must pass: li-based fixtures need "<li"; current responses
    # carry "data-entity-urn" on the nested card div.
    if "<li" not in lowered and "data-entity-urn" not in lowered:
        return []
    parser = _JobCardParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is resilient, guard anyway
        return []
    parser.finalize()
    resolved_at = fetched_at or utcnow_iso()
    postings: list[dict[str, Any]] = []
    for card in parser.cards:
        fields: dict[str, Any] = card.get("fields", {})
        href = card.get("href")
        url = canonicalize_url(urljoin(base_url, href)) if href else None
        job_id = extract_linkedin_job_id(card.get("urn") or url or "")
        postings.append(
            {
                "source": "linkedin",
                "source_job_id": job_id,
                "url": url,
                "title": fields.get("title") or card.get("link_title") or "",
                "company": fields.get("company") or "",
                "location": fields.get("location") or "",
                "posted_at": card.get("datetime"),
                "posted_text": fields.get("posted_text"),
                "fetched_at": resolved_at,
                "content_status": ContentStatus.PARTIAL.value,
            }
        )
    return postings


# ---------------------------------------------------------------------------
# Network path
# ---------------------------------------------------------------------------


def plan_search_urls(config: LinkedInSearchConfig, *, settings: Settings) -> list[str]:
    """Return the URLs a live run would request (used by ``--dry-run``)."""
    return [
        build_search_url(settings.linkedin_search_url, config, start=page * config.page_size)
        for page in range(config.max_pages)
    ]


def search_linkedin(
    config: LinkedInSearchConfig,
    *,
    settings: Settings,
    network_enabled: bool = False,
    dry_run: bool = False,
    client: HttpClient | None = None,
    sleep: Callable[[float], None] | None = None,
    on_attempt: Callable[[Any], None] | None = None,
) -> CollectorResult:
    """Collect one bounded search result set.

    Without ``network_enabled=True`` this raises
    :class:`CollectorDisabledError`; with ``dry_run=True`` it returns the planned
    URLs and performs no I/O at all.

    Live reads are bounded by ``Settings.raw_max_bytes`` at the transport, so a
    page larger than the configured raw ceiling is never buffered whole in
    memory: it is recorded as metadata only and not parsed.
    """
    planned = plan_search_urls(config, settings=settings)

    if dry_run:
        return CollectorResult(
            status=SearchRunStatus.DRY_RUN,
            planned_urls=planned,
            notes="dry run: no request was performed",
        )

    if not network_enabled:
        raise CollectorDisabledError(
            "LinkedIn collection is disabled; rerun with --confirm-network to allow "
            f"{len(planned)} bounded request(s), or use --dry-run to inspect the plan"
        )

    import time as _time

    transport = client or UrllibHttpClient(
        timeout=settings.http_timeout_seconds,
        user_agent=settings.user_agent,
        accept="text/html,application/xhtml+xml",
        # Bound the live search read at the configured safe raw ceiling, so an
        # oversized page is never fully buffered in memory before the persistence
        # guard in :func:`_record_response` can reject it. The ceiling applies to
        # error bodies too (see ``UrllibHttpClient``).
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
    seen_fingerprints: set[str] = set()

    for page_index, url in enumerate(planned):
        # The pacer enforces the minimum delay between outbound requests; its
        # first call is immediate and only records the starting timestamp.
        pacer.wait()
        try:
            response: HttpResponse = fetch_with_policy(
                transport,
                url,
                policy=policy,
                pacer=pacer,
                sleep=sleep,
                headers={"Accept-Language": "en-US,en;q=0.9"},
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

        if response.headers.get(TRUNCATION_HEADER) == "1":
            # The transport hit the download ceiling: the body is partial, so it
            # is neither parsed nor stored. Stop instead of trusting a truncated
            # page, mirroring the detail fetcher's fail-closed handling.
            result.notes = (
                f"page {page_index + 1} exceeded the "
                f"{settings.raw_max_bytes}-byte download limit; content not parsed"
            )
            break

        cards = parse_job_cards(response.body, fetched_at=response.fetched_at)
        if not cards:
            body_bytes = len((response.body or "").encode("utf-8", errors="replace"))
            if body_bytes:
                # A 200 with a non-empty body but zero cards is the signature of
                # markup drift, so keep the size in the note for diagnosis.
                result.notes = (
                    f"page {page_index + 1} returned no parsable job cards "
                    f"({body_bytes} byte(s) of HTML)"
                )
            else:
                result.notes = f"page {page_index + 1} returned an empty body"
            break

        for card in cards:
            if len(result.postings) >= config.max_items:
                break
            posting = normalize_job_posting(card, source="linkedin")
            if posting.content_status is ContentStatus.EMPTY:
                continue
            if posting.fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(posting.fingerprint)
            result.postings.append(posting)

        if len(result.postings) >= config.max_items:
            break

    result.items_found = len(result.postings)
    return result


def _record_response(
    result: CollectorResult,
    response: HttpResponse | None,
    url: str,
    *,
    max_bytes: int,
) -> None:
    """Attach a raw document for a response, bounding what is kept.

    ``max_bytes`` comes from ``Settings.raw_max_bytes`` so the live path honours
    ``JOBSCOUT_RAW_MAX_BYTES`` exactly like the fixture path does. A body the
    transport already marked as truncated (it hit ``max_download_bytes``) is
    recorded as metadata only, so a partial page is never persisted verbatim.
    """
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
    )


__all__ = [
    "CollectorResult",
    "DEFAULT_PAGE_SIZE",
    "LinkedInSearchConfig",
    "build_search_url",
    "parse_job_cards",
    "plan_search_urls",
    "search_linkedin",
]
