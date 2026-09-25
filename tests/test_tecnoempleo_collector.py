"""Tecnoempleo public search tests: parsing, robots enforcement, gating.

No real network request is ever made. The transport is the offline fake client,
``robots.txt`` is injected as text, and the search page is a local fixture
written to the documented server-rendered shape.
"""

from __future__ import annotations

import pytest

from conftest import FakeHttpClient, FIXTURES, fixture_text, no_network, response

from jobscout.collectors.base import CollectorDisabledError
from jobscout.collectors.tecnoempleo import (
    TecnoempleoSearchConfig,
    build_search_url,
    extract_tecnoempleo_job_id,
    parse_offer_links,
    plan_search_urls,
    postings_from_html,
    search_tecnoempleo,
)
from jobscout.db import count_jobs, get_search_run
from jobscout.models import ContentStatus, SearchRunStatus
from jobscout.pipeline import ingest_collector_result

PAGE_FIXTURE = FIXTURES / "tecnoempleo_search_page.html"
ROBOTS_ALLOW = "User-agent: *\nDisallow: /alertas-empleo-rss.php\nDisallow: /profesionales/\n"
ROBOTS_DENY = "User-agent: *\nDisallow: /ofertas-trabajo/\n"


def test_build_search_url_encodes_keyword_location_and_page() -> None:
    config = TecnoempleoSearchConfig(keywords="cloud security", location="madrid", max_pages=3)
    assert build_search_url(config) == (
        "https://www.tecnoempleo.com/ofertas-trabajo/madrid/?te=cloud+security"
    )
    assert build_search_url(config, page=2).endswith("&pagina=2")
    assert plan_search_urls(config) == [
        build_search_url(config, page=1),
        build_search_url(config, page=2),
        build_search_url(config, page=3),
    ]


def test_config_requires_keywords_and_validates_host_and_caps() -> None:
    with pytest.raises(ValueError):
        TecnoempleoSearchConfig(keywords="  ")
    with pytest.raises(ValueError):
        TecnoempleoSearchConfig(keywords="x", search_url="https://evil.test/ofertas-trabajo/")
    with pytest.raises(ValueError):
        TecnoempleoSearchConfig(keywords="x", max_pages=0)


def test_parse_offer_links_filters_non_offers_and_cross_host() -> None:
    links = parse_offer_links(fixture_text("tecnoempleo_search_page.html"))
    assert [link["source_job_id"] for link in links] == [
        "rf-7a491a37220693ec4248",
        "rf-1111aaaa2222bbbb3333",
        "rf-9999cccc8888dddd7777",
    ]
    assert links[0]["title"] == "Desarrollador/a Backend Python"
    assert all("evil.test" not in link["url"] for link in links)
    # The duplicate anchor collapses to one canonical entry.
    assert len({link["url"] for link in links}) == 3


def test_parse_offer_links_is_resilient_to_noise() -> None:
    assert parse_offer_links("") == []
    assert parse_offer_links("<html>no offers here</html>") == []


def test_extract_id_and_postings_contract() -> None:
    url = "https://www.tecnoempleo.com/sre-devops/todo-en-cloud/rf-1111aaaa2222bbbb3333"
    assert extract_tecnoempleo_job_id(url) == "rf-1111aaaa2222bbbb3333"
    postings = postings_from_html(
        fixture_text("tecnoempleo_search_page.html"), fetched_at="2026-09-18T10:00:00Z"
    )
    assert len(postings) == 3
    first = postings[0]
    assert first.source == "tecnoempleo"
    assert first.content_status is ContentStatus.PARTIAL
    assert first.source_job_id == "rf-7a491a37220693ec4248"
    assert first.fingerprint.startswith("v1:id:tecnoempleo:rf-7a491a37220693ec4248")


def test_dry_run_makes_no_request(settings, monkeypatch) -> None:
    no_network(monkeypatch)
    config = TecnoempleoSearchConfig(keywords="python", max_pages=2)
    result = search_tecnoempleo(config, settings=settings, dry_run=True)
    assert result.status is SearchRunStatus.DRY_RUN
    assert len(result.planned_urls) == 2
    assert result.postings == []


def test_collection_is_disabled_without_opt_in(settings, monkeypatch) -> None:
    no_network(monkeypatch)
    with pytest.raises(CollectorDisabledError) as excinfo:
        search_tecnoempleo(
            TecnoempleoSearchConfig(keywords="python"),
            settings=settings,
            network_enabled=False,
        )
    assert "--confirm-network" in str(excinfo.value)


def test_live_search_parses_page_and_records_raw_document(settings) -> None:
    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_search_page.html"), headers={"content-type": "text/html"})]
    )
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(keywords="backend", max_pages=1, max_items=10),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert len(result.postings) == 3
    assert result.pages_fetched == 1
    assert result.raw_documents[0].body is not None
    assert client.call_count == 1


def test_live_search_stops_completed_when_a_later_page_has_no_offers(settings) -> None:
    # Regression: a later, offer-less page is a normal end of pagination, not
    # unknown markup. Earlier behaviour aborted the whole query as FAILED_CLOSED.
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text("tecnoempleo_search_page.html"),
                headers={"content-type": "text/html"},
            ),
            response(
                200,
                "<html><body><p>No hay ofertas que coincidan</p></body></html>",
                headers={"content-type": "text/html"},
            ),
        ]
    )
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(keywords="backend", max_pages=2, max_items=10),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert result.pages_fetched == 2
    assert len(result.postings) == 3
    assert [posting.source_job_id for posting in result.postings] == [
        "rf-7a491a37220693ec4248",
        "rf-1111aaaa2222bbbb3333",
        "rf-9999cccc8888dddd7777",
    ]
    assert client.call_count == 2


def test_robots_disallow_fails_closed_before_search(settings) -> None:
    client = FakeHttpClient([])  # no search request is expected
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(keywords="backend"),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_DENY,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert "robots.txt disallows" in (result.error or "")
    assert client.call_count == 0


def test_missing_robots_is_treated_as_allow(settings) -> None:
    client = FakeHttpClient(
        [
            response(404, "not found"),
            response(200, fixture_text("tecnoempleo_search_page.html"), headers={"content-type": "text/html"}),
        ]
    )
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(keywords="backend", max_pages=1),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=None,
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert len(result.postings) == 3
    assert client.call_count == 2


def test_robots_refusal_fails_closed(settings) -> None:
    client = FakeHttpClient([response(403, "denied")])
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(keywords="backend"),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=None,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert "robots.txt" in (result.error or "")


def test_unsupported_markup_fails_closed(settings) -> None:
    client = FakeHttpClient([response(200, "<html><body>changed layout</body></html>")])
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(keywords="backend", max_pages=1),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert "unsupported search response" in (result.error or "")


def test_access_denied_fails_closed(settings) -> None:
    client = FakeHttpClient([response(403, "denied")])
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(keywords="backend", max_pages=1),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert result.raw_documents[0].status_code == 403


def test_ingestion_records_provider_cycle_and_search_rank(conn, settings) -> None:
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
    outcome = ingest_collector_result(
        conn, result, config=config, settings=settings, cycle_id="cycle-2026-09"
    )

    run = get_search_run(conn, outcome.run_id)
    assert run is not None
    assert run.provider == "tecnoempleo"
    assert run.cycle_id == "cycle-2026-09"
    assert run.query == "backend"
    assert run.params["keywords"] == "backend"
    rows = conn.execute(
        "SELECT position_kind, rank_in_run FROM search_run_items ORDER BY id"
    ).fetchall()
    assert [row["position_kind"] for row in rows] == ["search_rank"] * 3
    assert [row["rank_in_run"] for row in rows] == [1, 2, 3]
    assert count_jobs(conn) == 3
