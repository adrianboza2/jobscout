"""Job-detail fetcher tests: parsing, opt-in gating, persistence, and bounds.

No real LinkedIn request is ever made. Live-looking paths use the fake
transport from ``conftest``; the CLI smoke path uses the offline ``--fixture``
transport. Both are asserted to run under a socket blocker.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeHttpClient, FIXTURES, fixture_text, no_network, response

from jobscout.cli import EXIT_ERROR, EXIT_FAIL_CLOSED, EXIT_OK, main
from jobscout.collectors.base import CollectorDisabledError, TRUNCATION_HEADER
from jobscout.collectors.fixtures import FixtureDetailTransport, load_detail_fixture
from jobscout.collectors.linkedin_details import (
    DESCRIPTION_CLASS_MARKERS,
    DetailFetchConfig,
    DetailItem,
    DetailResult,
    build_detail_url,
    fetch_linkedin_details,
    parse_detail_description,
    plan_detail_targets,
    resolve_source_job_id,
    sanitize_final_url,
)
from jobscout.config import HARD_MAX_ITEMS, HARD_MAX_PAGES
from jobscout.db import (
    DETAIL_ATTEMPTS_BEFORE_SCORING,
    all_active_jobs,
    connect,
    count_jobs_needing_refresh,
    count_variants_unseen_since,
    deactivate_job,
    detail_fetches_for_job,
    get_job,
    init_db,
    insert_detail_fetch,
    insert_evaluation,
    insert_raw_document,
    job_detail_fetch_count,
    jobs_awaiting_details,
    jobs_needing_details,
    jobs_needing_refresh,
    latest_detail_fetch,
    latest_evaluation,
    list_jobs,
    store_posting,
    update_job_content,
    variants_unseen_since,
)
from jobscout.models import (
    ContentStatus,
    DetailFetchStatus,
    DuplicateKind,
    Evaluation,
    JobDetailFetch,
    RawDocument,
    RawDocumentKind,
    Recommendation,
    ScorerName,
    SearchRunStatus,
)
from jobscout.normalize import normalize_job_posting
from jobscout.pipeline import ingest_detail_results
from jobscout.timeutil import iso_days_ago, to_iso, utcnow

CARDS_FIXTURE = FIXTURES / "linkedin_search_cards_sample.json"
DETAIL_RESPONSES = FIXTURES / "linkedin_detail_responses.json"
DETAIL_PAGE = FIXTURES / "linkedin_detail_page.html"
PROFILE = FIXTURES / "profile_test.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _card_payload(source_job_id: str, **overrides) -> dict:
    payload = {
        "source": "linkedin",
        "source_job_id": source_job_id,
        "jobUrl": f"https://www.linkedin.com/jobs/view/{source_job_id}/role",
        "jobTitle": "Senior Backend Engineer",
        "companyName": "Acme Data Inc.",
        # Unique per job id so the weak dedupe key does not merge distinct cards.
        "location": f"Remote, Argentina ({source_job_id})",
    }
    payload.update(overrides)
    return payload


def _store_card(conn, source_job_id: str, **overrides):
    posting = normalize_job_posting(
        _card_payload(source_job_id, **overrides), source="linkedin"
    )
    job, _ = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    return job


def _store_foreign_card(conn, source: str, source_job_id: str, *, url: str):
    """Store a non-LinkedIn offer so LinkedIn-scoped selection can be tested."""
    posting = normalize_job_posting(
        {
            "source": source,
            "source_job_id": source_job_id,
            "jobUrl": url,
            "jobTitle": f"{source} platform engineer",
            "companyName": "Foreign Co",
            "location": f"Remote ({source})",
        },
        source=source,
    )
    job, _ = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    return job


def _detail(job, **overrides) -> DetailItem:
    payload = {
        "job_id": int(job.id),
        "source_job_id": job.source_job_id,
        "url": build_detail_url(
            "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
            job.source_job_id or "0",
        ),
    }
    payload.update(overrides)
    return DetailItem(**payload)


# ---------------------------------------------------------------------------
# HTML parsing (pure)
# ---------------------------------------------------------------------------


def test_parse_detail_description_extracts_known_container() -> None:
    text = parse_detail_description(fixture_text("linkedin_detail_page.html"))
    assert "Senior Backend Engineer" in text
    assert "Strong Python and SQL required" in text
    assert "PostgreSQL modelling experience" in text
    assert "Remote friendly team with English communication." in text
    # Content outside the description container and markup itself are dropped.
    assert "Show more" not in text
    assert "<" not in text


def test_parse_detail_description_ignores_unknown_markup() -> None:
    assert parse_detail_description("") == ""
    assert parse_detail_description("<html><body><p>Sign in</p></body></html>") == ""
    assert parse_detail_description("<div class='other'>not a description</div>") == ""


def test_parse_detail_description_survives_truncated_html() -> None:
    broken = (
        '<div class="show-more-less-html__markup"><p>Half a description that is long enough'
    )
    assert "Half a description" in parse_detail_description(broken)


@pytest.mark.parametrize(
    "classes",
    (
        "not-a-description__text",
        "description__textual",
        "pre-jobs-description__content",
        "job-details-module-header",
        "show-more-less-html__markupish",
    ),
)
def test_parse_detail_description_rejects_substring_lookalikes(classes: str) -> None:
    # A class that merely *contains* a known marker must not be treated as a
    # description container. Regression for substring false positives.
    html = (
        f'<div class="{classes}">'
        "<p>A paragraph long enough to look like a description.</p></div>"
    )
    assert parse_detail_description(html) == ""


@pytest.mark.parametrize(
    "classes",
    (
        "description__text",
        "description__text description__text--rich",
        "show-more-less-html__markup",
        "show-more-less-html__markup--clamp-after-5",
        "jobs-description__content",
        "job-details-module",
    ),
)
def test_parse_detail_description_keeps_supported_linkedin_classes(classes: str) -> None:
    # The supported class list and its BEM modifier forms must keep working,
    # including a bare modifier class as emitted by LinkedIn.
    html = (
        f'<div class="{classes}">'
        "<p>A paragraph long enough to look like a description.</p></div>"
    )
    assert "A paragraph long enough" in parse_detail_description(html)


@pytest.mark.parametrize("marker", DESCRIPTION_CLASS_MARKERS)
def test_parse_detail_description_rejects_empty_bem_modifier(marker: str) -> None:
    # A BEM modifier must carry a name. The bare "marker--" form (empty
    # modifier) is not a real class and must not match the marker itself.
    html = (
        f'<div class="{marker}--">'
        "<p>A paragraph long enough to look like a description.</p></div>"
    )
    assert parse_detail_description(html) == ""


def test_build_detail_url_supports_templates_and_base_urls() -> None:
    assert (
        build_detail_url("https://example.test/jobPosting/{job_id}", "123")
        == "https://example.test/jobPosting/123"
    )
    assert (
        build_detail_url("https://example.test/jobPosting/{id}", "456")
        == "https://example.test/jobPosting/456"
    )
    assert (
        build_detail_url("https://example.test/jobPosting", "789")
        == "https://example.test/jobPosting/789"
    )


# ---------------------------------------------------------------------------
# Source job id resolution
# ---------------------------------------------------------------------------

#: The real LinkedIn URL shape seen in stored data: the numeric id is a suffix
#: of a slug, so ``extract_linkedin_job_id`` does not match it.
SLUG_DETAIL_URL = (
    "https://es.linkedin.com/jobs/view/azure-cloud-engineer-at-kyndryl-4457430073"
)


def _job_stub(source_job_id, url):
    return SimpleNamespace(source_job_id=source_job_id, url=url)


def test_resolve_source_job_id_accepts_numeric_source_id_directly() -> None:
    # Regression: a stored digit-only source_job_id must be accepted even when
    # its URL is the slug form that URL extraction cannot parse. Before the fix
    # this returned None and the job was skipped.
    assert resolve_source_job_id(_job_stub("4457430073", SLUG_DETAIL_URL)) == "4457430073"
    assert resolve_source_job_id(_job_stub("4457430073", None)) == "4457430073"
    # An integer-valued source id is coerced through the same normalization.
    assert resolve_source_job_id(_job_stub(4457430073, None)) == "4457430073"


def test_resolve_source_job_id_falls_back_to_url_and_urn() -> None:
    assert (
        resolve_source_job_id(
            _job_stub(None, "https://www.linkedin.com/jobs/view/3911111111/role")
        )
        == "3911111111"
    )
    # A non-numeric source_job_id that is itself a URL or URN still extracts.
    assert resolve_source_job_id(_job_stub("urn:li:jobPosting:3911111111", None)) == "3911111111"
    assert (
        resolve_source_job_id(_job_stub("", "https://x.test/?currentJobId=3912222222"))
        == "3912222222"
    )


@pytest.mark.parametrize(
    "malformed",
    (
        "no-linkedin-id",
        "4457430073abc",
        "44",
        "4457 430073",
        "4457430073/extra",
        "",
        None,
    ),
)
def test_resolve_source_job_id_rejects_malformed_ids(malformed) -> None:
    # A malformed id is never returned verbatim, and a URL that carries no id
    # leaves nothing to resolve.
    job = _job_stub(malformed, "https://es.linkedin.com/jobs/view/role-at-acme")
    assert resolve_source_job_id(job) is None


def test_resolve_source_job_id_prefers_url_when_source_id_is_malformed() -> None:
    # A malformed source id must not block a valid URL fallback.
    job = _job_stub("4457430073abc", "https://www.linkedin.com/jobs/view/3911111111/role")
    assert resolve_source_job_id(job) == "3911111111"


def test_resolve_source_job_id_refuses_a_present_non_linkedin_source() -> None:
    # A GetManfred/Tecnoempleo digit id must never be read inside LinkedIn's
    # numeric id space, whether it arrives as a numeric source id or a URL.
    getmanfred = SimpleNamespace(
        source="getmanfred",
        source_job_id="8383",
        url="https://www.getmanfred.com/ofertas-empleo/backend-8383",
    )
    tecnoempleo = SimpleNamespace(
        source="tecnoempleo",
        source_job_id="205650",
        url="https://www.tecnoempleo.com/aceptacorp/re-205650",
    )
    assert resolve_source_job_id(getmanfred) is None
    assert resolve_source_job_id(tecnoempleo) is None
    # A LinkedIn source still resolves the slug/numeric-id case.
    assert (
        resolve_source_job_id(
            SimpleNamespace(
                source="linkedin", source_job_id="4457430073", url=SLUG_DETAIL_URL
            )
        )
        == "4457430073"
    )


def test_resolve_source_job_id_keeps_stub_compatibility_without_source() -> None:
    # Stubs and callers that predate the provider column keep working: only a
    # *present* non-LinkedIn source is refused.
    assert resolve_source_job_id(_job_stub("4457430073", SLUG_DETAIL_URL)) == "4457430073"
    assert (
        resolve_source_job_id(
            SimpleNamespace(source=None, source_job_id="4457430073", url=SLUG_DETAIL_URL)
        )
        == "4457430073"
    )


# ---------------------------------------------------------------------------
# Planning and bounds
# ---------------------------------------------------------------------------


def test_plan_detail_targets_resolves_numeric_id_from_slug_url(conn, settings) -> None:
    # End-to-end planning regression for the live failure: a numeric source id
    # whose URL is the slug form must be planned, not skipped.
    job = _store_card(conn, "4457430073", jobUrl=SLUG_DETAIL_URL)
    targets, truncated = plan_detail_targets([job], settings=settings, max_items=20)

    assert truncated == 0
    assert targets[0].source_job_id == "4457430073"
    assert targets[0].url == build_detail_url(settings.linkedin_detail_url, "4457430073")
    assert targets[0].reason is None


def test_plan_detail_targets_bounds_and_skips(conn, settings) -> None:
    first = _store_card(conn, "3911111111")
    second = _store_card(conn, "3912222222")
    unidentifiable = _store_card(conn, "no-linkedin-id", jobTitle="Mystery Role")

    targets, truncated = plan_detail_targets(
        [first, second, unidentifiable], settings=settings, max_items=3
    )
    assert truncated == 0
    assert [target.source_job_id for target in targets] == [
        "3911111111",
        "3912222222",
        None,
    ]
    assert targets[0].url == build_detail_url(settings.linkedin_detail_url, "3911111111")
    assert targets[2].url is None and targets[2].reason

    bounded, dropped = plan_detail_targets(
        [first, second, unidentifiable], settings=settings, max_items=1
    )
    assert len(bounded) == 1
    assert dropped == 2


def test_detail_config_enforces_absolute_hard_caps() -> None:
    assert DetailFetchConfig(max_items=HARD_MAX_ITEMS).max_items == HARD_MAX_ITEMS
    with pytest.raises(ValueError):
        DetailFetchConfig(max_items=HARD_MAX_ITEMS + 1)
    with pytest.raises(ValueError):
        DetailFetchConfig(max_items=0)


def test_jobs_needing_details_selects_only_un_enriched(conn) -> None:
    rich = _store_card(conn, "3911111111", description="Already complete description.")
    card = _store_card(conn, "3912222222")

    assert rich.content_status is ContentStatus.OK
    assert card.content_status is ContentStatus.PARTIAL
    assert [job.id for job in jobs_needing_details(conn)] == [card.id]
    assert [job.id for job in jobs_needing_details(conn, limit=1)] == [card.id]
    assert [job.id for job in jobs_needing_details(conn, job_ids=[rich.id])] == []
    assert [job.id for job in all_active_jobs(conn)] == [rich.id, card.id]


def test_jobs_awaiting_details_waits_until_attempts_run_out(conn) -> None:
    """A card is held back until its real failures reach the threshold."""
    card = _store_card(conn, "3912222222")
    assert jobs_awaiting_details(conn) == {int(card.id)}

    # ``skipped`` rows mean the request never happened, so they never count.
    _insert_fetch(conn, card, status=DetailFetchStatus.SKIPPED)
    _insert_fetch(conn, card, status=DetailFetchStatus.SKIPPED)
    assert jobs_awaiting_details(conn) == {int(card.id)}

    # A 404/410 is definitive (it deactivates the offer), not a retryable failure.
    _insert_fetch(conn, card, status=DetailFetchStatus.FAILED, http_status=404)
    _insert_fetch(conn, card, status=DetailFetchStatus.FAILED, http_status=410)
    assert jobs_awaiting_details(conn) == {int(card.id)}

    # Two real failures (including a NULL status, as a timeout writes) release it.
    for _ in range(DETAIL_ATTEMPTS_BEFORE_SCORING):
        _insert_fetch(conn, card, status=DetailFetchStatus.FAILED, http_status=403)
    assert jobs_awaiting_details(conn) == set()


def test_jobs_awaiting_details_uses_a_null_http_status_as_a_failure(conn) -> None:
    """A 403/429/timeout stores ``failed`` with no status; it must still count."""
    card = _store_card(conn, "3913333333")
    for _ in range(DETAIL_ATTEMPTS_BEFORE_SCORING):
        _insert_fetch(conn, card, status=DetailFetchStatus.FAILED)
    assert jobs_awaiting_details(conn) == set()


def test_jobs_awaiting_details_ignores_offers_with_description(conn) -> None:
    rich = _store_card(conn, "3914444444", description="Already complete description.")
    assert jobs_awaiting_details(conn) == set()
    assert rich.id not in jobs_awaiting_details(conn)


# ---------------------------------------------------------------------------
# fetch-details selection is LinkedIn-only (all four routes)
# ---------------------------------------------------------------------------


def _detail_selection_args(*, job_id=None, all=False, refresh=False):
    return SimpleNamespace(job_id=job_id, all=all, refresh=refresh)


def test_detail_selection_is_linkedin_only(conn, settings) -> None:
    from jobscout.cli import _select_detail_jobs

    linkedin_job = _store_card(conn, "3911111111")
    getmanfred_job = _store_foreign_card(
        conn,
        "getmanfred",
        "8383",
        url="https://www.getmanfred.com/ofertas-empleo/backend-8383",
    )
    tecnoempleo_job = _store_foreign_card(
        conn,
        "tecnoempleo",
        "205650",
        url="https://www.tecnoempleo.com/aceptacorp/re-205650",
    )

    # Default route: only the LinkedIn card is selected.
    default_jobs, _ = _select_detail_jobs(
        conn, _detail_selection_args(), limit=50
    )
    assert [job.id for job in default_jobs] == [linkedin_job.id]

    # --all route: a GetManfred or Tecnoempleo offer is never selected.
    all_jobs, _ = _select_detail_jobs(
        conn, _detail_selection_args(all=True), limit=50
    )
    assert {job.id for job in all_jobs} == {linkedin_job.id}

    # --job-id route: a foreign id is dropped even when explicitly requested.
    id_jobs, _ = _select_detail_jobs(
        conn,
        _detail_selection_args(job_id=[getmanfred_job.id, linkedin_job.id]),
        limit=50,
    )
    assert [job.id for job in id_jobs] == [linkedin_job.id]

    # Foreign offers remain active and un-enriched in the database.
    assert get_job(conn, tecnoempleo_job.id).is_active
    assert get_job(conn, getmanfred_job.id).is_active


def test_detail_refresh_selection_is_linkedin_only(conn, settings) -> None:
    from jobscout.cli import _select_detail_jobs

    linkedin_job = _store_card(conn, "3911111111")
    getmanfred_job = _store_foreign_card(
        conn,
        "getmanfred",
        "8383",
        url="https://www.getmanfred.com/ofertas-empleo/backend-8383",
    )
    for job in (linkedin_job, getmanfred_job):
        _insert_fetch(
            conn,
            job,
            status=DetailFetchStatus.OK,
            fetched_at="2026-07-01T00:00:00Z",
        )

    jobs, _ = _select_detail_jobs(
        conn,
        _detail_selection_args(refresh=True),
        limit=50,
        refresh_cutoff=STALE_CUTOFF,
    )
    assert [job.id for job in jobs] == [linkedin_job.id]


def test_plan_targets_of_a_mixed_selection_contains_only_linkedin(conn, settings) -> None:
    linkedin_job = _store_card(conn, "3911111111")
    getmanfred_job = _store_foreign_card(
        conn,
        "getmanfred",
        "8383",
        url="https://www.getmanfred.com/ofertas-empleo/backend-8383",
    )

    targets, _ = plan_detail_targets(
        [linkedin_job, getmanfred_job], settings=settings, max_items=10
    )
    assert targets[0].url is not None
    assert targets[1].url is None
    assert targets[1].reason
    assert all(target.url is None or "linkedin.com" in target.url for target in targets)


# ---------------------------------------------------------------------------
# Refresh selection: stale and retryable fetch evidence
# ---------------------------------------------------------------------------


#: A fetch before this instant is "stale" for the deterministic DB tests below.
STALE_CUTOFF = "2026-09-01T00:00:00Z"


def _insert_fetch(
    conn,
    job,
    *,
    status: DetailFetchStatus,
    http_status: int | None = None,
    fetched_at: str | None = None,
    stored_at: str = "2026-09-15T12:00:00Z",
    raw_document_id: int | None = None,
    source: str = "linkedin",
) -> None:
    """Persist one detail-fetch attempt with explicit timestamps/provenance."""
    insert_detail_fetch(
        conn,
        JobDetailFetch(
            job_id=int(job.id),
            source=source,
            url=f"https://www.linkedin.com/jobs/view/{job.source_job_id}/role",
            status=status,
            http_status=http_status,
            content_status="ok" if status is DetailFetchStatus.OK else None,
            fetched_at=fetched_at,
            stored_at=stored_at,
            raw_document_id=raw_document_id,
        ),
    )


def _resight(conn, source_job_id: str, *, now: str) -> None:
    """Re-observe a posting through the normal pipeline path (reactivates it)."""
    posting = normalize_job_posting(_card_payload(source_job_id), source="linkedin")
    store_posting(conn, posting, now=now)


def test_iso_days_ago_is_pure_and_deterministic() -> None:
    reference = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    assert iso_days_ago(0, now=reference) == "2026-09-15T12:00:00Z"
    assert iso_days_ago(1, now=reference) == "2026-09-14T12:00:00Z"
    assert iso_days_ago(30, now=reference) == "2026-08-16T12:00:00Z"
    # Half a day is 12 hours; the result is truncated to whole seconds.
    assert iso_days_ago(0.5, now=datetime(2026, 9, 15, 12, 0, 30)) == "2026-09-15T00:00:30Z"


def test_jobs_needing_refresh_selects_stale_and_retryable_evidence(conn) -> None:
    stale = _store_card(conn, "3911111111")
    fresh = _store_card(conn, "3912222222")
    transient = _store_card(conn, "3913333333")
    no_http_but_raw = _store_card(conn, "3914444444")
    http_recorded = _store_card(conn, "3915555555")
    skipped = _store_card(conn, "3916666666")
    no_fetch = _store_card(conn, "3917777777")

    _insert_fetch(
        conn, stale, status=DetailFetchStatus.OK, fetched_at="2026-07-01T00:00:00Z"
    )
    # After the cutoff: present but not stale.
    _insert_fetch(
        conn, fresh, status=DetailFetchStatus.OK, fetched_at="2026-09-10T00:00:00Z"
    )
    # No HTTP response was ever received: a transient failure worth retrying.
    _insert_fetch(conn, transient, status=DetailFetchStatus.FAILED)
    # A raw body proves a response was captured even without an http_status.
    raw_id = insert_raw_document(
        conn,
        RawDocument(
            kind=RawDocumentKind.JOB_DETAIL,
            sha256="a" * 64,
            stored_at="2026-09-15T12:00:00Z",
        ),
    )
    _insert_fetch(
        conn,
        no_http_but_raw,
        status=DetailFetchStatus.FAILED,
        raw_document_id=raw_id,
    )
    # A recorded HTTP response (500) is a real answer, not a retryable gap.
    _insert_fetch(
        conn, http_recorded, status=DetailFetchStatus.FAILED, http_status=500
    )
    # Skipped attempts and never-fetched jobs are never refresh candidates.
    _insert_fetch(conn, skipped, status=DetailFetchStatus.SKIPPED)

    selected = [job.id for job in jobs_needing_refresh(conn, cutoff=STALE_CUTOFF)]
    assert selected == [stale.id, transient.id]
    assert count_jobs_needing_refresh(conn, cutoff=STALE_CUTOFF) == 2
    # The never-fetched, skipped, fresh, and HTTP-answered jobs stay out.
    assert no_fetch.id not in selected
    assert skipped.id not in selected
    assert fresh.id not in selected
    assert no_http_but_raw.id not in selected
    assert http_recorded.id not in selected


def test_jobs_needing_refresh_refutes_only_resighted_404(conn) -> None:
    refuted = _store_card(conn, "3911111111")
    unrefuted = _store_card(conn, "3912222222")
    inactive = _store_card(conn, "3913333333")

    # A 404 at T1, then an ordinary re-sighting at T2 refutes the death signal.
    _insert_fetch(
        conn,
        refuted,
        status=DetailFetchStatus.FAILED,
        http_status=404,
        stored_at="2026-08-01T00:00:00Z",
    )
    _resight(conn, "3911111111", now="2026-09-16T00:00:00Z")

    # A 410 recorded after the last sighting is not refuted: keep it inactive.
    _insert_fetch(
        conn,
        unrefuted,
        status=DetailFetchStatus.FAILED,
        http_status=410,
        stored_at="2026-09-20T00:00:00Z",
    )
    deactivate_job(conn, int(unrefuted.id))

    # A stale success on an inactive offer never comes back through refresh.
    _insert_fetch(
        conn, inactive, status=DetailFetchStatus.OK, fetched_at="2026-07-01T00:00:00Z"
    )
    deactivate_job(conn, int(inactive.id))

    selected = [job.id for job in jobs_needing_refresh(conn, cutoff=STALE_CUTOFF)]
    assert selected == [refuted.id]


def test_jobs_needing_refresh_reads_only_the_latest_fetch(conn) -> None:
    hidden = _store_card(conn, "3911111111")
    visible = _store_card(conn, "3912222222")

    # Stale success, then a newer skip: the latest attempt wins and hides it.
    _insert_fetch(
        conn, hidden, status=DetailFetchStatus.OK, fetched_at="2026-07-01T00:00:00Z"
    )
    _insert_fetch(
        conn,
        hidden,
        status=DetailFetchStatus.SKIPPED,
        stored_at="2026-08-01T00:00:00Z",
    )

    # An older skip, then a newer stale success: the latest attempt selects it.
    _insert_fetch(
        conn,
        visible,
        status=DetailFetchStatus.SKIPPED,
        stored_at="2026-07-01T00:00:00Z",
    )
    _insert_fetch(
        conn, visible, status=DetailFetchStatus.OK, fetched_at="2026-08-01T00:00:00Z"
    )

    assert [job.id for job in jobs_needing_refresh(conn, cutoff=STALE_CUTOFF)] == [
        visible.id
    ]


def test_jobs_needing_refresh_limit_job_ids_and_order(conn) -> None:
    newest = _store_card(conn, "3911111111")
    oldest = _store_card(conn, "3912222222")
    tied = _store_card(conn, "3913333333")

    _insert_fetch(
        conn, newest, status=DetailFetchStatus.OK, fetched_at="2026-08-15T00:00:00Z"
    )
    _insert_fetch(
        conn, oldest, status=DetailFetchStatus.OK, fetched_at="2026-06-01T00:00:00Z"
    )
    _insert_fetch(
        conn, tied, status=DetailFetchStatus.OK, fetched_at="2026-06-01T00:00:00Z"
    )

    # Oldest evidence first; the equal timestamps break by ascending job id.
    ordered = [
        job.id for job in jobs_needing_refresh(conn, cutoff=STALE_CUTOFF, limit=3)
    ]
    assert ordered == [oldest.id, tied.id, newest.id]
    assert [
        job.id for job in jobs_needing_refresh(conn, cutoff=STALE_CUTOFF, limit=2)
    ] == [oldest.id, tied.id]
    assert [
        job.id
        for job in jobs_needing_refresh(conn, cutoff=STALE_CUTOFF, job_ids=[newest.id])
    ] == [newest.id]
    assert count_jobs_needing_refresh(conn, cutoff=STALE_CUTOFF) == 3
    # A cutoff before every fetch selects nothing.
    assert jobs_needing_refresh(conn, cutoff="2026-01-01T00:00:00Z") == []


# ---------------------------------------------------------------------------
# Opt-in, dry-run, and refusal
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_request(conn, settings, monkeypatch) -> None:
    no_network(monkeypatch)
    job = _store_card(conn, "3911111111")
    result = fetch_linkedin_details([job], settings=settings, dry_run=True)
    assert result.status is SearchRunStatus.DRY_RUN
    assert result.planned_urls == [
        build_detail_url(settings.linkedin_detail_url, "3911111111")
    ]
    assert result.items == []
    assert result.requests_made == 0


def test_disabled_without_explicit_opt_in(conn, settings, monkeypatch) -> None:
    no_network(monkeypatch)
    job = _store_card(conn, "3911111111")
    with pytest.raises(CollectorDisabledError) as excinfo:
        fetch_linkedin_details([job], settings=settings)
    assert "--confirm-network" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Fetch behaviour
# ---------------------------------------------------------------------------


def test_fetch_parses_and_bounds_requests(conn, settings) -> None:
    jobs = [
        _store_card(conn, "3911111111"),
        _store_card(conn, "3912222222"),
        _store_card(conn, "3913333333"),
    ]
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient(
        [
            response(200, body, headers={"content-type": "text/html"}),
            response(200, body, headers={"content-type": "text/html"}),
        ]
    )
    result = fetch_linkedin_details(
        jobs,
        settings=settings,
        network_enabled=True,
        max_items=2,
        client=client,
        sleep=lambda _: None,
    )
    assert client.call_count == 2
    assert result.status is SearchRunStatus.COMPLETED
    assert result.requested == 2
    assert result.truncated == 1
    assert result.requests_made == 2
    assert [item.status for item in result.items] == [
        DetailFetchStatus.OK,
        DetailFetchStatus.OK,
    ]
    assert "Strong Python and SQL required" in result.items[0].description
    assert len(result.raw_documents) == 2
    assert result.raw_documents[0].kind is RawDocumentKind.JOB_DETAIL
    assert all(document.sha256 for document in result.raw_documents)


def test_fetch_fails_closed_on_access_denied(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111"), _store_card(conn, "3912222222")]
    client = FakeHttpClient([response(403, "<html>denied</html>")])
    result = fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert client.call_count == 1
    assert result.items[0].status is DetailFetchStatus.FAILED
    assert "access denied" in (result.items[0].error or "")
    # The refusal is still recorded for provenance.
    assert result.raw_documents and result.raw_documents[0].status_code == 403


def test_fetch_does_not_retry_rate_limit(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    client = FakeHttpClient([response(429, "slow down")])
    result = fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert client.call_count == 1


def test_fetch_retries_transient_then_succeeds(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient(
        [
            response(503, "busy"),
            response(200, body, headers={"content-type": "text/html"}),
        ]
    )
    jitterless = settings.with_overrides(request_jitter_seconds=0.0)
    sleeps: list[float] = []
    result = fetch_linkedin_details(
        jobs,
        settings=jitterless,
        network_enabled=True,
        client=client,
        sleep=sleeps.append,
    )
    assert client.call_count == 2
    assert result.items[0].status is DetailFetchStatus.OK
    # base_delay = max(1.0, request_delay/4) = 2.0 for the default 8s pacing.
    assert 2.0 in sleeps


def test_fetch_gives_up_after_bounded_retries(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    client = FakeHttpClient([response(503, "busy")] * 5)
    jitterless = settings.with_overrides(request_jitter_seconds=0.0)
    sleeps: list[float] = []
    result = fetch_linkedin_details(
        jobs,
        settings=jitterless,
        network_enabled=True,
        client=client,
        sleep=sleeps.append,
    )
    assert result.status is SearchRunStatus.FAILED
    # 1 initial attempt + 2 retries, then the run aborts.
    assert client.call_count == 3
    assert result.items[0].status is DetailFetchStatus.FAILED


def test_fetch_records_not_found_and_continues(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111"), _store_card(conn, "3912222222")]
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient(
        [response(404, "gone"), response(200, body, headers={"content-type": "text/html"})]
    )
    result = fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert client.call_count == 2
    assert result.items[0].status is DetailFetchStatus.FAILED
    assert result.items[1].status is DetailFetchStatus.OK


# ---------------------------------------------------------------------------
# Offer lifecycle: definitive 404/410 deactivation
# ---------------------------------------------------------------------------


def test_definitive_missing_requires_failed_status() -> None:
    # The lifecycle signal is machine-readable and can only be raised by a
    # FAILED result whose HTTP status is a definitive 404/410.
    assert DetailItem(job_id=1, http_status=404).status is DetailFetchStatus.FAILED
    assert DetailItem(
        job_id=1, status=DetailFetchStatus.FAILED, http_status=404
    ).is_definitive_missing
    assert DetailItem(
        job_id=1, status=DetailFetchStatus.FAILED, http_status=410
    ).is_definitive_missing
    assert not DetailItem(
        job_id=1, status=DetailFetchStatus.FAILED, http_status=503
    ).is_definitive_missing
    assert not DetailItem(job_id=1, status=DetailFetchStatus.FAILED).is_definitive_missing
    assert not DetailItem(
        job_id=1, status=DetailFetchStatus.PARTIAL, http_status=404
    ).is_definitive_missing


def test_fetch_records_machine_readable_not_found_status(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    client = FakeHttpClient([response(410, "gone")])
    result = fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    item = result.items[0]
    assert item.status is DetailFetchStatus.FAILED
    assert item.http_status == 410
    assert item.is_definitive_missing is True
    # The status is exposed in the serialized result, not only in the error text.
    assert item.to_dict()["http_status"] == 410


def _missing_detail(job, *, http_status: int) -> DetailItem:
    return _detail(
        job,
        status=DetailFetchStatus.FAILED,
        http_status=http_status,
        error=f"not found (HTTP {http_status})",
    )


def test_definitive_missing_detail_deactivates_job_and_keeps_history(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    insert_evaluation(
        conn,
        Evaluation(
            job_id=job.id,
            scorer=ScorerName.HEURISTIC,
            overall_score=77,
            recommendation="apply",
        ),
    )

    result = DetailResult(items=[_missing_detail(job, http_status=404)], requested=1)
    outcome = ingest_detail_results(conn, result, settings=settings)

    assert get_job(conn, job.id).is_active is False
    assert outcome.deactivated == 1
    # The failure still counts as failed, and the provenance row keeps the status.
    assert outcome.failed == 1
    fetch = latest_detail_fetch(conn, job.id)
    assert fetch.status is DetailFetchStatus.FAILED
    assert fetch.http_status == 404
    # The evaluation survives and the default listing hides the inactive job.
    assert latest_evaluation(conn, job.id).overall_score == 77
    assert list_jobs(conn, limit=10) == []
    assert [item.job.id for item in list_jobs(conn, limit=10, include_inactive=True)] == [job.id]


@pytest.mark.parametrize("http_status", [500, 503, 403, None])
def test_non_definitive_failure_does_not_deactivate(conn, settings, http_status) -> None:
    job = _store_card(conn, "3911111111")
    result = DetailResult(
        items=[
            _detail(
                job,
                status=DetailFetchStatus.FAILED,
                http_status=http_status,
                error="temporary failure",
            )
        ],
        requested=1,
    )
    outcome = ingest_detail_results(conn, result, settings=settings)

    assert get_job(conn, job.id).is_active is True
    assert outcome.deactivated == 0
    assert [item.job.id for item in list_jobs(conn, limit=10)] == [job.id]


def test_resighting_after_deactivation_reactivates_the_offer(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    ingest_detail_results(
        conn,
        DetailResult(items=[_missing_detail(job, http_status=410)], requested=1),
        settings=settings,
    )
    assert get_job(conn, job.id).is_active is False

    # An ordinary re-sighting through the search path restores the offer, exactly
    # as ``store_posting`` already behaves.
    posting = normalize_job_posting(_card_payload("3911111111"), source="linkedin")
    again, decision = store_posting(conn, posting, now="2026-09-16T10:00:00Z")

    assert decision.kind is DuplicateKind.EXACT_DUPLICATE
    assert again.id == job.id
    assert again.is_active is True
    assert [item.job.id for item in list_jobs(conn, limit=10)] == [job.id]
    assert [item.id for item in jobs_needing_details(conn)] == [job.id]


# ---------------------------------------------------------------------------
# Offer lifecycle: allowed redirect final URL (OL-5)
# ---------------------------------------------------------------------------

REQUESTED_DETAIL_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/3911111111"
)
REDIRECT_DETAIL_URL = (
    "https://es.linkedin.com/jobs-guest/jobs/api/jobPosting/3911111111"
)


def test_sanitize_final_url_keeps_only_safe_redirects() -> None:
    # A different http(s) URL is the only accepted shape.
    assert sanitize_final_url(REQUESTED_DETAIL_URL, REDIRECT_DETAIL_URL) == REDIRECT_DETAIL_URL
    # No redirect observed: an identical, empty, or missing URL is not a redirect.
    assert sanitize_final_url(REQUESTED_DETAIL_URL, REQUESTED_DETAIL_URL) is None
    assert sanitize_final_url(REQUESTED_DETAIL_URL, "") is None
    assert sanitize_final_url(REQUESTED_DETAIL_URL, None) is None
    assert sanitize_final_url(None, "") is None
    # Credentials are never persisted, even on an otherwise valid URL.
    assert sanitize_final_url(
        REQUESTED_DETAIL_URL, "https://user:pass@www.linkedin.com/jobs/view/1/"
    ) is None
    assert sanitize_final_url(
        REQUESTED_DETAIL_URL, "https://@www.linkedin.com/jobs/view/1/"
    ) is None
    # Non-HTTP schemes are refused.
    assert sanitize_final_url(
        REQUESTED_DETAIL_URL, "ftp://www.linkedin.com/jobs/view/1/"
    ) is None
    assert sanitize_final_url(REQUESTED_DETAIL_URL, "file:///etc/passwd") is None


def test_fetch_captures_allowed_redirect_final_url(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient(
        [response(200, body, headers={"content-type": "text/html"}, url=REDIRECT_DETAIL_URL)]
    )
    result = fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    item = result.items[0]
    assert item.status is DetailFetchStatus.OK
    assert item.url == REQUESTED_DETAIL_URL
    assert item.final_url == REDIRECT_DETAIL_URL
    # Capturing the final URL relies on the existing response and sends no
    # additional request.
    assert client.call_count == 1


def test_fetch_records_no_final_url_without_redirect(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient(
        [response(200, body, headers={"content-type": "text/html"}, url=REQUESTED_DETAIL_URL)]
    )
    result = fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    # The requested URL is echoed back, so there was no redirect to persist.
    assert result.items[0].final_url is None


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "https://user:pass@www.linkedin.com/jobs/view/1/",
        "ftp://www.linkedin.com/jobs/view/1/",
        "file:///etc/passwd",
    ),
)
def test_fetch_drops_unsafe_redirect_final_url(conn, settings, unsafe_url) -> None:
    jobs = [_store_card(conn, "3911111111")]
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient(
        [response(200, body, headers={"content-type": "text/html"}, url=unsafe_url)]
    )
    result = fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.items[0].status is DetailFetchStatus.OK
    # The description still enriches the job; only the unsafe URL is dropped.
    assert result.items[0].final_url is None


def test_ingest_persists_redirect_final_url(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient(
        [response(200, body, headers={"content-type": "text/html"}, url=REDIRECT_DETAIL_URL)]
    )
    result = fetch_linkedin_details(
        jobs=[job], settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    outcome = ingest_detail_results(conn, result, settings=settings)

    assert outcome.enriched == 1
    fetch = latest_detail_fetch(conn, job.id)
    assert fetch is not None
    assert fetch.url == REQUESTED_DETAIL_URL
    assert fetch.final_url == REDIRECT_DETAIL_URL
    assert fetch.to_dict()["final_url"] == REDIRECT_DETAIL_URL
    # A redirect is not an expiry: the offer stays active.
    assert get_job(conn, job.id).is_active is True
    assert outcome.deactivated == 0


def test_ingest_persists_null_final_url_without_redirect(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient(
        [response(200, body, headers={"content-type": "text/html"}, url=REQUESTED_DETAIL_URL)]
    )
    result = fetch_linkedin_details(
        jobs=[job], settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    ingest_detail_results(conn, result, settings=settings)

    fetch = latest_detail_fetch(conn, job.id)
    assert fetch is not None
    assert fetch.final_url is None


@pytest.mark.parametrize("http_status", [404, 410, 500, 503])
def test_failed_fetch_never_captures_final_url(conn, settings, http_status) -> None:
    jobs = [_store_card(conn, "3911111111")]
    # The failing response even advertises a different URL, but a failure is not
    # a redirect: it must neither become redirect evidence nor change the
    # lifecycle beyond the existing 404/410 rule.
    client = FakeHttpClient([response(http_status, "nope", url=REDIRECT_DETAIL_URL)])
    bounded = settings.with_overrides(http_max_retries=0, request_jitter_seconds=0.0)
    result = fetch_linkedin_details(
        jobs, settings=bounded, network_enabled=True, client=client, sleep=lambda _: None
    )
    item = result.items[0]
    assert item.status is DetailFetchStatus.FAILED
    assert item.final_url is None
    assert item.is_definitive_missing is (http_status in (404, 410))


def test_ingest_propagates_item_final_url_to_provenance(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    ingest_detail_results(
        conn,
        DetailResult(
            items=[
                _detail(
                    job,
                    status=DetailFetchStatus.OK,
                    http_status=200,
                    final_url=REDIRECT_DETAIL_URL,
                )
            ],
            requested=1,
        ),
        settings=settings,
    )
    fetch = latest_detail_fetch(conn, job.id)
    assert fetch is not None
    # The pipeline carries the collector's captured value into provenance.
    assert fetch.final_url == REDIRECT_DETAIL_URL


def test_fetch_rejects_oversized_download(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    tiny = settings.with_overrides(detail_max_bytes=64)
    big_body = (
        "<div class='show-more-less-html__markup'>"
        + ("Python and SQL. " * 40)
        + "</div>"
    )
    client = FakeHttpClient([response(200, big_body, headers={"content-type": "text/html"})])
    result = fetch_linkedin_details(
        jobs, settings=tiny, network_enabled=True, client=client, sleep=lambda _: None
    )
    item = result.items[0]
    assert item.status is DetailFetchStatus.FAILED
    assert "download limit" in (item.error or "")
    assert item.description is None
    # The oversized body is never parsed or stored as raw content, even though
    # the response itself is still recorded for provenance.
    assert item.content_hash == ""
    assert result.raw_documents
    assert result.raw_documents[0].body is None
    assert result.raw_documents[0].truncated is True


def test_oversized_detail_is_not_persisted_in_a_small_detail_cap_run(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    tiny = settings.with_overrides(detail_max_bytes=64)
    big_body = (
        '<div class="description__text"><p>'
        + ("A long description sentence. " * 20)
        + "</p></div>"
    )
    client = FakeHttpClient([response(200, big_body, headers={"content-type": "text/html"})])
    result = fetch_linkedin_details(
        [job], settings=tiny, network_enabled=True, client=client, sleep=lambda _: None
    )
    ingest_detail_results(conn, result, settings=tiny)

    assert get_job(conn, job.id).description is None
    fetch = latest_detail_fetch(conn, job.id)
    assert fetch.status is DetailFetchStatus.FAILED
    assert fetch.description_chars == 0
    if fetch.raw_document_id is not None:
        raw = conn.execute(
            "SELECT body FROM raw_documents WHERE id = ?", (fetch.raw_document_id,)
        ).fetchone()
        assert raw["body"] is None


def test_detail_body_above_raw_cap_is_not_stored(conn, settings) -> None:
    # Even when the detail ceiling is larger than the raw ceiling, a body over
    # the raw ceiling must never be persisted verbatim.
    jobs = [_store_card(conn, "3911111111")]
    mismatched = settings.with_overrides(detail_max_bytes=10_000, raw_max_bytes=64)
    body = (
        '<div class="show-more-less-html__markup"><p>'
        + ("Python and SQL. " * 20)
        + "</p></div>"
    )
    client = FakeHttpClient([response(200, body, headers={"content-type": "text/html"})])
    result = fetch_linkedin_details(
        jobs, settings=mismatched, network_enabled=True, client=client, sleep=lambda _: None
    )
    # It is under the detail ceiling, so it may be parsed...
    assert result.items[0].status is DetailFetchStatus.OK
    # ...but the raw body is still bounded by the lower raw ceiling.
    assert result.raw_documents
    assert result.raw_documents[0].body is None
    assert result.raw_documents[0].truncated is True


def test_fetch_honours_transport_truncation_header(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient(
        [response(200, body, headers={"content-type": "text/html", TRUNCATION_HEADER: "1"})]
    )
    result = fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.items[0].status is DetailFetchStatus.FAILED
    assert result.items[0].description is None


def test_fetch_reports_unidentifiable_jobs_as_skipped(conn, settings) -> None:
    job = _store_card(conn, "no-linkedin-id", jobTitle="Mystery Role")
    client = FakeHttpClient([])  # any request would raise in FakeHttpClient
    result = fetch_linkedin_details(
        [job], settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert client.call_count == 0
    assert result.items[0].status is DetailFetchStatus.SKIPPED
    assert result.items[0].error


def test_fetch_uses_numeric_source_id_from_slug_url(conn, settings) -> None:
    # Regression for the live "all 20 skipped" bug: a numeric source id with a
    # slug URL must be fetched, targeting the planned detail URL exactly.
    job = _store_card(conn, "4457430073", jobUrl=SLUG_DETAIL_URL)
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient([response(200, body, headers={"content-type": "text/html"})])
    result = fetch_linkedin_details(
        [job], settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )

    assert client.call_count == 1
    assert client.calls[0]["url"] == build_detail_url(
        settings.linkedin_detail_url, "4457430073"
    )
    assert result.items[0].source_job_id == "4457430073"
    assert result.items[0].status is DetailFetchStatus.OK
    assert "Strong Python and SQL required" in (result.items[0].description or "")


def test_fetch_never_uses_ambient_credentials(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient([response(200, body, headers={"content-type": "text/html"})])
    fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    headers = client.calls[0]["headers"]
    assert "Cookie" not in headers
    assert "Authorization" not in headers
    assert headers["Accept-Language"].startswith("en")


# ---------------------------------------------------------------------------
# Persistence and content preservation
# ---------------------------------------------------------------------------


def test_ingest_persists_enrichment_and_provenance(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    assert job.content_status is ContentStatus.PARTIAL

    body = fixture_text("linkedin_detail_page.html")
    client = FakeHttpClient([response(200, body, headers={"content-type": "text/html"})])
    result = fetch_linkedin_details(
        [job], settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    outcome = ingest_detail_results(
        conn, result, settings=settings, now="2026-09-15T12:00:00Z"
    )

    refreshed = get_job(conn, job.id)
    assert refreshed.content_status is ContentStatus.OK
    assert "Strong Python and SQL required" in refreshed.description
    assert refreshed.content_hash and refreshed.content_hash != job.content_hash

    fetches = detail_fetches_for_job(conn, job.id)
    assert len(fetches) == 1
    fetch = fetches[0]
    assert fetch.status is DetailFetchStatus.OK
    assert fetch.http_status == 200
    assert fetch.url and "jobPosting/3911111111" in fetch.url
    assert fetch.sha256
    assert fetch.content_hash == refreshed.content_hash
    assert fetch.fetched_at == "2026-09-15T12:00:00Z"
    assert fetch.description_chars == len(refreshed.description)
    assert fetch.raw_document_id is not None

    raw = conn.execute(
        "SELECT * FROM raw_documents WHERE id = ?", (fetch.raw_document_id,)
    ).fetchone()
    assert raw["kind"] == "job_detail"

    assert outcome.enriched == 1
    assert outcome.changed == 1
    assert outcome.run_id is not None
    run = conn.execute(
        "SELECT * FROM search_runs WHERE id = ?", (outcome.run_id,)
    ).fetchone()
    assert run["provider"] == "linkedin-details"
    assert run["items_found"] == 1


def test_failed_detail_never_overwrites_full_description(conn, settings) -> None:
    job = _store_card(
        conn, "3911111111", description="A complete stored description that must survive."
    )
    assert job.content_status is ContentStatus.OK

    result = DetailResult(
        items=[
            _detail(
                job,
                status=DetailFetchStatus.FAILED,
                http_status=403,
                error="access denied (HTTP 403); failing closed",
            )
        ],
        requested=1,
    )
    outcome = ingest_detail_results(conn, result, settings=settings)

    refreshed = get_job(conn, job.id)
    assert refreshed.description == "A complete stored description that must survive."
    assert refreshed.content_status is ContentStatus.OK
    assert refreshed.content_hash == job.content_hash

    fetch = latest_detail_fetch(conn, job.id)
    assert fetch.status is DetailFetchStatus.FAILED
    assert "403" in (fetch.error or "")
    assert outcome.failed == 1
    assert outcome.changed == 0


def test_partial_detail_never_overwrites_full_description(conn, settings) -> None:
    job = _store_card(conn, "3911111111", description="Complete stored description.")
    result = DetailResult(
        items=[
            _detail(
                job,
                status=DetailFetchStatus.PARTIAL,
                description="tiny",
                content_status=ContentStatus.PARTIAL,
                error="no description container found in the response",
            )
        ],
        requested=1,
    )
    ingest_detail_results(conn, result, settings=settings)

    refreshed = get_job(conn, job.id)
    assert refreshed.description == "Complete stored description."
    assert refreshed.content_status is ContentStatus.OK
    assert refreshed.content_hash == job.content_hash


def test_shorter_successful_detail_keeps_longer_full_description(conn, settings) -> None:
    job = _store_card(
        conn, "3911111111", description="A longer complete stored description."
    )
    result = DetailResult(
        items=[
            _detail(
                job,
                status=DetailFetchStatus.OK,
                description="short",
                content_status=ContentStatus.OK,
            )
        ],
        requested=1,
    )
    outcome = ingest_detail_results(conn, result, settings=settings)

    refreshed = get_job(conn, job.id)
    assert refreshed.description == "A longer complete stored description."
    assert refreshed.content_hash == job.content_hash
    assert outcome.preserved == 1
    # The fetch itself is still recorded for provenance.
    assert latest_detail_fetch(conn, job.id).status is DetailFetchStatus.OK


def test_successful_detail_replaces_longer_partial_snippet(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    update_job_content(
        conn,
        job.id,
        description="x" * 200,
        content_status=ContentStatus.PARTIAL,
        content_hash="stale",
    )
    result = DetailResult(
        items=[
            _detail(
                job,
                status=DetailFetchStatus.OK,
                description="Full description.",
                content_status=ContentStatus.OK,
            )
        ],
        requested=1,
    )
    ingest_detail_results(conn, result, settings=settings)

    refreshed = get_job(conn, job.id)
    assert refreshed.description == "Full description."
    assert refreshed.content_status is ContentStatus.OK


def test_partial_detail_enriches_a_partial_job(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    snippet = "A snippet that is longer than nothing at all."
    result = DetailResult(
        items=[
            _detail(
                job,
                status=DetailFetchStatus.PARTIAL,
                description=snippet,
                content_status=ContentStatus.PARTIAL,
            )
        ],
        requested=1,
    )
    ingest_detail_results(conn, result, settings=settings)

    refreshed = get_job(conn, job.id)
    assert refreshed.description == snippet
    assert refreshed.content_status is ContentStatus.PARTIAL


def test_skipped_detail_is_recorded_without_touching_content(conn, settings) -> None:
    job = _store_card(conn, "3911111111")
    result = DetailResult(
        items=[
            _detail(
                job,
                status=DetailFetchStatus.SKIPPED,
                error="no LinkedIn job id could be resolved from the stored job",
            )
        ],
        requested=1,
    )
    outcome = ingest_detail_results(conn, result, settings=settings)

    assert get_job(conn, job.id).description is None
    assert outcome.skipped == 1
    assert latest_detail_fetch(conn, job.id).status is DetailFetchStatus.SKIPPED


def test_failed_detail_provenance_does_not_claim_a_content_hash(conn, settings) -> None:
    job = _store_card(
        conn, "3911111111", description="A complete stored description that must survive."
    )
    result = DetailResult(
        items=[
            _detail(
                job,
                status=DetailFetchStatus.FAILED,
                http_status=503,
                error="transient failure (HTTP 503)",
            )
        ],
        requested=1,
    )
    ingest_detail_results(conn, result, settings=settings)

    fetch = latest_detail_fetch(conn, job.id)
    assert fetch.status is DetailFetchStatus.FAILED
    # A failed attempt produced no description, so it must not borrow the job's
    # existing content hash.
    assert fetch.content_hash is None
    assert fetch.sha256 is None
    assert get_job(conn, job.id).content_hash == job.content_hash


def test_preserved_detail_provenance_does_not_claim_a_content_hash(conn, settings) -> None:
    job = _store_card(
        conn, "3911111111", description="A longer complete stored description."
    )
    result = DetailResult(
        items=[
            _detail(
                job,
                status=DetailFetchStatus.OK,
                description="short",
                content_status=ContentStatus.OK,
            )
        ],
        requested=1,
    )
    outcome = ingest_detail_results(conn, result, settings=settings)

    fetch = latest_detail_fetch(conn, job.id)
    assert fetch.status is DetailFetchStatus.OK
    # The shorter response was preserved-as-is; this attempt did not produce the
    # stored description, so its provenance hash is empty.
    assert fetch.content_hash is None
    assert outcome.preserved == 1
    assert get_job(conn, job.id).content_hash == job.content_hash


# ---------------------------------------------------------------------------
# Offline fixture transport
# ---------------------------------------------------------------------------


def test_fixture_transport_matches_by_job_id(monkeypatch) -> None:
    no_network(monkeypatch)
    fixture = load_detail_fixture(DETAIL_RESPONSES)
    transport = FixtureDetailTransport(fixture.entries)
    hit = transport.fetch_once(
        "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/3911111111"
    )
    assert hit.status_code == 200
    assert "Senior Backend Engineer" in hit.body
    assert len(transport.calls) == 1

    missing = transport.fetch_once(
        "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/9999999999"
    )
    assert missing.status_code == 404


def test_fetch_does_not_store_credential_looking_raw_body(conn, settings) -> None:
    jobs = [_store_card(conn, "3911111111")]
    secret = "sk-proj-abcdefghijklmnopqrstuvwx"
    body = (
        '<div class="show-more-less-html__markup">Python and SQL. '
        f"{secret}</div>"
    )
    client = FakeHttpClient([response(200, body, headers={"content-type": "text/html"})])
    result = fetch_linkedin_details(
        jobs, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    document = result.raw_documents[0]
    assert document.body is None
    assert document.truncated is True
    assert "credential" in (document.notes or "")
    assert secret not in json.dumps(
        [item.to_dict() for item in result.items], ensure_ascii=False
    )


def test_load_detail_fixture_rejects_missing_file(tmp_path: Path) -> None:
    from jobscout.collectors.base import CollectorError

    with pytest.raises(CollectorError):
        load_detail_fixture(tmp_path / "nope.json")


def test_detail_fixture_is_marked_synthetic() -> None:
    payload = json.loads(DETAIL_RESPONSES.read_text(encoding="utf-8"))
    assert "Synthetic" in payload["note"]
    assert len(payload["details"]) == 3


def test_init_db_upgrades_v1_without_data_loss(tmp_path: Path) -> None:
    from jobscout.db import SCHEMA_SQL, SCHEMA_VERSION, connect, init_db

    connection = connect(tmp_path / "legacy.sqlite3")
    try:
        connection.executescript(SCHEMA_SQL)
        posting = normalize_job_posting(_card_payload("3911111111"), source="linkedin")
        job, _ = store_posting(connection, posting, now="2026-09-15T10:00:00Z")
        # Simulate a database created before the detail-fetch table existed.
        connection.execute("DROP TABLE job_detail_fetches")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

        assert init_db(connection) == SCHEMA_VERSION
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "job_detail_fetches" in tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert get_job(connection, job.id).source_job_id == "3911111111"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "details.sqlite3"


@pytest.fixture
def fast_pacing(monkeypatch) -> None:
    """Keep the CLI's real pacing policy but make tests fast (offline fixtures)."""
    monkeypatch.setenv("JOBSCOUT_REQUEST_DELAY_SECONDS", "0")
    monkeypatch.setenv("JOBSCOUT_REQUEST_JITTER_SECONDS", "0")


def test_fetch_details_refuses_without_confirmation(tmp_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    db = tmp_path / "refuse.sqlite3"
    exit_code = main(["--db", str(db), "fetch-details"])
    assert exit_code == EXIT_FAIL_CLOSED
    err = capsys.readouterr().err
    assert "--confirm-network" in err
    assert "--dry-run" in err
    assert "--fixture" in err
    # The refusal happens before the database is opened.
    assert not db.exists()


def test_fetch_details_dry_run_needs_no_network(db_path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    assert main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)]) == EXIT_OK
    capsys.readouterr()

    assert main(["--db", str(db_path), "fetch-details", "--dry-run", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["selected"] == 3
    assert len(payload["planned_urls"]) == 3
    assert all("jobPosting/" in url for url in payload["planned_urls"])

    # A dry run writes nothing.
    assert main(["--db", str(db_path), "runs", "--json"]) == EXIT_OK
    runs = json.loads(capsys.readouterr().out)
    assert runs["detail_fetches"] == 0
    assert len(runs["runs"]) == 1


def test_fetch_details_dry_run_respects_max_items(db_path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()
    assert (
        main(["--db", str(db_path), "fetch-details", "--max-items", "2", "--dry-run", "--json"])
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] == 2
    assert len(payload["planned_urls"]) == 2


def test_fetch_details_dry_run_reports_max_items_truncation(db_path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()

    # Three cards stored, capped at two: the JSON report must say one was dropped.
    assert (
        main(["--db", str(db_path), "fetch-details", "--max-items", "2", "--dry-run", "--json"])
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] == 2
    assert payload["truncated"] == 1

    # A cap above the selection reports no truncation.
    assert (
        main(["--db", str(db_path), "fetch-details", "--max-items", "10", "--dry-run", "--json"])
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["truncated"] == 0


# ---------------------------------------------------------------------------
# CLI: refresh selection (--refresh / --stale-days)
# ---------------------------------------------------------------------------


def _seed_refresh_db(db_path: Path) -> dict[str, int]:
    """Seed a file database with one offer per refresh-predicate branch.

    Timestamps are relative to the test's own ``utcnow`` so the CLI's later
    cutoff stays deterministic regardless of the day the suite runs on.
    """
    reference = utcnow()
    connection = connect(db_path)
    try:
        init_db(connection)
        stale = _store_card(connection, "3921000001")
        fresh = _store_card(connection, "3921000002")
        transient = _store_card(connection, "3921000003")
        skipped = _store_card(connection, "3921000004")
        never_fetched = _store_card(connection, "3921000005")

        _insert_fetch(
            connection,
            stale,
            status=DetailFetchStatus.OK,
            fetched_at=iso_days_ago(60, now=reference),
        )
        _insert_fetch(
            connection,
            fresh,
            status=DetailFetchStatus.OK,
            fetched_at=to_iso(reference),
        )
        _insert_fetch(connection, transient, status=DetailFetchStatus.FAILED)
        _insert_fetch(connection, skipped, status=DetailFetchStatus.SKIPPED)
        return {
            "stale": int(stale.id),
            "fresh": int(fresh.id),
            "transient": int(transient.id),
            "skipped": int(skipped.id),
            "never_fetched": int(never_fetched.id),
        }
    finally:
        connection.close()


def _age_detail_fetches(db_path: Path, fetched_at: str) -> None:
    connection = connect(db_path)
    try:
        with connection:
            connection.execute(
                "UPDATE job_detail_fetches SET fetched_at = ?", (fetched_at,)
            )
    finally:
        connection.close()


def test_fetch_details_stale_days_requires_refresh(
    db_path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)

    # --stale-days without --refresh is rejected before any file is created.
    assert (
        main(["--db", str(db_path), "fetch-details", "--stale-days", "5", "--dry-run"])
        == EXIT_ERROR
    )
    assert "--stale-days requires --refresh" in capsys.readouterr().err
    assert not db_path.exists()

    # A negative age floor is rejected even with a valid selection.
    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--refresh",
                "--stale-days",
                "-1",
                "--dry-run",
            ]
        )
        == EXIT_ERROR
    )
    assert "0 or greater" in capsys.readouterr().err
    assert not db_path.exists()


def test_fetch_details_refresh_dry_run_is_readonly(
    db_path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    _seed_refresh_db(db_path)
    before_bytes = db_path.read_bytes()
    before_names = sorted(path.name for path in db_path.parent.iterdir())

    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--refresh",
                "--stale-days",
                "5",
                "--dry-run",
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["selection"] == "refresh"
    assert payload["stale_days"] == 5
    assert payload["cutoff"]
    # Stale success + transient failure; fresh, skipped and never-fetched stay out.
    assert payload["selected"] == 2
    assert len(payload["planned_urls"]) == 2

    # The text report carries the same additive refresh context.
    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--refresh",
                "--stale-days",
                "5",
                "--dry-run",
            ]
        )
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert "refresh selection:" in output
    assert "stale-days 5 days" in output

    # Nothing was written, not even a journal sidecar.
    assert db_path.read_bytes() == before_bytes
    assert sorted(path.name for path in db_path.parent.iterdir()) == before_names


def test_fetch_details_existing_selections_are_unchanged(
    db_path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()
    main(["--db", str(db_path), "list", "--json"])
    first_id = json.loads(capsys.readouterr().out)["jobs"][0]["job"]["id"]

    # --job-id still selects exactly the requested job.
    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--job-id",
                str(first_id),
                "--dry-run",
                "--json",
            ]
        )
        == EXIT_OK
    )
    by_id = json.loads(capsys.readouterr().out)
    assert by_id["selected"] == 1
    assert "selection" not in by_id and "cutoff" not in by_id

    # --all still selects every active job.
    assert (
        main(["--db", str(db_path), "fetch-details", "--all", "--dry-run", "--json"])
        == EXIT_OK
    )
    all_payload = json.loads(capsys.readouterr().out)
    assert all_payload["selected"] == 3
    assert "selection" not in all_payload and "cutoff" not in all_payload

    # The default still selects only un-enriched jobs (the three cards).
    assert (
        main(["--db", str(db_path), "fetch-details", "--dry-run", "--json"]) == EXIT_OK
    )
    default_payload = json.loads(capsys.readouterr().out)
    assert default_payload["selected"] == 3
    assert "selection" not in default_payload and "cutoff" not in default_payload


def test_fetch_details_refresh_live_via_fixture_keeps_history(
    db_path, capsys, monkeypatch, fast_pacing
) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()
    assert (
        main(["--db", str(db_path), "fetch-details", "--fixture", str(DETAIL_RESPONSES)])
        == EXIT_OK
    )
    capsys.readouterr()

    # Age the stored evidence so the refresh predicate sees stale successes.
    _age_detail_fetches(db_path, "2026-07-01T00:00:00Z")

    # A fixture run is offline, so --confirm-network is not required for refresh.
    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--refresh",
                "--stale-days",
                "5",
                "--fixture",
                str(DETAIL_RESPONSES),
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["selection"] == "refresh"
    assert payload["stale_days"] == 5
    assert payload["cutoff"]
    assert payload["enriched"] == 3

    # The refresh appended provenance; the first attempts were not deleted.
    assert job_detail_fetch_count_for(db_path) == 6


def test_fetch_details_dry_run_does_not_create_a_database(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    db = tmp_path / "never-created.sqlite3"

    assert main(["--db", str(db), "fetch-details", "--dry-run", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["selected"] == 0
    assert payload["planned_urls"] == []
    assert payload["note"]

    # The filesystem is untouched: no database file, no journal sidecars.
    assert not db.exists()
    assert list(tmp_path.iterdir()) == []

    # The text output carries the same clear message.
    assert main(["--db", str(db), "fetch-details", "--dry-run"]) == EXIT_OK
    assert "nothing to plan" in capsys.readouterr().out
    assert not db.exists()
    assert list(tmp_path.iterdir()) == []


def test_fetch_details_dry_run_tolerates_an_uninitialized_database(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    db = tmp_path / "empty.sqlite3"
    db.write_bytes(b"")

    assert main(["--db", str(db), "fetch-details", "--dry-run", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] == 0
    assert payload["planned_urls"] == []
    assert payload["note"]
    # It is reported, not initialized or written.
    assert db.read_bytes() == b""


def test_fetch_details_dry_run_leaves_an_existing_database_untouched(
    db_path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()

    before_names = sorted(path.name for path in db_path.parent.iterdir())
    before_bytes = db_path.read_bytes()

    assert main(["--db", str(db_path), "fetch-details", "--dry-run", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] == 3
    assert payload["note"] is None

    assert db_path.read_bytes() == before_bytes
    assert sorted(path.name for path in db_path.parent.iterdir()) == before_names


def test_fetch_details_enforces_hard_caps(db_path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()
    exit_code = main(
        [
            "--db",
            str(db_path),
            "fetch-details",
            "--max-items",
            str(HARD_MAX_ITEMS + 1),
            "--dry-run",
        ]
    )
    assert exit_code == EXIT_ERROR
    assert "absolute upper bound" in capsys.readouterr().err
    # The search caps remain untouched.
    assert HARD_MAX_PAGES == 10


def test_fetch_details_fixture_smoke_then_score_and_rank(
    db_path, capsys, monkeypatch, fast_pacing
) -> None:
    no_network(monkeypatch)
    assert main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)]) == EXIT_OK
    capsys.readouterr()

    # Stored cards have no description yet.
    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    cards = json.loads(capsys.readouterr().out)["jobs"]
    assert all(item["job"]["content_status"] == "partial" for item in cards)

    assert (
        main(["--db", str(db_path), "fetch-details", "--fixture", str(DETAIL_RESPONSES)])
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert "3 enriched" in output
    assert "score --all --rescore" in output

    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    enriched = json.loads(capsys.readouterr().out)["jobs"]
    assert all(item["job"]["content_status"] == "ok" for item in enriched)
    assert all(item["job"]["description"] for item in enriched)

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--rescore",
                "--scorer",
                "heuristic",
                "--json",
            ]
        )
        == EXIT_OK
    )
    scored = json.loads(capsys.readouterr().out)
    # The fixture's "Senior Backend Engineer" is held back by the seniority filter.
    assert scored["succeeded"] == 2
    assert scored["skipped_seniority"] == 1
    assert scored["failed"] == []

    assert main(["--db", str(db_path), "rank", "--top", "3", "--json"]) == EXIT_OK
    ranked = json.loads(capsys.readouterr().out)
    assert ranked["count"] == 2
    assert ranked["jobs"][0]["score"] is not None
    assert ranked["jobs"][0]["score"] >= ranked["jobs"][-1]["score"]

    assert main(["--db", str(db_path), "runs", "--json"]) == EXIT_OK
    runs = json.loads(capsys.readouterr().out)
    assert runs["detail_fetches"] == job_detail_fetch_count_for(db_path) == 3


def test_fetch_details_fixture_fails_closed_on_access_denied(
    db_path, capsys, monkeypatch, tmp_path: Path, fast_pacing
) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()

    fixture = tmp_path / "denied.json"
    fixture.write_text(
        json.dumps(
            {
                "details": [
                    {
                        "job_id": "3911111111",
                        "status_code": 200,
                        "body": '<div class="show-more-less-html__markup">Python and SQL.</div>',
                    },
                    {"job_id": "3912222222", "status_code": 403, "body": "denied"},
                ]
            }
        ),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "--db",
            str(db_path),
            "fetch-details",
            "--fixture",
            str(fixture),
            "--all",
            "--json",
        ]
    )
    assert exit_code == EXIT_FAIL_CLOSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed_closed"
    assert payload["enriched"] == 1
    assert payload["failed"] == 1

    # The first job is enriched; the refused job keeps its card content.
    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    jobs = json.loads(capsys.readouterr().out)["jobs"]
    by_source_id = {item["job"]["source_job_id"]: item["job"] for item in jobs}
    assert by_source_id["3911111111"]["content_status"] == "ok"
    assert by_source_id["3912222222"]["content_status"] == "partial"


def test_fetch_details_fixture_needs_no_network_confirmation(
    db_path, capsys, monkeypatch, fast_pacing
) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--fixture",
                str(DETAIL_RESPONSES),
                "--max-items",
                "1",
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["enriched"] == 1
    assert payload["requested"] == 1
    # Three eligible jobs, capped at one: the live result reports the two the
    # cap dropped, not only the stored search-run params.
    assert payload["truncated"] == 2


def test_fetch_details_fixture_reports_max_items_truncation_in_text(
    db_path, capsys, monkeypatch, fast_pacing
) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--fixture",
                str(DETAIL_RESPONSES),
                "--max-items",
                "1",
            ]
        )
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert "1 enriched" in output
    assert "2 selected job(s) omitted by --max-items" in output


def test_fetch_details_json_output_reports_provenance_columns(
    db_path, capsys, monkeypatch, fast_pacing
) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()
    main(["--db", str(db_path), "fetch-details", "--fixture", str(DETAIL_RESPONSES)])
    capsys.readouterr()

    row = conn_fetch_one(db_path, "SELECT * FROM job_detail_fetches ORDER BY id LIMIT 1")
    assert row["status"] == "ok"
    assert row["url"] and "jobPosting/" in row["url"]
    assert row["sha256"] and row["content_hash"]
    assert row["fetched_at"]
    assert row["http_status"] == 200
    assert row["raw_document_id"] is not None
    assert row["error"] is None


# -- tiny sqlite helpers that do not depend on the in-memory ``conn`` fixture --


def conn_fetch_one(db_path: Path, sql: str):
    from jobscout.db import connect

    connection = connect(db_path)
    try:
        return connection.execute(sql).fetchone()
    finally:
        connection.close()


def job_detail_fetch_count_for(db_path: Path) -> int:
    from jobscout.db import connect

    connection = connect(db_path)
    try:
        return job_detail_fetch_count(connection)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Unseen re-check selection (--unseen-days) — EO-04, 2026-11-13
# ---------------------------------------------------------------------------


#: A sighting/detail attempt before this instant is "unseen" for these tests.
UNSEEN_CUTOFF = "2026-09-01T00:00:00Z"


def _set_job_last_seen(conn, job, *, at: str) -> None:
    """Overwrite the job-level sighting timestamp (selection reads only it)."""
    with conn:
        conn.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ?", (at, int(job.id)))


def _score(conn, job, *, overall: float) -> None:
    insert_evaluation(
        conn,
        Evaluation(
            job_id=int(job.id),
            scorer=ScorerName.HEURISTIC,
            overall_score=overall,
            recommendation=Recommendation.APPLY,
        ),
    )


def _insert_variant(
    conn, job, *, source: str, source_job_id: str, url: str, last_seen_at: str
) -> int:
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO job_variants (
                job_id, source, source_job_id, url, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (int(job.id), source, source_job_id, url, last_seen_at, last_seen_at),
        )
    return int(cursor.lastrowid)


def test_variants_unseen_since_order_scores_first_then_oldest_sighting(conn) -> None:
    high = _store_card(conn, "3923000001")
    low = _store_card(conn, "3923000002")
    unscored = _store_card(conn, "3923000003")
    unscored_older = _store_card(conn, "3923000004")
    _set_job_last_seen(conn, high, at="2026-08-10T00:00:00Z")
    _set_job_last_seen(conn, low, at="2026-08-01T00:00:00Z")
    _set_job_last_seen(conn, unscored, at="2026-07-01T00:00:00Z")
    _set_job_last_seen(conn, unscored_older, at="2026-06-01T00:00:00Z")
    _score(conn, high, overall=91.0)
    _score(conn, low, overall=47.5)

    selected = [
        job.id for job in variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF)
    ]
    # Scored offers first (highest score), unscored last; each group oldest
    # sighting first, so a bounded batch spends the budget on the best offers.
    assert selected == [high.id, low.id, unscored_older.id, unscored.id]
    assert count_variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF) == 4
    # A cap reports the same prefix; the count stays the full selection.
    capped = variants_unseen_since(
        conn, source="linkedin", cutoff=UNSEEN_CUTOFF, limit=2
    )
    assert [job.id for job in capped] == [high.id, low.id]


def test_variants_unseen_since_skips_recent_sightings_and_checks(conn) -> None:
    recently_seen = _store_card(conn, "3923100001")
    recently_checked = _store_card(conn, "3923100002")
    never_checked = _store_card(conn, "3923100003")
    stale_check = _store_card(conn, "3923100004")
    resighted_after_check = _store_card(conn, "3923100005")
    closed = _store_card(conn, "3923100006")

    # Unseen but sighted after the cutoff: still in the searches' reach.
    _set_job_last_seen(conn, recently_seen, at="2026-09-10T00:00:00Z")
    # Quiet offer whose last detail attempt is recent: already been re-checked.
    _set_job_last_seen(conn, recently_checked, at="2026-08-01T00:00:00Z")
    _insert_fetch(
        conn, recently_checked, status=DetailFetchStatus.OK, fetched_at="2026-09-05T00:00:00Z"
    )
    # Quiet offer never detail-checked: selected.
    _set_job_last_seen(conn, never_checked, at="2026-08-01T00:00:00Z")
    # Quiet offer whose last attempt is older than the cutoff: selected again.
    _set_job_last_seen(conn, stale_check, at="2026-08-01T00:00:00Z")
    _insert_fetch(
        conn, stale_check, status=DetailFetchStatus.OK, fetched_at="2026-08-15T00:00:00Z"
    )
    # An older stale attempt followed by a recent one: the latest attempt wins.
    _set_job_last_seen(conn, resighted_after_check, at="2026-08-01T00:00:00Z")
    _insert_fetch(
        conn, resighted_after_check, status=DetailFetchStatus.OK, fetched_at="2026-07-01T00:00:00Z"
    )
    _insert_fetch(
        conn,
        resighted_after_check,
        status=DetailFetchStatus.FAILED,
        http_status=404,
        stored_at="2026-09-06T00:00:00Z",
    )
    # A closed offer is never requested again, whatever its age.
    _set_job_last_seen(conn, closed, at="2026-06-01T00:00:00Z")
    deactivate_job(conn, int(closed.id))

    selected = [
        job.id for job in variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF)
    ]
    assert set(selected) == {never_checked.id, stale_check.id}
    assert count_variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF) == 2


def test_variants_unseen_since_ignores_non_answers(conn) -> None:
    rate_limited = _store_card(conn, "3923400001")
    transport = _store_card(conn, "3923400002")
    skipped = _store_card(conn, "3923400003")
    temporary = _store_card(conn, "3923400004")
    for job in (rate_limited, transport, skipped, temporary):
        _set_job_last_seen(conn, job, at="2026-07-01T00:00:00Z")

    # A 429/403 is the site refusing, not answering: the offer was not checked.
    _insert_fetch(
        conn,
        rate_limited,
        status=DetailFetchStatus.FAILED,
        http_status=429,
        stored_at="2026-09-10T00:00:00Z",
    )
    # No HTTP response at all: a transport failure.
    _insert_fetch(
        conn,
        transport,
        status=DetailFetchStatus.FAILED,
        stored_at="2026-09-10T00:00:00Z",
    )
    # The fetcher skipped the placeholder without asking anything.
    _insert_fetch(
        conn,
        skipped,
        status=DetailFetchStatus.SKIPPED,
        stored_at="2026-09-10T00:00:00Z",
    )
    # A 5xx is a real answer but not a definitive one; still not a check.
    _insert_fetch(
        conn,
        temporary,
        status=DetailFetchStatus.FAILED,
        http_status=503,
        stored_at="2026-09-10T00:00:00Z",
    )

    selected = {
        job.id
        for job in variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF)
    }
    assert selected == {rate_limited.id, transport.id, skipped.id, temporary.id}
    assert count_variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF) == 4


def test_variants_unseen_since_ignores_another_providers_recent_check(conn) -> None:
    job = _store_card(conn, "3923500001")
    _set_job_last_seen(conn, job, at="2026-07-01T00:00:00Z")
    tecno_url = "https://www.tecnoempleo.com/acme/rf-9999"
    _insert_variant(
        conn,
        job,
        source="tecnoempleo",
        source_job_id="rf-9999",
        url=tecno_url,
        last_seen_at="2026-07-01T00:00:00Z",
    )
    # LinkedIn answered recently; Tecnoempleo was never asked.
    _insert_fetch(
        conn,
        job,
        source="linkedin",
        status=DetailFetchStatus.OK,
        fetched_at="2026-09-10T00:00:00Z",
    )

    assert variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF) == []
    tecno_selected = variants_unseen_since(
        conn, source="tecnoempleo", cutoff=UNSEEN_CUTOFF
    )
    assert [entry.id for entry in tecno_selected] == [job.id]
    assert tecno_selected[0].url == tecno_url


def test_variants_unseen_since_returns_only_the_newest_provider_variant(conn) -> None:
    job = _store_card(conn, "3923200001")
    _set_job_last_seen(conn, job, at="2026-07-01T00:00:00Z")
    # A repost under a new id: two active variants of the same provider, the
    # first one created when the card was stored.
    older_url = "https://www.linkedin.com/jobs/view/3923200001/old"
    newer_url = "https://www.linkedin.com/jobs/view/3923200009/new"
    with conn:
        conn.execute(
            "UPDATE job_variants SET url = ?, last_seen_at = ? WHERE job_id = ?",
            (older_url, "2026-07-01T00:00:00Z", int(job.id)),
        )
    _insert_variant(
        conn,
        job,
        source="linkedin",
        source_job_id="3923200009",
        url=newer_url,
        last_seen_at="2026-07-15T00:00:00Z",
    )

    selected = variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF)
    # One entry per offer, carrying the newest variant's own identity.
    assert [entry.id for entry in selected] == [job.id]
    assert selected[0].url == newer_url
    assert selected[0].source_job_id == "3923200009"
    assert count_variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF) == 1


def test_variants_unseen_since_falls_back_to_last_seen_without_evaluations(conn) -> None:
    older = _store_card(conn, "3923300001")
    newer = _store_card(conn, "3923300002")
    _set_job_last_seen(conn, older, at="2026-06-01T00:00:00Z")
    _set_job_last_seen(conn, newer, at="2026-08-01T00:00:00Z")

    # No evaluations at all: every row is unscored and the stalest sighting wins.
    assert [
        job.id for job in variants_unseen_since(conn, source="linkedin", cutoff=UNSEEN_CUTOFF)
    ] == [older.id, newer.id]


def _seed_unseen_db(db_path: Path) -> dict[str, int]:
    """Seed one offer per unseen branch, relative to the test's own clock."""
    reference = utcnow()
    connection = connect(db_path)
    try:
        init_db(connection)
        good = _store_card(connection, "3924000001")
        quiet = _store_card(connection, "3924000002")
        fresh = _store_card(connection, "3924000003")
        _set_job_last_seen(connection, good, at=iso_days_ago(30, now=reference))
        _set_job_last_seen(connection, quiet, at=iso_days_ago(20, now=reference))
        _set_job_last_seen(connection, fresh, at=iso_days_ago(2, now=reference))
        _score(connection, good, overall=88.0)
        # The best offer was already re-checked recently and must wait its turn.
        _insert_fetch(
            connection,
            quiet,
            status=DetailFetchStatus.OK,
            fetched_at=iso_days_ago(1, now=reference),
        )
        return {"good": int(good.id), "quiet": int(quiet.id), "fresh": int(fresh.id)}
    finally:
        connection.close()


def test_fetch_details_unseen_days_dry_run_json(db_path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    ids = _seed_unseen_db(db_path)
    before_bytes = db_path.read_bytes()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--unseen-days",
                "14",
                "--dry-run",
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["selection"] == "unseen"
    assert payload["unseen_days"] == 14
    assert payload["cutoff"]
    # Only the stale offer whose last detail attempt is also older than 14 days.
    assert payload["selected"] == 1
    assert len(payload["planned_urls"]) == 1
    assert "3924000001" in payload["planned_urls"][0]

    # The text report carries the same additive unseen context.
    assert (
        main(
            ["--db", str(db_path), "fetch-details", "--unseen-days", "14", "--dry-run"]
        )
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert "unseen selection:" in output
    assert "unseen for 14 days" in output
    assert payload["cutoff"] in output

    # A dry run writes nothing, not even a journal sidecar.
    assert db_path.read_bytes() == before_bytes
    assert ids["quiet"] != ids["good"]


def test_fetch_details_unseen_days_reports_truncation(db_path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    _seed_unseen_db(db_path)
    main(
        [
            "--db",
            str(db_path),
            "fetch-details",
            "--unseen-days",
            "14",
            "--max-items",
            "1",
            "--dry-run",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] == 1
    assert payload["truncated"] == 0


def test_fetch_details_unseen_days_empty_selection_is_a_success(
    db_path, capsys, monkeypatch, fast_pacing
) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(CARDS_FIXTURE)])
    capsys.readouterr()

    # The fixture cards were just seen, so nothing is unseen and nothing is
    # requested, not even through the offline fixture transport.
    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--unseen-days",
                "14",
                "--fixture",
                str(DETAIL_RESPONSES),
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["selection"] == "unseen"
    assert payload["cutoff"]
    assert payload["requested"] == 0
    assert payload["enriched"] == 0
    assert job_detail_fetch_count_for(db_path) == 0


def test_fetch_details_unseen_days_rejects_zero(db_path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    assert (
        main(
            ["--db", str(db_path), "fetch-details", "--unseen-days", "0", "--dry-run"]
        )
        == EXIT_ERROR
    )
    assert "--unseen-days must be 1 or greater" in capsys.readouterr().err
    assert not db_path.exists()


def test_fetch_details_unseen_days_rejects_more_than_a_decade(
    db_path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    # Huge values would overflow the cutoff arithmetic before any selection.
    assert (
        main(
            ["--db", str(db_path), "fetch-details", "--unseen-days", "4000", "--dry-run"]
        )
        == EXIT_ERROR
    )
    assert "--unseen-days must be 3650 or less" in capsys.readouterr().err
    assert not db_path.exists()
    assert (
        main(
            [
                "--db",
                str(db_path),
                "fetch-getmanfred-details",
                "--unseen-days",
                "999999999",
                "--dry-run",
            ]
        )
        == EXIT_ERROR
    )
    assert "--unseen-days must be 3650 or less" in capsys.readouterr().err


def test_fetch_details_unseen_days_is_exclusive_with_refresh(db_path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--db",
                str(db_path),
                "fetch-details",
                "--refresh",
                "--unseen-days",
                "14",
                "--dry-run",
            ]
        )


def test_provider_detail_commands_accept_unseen_days(db_path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    connection = connect(db_path)
    try:
        init_db(connection)
        tecno = _store_foreign_card(
            connection,
            "tecnoempleo",
            "rf-205650",
            url="https://www.tecnoempleo.com/aceptacorp/rf-205650",
        )
        getmanfred = _store_foreign_card(
            connection,
            "getmanfred",
            "8383",
            url="https://www.getmanfred.com/ofertas-empleo/8383/backend-8383",
        )
        for job in (tecno, getmanfred):
            _set_job_last_seen(connection, job, at="2026-06-01T00:00:00Z")
    finally:
        connection.close()

    for command, source_id in (
        ("fetch-tecnoempleo-details", "rf-205650"),
        ("fetch-getmanfred-details", "8383"),
    ):
        assert (
            main(
                ["--db", str(db_path), command, "--unseen-days", "14", "--dry-run", "--json"]
            )
            == EXIT_OK
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["selection"] == "unseen"
        assert payload["unseen_days"] == 14
        assert payload["cutoff"]
        # Each command only ever selects its own provider's offers.
        assert payload["selected"] == 1
        assert len(payload["planned_urls"]) == 1
        assert source_id in payload["planned_urls"][0]

        assert (
            main(["--db", str(db_path), command, "--unseen-days", "0", "--dry-run"])
            == EXIT_ERROR
        )
        assert "--unseen-days must be 1 or greater" in capsys.readouterr().err
