"""Tecnoempleo public search tests: parsing, robots enforcement, gating.

No real network request is ever made. The transport is the offline fake client,
``robots.txt`` is injected as text, and the search page is a local fixture
written to the documented server-rendered shape.
"""

from __future__ import annotations

import pytest

from conftest import FakeHttpClient, FIXTURES, fixture_text, no_network, response

from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.collectors.base import CollectorDisabledError
from jobscout.collectors.tecnoempleo import (
    CARD_DESCRIPTION_MAX_CHARS,
    TECNOEMPLEO_BROWSE_LABEL,
    TecnoempleoSearchConfig,
    browse_plan,
    build_search_url,
    extract_tecnoempleo_job_id,
    parse_offer_cards,
    parse_offer_links,
    plan_search_urls,
    postings_from_html,
    search_tecnoempleo,
)
from jobscout.db import count_jobs, get_search_run
from jobscout.models import ContentStatus, SearchRunStatus
from jobscout.pipeline import ingest_collector_result

PAGE_FIXTURE = FIXTURES / "tecnoempleo_search_page.html"
CARDS_FIXTURE = FIXTURES / "tecnoempleo_search_cards.html"
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


def test_build_browse_url_is_exact_and_keeps_literal_commas() -> None:
    config = TecnoempleoSearchConfig(
        keywords=TECNOEMPLEO_BROWSE_LABEL, browse=True, max_pages=3
    )
    assert build_search_url(config) == (
        "https://www.tecnoempleo.com/ofertas-trabajo/"
        "?pr=,263,&es=,34,43,40,39,8,45,19,"
    )
    assert build_search_url(config, page=2) == (
        "https://www.tecnoempleo.com/ofertas-trabajo/"
        "?pr=,263,&es=,34,43,40,39,8,45,19,&pagina=2"
    )
    assert "%2C" not in build_search_url(config, page=2)
    assert plan_search_urls(config) == [
        build_search_url(config, page=page) for page in (1, 2, 3)
    ]


def test_browse_plan_matches_the_owner_sweep_budget() -> None:
    assert browse_plan() == {
        "label": "función: sistemas, devops y seguridad (Madrid)",
        "url": (
            "https://www.tecnoempleo.com/ofertas-trabajo/"
            "?pr=,263,&es=,34,43,40,39,8,45,19,"
        ),
        "max_pages": 5,
        "max_items": 150,
    }


def test_config_requires_keywords_and_validates_host_and_caps() -> None:
    with pytest.raises(ValueError):
        TecnoempleoSearchConfig(keywords="  ")
    with pytest.raises(ValueError):
        TecnoempleoSearchConfig(keywords="x", search_url="https://evil.test/ofertas-trabajo/")
    with pytest.raises(ValueError):
        TecnoempleoSearchConfig(keywords="x", max_pages=0)


RETIRED_TECNO_QUERIES = (
    "Platform Engineer",
    "Azure Security Engineer",
    "IAM Engineer",
    "Junior Cloud Security Engineer",
    "Security Infrastructure Engineer",
    "Junior Cloud Engineer",
    "Junior DevOps Engineer",
    "Cloud Security Engineer",
    "DevSecOps Engineer",
    "Junior DevSecOps Engineer",
    "Azure Cloud Security",
    "Azure DevSecOps",
)
#: The CP-8 additions carry the 2026-09-30 decision date.
CP8_RETIRED_TECNO_QUERIES = (
    "Junior Cloud Security Engineer",
    "Security Infrastructure Engineer",
)
#: The function-browse replacement carries the 2026-10-27 decision date.
BROWSE_RETIRED_TECNO_QUERIES = (
    "Junior Cloud Engineer",
    "Junior DevOps Engineer",
    "Cloud Security Engineer",
    "DevSecOps Engineer",
    "Junior DevSecOps Engineer",
    "Azure Cloud Security",
    "Azure DevSecOps",
)


def test_retired_tecno_queries_fail_closed_at_the_config_choke_point() -> None:
    for query in RETIRED_TECNO_QUERIES:
        with pytest.raises(ValueError) as excinfo:
            TecnoempleoSearchConfig(keywords=query)
        message = str(excinfo.value)
        assert query in message
        assert "retired" in message.lower()
        if query in BROWSE_RETIRED_TECNO_QUERIES:
            expected_on = "2026-10-27"
        elif query in CP8_RETIRED_TECNO_QUERIES:
            expected_on = "2026-09-30"
        else:
            expected_on = "2026-09-29"
        assert expected_on in message
    # Normalization folds case/whitespace before the equality check.
    with pytest.raises(ValueError):
        TecnoempleoSearchConfig(keywords="  platform   ENGINEER ")


def test_retirement_allows_longer_and_other_provider_queries() -> None:
    # A longer query that merely contains a retired literal is still allowed.
    assert TecnoempleoSearchConfig(keywords="Senior Platform Engineer").keywords
    assert TecnoempleoSearchConfig(keywords="Platform Engineering").keywords
    # Identity/IAM coverage remains active for Tecnoempleo, and so does the one
    # text query the browse does not replace.
    assert TecnoempleoSearchConfig(keywords="Identity and Access Management Engineer").keywords
    assert TecnoempleoSearchConfig(keywords="devops").keywords
    assert TecnoempleoSearchConfig(keywords="prácticas").keywords


def test_retired_tecno_query_fails_a_dry_run_with_a_clear_message(
    tmp_path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    for query in RETIRED_TECNO_QUERIES:
        assert (
            main(
                [
                    "--db",
                    str(tmp_path / "tecno.sqlite3"),
                    "search-tecnoempleo",
                    "--keywords",
                    query,
                    "--dry-run",
                ]
            )
            == EXIT_ERROR
        )
        err = capsys.readouterr().err
        assert "retired" in err.lower()
        assert query in err
    # A dry run never creates the database.
    assert not (tmp_path / "tecno.sqlite3").exists()


def test_parse_offer_links_filters_non_offers_and_cross_host() -> None:
    links = parse_offer_links(fixture_text("tecnoempleo_search_page.html"))
    assert [link["source_job_id"] for link in links] == [
        "rf-7a491a37220693ec4248",
        "rf-1111aaaa2222bbbb3333",
    ]
    assert links[0]["title"] == "Desarrollador/a Backend Python"
    assert all("evil.test" not in link["url"] for link in links)
    # The duplicate anchor collapses to one canonical entry.
    assert len({link["url"] for link in links}) == 2


def test_parse_offer_links_is_resilient_to_noise() -> None:
    assert parse_offer_links("") == []
    assert parse_offer_links("<html>no offers here</html>") == []


def test_extract_id_and_postings_contract() -> None:
    url = "https://www.tecnoempleo.com/sre-devops/todo-en-cloud/rf-1111aaaa2222bbbb3333"
    assert extract_tecnoempleo_job_id(url) == "rf-1111aaaa2222bbbb3333"
    postings = postings_from_html(
        fixture_text("tecnoempleo_search_page.html"), fetched_at="2026-09-18T10:00:00Z"
    )
    assert len(postings) == 2
    first = postings[0]
    assert first.source == "tecnoempleo"
    assert first.content_status is ContentStatus.PARTIAL
    assert first.source_job_id == "rf-7a491a37220693ec4248"
    assert first.fingerprint.startswith("v1:id:tecnoempleo:rf-7a491a37220693ec4248")


def test_parse_offer_cards_extracts_real_card_fields() -> None:
    cards = parse_offer_cards(fixture_text("tecnoempleo_search_cards.html"))
    assert len(cards) == 2
    first = cards[0]
    assert first["url"] == (
        "https://www.tecnoempleo.com/backend-python-engineer/acme-corp/"
        "rf-aaaa1111bbbb2222cccc"
    )
    assert first["source_job_id"] == "rf-aaaa1111bbbb2222cccc"
    assert first["title"] == "Backend Python Engineer"
    assert first["company"] == "Acme Corp"
    assert first["location"] == "100% remoto"
    assert first["posted_at"] == "2026-09-25T00:00:00Z"
    assert first["description"]
    assert len(first["description"]) <= CARD_DESCRIPTION_MAX_CHARS

    second = cards[1]
    assert second["title"] == "Data Engineer"
    assert second["company"] == "Globex"
    assert second["location"] == "Madrid"
    assert second["posted_at"] == "2026-09-23T00:00:00Z"
    assert all("evil.test" not in card["url"] for card in cards)


def test_parse_offer_cards_unknown_fields_stay_empty() -> None:
    card = (
        '<div class="p-3 border rounded mb-3" '
        "onclick=\"location.href='https://www.tecnoempleo.com/plain/co/rf-abc123'\">"
        "<h3><a href=\"https://www.tecnoempleo.com/plain/co/rf-abc123\" "
        'title="Plain Offer">Plain Offer</a></h3></div>'
    )
    cards = parse_offer_cards(card)
    assert len(cards) == 1
    assert cards[0]["company"] is None
    assert cards[0]["location"] is None
    assert cards[0]["posted_at"] is None
    assert cards[0]["description"] is None
    assert parse_offer_cards("<html>no cards</html>") == []
    assert parse_offer_cards("") == []


def test_postings_from_html_populates_real_card_fields() -> None:
    postings = postings_from_html(
        fixture_text("tecnoempleo_search_cards.html"),
        fetched_at="2026-09-25T10:00:00Z",
    )
    assert len(postings) == 2
    first = postings[0]
    assert first.title == "Backend Python Engineer"
    assert first.company == "Acme Corp"
    assert first.location == "100% remoto"
    assert first.posted_at == "2026-09-25T00:00:00Z"
    assert first.description
    assert first.content_status is ContentStatus.PARTIAL
    # The breadcrumb/title must never be stored as the company.
    assert first.company != first.title
    assert all(posting.company != posting.title for posting in postings)


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
    assert len(result.postings) == 2
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
    assert len(result.postings) == 2
    assert [posting.source_job_id for posting in result.postings] == [
        "rf-7a491a37220693ec4248",
        "rf-1111aaaa2222bbbb3333",
    ]
    assert client.call_count == 2


def test_live_search_completes_for_the_site_no_results_marker(settings) -> None:
    client = FakeHttpClient(
        [response(200, fixture_text("tecnoempleo_search_no_results.html"), headers={"content-type": "text/html"})]
    )
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(
            keywords="backend", location="madrid", max_pages=1
        ),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert result.postings == []
    assert result.items_found == 0
    assert result.notes == "no offers for this query"


def test_live_search_without_results_marker_still_fails_closed(settings) -> None:
    client = FakeHttpClient([response(200, "<html><body><p>changed layout</p></body></html>")])
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(keywords="backend", max_pages=1),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert result.postings == []
    assert result.items_found == 0
    assert "unsupported search response" in (result.error or "")


def test_live_search_completes_when_a_later_page_is_404(settings) -> None:
    # A 404 on page 2 means the result set ended. The offers already collected
    # must be kept and the run must complete, not fail.
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text("tecnoempleo_search_page_three_cards.html"),
                headers={"content-type": "text/html"},
            ),
            response(404, "not found"),
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
    assert result.pages_fetched == 1
    assert len(result.postings) == 3
    assert result.notes is not None and "pagination ended at page 2: 404" in result.notes
    assert client.call_count == 2


def test_browse_run_uses_the_label_and_exact_urls(conn, settings) -> None:
    # The browse reuses the search-results markup; page 2 ending in 404 is a
    # normal end of pagination. The run query stays the provenance label while
    # ``planned_urls`` carries the real browse URLs.
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text("tecnoempleo_search_page_three_cards.html"),
                headers={"content-type": "text/html"},
            ),
            response(404, "not found"),
        ]
    )
    config = TecnoempleoSearchConfig(
        keywords=TECNOEMPLEO_BROWSE_LABEL, browse=True, max_pages=2, max_items=10
    )
    result = search_tecnoempleo(
        config,
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert len(result.postings) == 3
    assert result.notes is not None and "pagination ended at page 2: 404" in result.notes
    assert result.planned_urls == [
        "https://www.tecnoempleo.com/ofertas-trabajo/"
        "?pr=,263,&es=,34,43,40,39,8,45,19,",
        "https://www.tecnoempleo.com/ofertas-trabajo/"
        "?pr=,263,&es=,34,43,40,39,8,45,19,&pagina=2",
    ]

    outcome = ingest_collector_result(conn, result, config=config, settings=settings)
    run = get_search_run(conn, outcome.run_id)
    assert run is not None
    assert run.query == TECNOEMPLEO_BROWSE_LABEL
    assert run.params["keywords"] == TECNOEMPLEO_BROWSE_LABEL
    assert run.params["planned_urls"] == result.planned_urls


def test_cli_browse_dry_run_lists_the_two_urls(tmp_path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    assert (
        main(
            [
                "--db",
                str(tmp_path / "browse.sqlite3"),
                "search-tecnoempleo",
                "--browse",
                "--max-pages",
                "2",
                "--dry-run",
            ]
        )
        == EXIT_OK
    )
    out = capsys.readouterr().out
    assert (
        "https://www.tecnoempleo.com/ofertas-trabajo/"
        "?pr=,263,&es=,34,43,40,39,8,45,19,"
    ) in out
    assert out.count("https://www.tecnoempleo.com/ofertas-trabajo/") == 2
    assert "&pagina=2" in out
    assert not (tmp_path / "browse.sqlite3").exists()


def test_cli_browse_rejects_keywords_and_location_without_network(
    tmp_path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    for extra in (["--keywords", "python"], ["--location", "madrid"]):
        assert (
            main(
                [
                    "--db",
                    str(tmp_path / "browse.sqlite3"),
                    "search-tecnoempleo",
                    "--browse",
                    *extra,
                    "--dry-run",
                ]
            )
            == EXIT_ERROR
        )
        err = capsys.readouterr().err
        assert "cannot be combined" in err
    assert not (tmp_path / "browse.sqlite3").exists()


def test_first_page_404_fails_with_no_postings(settings) -> None:
    client = FakeHttpClient([response(404, "not found")])
    result = search_tecnoempleo(
        TecnoempleoSearchConfig(keywords="backend", max_pages=1, max_items=10),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
        robots_text=ROBOTS_ALLOW,
    )
    assert result.status is SearchRunStatus.FAILED
    assert result.postings == []
    assert result.pages_fetched == 0
    assert client.call_count == 1


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
    assert len(result.postings) == 2
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
    assert [row["position_kind"] for row in rows] == ["search_rank"] * 2
    assert [row["rank_in_run"] for row in rows] == [1, 2]
    assert count_jobs(conn) == 2
