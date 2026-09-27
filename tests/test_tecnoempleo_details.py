"""Tecnoempleo detail-enrichment tests: parsing, robots, gating, persistence.

No real network request is ever made. The transport is the offline fake client,
``robots.txt`` is injected as text, and detail pages are local fixtures written
to the documented server-rendered shape.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeHttpClient, FIXTURES, fixture_text, no_network, response

from jobscout.cli import EXIT_ERROR, EXIT_FAIL_CLOSED, EXIT_OK, main
from jobscout.collectors.base import CollectorDisabledError, TRUNCATION_HEADER
from jobscout.collectors.tecnoempleo import TecnoempleoSearchConfig, search_tecnoempleo
from jobscout.collectors.tecnoempleo_details import (
    TecnoempleoDetailConfig,
    fetch_tecnoempleo_details,
    parse_detail_company,
    parse_detail_description,
    parse_detail_fields,
    parse_detail_location,
    parse_detail_posted_at,
    parse_detail_technologies,
    plan_detail_targets,
    resolve_detail_url,
)
from jobscout.db import count_jobs, get_job, latest_detail_fetch
from jobscout.models import ContentStatus, DetailFetchStatus, SearchRunStatus
from jobscout.pipeline import ingest_collector_result, ingest_detail_results

PAGE_FIXTURE = FIXTURES / "tecnoempleo_search_page.html"
DETAIL_FIXTURE = FIXTURES / "tecnoempleo_detail_page.html"
REAL_DETAIL_FIXTURE = FIXTURES / "tecnoempleo_detail_page_real.html"
DRIFT_FIXTURE = FIXTURES / "tecnoempleo_detail_page_drift.html"
ROBOTS_ALLOW = "User-agent: *\nDisallow: /alertas-empleo-rss.php\nDisallow: /profesionales/\n"
# Denies the offer detail path itself (not only the search path), so the detail
# adapter must refuse before sending any request.
ROBOTS_DENY = "User-agent: *\nDisallow: /backend-python-engineer/\n"
BACKEND_URL = (
    "https://www.tecnoempleo.com/desarrollador-backend-python/"
    "grupo-digital/rf-7a491a37220693ec4248"
)


def _store_search_offers(conn, settings) -> list:
    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_search_page.html"), headers={"content-type": "text/html"})]
    )
    config = TecnoempleoSearchConfig(keywords="backend", max_pages=1, max_items=10)
    result = search_tecnoempleo(
        config,
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    ingest_collector_result(conn, result, config=config, settings=settings)
    from jobscout.db import all_active_jobs

    return all_active_jobs(conn)


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------


def test_parse_detail_description_extracts_recognized_markup() -> None:
    text = parse_detail_description(fixture_text("tecnoempleo_detail_page.html"))
    assert "desarrollador backend con experiencia en Python y SQL" in text
    assert "FastAPI" in text
    assert "<" not in text


def test_parse_detail_fields_extracts_company_location_date_and_technologies() -> None:
    fields = parse_detail_fields(fixture_text("tecnoempleo_detail_page.html"))
    assert fields.recognized is True
    assert fields.company == "Grupo Digital"
    assert fields.location == "Madrid"
    assert fields.posted_at == "2026-09-10T00:00:00Z"
    assert "Python" in fields.technologies
    assert "Kubernetes" in fields.technologies
    assert "Azure" in fields.technologies


def test_parse_detail_real_markup_extracts_company_location_and_date() -> None:
    fields = parse_detail_fields(fixture_text("tecnoempleo_detail_page_real.html"))
    assert fields.recognized is True
    # The company comes from the /<slug>/re-<id> link, not the breadcrumb name.
    assert fields.company == "Aceptacorp"
    assert fields.company != "Backend Engineer"
    # The city comes from the real <title> pattern...
    assert fields.location == "Madrid"
    # ...and the date from the strict dd/mm/yyyy in og:description.
    assert fields.posted_at == "2026-07-03T00:00:00Z"


def test_parse_detail_company_prefers_real_anchor_over_breadcrumb() -> None:
    html = (
        "<title>Oferta de Empleo Backend Engineer en Madrid, Aceptacorp - "
        "Tecnoempleo.com</title>"
        "<ol itemscope itemtype='https://schema.org/BreadcrumbList'>"
        "<li itemprop='itemListElement'>"
        "<span itemprop='name'>Backend Engineer</span></li></ol>"
        "<a href='https://www.tecnoempleo.com/aceptacorp/re-205650' "
        "title='Ofertas de empleo de Aceptacorp'>"
        "<span itemprop='name'>Aceptacorp</span></a>"
    )
    assert parse_detail_company(html) == "Aceptacorp"


def test_parse_detail_company_breadcrumb_name_is_not_a_company() -> None:
    # Only the breadcrumb carries a company-shaped itemprop; it must be refused.
    html = (
        "<title>Oferta de Empleo Backend Engineer en Madrid, Aceptacorp - "
        "Tecnoempleo.com</title>"
        "<ol itemscope itemtype='https://schema.org/BreadcrumbList'>"
        "<li itemprop='itemListElement'>"
        "<span itemprop='name'>Backend Engineer</span></li></ol>"
    )
    assert parse_detail_company(html) is None


def test_parse_detail_company_falls_back_to_twitter_data1() -> None:
    html = (
        "<title>Oferta de Empleo Backend Engineer en Madrid, Aceptacorp - "
        "Tecnoempleo.com</title>"
        "<meta name='twitter:data1' content='Aceptacorp '>"
    )
    assert parse_detail_company(html) == "Aceptacorp"


def test_parse_detail_company_guardrail_refuses_offer_title() -> None:
    # A company equal to the offer title is a markup regression, not a company.
    html = (
        "<title>Oferta de Empleo Backend Engineer en Madrid, Aceptacorp - "
        "Tecnoempleo.com</title>"
        "<a href='https://www.tecnoempleo.com/backend-engineer/re-1' "
        "title='Ofertas de empleo de Backend Engineer'>Backend Engineer</a>"
    )
    assert parse_detail_company(html) is None


def test_parse_detail_location_and_date_require_real_title_and_og() -> None:
    assert parse_detail_location("<title>Oferta</title>") is None
    assert parse_detail_posted_at("<meta property='og:description' content='no date here'>") is None
    # An impossible calendar date must not be turned into a timestamp.
    assert parse_detail_posted_at(
        "<meta property='og:description' content='31/02/2026 - Oferta'>"
    ) is None


def test_parse_detail_ignores_unknown_markup_never_invents_content() -> None:
    drift = fixture_text("tecnoempleo_detail_page_drift.html")
    assert parse_detail_description(drift) == ""
    assert parse_detail_company(drift) is None
    assert parse_detail_location(drift) is None
    assert parse_detail_posted_at(drift) is None
    assert parse_detail_technologies(drift) == []
    assert parse_detail_description("") == ""
    assert parse_detail_description("<div class='descripcion-lookalike'>nope</div>") == ""


def test_parse_detail_survives_truncated_html() -> None:
    broken = (
        '<section class="descripcion-oferta"><p>A description long enough to be real'
    )
    assert "long enough" in parse_detail_description(broken)


# ---------------------------------------------------------------------------
# URL resolution and planning
# ---------------------------------------------------------------------------


def _job_stub(url, source_job_id="rf-1111"):
    return SimpleNamespace(id=1, url=url, source_job_id=source_job_id)


def test_resolve_detail_url_only_accepts_same_host_offer_links() -> None:
    assert resolve_detail_url(_job_stub(BACKEND_URL)) == BACKEND_URL
    assert resolve_detail_url(_job_stub("https://evil.test/rf-1111")) is None
    assert resolve_detail_url(_job_stub("ftp://www.tecnoempleo.com/rf-1111")) is None
    assert resolve_detail_url(_job_stub("https://www.tecnoempleo.com/no-id-here")) is None
    assert resolve_detail_url(_job_stub(None)) is None


def test_plan_detail_targets_skips_unresolvable_jobs() -> None:
    good = _job_stub(BACKEND_URL, "rf-7a491a37220693ec4248")
    bad = _job_stub("https://evil.test/rf-9999", "rf-9999")
    targets, truncated = plan_detail_targets([good, bad], max_items=5)
    assert truncated == 0
    assert targets[0].url == BACKEND_URL
    assert targets[1].url is None and targets[1].reason


def test_detail_config_enforces_hard_caps() -> None:
    from jobscout.config import HARD_MAX_ITEMS

    assert TecnoempleoDetailConfig(max_items=HARD_MAX_ITEMS).max_items == HARD_MAX_ITEMS
    with pytest.raises(ValueError):
        TecnoempleoDetailConfig(max_items=HARD_MAX_ITEMS + 1)
    with pytest.raises(ValueError):
        TecnoempleoDetailConfig(max_items=0)


# ---------------------------------------------------------------------------
# Opt-in, dry run, robots
# ---------------------------------------------------------------------------


def test_detail_dry_run_makes_no_request(conn, settings, monkeypatch) -> None:
    no_network(monkeypatch)
    jobs = _store_search_offers(conn, settings)
    result = fetch_tecnoempleo_details(jobs, settings=settings, dry_run=True)
    assert result.status is SearchRunStatus.DRY_RUN
    assert result.items == []
    assert result.requests_made == 0


def test_detail_disabled_without_opt_in(conn, settings, monkeypatch) -> None:
    no_network(monkeypatch)
    jobs = _store_search_offers(conn, settings)
    with pytest.raises(CollectorDisabledError) as excinfo:
        fetch_tecnoempleo_details(jobs, settings=settings)
    assert "--confirm-network" in str(excinfo.value)


def test_detail_robots_disallow_fails_closed_before_request(conn, settings) -> None:
    jobs = _store_search_offers(conn, settings)
    client = FakeHttpClient([])  # no detail request is expected
    result = fetch_tecnoempleo_details(
        jobs,
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
    jobs = _store_search_offers(conn, settings)
    backend = next(job for job in jobs if job.source_job_id == "rf-7a491a37220693ec4248")
    client = FakeHttpClient(
        [
            response(200, ROBOTS_ALLOW, headers={"content-type": "text/plain"}),
            response(
                200,
                fixture_text(DETAIL_FIXTURE),
                headers={"content-type": "text/html"},
                url=BACKEND_URL,
            ),
        ]
    )
    result = fetch_tecnoempleo_details(
        [backend],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert client.calls[0]["url"] == settings.tecnoempleo_robots_url
    assert client.calls[1]["url"] != settings.tecnoempleo_robots_url
    assert result.items[0].status is DetailFetchStatus.OK


def test_sanitize_final_url_uses_the_strict_shared_policy() -> None:
    from jobscout.collectors.tecnoempleo_details import _sanitize_final_url

    requested = BACKEND_URL
    allowed = "https://www.tecnoempleo.com/desarrollador-backend-python/grupo-digital/other"
    assert _sanitize_final_url(requested, allowed) == allowed
    # No redirect observed.
    assert _sanitize_final_url(requested, requested) is None
    assert _sanitize_final_url(requested, "") is None
    assert _sanitize_final_url(requested, None) is None
    # HTTPS only (never a plaintext downgrade), no credentials, default port,
    # allowed host.
    assert _sanitize_final_url(requested, "http://www.tecnoempleo.com/x") is None
    assert _sanitize_final_url(requested, "https://www.tecnoempleo.com:8443/x") is None
    assert _sanitize_final_url(requested, "https://user:pass@www.tecnoempleo.com/x") is None
    assert _sanitize_final_url(requested, "https://evil.test/x") is None
    assert _sanitize_final_url(requested, "https://www.tecnoempleo.com.evil.test/x") is None
    assert _sanitize_final_url(requested, "file:///etc/passwd") is None


# ---------------------------------------------------------------------------
# Fetch behaviour
# ---------------------------------------------------------------------------


def test_detail_recognized_page_is_ok_with_non_empty_description(conn, settings) -> None:
    jobs = _store_search_offers(conn, settings)
    backend = next(job for job in jobs if job.source_job_id == "rf-7a491a37220693ec4248")
    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_detail_page.html"), headers={"content-type": "text/html"})]
    )
    result = fetch_tecnoempleo_details(
        [backend],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    item = result.items[0]
    assert item.status is not None
    from jobscout.models import DetailFetchStatus

    assert item.status is DetailFetchStatus.OK
    assert item.content_status is ContentStatus.OK
    assert item.description
    assert item.company == "Grupo Digital"
    assert "Python" in item.technologies


def test_detail_real_page_is_ok_and_populates_fields(conn, settings) -> None:
    from jobscout.models import DetailFetchStatus

    jobs = _store_search_offers(conn, settings)
    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_detail_page_real.html"), headers={"content-type": "text/html"})]
    )
    result = fetch_tecnoempleo_details(
        jobs[:1],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    item = result.items[0]
    assert item.status is DetailFetchStatus.OK
    assert item.content_status is ContentStatus.OK
    assert item.company == "Aceptacorp"
    assert item.location == "Madrid"
    assert item.posted_at == "2026-07-03T00:00:00Z"
    assert item.company != "Backend Engineer"


def test_detail_cloudflare_beacon_page_body_is_archived(conn, settings) -> None:
    """The public Cloudflare beacon token must not drop the raw provenance.

    Regression for the systematic Tecnoempleo body suppression: the beacon's
    32-hex public ``token`` tripped the labelled-credential pattern even though
    it is not a credential.
    """
    from jobscout.models import DetailFetchStatus

    jobs = _store_search_offers(conn, settings)
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text("tecnoempleo_detail_page_cloudflare_beacon.html"),
                headers={"content-type": "text/html"},
            )
        ]
    )
    result = fetch_tecnoempleo_details(
        jobs[:1],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    document = result.raw_documents[0]
    assert document.body is not None
    assert document.truncated is False
    assert "credential" not in (document.notes or "")
    assert result.items[0].status is DetailFetchStatus.OK
    assert result.items[0].description


def test_detail_unknown_markup_stays_partial_honestly(conn, settings) -> None:
    from jobscout.models import DetailFetchStatus

    jobs = _store_search_offers(conn, settings)
    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_detail_page_drift.html"), headers={"content-type": "text/html"})]
    )
    result = fetch_tecnoempleo_details(
        jobs[:1],
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
    from jobscout.models import DetailFetchStatus

    jobs = _store_search_offers(conn, settings)
    client = FakeHttpClient([response(403, "denied")])
    result = fetch_tecnoempleo_details(
        jobs,
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
    from jobscout.models import DetailFetchStatus

    jobs = _store_search_offers(conn, settings)
    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_detail_page.html"), headers={"content-type": "text/html", TRUNCATION_HEADER: "1"})]
    )
    result = fetch_tecnoempleo_details(
        jobs[:1],
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


def test_detail_records_not_found_and_continues(conn, settings) -> None:
    from jobscout.models import DetailFetchStatus

    jobs = _store_search_offers(conn, settings)
    body = fixture_text("tecnoempleo_detail_page.html")
    client = FakeHttpClient(
        [response(404, "gone"), response(200, body, headers={"content-type": "text/html"})]
    )
    result = fetch_tecnoempleo_details(
        jobs[:2],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert client.call_count == 2
    assert result.items[0].status is DetailFetchStatus.FAILED
    assert result.items[1].status is DetailFetchStatus.OK


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_ingest_detail_enriches_job_and_writes_provenance(conn, settings) -> None:
    from jobscout.models import DetailFetchStatus

    jobs = _store_search_offers(conn, settings)
    backend = next(job for job in jobs if job.source_job_id == "rf-7a491a37220693ec4248")
    assert backend.content_status is ContentStatus.PARTIAL

    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_detail_page.html"), headers={"content-type": "text/html"})]
    )
    result = fetch_tecnoempleo_details(
        [backend],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    outcome = ingest_detail_results(
        conn, result, settings=settings, provider="tecnoempleo"
    )

    refreshed = get_job(conn, backend.id)
    assert refreshed.content_status is ContentStatus.OK
    assert refreshed.description
    assert refreshed.company == "Grupo Digital"
    assert refreshed.location == "Madrid"
    assert refreshed.extra.get("technologies")

    fetch = latest_detail_fetch(conn, backend.id)
    assert fetch.status is DetailFetchStatus.OK
    assert fetch.source == "tecnoempleo"
    assert outcome.enriched == 1
    run = conn.execute(
        "SELECT provider FROM search_runs WHERE id = ?", (outcome.run_id,)
    ).fetchone()
    assert run["provider"] == "tecnoempleo-details"


def test_ingest_detail_source_scoped_selection(conn, settings) -> None:
    """Source-scoped selection keeps Tecnoempleo enrichment separate from LinkedIn."""
    from jobscout.db import jobs_needing_details

    jobs = _store_search_offers(conn, settings)
    backend = next(job for job in jobs if job.source_job_id == "rf-7a491a37220693ec4248")
    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_detail_page.html"), headers={"content-type": "text/html"})]
    )
    result = fetch_tecnoempleo_details(
        [backend],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    ingest_detail_results(conn, result, settings=settings, provider="tecnoempleo")
    remaining = jobs_needing_details(conn, source="tecnoempleo")
    assert backend.id not in {job.id for job in remaining}
    # No Tecnoempleo offer is selected by a LinkedIn-scoped query.
    assert jobs_needing_details(conn, source="linkedin") == []


def test_ingest_detail_partial_never_overwrites_full_description(conn, settings) -> None:
    jobs = _store_search_offers(conn, settings)
    backend = next(job for job in jobs if job.source_job_id == "rf-7a491a37220693ec4248")
    # Seed a full description first, then attempt an unrecognized detail page.
    conn.execute(
        "UPDATE jobs SET description = ?, content_status = 'ok' WHERE id = ?",
        ("A complete stored description that must survive.", backend.id),
    )
    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_detail_page_drift.html"), headers={"content-type": "text/html"})]
    )
    result = fetch_tecnoempleo_details(
        [backend],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    ingest_detail_results(conn, result, settings=settings, provider="tecnoempleo")
    assert get_job(conn, backend.id).description == (
        "A complete stored description that must survive."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_fetch_tecnoempleo_details_refuses_without_confirmation(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    db_path = tmp_path / "tecno-details.sqlite3"
    exit_code = main(["--db", str(db_path), "fetch-tecnoempleo-details"])
    assert exit_code == EXIT_FAIL_CLOSED
    err = capsys.readouterr().err
    assert "--confirm-network" in err


def test_cli_fetch_tecnoempleo_details_dry_run_no_network(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    db_path = tmp_path / "tecno-details-dry.sqlite3"
    exit_code = main(
        ["--db", str(db_path), "fetch-tecnoempleo-details", "--dry-run", "--json"]
    )
    assert exit_code == EXIT_OK
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["planned_urls"] == []
