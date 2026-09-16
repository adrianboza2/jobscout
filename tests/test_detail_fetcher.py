"""Job-detail fetcher tests: parsing, opt-in gating, persistence, and bounds.

No real LinkedIn request is ever made. Live-looking paths use the fake
transport from ``conftest``; the CLI smoke path uses the offline ``--fixture``
transport. Both are asserted to run under a socket blocker.
"""

from __future__ import annotations

import json
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
)
from jobscout.config import HARD_MAX_ITEMS, HARD_MAX_PAGES
from jobscout.db import (
    all_active_jobs,
    detail_fetches_for_job,
    get_job,
    job_detail_fetch_count,
    jobs_needing_details,
    latest_detail_fetch,
    store_posting,
    update_job_content,
)
from jobscout.models import (
    ContentStatus,
    DetailFetchStatus,
    RawDocumentKind,
    SearchRunStatus,
)
from jobscout.normalize import normalize_job_posting
from jobscout.pipeline import ingest_detail_results

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
                "--json",
            ]
        )
        == EXIT_OK
    )
    scored = json.loads(capsys.readouterr().out)
    assert scored["succeeded"] == 3
    assert scored["failed"] == []

    assert main(["--db", str(db_path), "rank", "--top", "3", "--json"]) == EXIT_OK
    ranked = json.loads(capsys.readouterr().out)
    assert ranked["count"] == 3
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
