"""GetManfred detail-enrichment tests: JSON-LD parsing, robots, gating, persistence.

No real network request is ever made. The transport is the offline fake client,
``robots.txt`` is injected as text (or served by the fake client), and the detail
page fixture is the real captured ``JobPosting`` JSON-LD block.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeHttpClient, fixture_text, no_network, response

from jobscout.cli import EXIT_FAIL_CLOSED, EXIT_OK, main
from jobscout.collectors.base import CollectorDisabledError, TRUNCATION_HEADER
from jobscout.collectors.getmanfred_details import (
    GetManfredDetailConfig,
    extract_json_ld_scripts,
    fetch_getmanfred_details,
    parse_detail_company,
    parse_detail_description,
    parse_detail_employment_type,
    parse_detail_fields,
    parse_detail_location,
    parse_detail_posted_at,
    parse_detail_salary,
    parse_json_ld_job_posting,
    plan_detail_targets,
    resolve_detail_url,
)
from jobscout.collectors.linkedin_details import DetailItem, DetailResult
from jobscout.db import (
    detail_fetches_for_job,
    get_job,
    insert_detail_fetch,
    latest_detail_fetch,
    store_posting,
)
from jobscout.models import (
    ContentStatus,
    DetailFetchStatus,
    JobDetailFetch,
    SearchRunStatus,
)
from jobscout.normalize import normalize_job_posting
from jobscout.pipeline import ingest_detail_results

DETAIL_FIXTURE = "getmanfred_detail_page_jobposting.html"
OFFER_URL = (
    "https://www.getmanfred.com/ofertas-empleo/8476/"
    "datcisions-backend-engineer-php-sept26"
)
ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
# Denies the offer detail path itself, so the adapter must refuse before any
# detail request is sent.
ROBOTS_DENY = "User-agent: *\nDisallow: /ofertas-empleo/\n"


def _json_ld_script(payload: str) -> str:
    return (
        '<script id="jobPostingSchema" type="application/ld+json">'
        f"{payload}</script>"
    )


def _store_offer(
    conn,
    *,
    url: str = OFFER_URL,
    source_job_id: str = "8476",
    description: str | None = None,
    content_status: ContentStatus = ContentStatus.PARTIAL,
):
    """Store one active GetManfred inventory offer (partial by default)."""
    raw: dict[str, object] = {
        "source": "getmanfred",
        "source_job_id": source_job_id,
        "url": url,
        "title": "Backend Engineer Php Sept26",
        "company": "",
        "content_status": str(content_status),
    }
    if description is not None:
        raw["description"] = description
    posting = normalize_job_posting(raw, source="getmanfred")
    job, _ = store_posting(conn, posting)
    return get_job(conn, int(job.id))


def _store_linkedin_offer(conn):
    posting = normalize_job_posting(
        {
            "source": "linkedin",
            "source_job_id": "4457430073",
            "url": "https://www.linkedin.com/jobs/view/x-4457430073",
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "content_status": str(ContentStatus.PARTIAL),
        },
        source="linkedin",
    )
    job, _ = store_posting(conn, posting)
    return get_job(conn, int(job.id))


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------


def test_parse_real_captured_json_ld_extracts_recognized_fields() -> None:
    text = fixture_text(DETAIL_FIXTURE)
    fields = parse_detail_fields(text)
    assert fields.recognized is True
    assert "Datcisions nace para resolver este drama" in fields.description
    assert fields.company == "Datcisions"
    # The address carries an empty addressLocality, so location stays absent.
    assert fields.location is None
    assert fields.posted_at == "2026-09-18T07:41:22Z"
    # FULL_TIME maps to the canonical bucket the database already uses.
    assert fields.employment_type == "full_time"
    assert fields.salary == "€35-45K"
    assert len(fields.description) >= 400


def test_parse_detail_missing_optional_fields_stay_none() -> None:
    payload = (
        '{"@context":"https://schema.org","@type":"JobPosting",'
        '"description":"A description long enough to be real."}'
    )
    fields = parse_detail_fields(_json_ld_script(payload))
    assert fields.recognized is True
    assert fields.company is None
    assert fields.location is None
    assert fields.posted_at is None
    assert fields.employment_type is None
    assert fields.salary is None


def test_parse_detail_short_description_is_not_recognized() -> None:
    payload = '{"@type":"JobPosting","description":"short"}'
    assert parse_detail_fields(_json_ld_script(payload)).recognized is False


def test_parse_detail_unknown_or_malformed_markup_never_invents_content() -> None:
    drift = "<html><body><h1>Not a JSON-LD offer</h1></body></html>"
    assert parse_json_ld_job_posting(drift) is None
    assert parse_detail_description(drift) == ""
    assert parse_detail_company(drift) is None
    assert parse_detail_location(drift) is None
    assert parse_detail_posted_at(drift) is None
    assert parse_detail_employment_type(drift) is None
    assert parse_detail_salary(drift) is None
    assert parse_detail_description("") == ""
    # A JSON-LD script with invalid JSON is skipped, not guessed.
    broken = _json_ld_script("{not json")
    assert parse_json_ld_job_posting(broken) is None
    # A JSON-LD script without JobPosting is skipped.
    other = _json_ld_script('{"@type":"Organization","name":"Datcisions"}')
    assert parse_json_ld_job_posting(other) is None


def test_parse_detail_handles_graph_and_employment_list() -> None:
    payload = (
        '{"@context":"https://schema.org","@graph":['
        '{"@type":"Organization","name":"Other"},'
        '{"@type":"JobPosting","description":"A description long enough to be real.",'
        '"employmentType":["OTHER","FULL_TIME"]}]}'
    )
    fields = parse_detail_fields(_json_ld_script(payload))
    assert fields.recognized is True
    assert fields.company is None
    assert fields.employment_type == "full_time"


def test_parse_detail_description_renders_html_markup() -> None:
    payload = (
        '{"@type":"JobPosting",'
        '"description":"<p>A description with <strong>markup</strong> that is real.</p>"}'
    )
    text = parse_detail_description(_json_ld_script(payload))
    assert "markup" in text
    assert "<" not in text


def test_extract_json_ld_scripts_requires_the_declared_type() -> None:
    html = (
        '<script type="text/javascript">var x = 1;</script>'
        + _json_ld_script('{"@type":"JobPosting","description":"A real description here."}')
    )
    scripts = extract_json_ld_scripts(html)
    assert len(scripts) == 1
    assert "JobPosting" in scripts[0]


# ---------------------------------------------------------------------------
# URL resolution and planning
# ---------------------------------------------------------------------------


def _job_stub(url, source_job_id="8476"):
    return SimpleNamespace(id=1, url=url, source_job_id=source_job_id)


def test_resolve_detail_url_only_accepts_known_offer_urls() -> None:
    assert resolve_detail_url(_job_stub(OFFER_URL)) == OFFER_URL
    assert resolve_detail_url(_job_stub("https://evil.test/ofertas-empleo/1/x")) is None
    assert resolve_detail_url(_job_stub("http://www.getmanfred.com/ofertas-empleo/1/x")) is None
    assert resolve_detail_url(_job_stub("https://www.getmanfred.com/other/1/x")) is None
    assert resolve_detail_url(
        _job_stub("https://user:pass@www.getmanfred.com/ofertas-empleo/1/x")
    ) is None
    assert resolve_detail_url(_job_stub("https://www.getmanfred.com/ofertas-empleo/no-id")) is None
    assert resolve_detail_url(_job_stub(None)) is None


def test_plan_detail_targets_skips_unresolvable_jobs() -> None:
    good = _job_stub(OFFER_URL)
    bad = _job_stub("https://evil.test/ofertas-empleo/9999/x", "9999")
    targets, truncated = plan_detail_targets([good, bad], max_items=5)
    assert truncated == 0
    assert targets[0].url == OFFER_URL
    assert targets[1].url is None and targets[1].reason


def test_detail_config_enforces_hard_caps() -> None:
    from jobscout.config import HARD_MAX_ITEMS

    assert GetManfredDetailConfig(max_items=HARD_MAX_ITEMS).max_items == HARD_MAX_ITEMS
    with pytest.raises(ValueError):
        GetManfredDetailConfig(max_items=HARD_MAX_ITEMS + 1)
    with pytest.raises(ValueError):
        GetManfredDetailConfig(max_items=0)


def test_getmanfred_detail_settings_validate_env() -> None:
    from jobscout.config import HARD_MAX_ITEMS, ConfigError, Settings

    settings = Settings.from_env(
        env={"JOBSCOUT_GETMANFRED_DETAIL_MAX_ITEMS": "20"}, dotenv_path=None
    )
    assert settings.getmanfred_detail_max_items == 20
    assert settings.getmanfred_robots_url == "https://www.getmanfred.com/robots.txt"
    with pytest.raises(ConfigError):
        Settings.from_env(
            env={"JOBSCOUT_GETMANFRED_DETAIL_MAX_ITEMS": str(HARD_MAX_ITEMS + 1)},
            dotenv_path=None,
        )
    with pytest.raises(ConfigError):
        Settings.from_env(
            env={"JOBSCOUT_GETMANFRED_ROBOTS_URL": "http://www.getmanfred.com/robots.txt"},
            dotenv_path=None,
        )


# ---------------------------------------------------------------------------
# Opt-in, dry run, robots
# ---------------------------------------------------------------------------


def test_detail_dry_run_makes_no_request(conn, settings, monkeypatch) -> None:
    no_network(monkeypatch)
    job = _store_offer(conn)
    result = fetch_getmanfred_details([job], settings=settings, dry_run=True)
    assert result.status is SearchRunStatus.DRY_RUN
    assert result.items == []
    assert result.requests_made == 0
    # The plan is still reported.
    assert result.planned_urls == [OFFER_URL]


def test_detail_disabled_without_opt_in(conn, settings, monkeypatch) -> None:
    no_network(monkeypatch)
    job = _store_offer(conn)
    with pytest.raises(CollectorDisabledError) as excinfo:
        fetch_getmanfred_details([job], settings=settings)
    assert "--confirm-network" in str(excinfo.value)


def test_detail_robots_disallow_fails_closed_before_request(conn, settings) -> None:
    job = _store_offer(conn)
    client = FakeHttpClient([])  # no detail request is expected
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_DENY,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert "robots.txt disallows" in (result.error or "")
    assert client.call_count == 0


def test_detail_refetches_robots_before_the_first_offer_request(conn, settings) -> None:
    job = _store_offer(conn)
    client = FakeHttpClient(
        [
            response(200, ROBOTS_ALLOW, headers={"content-type": "text/plain"}),
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html"},
                url=OFFER_URL,
            ),
        ]
    )
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
    )
    assert client.calls[0]["url"] == settings.getmanfred_robots_url
    assert client.calls[1]["url"] == OFFER_URL
    assert result.items[0].status is DetailFetchStatus.OK


def test_sanitize_final_url_uses_the_strict_shared_policy() -> None:
    from jobscout.collectors.getmanfred_details import _sanitize_final_url

    requested = OFFER_URL
    allowed = "https://www.getmanfred.com/ofertas-empleo/8476/other-slug"
    assert _sanitize_final_url(requested, allowed) == allowed
    # No redirect observed.
    assert _sanitize_final_url(requested, requested) is None
    assert _sanitize_final_url(requested, "") is None
    assert _sanitize_final_url(requested, None) is None
    # HTTPS only, no credentials, default port, allowed host.
    assert _sanitize_final_url(requested, "http://www.getmanfred.com/x") is None
    assert _sanitize_final_url(requested, "https://www.getmanfred.com:8443/x") is None
    assert _sanitize_final_url(requested, "https://user:pass@www.getmanfred.com/x") is None
    assert _sanitize_final_url(requested, "https://evil.test/x") is None
    assert _sanitize_final_url(requested, "https://www.getmanfred.com.evil.test/x") is None
    assert _sanitize_final_url(requested, "file:///etc/passwd") is None


# ---------------------------------------------------------------------------
# Fetch behaviour
# ---------------------------------------------------------------------------


def test_detail_recognized_page_is_ok_with_extracted_fields(conn, settings) -> None:
    job = _store_offer(conn)
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html"},
                url=OFFER_URL,
            )
        ]
    )
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    item = result.items[0]
    assert item.status is DetailFetchStatus.OK
    assert item.content_status is ContentStatus.OK
    assert item.description
    assert item.company == "Datcisions"
    assert item.location is None
    assert item.posted_at == "2026-09-18T07:41:22Z"
    assert item.employment_type == "full_time"
    assert item.salary == "€35-45K"


def test_detail_unknown_markup_stays_partial_honestly(conn, settings) -> None:
    job = _store_offer(conn)
    client = FakeHttpClient([response(200, "<html><body>drift</body></html>")])
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    item = result.items[0]
    assert item.status is DetailFetchStatus.PARTIAL
    assert item.content_status is ContentStatus.PARTIAL
    assert item.description is None
    assert "no recognized" in (item.error or "")


def test_detail_access_denied_fails_closed_and_records_provenance(conn, settings) -> None:
    job = _store_offer(conn)
    client = FakeHttpClient([response(403, "denied")])
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert client.call_count == 1
    assert result.items[0].status is DetailFetchStatus.FAILED
    assert result.raw_documents and result.raw_documents[0].status_code == 403


def test_detail_truncated_body_is_failed_not_parsed(conn, settings) -> None:
    job = _store_offer(conn)
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html", TRUNCATION_HEADER: "1"},
            )
        ]
    )
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    item = result.items[0]
    assert item.status is DetailFetchStatus.FAILED
    assert item.description is None
    assert "download limit" in (item.error or "")


def test_detail_records_safe_same_host_redirect(conn, settings) -> None:
    job = _store_offer(conn)
    redirected = "https://www.getmanfred.com/ofertas-empleo/8476/renamed"
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html"},
                url=redirected,
            )
        ]
    )
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.items[0].final_url == redirected


def test_detail_cross_host_redirect_is_not_recorded(conn, settings) -> None:
    job = _store_offer(conn)
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html"},
                url="https://evil.test/ofertas-empleo/8476/x",
            )
        ]
    )
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.items[0].final_url is None


# ---------------------------------------------------------------------------
# Selection (provider-scoped)
# ---------------------------------------------------------------------------


def test_selection_scopes_every_route_to_getmanfred(conn, settings) -> None:
    from jobscout.cli import _select_getmanfred_detail_jobs

    getmanfred_job = _store_offer(conn)
    linkedin_job = _store_linkedin_offer(conn)

    default_jobs, _ = _select_getmanfred_detail_jobs(
        conn, SimpleNamespace(job_id=None, all=False), limit=10
    )
    assert {job.source for job in default_jobs} == {"getmanfred"}

    all_jobs, _ = _select_getmanfred_detail_jobs(
        conn, SimpleNamespace(job_id=None, all=True), limit=10
    )
    assert {job.source for job in all_jobs} == {"getmanfred"}

    id_jobs, _ = _select_getmanfred_detail_jobs(
        conn,
        SimpleNamespace(job_id=[getmanfred_job.id, linkedin_job.id], all=False),
        limit=10,
    )
    assert [job.id for job in id_jobs] == [getmanfred_job.id]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_ingest_detail_enriches_job_and_writes_provenance(conn, settings) -> None:
    job = _store_offer(conn)
    assert job.content_status is ContentStatus.PARTIAL

    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html"},
                url=OFFER_URL,
            )
        ]
    )
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    outcome = ingest_detail_results(
        conn, result, settings=settings, provider="getmanfred"
    )

    refreshed = get_job(conn, int(job.id))
    assert refreshed.content_status is ContentStatus.OK
    assert refreshed.description
    assert refreshed.company == "Datcisions"
    assert refreshed.location is None
    assert refreshed.posted_at == "2026-09-18T07:41:22Z"
    assert refreshed.employment_type == "full_time"
    assert refreshed.salary == "€35-45K"

    fetch = latest_detail_fetch(conn, int(job.id))
    assert fetch.status is DetailFetchStatus.OK
    assert fetch.source == "getmanfred"
    assert outcome.enriched == 1
    run = conn.execute(
        "SELECT provider FROM search_runs WHERE id = ?", (outcome.run_id,)
    ).fetchone()
    assert run["provider"] == "getmanfred-details"


def test_ingest_detail_provider_isolation(conn, settings) -> None:
    from jobscout.db import jobs_needing_details

    getmanfred_job = _store_offer(conn)
    linkedin_job = _store_linkedin_offer(conn)
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html"},
                url=OFFER_URL,
            )
        ]
    )
    result = fetch_getmanfred_details(
        [getmanfred_job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    ingest_detail_results(conn, result, settings=settings, provider="getmanfred")

    assert getmanfred_job.id not in {
        job.id for job in jobs_needing_details(conn, source="getmanfred")
    }
    # A LinkedIn-scoped query never selects the enriched GetManfred offer.
    assert linkedin_job.id in {
        job.id for job in jobs_needing_details(conn, source="linkedin")
    }
    fetch = latest_detail_fetch(conn, int(getmanfred_job.id))
    assert fetch.source == "getmanfred"


def test_ingest_detail_partial_never_overwrites_full_description(conn, settings) -> None:
    job = _store_offer(
        conn,
        description="A complete stored description that must survive.",
        content_status=ContentStatus.OK,
    )
    client = FakeHttpClient([response(200, "<html><body>drift</body></html>")])
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    ingest_detail_results(conn, result, settings=settings, provider="getmanfred")
    assert get_job(conn, int(job.id)).description == (
        "A complete stored description that must survive."
    )


def _record_successful_fetch(conn, job, *, source: str) -> None:
    """Seed one successful provenance row as if ``source`` had enriched the job."""
    insert_detail_fetch(
        conn,
        JobDetailFetch(
            job_id=int(job.id),
            source=source,
            url="https://example.test/detail",
            status=DetailFetchStatus.OK,
            content_status=str(ContentStatus.OK),
            stored_at="2026-09-20T00:00:00Z",
        ),
    )


def _fetch_recognized_page(job, settings):
    """Run one GetManfred detail fetch against the real captured JSON-LD page."""
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html"},
                url=OFFER_URL,
            )
        ]
    )
    return fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )


def test_foreign_provenance_allows_a_shorter_detail_to_correct_the_text(
    conn, settings
) -> None:
    """DP-6 repair: a description known to be foreign is replaced even if shorter."""
    foreign_description = "Foreign LinkedIn-sourced description. " * 200
    job = _store_offer(
        conn, description=foreign_description, content_status=ContentStatus.OK
    )
    _record_successful_fetch(conn, job, source="linkedin")

    outcome = ingest_detail_results(
        conn,
        _fetch_recognized_page(job, settings),
        settings=settings,
        provider="getmanfred",
    )

    corrected = parse_detail_description(fixture_text(DETAIL_FIXTURE))
    refreshed = get_job(conn, int(job.id))
    assert len(corrected) < len(foreign_description)
    assert refreshed.description == corrected
    assert refreshed.content_status is ContentStatus.OK
    assert outcome.changed == 1
    assert outcome.preserved == 0

    # No historical provenance row is deleted; the new success is on top.
    fetches = detail_fetches_for_job(conn, int(job.id))
    assert [fetch.source for fetch in fetches] == ["getmanfred", "linkedin"]


def test_same_provider_provenance_preserves_a_longer_full_description(
    conn, settings
) -> None:
    existing_description = ("Existing GetManfred description. " * 200).rstrip()
    job = _store_offer(
        conn, description=existing_description, content_status=ContentStatus.OK
    )
    _record_successful_fetch(conn, job, source="getmanfred")

    ingest_detail_results(
        conn,
        _fetch_recognized_page(job, settings),
        settings=settings,
        provider="getmanfred",
    )

    refreshed = get_job(conn, int(job.id))
    assert refreshed.description == existing_description
    # The attempt is still recorded as a successful fetch for provenance.
    assert latest_detail_fetch(conn, int(job.id)).status is DetailFetchStatus.OK


def test_foreign_provenance_does_not_let_a_partial_replace_full_text(
    conn, settings
) -> None:
    foreign_description = "Foreign text a partial attempt must not replace."
    job = _store_offer(
        conn, description=foreign_description, content_status=ContentStatus.OK
    )
    _record_successful_fetch(conn, job, source="linkedin")

    client = FakeHttpClient([response(200, "<html><body>drift</body></html>")])
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.items[0].status is DetailFetchStatus.PARTIAL
    ingest_detail_results(conn, result, settings=settings, provider="getmanfred")

    assert get_job(conn, int(job.id)).description == foreign_description


def test_foreign_provenance_does_not_let_a_failed_fetch_replace_full_text(
    conn, settings
) -> None:
    foreign_description = "Foreign text a failed attempt must not replace."
    job = _store_offer(
        conn, description=foreign_description, content_status=ContentStatus.OK
    )
    _record_successful_fetch(conn, job, source="linkedin")

    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html", TRUNCATION_HEADER: "1"},
            )
        ]
    )
    result = fetch_getmanfred_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.items[0].status is DetailFetchStatus.FAILED
    ingest_detail_results(conn, result, settings=settings, provider="getmanfred")

    assert get_job(conn, int(job.id)).description == foreign_description


def test_unknown_provenance_keeps_the_ordinary_length_policy(conn, settings) -> None:
    existing_description = ("Stored description with no provenance " * 100).rstrip()
    job = _store_offer(
        conn, description=existing_description, content_status=ContentStatus.OK
    )
    # No provenance row exists: the prior source is unknown, so the ordinary
    # "keep the longer full description" rule still applies.
    ingest_detail_results(
        conn,
        _fetch_recognized_page(job, settings),
        settings=settings,
        provider="getmanfred",
    )

    assert get_job(conn, int(job.id)).description == existing_description


def test_a_fetch_from_another_provider_still_preserves_the_full_text(
    conn, settings
) -> None:
    """Condition (b): only the offer's own provider may trigger the correction."""
    foreign_description = "Foreign text that must not be replaced by another provider."
    job = _store_offer(
        conn, description=foreign_description, content_status=ContentStatus.OK
    )
    _record_successful_fetch(conn, job, source="linkedin")

    # A LinkedIn detail run (provider != job.source) must not rewrite a
    # GetManfred offer, even though the stored description is foreign.
    result = DetailResult(
        items=[
            DetailItem(
                job_id=int(job.id),
                source_job_id=job.source_job_id,
                url="https://www.linkedin.com/jobs/view/x-1",
                status=DetailFetchStatus.OK,
                description="shorter linkedin text",
                content_status=ContentStatus.OK,
            )
        ],
        requested=1,
    )
    ingest_detail_results(conn, result, settings=settings, provider="linkedin")

    assert get_job(conn, int(job.id)).description == foreign_description


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_fetch_getmanfred_details_refuses_without_confirmation(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    db_path = tmp_path / "getmanfred-details.sqlite3"
    exit_code = main(["--db", str(db_path), "fetch-getmanfred-details"])
    assert exit_code == EXIT_FAIL_CLOSED
    err = capsys.readouterr().err
    assert "--confirm-network" in err


def test_cli_fetch_getmanfred_details_dry_run_no_network(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    import json

    no_network(monkeypatch)
    db_path = tmp_path / "getmanfred-details-dry.sqlite3"
    exit_code = main(
        ["--db", str(db_path), "fetch-getmanfred-details", "--dry-run", "--json"]
    )
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["planned_urls"] == []
