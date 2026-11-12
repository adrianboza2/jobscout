"""Ingestion pipeline: raw payloads in, deduplicated jobs out.

Both the offline fixture path and the opt-in LinkedIn path funnel through here,
so normalization, deduplication, provenance, and run accounting behave the same
way regardless of where the data came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .collectors.base import CollectorResult
from .collectors.fixtures import load_fixture, postings_from_fixture
from .collectors.linkedin_details import DetailItem, DetailResult
from .config import Settings, is_safe_to_store
from .db import (
    _sync_job_active,
    close_job_variant,
    create_search_run,
    deactivate_job,
    deactivate_job_variant,
    ensure_job_variant,
    finish_search_run,
    get_job,
    insert_detail_fetch,
    insert_raw_document,
    latest_successful_detail_fetch_source,
    record_run_item,
    resolve_variant_id,
    store_posting,
    update_job_content,
    update_job_variant_detail,
)
from .models import (
    ContentStatus,
    DetailFetchStatus,
    DuplicateKind,
    Job,
    JobDetailFetch,
    JobPosting,
    PositionKind,
    RawDocument,
    RawDocumentKind,
    SearchRun,
    SearchRunItem,
    SearchRunStatus,
)
from .normalize import content_hash
from .timeutil import to_iso, utcnow_iso


@dataclass
class IngestOutcome:
    """Accounting for one ingestion."""

    run_id: int | None = None
    status: SearchRunStatus = SearchRunStatus.COMPLETED
    items_found: int = 0
    new_jobs: int = 0
    duplicate_jobs: int = 0
    new_job_ids: list[int] = field(default_factory=list)
    matched_job_ids: list[int] = field(default_factory=list)
    raw_document_ids: list[int] = field(default_factory=list)
    notes: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": str(self.status),
            "items_found": self.items_found,
            "new_jobs": self.new_jobs,
            "duplicate_jobs": self.duplicate_jobs,
            "new_job_ids": list(self.new_job_ids),
            "matched_job_ids": list(self.matched_job_ids),
            "raw_document_ids": list(self.raw_document_ids),
            "notes": self.notes,
            "error": self.error,
        }


def persist_raw_documents(
    conn: Any, documents: Iterable[RawDocument], *, run_id: int | None = None
) -> list[int]:
    """Store raw payloads, returning their ids (reusing identical rows)."""
    ids: list[int] = []
    for document in documents:
        document.run_id = run_id if document.run_id is None else document.run_id
        ids.append(insert_raw_document(conn, document))
    return ids


def ingest_postings(
    conn: Any,
    postings: list[JobPosting],
    *,
    provider: str,
    query: str | None = None,
    location: str | None = None,
    label: str | None = None,
    params: dict[str, Any] | None = None,
    status: SearchRunStatus = SearchRunStatus.COMPLETED,
    raw_document_ids: list[int] | None = None,
    notes: str | None = None,
    error: str | None = None,
    items_found: int | None = None,
    cycle_id: str | None = None,
    position_kind: PositionKind | str | None = None,
    now: str | None = None,
) -> IngestOutcome:
    """Persist normalized postings under a new search run.

    ``position_kind`` records how ``rank_in_run`` must be read: an ordered search
    rank, or ``none`` for an inventory that has no result order. When it is
    ``none`` the stored rank is forced to ``0`` so no caller can mistake sitemap
    order for relevance.
    """
    timestamp = now or utcnow_iso()
    # ``None`` keeps the historical behavior (sequential observation order),
    # while an explicit ``none`` records "this source has no result order" and
    # forces rank 0 so sitemap order is never read as relevance.
    rank_is_meaningful = (
        position_kind is None or str(position_kind) != str(PositionKind.NONE)
    )
    run = create_search_run(
        conn,
        SearchRun(
            provider=provider,
            label=label,
            query=query,
            location=location,
            cycle_id=cycle_id,
            params=params or {},
            status=SearchRunStatus.RUNNING,
            started_at=timestamp,
            raw_document_id=(raw_document_ids or [None])[0],
        ),
    )
    outcome = IngestOutcome(run_id=run.id, status=status, raw_document_ids=list(raw_document_ids or []))

    for index, posting in enumerate(postings, start=1):
        doc_id = posting.raw_document_id
        job, decision = store_posting(conn, posting, now=timestamp, raw_document_id=doc_id)
        if job.id is None:  # pragma: no cover - defensive
            continue
        record_run_item(
            conn,
            SearchRunItem(
                run_id=int(run.id),
                job_id=job.id,
                decision=decision.kind,
                rank_in_run=index if rank_is_meaningful else 0,
                position_kind=position_kind,
                created_at=timestamp,
            ),
        )
        if decision.kind is DuplicateKind.NEW:
            outcome.new_jobs += 1
            outcome.new_job_ids.append(job.id)
        else:
            outcome.duplicate_jobs += 1
            outcome.matched_job_ids.append(job.id)

    outcome.items_found = items_found if items_found is not None else len(postings)
    finish_search_run(
        conn,
        int(run.id),
        status=status,
        pages_fetched=None,
        items_found=outcome.items_found,
        new_jobs=outcome.new_jobs,
        duplicate_jobs=outcome.duplicate_jobs,
        error=error,
        notes=notes or f"{outcome.new_jobs} new job(s), {outcome.duplicate_jobs} duplicate(s)",
    )
    outcome.notes = notes
    outcome.error = error
    return outcome


def ingest_fixture(
    conn: Any,
    path: str | Path,
    *,
    source: str = "fixture",
    label: str | None = None,
    settings: Settings | None = None,
    cycle_id: str | None = None,
    position_kind: PositionKind | str | None = None,
    now: str | None = None,
) -> IngestOutcome:
    """Ingest a JSON fixture end to end: raw document, normalize, dedupe, store."""
    payload = load_fixture(path)
    provider = source or payload.provider
    timestamp = now or utcnow_iso()

    safe, reason = is_safe_to_store(
        payload.text, max_bytes=(settings.raw_max_bytes if settings else 262_144)
    )
    document = RawDocument(
        kind=RawDocumentKind.FIXTURE,
        source=provider,
        url=payload.path.resolve().as_uri(),
        status_code=None,
        content_type="application/json",
        sha256=payload.sha256,
        byte_size=payload.byte_size,
        fetched_at=payload.fetched_at or _file_mtime_iso(payload.path),
        stored_at=timestamp,
        truncated=not safe,
        notes=None if safe else f"fixture body not stored: {reason}",
        body=payload.text if safe else None,
    )
    raw_ids = persist_raw_documents(conn, [document])

    postings = postings_from_fixture(payload)
    for posting in postings:
        posting.raw_document_id = raw_ids[0] if raw_ids else None

    outcome = ingest_postings(
        conn,
        postings,
        provider=provider,
        query=payload.query,
        location=payload.location,
        label=label or payload.label or f"fixture:{payload.path.name}",
        params={"fixture_path": str(payload.path), "fixture_sha256": payload.sha256},
        status=SearchRunStatus.COMPLETED,
        raw_document_ids=raw_ids,
        notes=f"fixture {payload.path.name}: {len(postings)} posting(s) normalized",
        cycle_id=cycle_id,
        position_kind=position_kind,
        now=timestamp,
    )
    return outcome


def _default_run_params(config: Any, result: CollectorResult) -> dict[str, Any]:
    """Build run ``params`` for a collector config, without provider coupling.

    A config may expose ``to_run_params(result)`` for exact, adapter-specific
    provenance. Otherwise the common collector fields are read defensively, so a
    new adapter that only exposes ``keywords``/``max_pages`` still records a
    usable run.
    """
    builder = getattr(config, "to_run_params", None)
    if callable(builder):
        return dict(builder(result))
    params: dict[str, Any] = {
        key: getattr(config, key, None)
        for key in ("keywords", "location", "max_pages", "max_items", "page_size")
    }
    params["planned_urls"] = list(result.planned_urls)
    params["network_enabled"] = result.status is not SearchRunStatus.DRY_RUN
    return params


def ingest_collector_result(
    conn: Any,
    result: CollectorResult,
    *,
    config: Any | None = None,
    settings: Settings | None = None,
    provider: str | None = None,
    query: str | None = None,
    location: str | None = None,
    params: dict[str, Any] | None = None,
    label: str | None = None,
    cycle_id: str | None = None,
    position_kind: PositionKind | str | None = None,
    now: str | None = None,
) -> IngestOutcome:
    """Persist any provider's collector result, including refusals and dry runs.

    The provider is no longer hardcoded: it is taken from the explicit argument,
    then the config's ``provider`` attribute, and only then defaults to
    ``linkedin`` so existing callers keep working unchanged. ``query`` and
    ``location`` follow the same precedence, and a config that exposes
    ``to_run_params`` keeps its exact historical run ``params``.
    """
    timestamp = now or utcnow_iso()
    resolved_provider = provider or getattr(config, "provider", None) or "linkedin"
    resolved_query = query if query is not None else getattr(config, "keywords", None)
    resolved_location = (
        location if location is not None else getattr(config, "location", None)
    )
    resolved_params = (
        params if params is not None else _default_run_params(config, result)
    )
    resolved_position = (
        position_kind if position_kind is not None else getattr(config, "position_kind", None)
    )
    if label is None:
        label = (
            f"{resolved_provider}:{resolved_query}"
            if resolved_query
            else f"{resolved_provider}:inventory"
        )

    raw_ids = persist_raw_documents(conn, result.raw_documents)
    for posting in result.postings:
        posting.raw_document_id = raw_ids[0] if raw_ids else None

    outcome = ingest_postings(
        conn,
        result.postings,
        provider=resolved_provider,
        query=resolved_query,
        location=resolved_location,
        label=label,
        params=resolved_params,
        status=result.status,
        raw_document_ids=raw_ids,
        notes=result.notes,
        error=result.error,
        items_found=result.items_found or len(result.postings),
        cycle_id=cycle_id,
        position_kind=resolved_position,
        now=timestamp,
    )
    if result.pages_fetched:
        with conn:
            conn.execute(
                "UPDATE search_runs SET pages_fetched = ? WHERE id = ?",
                (int(result.pages_fetched), int(outcome.run_id or 0)),
            )
    return outcome


@dataclass
class DetailIngestOutcome:
    """Accounting for one detail-enrichment run."""

    run_id: int | None = None
    status: SearchRunStatus = SearchRunStatus.COMPLETED
    requested: int = 0
    enriched: int = 0
    partial: int = 0
    skipped: int = 0
    failed: int = 0
    changed: int = 0
    preserved: int = 0
    deactivated: int = 0
    job_ids: list[int] = field(default_factory=list)
    raw_document_ids: list[int] = field(default_factory=list)
    notes: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": str(self.status),
            "requested": self.requested,
            "enriched": self.enriched,
            "partial": self.partial,
            "skipped": self.skipped,
            "failed": self.failed,
            "changed": self.changed,
            "preserved": self.preserved,
            "deactivated": self.deactivated,
            "job_ids": list(self.job_ids),
            "raw_document_ids": list(self.raw_document_ids),
            "notes": self.notes,
            "error": self.error,
        }


def _description_content_hash(job: Job, description: str) -> str:
    """Recompute a job's content hash exactly like :func:`finalize_posting`."""
    return content_hash(
        "\n".join(
            [
                job.title or "",
                job.company or "",
                job.location or "",
                description,
            ]
        )
    )


def _foreign_detail_provenance(
    job: Job, provider: str, prior_successful_source: str | None
) -> bool:
    """True when the stored full description is known to come from another provider.

    Cross-provider contamination (DP-1) left some offers — today, GetManfred ones
    — carrying a description fetched inside another provider's id space. When this
    fetch's provider is the offer's own provider, and the latest *successful*
    provenance row came from a different provider, a successful same-provider
    fetch is allowed to correct the stored text even when it is shorter. Every
    other combination keeps the ordinary preservation policy: an unknown prior
    source, a same-provider prior success, or a fetch from a provider other than
    the offer's own all preserve instead.
    """
    job_source = str(getattr(job, "source", "") or "")
    if not job_source or str(provider) != job_source:
        return False
    prior = str(prior_successful_source or "")
    if not prior:
        return False
    return prior != job_source


def _merge_detail_description(
    job: Job,
    item: DetailItem,
    *,
    provider: str = "linkedin",
    prior_successful_source: str | None = None,
) -> tuple[str, ContentStatus, str]:
    """Decide the description to persist for one detail result.

    Returns ``(description, content_status, produced_hash)``. ``produced_hash``
    is non-empty **only** when this attempt produced the description that should
    be stored; when the attempt is preserved/failed/skipped it is empty, so the
    provenance row never claims a hash this fetch did not create.

    Invariant: a partial or failed fetch never replaces a previously successful
    full description. A successful fetch replaces a shorter/partial one, except
    in the single source-mismatch case (see :func:`_foreign_detail_provenance`):
    when the stored full description is known to have come from a different
    provider than the offer's own, a successful same-provider fetch corrects it
    regardless of length.
    """
    existing_description = job.description or ""
    existing_status = job.content_status
    existing_full = (
        existing_status is ContentStatus.OK and bool(existing_description.strip())
    )
    new_description = item.description or ""

    if item.status is DetailFetchStatus.OK and new_description:
        if (
            existing_full
            and len(new_description) <= len(existing_description)
            and not _foreign_detail_provenance(
                job, provider, prior_successful_source
            )
        ):
            return existing_description, existing_status, ""
        return (
            new_description,
            ContentStatus.OK,
            _description_content_hash(job, new_description),
        )

    if item.status is DetailFetchStatus.PARTIAL and new_description:
        if existing_full:
            return existing_description, existing_status, ""
        if len(new_description) > len(existing_description):
            return (
                new_description,
                ContentStatus.PARTIAL,
                _description_content_hash(job, new_description),
            )

    return existing_description, existing_status, ""


def _detail_extra_fields(item: Any) -> dict[str, Any]:
    """Structured fields a provider detail item contributes to the stored job.

    The LinkedIn detail fetcher exposes ``salary`` when the description states a
    euro pay range, so it is propagated like the other providers' fields.
    The Tecnoempleo adapter exposes ``company``/``location``/``posted_at`` and a
    ``technologies`` list; the GetManfred adapter additionally exposes
    ``employment_type`` and ``salary``. Only non-empty values are propagated, and
    a field the existing row already carries is never overwritten with an empty
    value.
    """
    fields: dict[str, Any] = {}
    for name in ("company", "location", "posted_at", "employment_type", "salary"):
        value = getattr(item, name, None)
        if value:
            fields[name] = value
    technologies = getattr(item, "technologies", None)
    if technologies:
        fields["extra"] = {"technologies": list(technologies)}
    return fields


def ingest_detail_results(
    conn: Any,
    result: DetailResult,
    *,
    settings: Settings,
    label: str | None = None,
    now: str | None = None,
    provider: str | None = None,
) -> DetailIngestOutcome:
    """Persist a detail-fetch result: provenance rows plus job enrichment.

    ``provider`` is the platform slug the detail run belongs to. It defaults to
    ``linkedin`` so every existing caller keeps its exact historical behaviour,
    while an adapter such as Tecnoempleo can record ``tecnoempleo-details`` and
    a per-item ``source`` on the provenance row.
    """
    timestamp = now or utcnow_iso()
    provider = str(provider or "linkedin")
    detail_provider = f"{provider}-details"
    run = create_search_run(
        conn,
        SearchRun(
            provider=detail_provider,
            label=label or f"{provider}:job-details",
            query="job-details",
            params={
                "requested": result.requested,
                "requests_made": result.requests_made,
                "planned_urls": list(result.planned_urls),
                "truncated": result.truncated,
            },
            status=SearchRunStatus.RUNNING,
            started_at=timestamp,
        ),
    )
    raw_ids = persist_raw_documents(conn, result.raw_documents, run_id=run.id)
    outcome = DetailIngestOutcome(
        run_id=run.id,
        status=result.status,
        requested=result.requested,
        raw_document_ids=raw_ids,
        notes=result.notes,
        error=result.error,
    )

    for item in result.items:
        if item.raw_document_index is not None and item.raw_document_index < len(raw_ids):
            item.raw_document_id = raw_ids[item.raw_document_index]

        job = get_job(conn, item.job_id)
        if job is None:
            outcome.failed += 1
            outcome.notes = (outcome.notes or "") + f" job #{item.job_id} disappeared"
            continue

        # Resolve the provider variant this attempt belongs to, creating it from
        # the item's own identity if the job predates variants. This is the
        # provider-scoped row that detail/availability provenance is attached to;
        # the canonical job stays the deduplicated parent.
        variant_id = resolve_variant_id(
            conn,
            job_id=int(job.id),
            source=provider,
            source_job_id=item.source_job_id,
            url=item.url,
        )
        if variant_id is None:
            variant_id = ensure_job_variant(
                conn,
                int(job.id),
                source=provider,
                source_job_id=item.source_job_id,
                url=item.url,
                timestamp=timestamp,
                raw_document_id=item.raw_document_id,
            )

        description, content_status, produced_hash = _merge_detail_description(
            job,
            item,
            provider=provider,
            prior_successful_source=latest_successful_detail_fetch_source(
                conn, int(job.id)
            ),
        )
        extra_fields = _detail_extra_fields(item)
        changed = (
            description != (job.description or "")
            or str(content_status) != str(job.content_status)
            or bool(extra_fields)
        )
        if changed:
            update_job_content(
                conn,
                int(job.id),
                description=description or None,
                content_status=content_status,
                content_hash=produced_hash or job.content_hash,
                **extra_fields,
            )
            outcome.changed += 1

        if item.status is DetailFetchStatus.OK:
            variant_content_status: ContentStatus | None = ContentStatus.OK
        elif item.status is DetailFetchStatus.PARTIAL and item.description:
            variant_content_status = ContentStatus.PARTIAL
        else:
            variant_content_status = None
        update_job_variant_detail(
            conn,
            variant_id,
            status=str(item.status),
            content_status=variant_content_status,
            content_hash=produced_hash or None,
            description_chars=len(item.description or ""),
            fetched_at=item.fetched_at,
            last_seen_at=timestamp,
        )

        insert_detail_fetch(
            conn,
            JobDetailFetch(
                job_id=int(job.id),
                run_id=run.id,
                source=provider,
                url=item.url,
                # Carry the already-sanitized redirect target through to
                # provenance; ``None`` means no redirect was observed.
                final_url=item.final_url,
                status=item.status,
                http_status=item.http_status,
                content_status=str(content_status),
                description_chars=len(item.description or ""),
                byte_size=item.byte_size,
                sha256=item.sha256,
                # Only record a hash this attempt actually produced. A failed or
                # preserved attempt leaves it NULL instead of borrowing the
                # job's existing hash.
                content_hash=produced_hash or None,
                fetched_at=item.fetched_at,
                stored_at=timestamp,
                raw_document_id=item.raw_document_id,
                variant_id=variant_id,
                error=item.error,
                notes=item.notes,
            ),
        )

        # A definitive 404/410, or a page that says the offer is closed, proves
        # this provider's listing is gone: deactivate the variant. The canonical
        # job stays active while any other provider variant is still active, so
        # one provider's disappearance does not hide a live listing elsewhere.
        if _listing_gone(item):
            closed = bool(getattr(item, "closed", False))
            if deactivate_listing(conn, int(job.id), variant_id, closed=closed):
                outcome.deactivated += 1

        if item.status is DetailFetchStatus.OK:
            outcome.enriched += 1
            outcome.job_ids.append(int(job.id))
            if not changed:
                outcome.preserved += 1
        elif item.status is DetailFetchStatus.PARTIAL:
            outcome.partial += 1
            outcome.job_ids.append(int(job.id))
        elif item.status is DetailFetchStatus.SKIPPED:
            outcome.skipped += 1
        else:
            outcome.failed += 1

    finish_search_run(
        conn,
        int(run.id),
        status=result.status,
        items_found=result.requested,
        error=result.error,
        notes=(
            f"{outcome.enriched} enriched, {outcome.partial} partial, "
            f"{outcome.skipped} skipped, {outcome.failed} failed"
        ),
    )
    return outcome


def _listing_gone(item: Any) -> bool:
    """A FAILED 404/410, or a page the adapter recognised as a closed offer."""
    return bool(item.is_definitive_missing or getattr(item, "closed", False))


def deactivate_listing(
    conn: Any, job_id: int, variant_id: int | None, *, closed: bool = False
) -> bool:
    """Deactivate one provider listing; True when the canonical job turned inactive.

    ``closed`` marks the variant so a re-sighting cannot revive it; a 404/410
    stays refutable by a re-sighting, as before.
    """
    if variant_id is not None:
        (close_job_variant if closed else deactivate_job_variant)(conn, variant_id)
        return _sync_job_active(conn, job_id)
    return deactivate_job(conn, job_id)


def closed_listings(conn: Any) -> list[dict[str, Any]]:
    """Active listings whose last stored detail page says the offer is closed.

    Offline backfill for pages fetched before the detail adapters recognised a
    closed offer. Each active LinkedIn/GetManfred variant is judged on its most
    recent stored page with the adapter's own parser; a variant with no stored
    page, or a truncated one without the marker, is never selected. A fetch from
    before variants existed is only attributed when the provider has a single
    variant, so an old closed page never condemns a live repost.
    """
    from .collectors.getmanfred_details import parse_detail_closed as getmanfred_closed
    from .collectors.linkedin_details import parse_detail_closed as linkedin_closed

    parsers = {"linkedin": linkedin_closed, "getmanfred": getmanfred_closed}
    rows = conn.execute(
        """
        SELECT j.id AS job_id, j.title, j.company, v.id AS variant_id, v.source,
               (SELECT f.id FROM job_detail_fetches f
                 JOIN raw_documents r ON r.id = f.raw_document_id
                WHERE f.job_id = j.id AND f.source = v.source
                  AND (f.variant_id = v.id
                       OR (f.variant_id IS NULL AND (
                           SELECT COUNT(*) FROM job_variants o
                           WHERE o.job_id = j.id AND o.source = v.source) = 1))
                  AND r.body IS NOT NULL
                ORDER BY f.id DESC LIMIT 1) AS fetch_id
        FROM jobs j JOIN job_variants v ON v.job_id = j.id
        WHERE j.is_active = 1 AND v.is_active = 1 AND v.source IN ('linkedin', 'getmanfred')
        ORDER BY j.id, v.id
        """
    ).fetchall()
    selected: list[dict[str, Any]] = []
    for row in rows:
        if row["fetch_id"] is None:
            continue
        body = conn.execute(
            "SELECT r.body FROM job_detail_fetches f JOIN raw_documents r "
            "ON r.id = f.raw_document_id WHERE f.id = ?",
            (int(row["fetch_id"]),),
        ).fetchone()["body"]
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        if parsers[row["source"]](body or ""):
            selected.append(
                {
                    "job_id": int(row["job_id"]),
                    "variant_id": int(row["variant_id"]),
                    "source": row["source"],
                    "title": row["title"],
                    "company": row["company"],
                    "detail_fetch_id": int(row["fetch_id"]),
                }
            )
    return selected


def _file_mtime_iso(path: Path) -> str | None:
    from datetime import datetime, timezone

    try:
        return to_iso(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
    except OSError:  # pragma: no cover - filesystem dependent
        return None


__all__ = [
    "DetailIngestOutcome",
    "closed_listings",
    "deactivate_listing",
    "IngestOutcome",
    "ingest_collector_result",
    "ingest_detail_results",
    "ingest_fixture",
    "ingest_postings",
    "persist_raw_documents",
]
