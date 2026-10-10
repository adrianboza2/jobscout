"""Fixture-based collection.

Fixtures are the default way to exercise the whole pipeline: the same
normalization, deduplication, persistence, and scoring code runs, but no
network request is ever made. This keeps tests and local dry runs honest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..models import ContentStatus, JobPosting, RawDocumentKind
from ..normalize import extract_linkedin_job_id, normalize_postings
from ..timeutil import utcnow_iso
from .base import CollectorError, HttpClient, HttpResponse, TransportError


@dataclass
class FixturePayload:
    """Parsed fixture contents."""

    path: Path
    provider: str
    label: str | None = None
    query: str | None = None
    location: str | None = None
    fetched_at: str | None = None
    jobs: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    sha256: str = ""
    byte_size: int = 0


_JOB_LIST_KEYS = ("jobs", "results", "items", "data", "postings")


def _coerce_job_list(payload: Any, path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], {}
    if isinstance(payload, dict):
        for key in _JOB_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)], dict(payload)
        # A single job object is also valid.
        if payload.get("title") or payload.get("url"):
            return [payload], {}
        raise CollectorError(
            f"fixture {path} has no job list; expected a list or one of {_JOB_LIST_KEYS}"
        )
    raise CollectorError(f"fixture {path} must contain a JSON object or array")


def load_fixture(path: str | Path) -> FixturePayload:
    """Read and parse a JSON fixture with optional metadata fields."""
    fixture_path = Path(path)
    if not fixture_path.is_file():
        raise CollectorError(f"fixture not found: {fixture_path}")
    text = fixture_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CollectorError(f"fixture {fixture_path} is not valid JSON: {exc}") from exc

    jobs, metadata = _coerce_job_list(payload, fixture_path)
    provider = str(metadata.get("provider") or metadata.get("source") or "fixture")
    fetched_at = metadata.get("fetched_at") or metadata.get("fetchedAt")
    resolved: list[dict[str, Any]] = []
    for job in jobs:
        normalized = dict(job)
        normalized.setdefault("source", provider)
        if fetched_at and not normalized.get("fetched_at"):
            normalized["fetched_at"] = fetched_at
        resolved.append(normalized)

    return FixturePayload(
        path=fixture_path,
        provider=provider,
        label=metadata.get("label"),
        query=metadata.get("query") or metadata.get("keywords"),
        location=metadata.get("location"),
        fetched_at=fetched_at,
        jobs=resolved,
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        byte_size=len(text.encode("utf-8")),
    )


def fixture_raw_document_kind() -> RawDocumentKind:
    return RawDocumentKind.FIXTURE


def postings_from_fixture(payload: FixturePayload) -> list[JobPosting]:
    """Normalize fixture jobs, tagging incomplete records."""
    postings = normalize_postings(payload.jobs, source=payload.provider)
    for posting in postings:
        if posting.content_status is ContentStatus.EMPTY:
            posting.content_status = ContentStatus.PARTIAL
    return postings


# ---------------------------------------------------------------------------
# Job-detail fixtures (offline responses for the detail fetcher)
# ---------------------------------------------------------------------------


@dataclass
class DetailFixtureEntry:
    """One canned detail response, keyed by URL and/or LinkedIn job id."""

    job_id: str | None = None
    url: str | None = None
    status_code: int = 200
    body: str = ""
    content_type: str | None = "text/html"
    fetched_at: str | None = None
    error: str | None = None


@dataclass
class DetailFixture:
    """Parsed job-detail fixture."""

    path: Path
    entries: list[DetailFixtureEntry] = field(default_factory=list)


_DETAIL_LIST_KEYS = ("details", "responses", "results", "items", "jobs")


def load_detail_fixture(path: str | Path) -> DetailFixture:
    """Read a JSON file of canned detail responses.

    Accepts either a top-level list or an object with one of
    ``details``/``responses``/``results``/``items``/``jobs``. Each entry may
    carry ``job_id``, ``url``, ``status_code``, ``body`` (or ``body_html`` /
    ``html``), ``content_type``, ``fetched_at``, and ``error``.
    """
    fixture_path = Path(path)
    if not fixture_path.is_file():
        raise CollectorError(f"detail fixture not found: {fixture_path}")
    text = fixture_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CollectorError(
            f"detail fixture {fixture_path} is not valid JSON: {exc}"
        ) from exc

    metadata: dict[str, Any] = {}
    if isinstance(payload, list):
        raw_entries = payload
    elif isinstance(payload, dict):
        raw_entries = None
        for key in _DETAIL_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                raw_entries = value
                metadata = dict(payload)
                break
        if raw_entries is None:
            raise CollectorError(
                f"detail fixture {fixture_path} has no response list; expected one of "
                f"{_DETAIL_LIST_KEYS}"
            )
    else:
        raise CollectorError(
            f"detail fixture {fixture_path} must contain a JSON object or array"
        )

    entries: list[DetailFixtureEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        body = raw.get("body")
        if body is None:
            body = raw.get("body_html")
        if body is None:
            body = raw.get("html")
        job_id = raw.get("job_id")
        if job_id is None:
            job_id = raw.get("source_job_id")
        if job_id is None:
            job_id = raw.get("id")
        status_code = raw.get("status_code")
        if status_code is None:
            status_code = raw.get("http_status")
        if status_code is None:
            status_code = raw.get("status")
        entries.append(
            DetailFixtureEntry(
                job_id=str(job_id) if job_id is not None else None,
                url=str(raw["url"]) if raw.get("url") else None,
                status_code=int(status_code) if status_code is not None else 200,
                body=str(body or ""),
                content_type=raw.get("content_type") or "text/html",
                fetched_at=raw.get("fetched_at") or metadata.get("fetched_at"),
                error=raw.get("error"),
            )
        )

    return DetailFixture(
        path=fixture_path,
        entries=entries,
    )


class FixtureDetailTransport(HttpClient):
    """Offline detail transport backed by a fixture. Never touches the network."""

    def __init__(
        self,
        entries: Iterable[DetailFixtureEntry],
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._by_url: dict[str, DetailFixtureEntry] = {}
        self._by_job_id: dict[str, DetailFixtureEntry] = {}
        for entry in entries:
            if entry.url:
                self._by_url[entry.url] = entry
            if entry.job_id:
                self._by_job_id[str(entry.job_id)] = entry

    def _lookup(self, url: str) -> DetailFixtureEntry | None:
        entry = self._by_url.get(url)
        if entry is not None:
            return entry
        job_id = extract_linkedin_job_id(url)
        if job_id:
            return self._by_job_id.get(job_id)
        return None

    def fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        self.calls.append(
            {"url": url, "method": method, "headers": dict(headers or {}), "body": body}
        )
        entry = self._lookup(url)
        if entry is None:
            return HttpResponse(
                url=url,
                status_code=404,
                body="",
                headers={},
                fetched_at=utcnow_iso(),
            )
        if entry.error:
            raise TransportError(f"fixture entry failed: {entry.error}")
        return HttpResponse(
            url=entry.url or url,
            status_code=entry.status_code,
            body=entry.body,
            headers={"content-type": entry.content_type or "text/html"},
            fetched_at=entry.fetched_at or utcnow_iso(),
        )
