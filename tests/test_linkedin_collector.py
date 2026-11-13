"""LinkedIn collector tests: parsing, opt-in gating, and fail-closed behaviour.

No real LinkedIn request is ever made. The transport is always the fake client
from ``conftest``, and the disabled/dry-run paths are asserted explicitly.
"""

from __future__ import annotations

import pytest

from conftest import (
    FakeHttpClient,
    FakeOpener,
    FakeRawResponse,
    FIXTURES,
    fixture_text,
    no_network,
    response,
)

from jobscout.collectors import linkedin
from jobscout.collectors.base import CollectorDisabledError, UrllibHttpClient
from jobscout.collectors.linkedin import (
    LinkedInSearchConfig,
    build_search_url,
    parse_job_cards,
    plan_search_urls,
    search_linkedin,
)
from jobscout.config import HARD_MAX_ITEMS, HARD_MAX_PAGES
from jobscout.db import count_jobs
from jobscout.models import SearchRunStatus
from jobscout.pipeline import ingest_collector_result

HTML_FIXTURE = FIXTURES / "linkedin_search_page.html"
DRIFT_FIXTURE = FIXTURES / "linkedin_search_page_drift.html"


def test_build_search_url_is_bounded_and_encoded() -> None:
    config = LinkedInSearchConfig(
        keywords="senior backend engineer",
        location="Buenos Aires, Argentina",
        distance_miles=25,
        remote=True,
        employment_type="F",
        max_pages=3,
        max_items=10,
    )
    url = build_search_url("https://example.test/search", config, start=25)
    assert url.startswith("https://example.test/search?")
    assert "keywords=senior+backend+engineer" in url
    assert "start=25" in url
    assert "location=Buenos+Aires%2C+Argentina" in url
    assert "distance=25" in url
    # Every search asks for the last seven days, ahead of the page offset
    # (owner, 2026-11-13: unfiltered searches repeated the same results). The
    # date sort is not sent: the guest endpoint was tried live and ignores it.
    assert "f_TPR=r604800" in url
    assert url.index("f_TPR=") < url.index("start=")
    assert "sortBy" not in url


def test_every_planned_page_requests_last_week(settings) -> None:
    config = LinkedInSearchConfig(keywords="devops junior", location="Madrid")
    assert plan_search_urls(config, settings=settings)
    for url in plan_search_urls(config, settings=settings):
        assert "f_TPR=r604800" in url
        assert "sortBy" not in url


def test_search_config_validates_required_fields() -> None:
    with pytest.raises(ValueError):
        LinkedInSearchConfig(keywords="  ")
    with pytest.raises(ValueError):
        LinkedInSearchConfig(keywords="x", max_pages=0)
    with pytest.raises(ValueError):
        LinkedInSearchConfig(keywords="x", max_items=0)


def test_search_config_enforces_absolute_hard_caps() -> None:
    # The documented ceilings are allowed; anything above them is rejected, so
    # a library caller cannot request unbounded collection.
    config = LinkedInSearchConfig(
        keywords="x", max_pages=HARD_MAX_PAGES, max_items=HARD_MAX_ITEMS
    )
    assert config.max_pages == HARD_MAX_PAGES
    assert config.max_items == HARD_MAX_ITEMS

    with pytest.raises(ValueError):
        LinkedInSearchConfig(keywords="x", max_pages=HARD_MAX_PAGES + 1)
    with pytest.raises(ValueError):
        LinkedInSearchConfig(keywords="x", max_items=HARD_MAX_ITEMS + 1)


RETIRED_LINKEDIN_QUERIES = (
    "Identity and Access Management Engineer",
    "IAM Engineer",
)


def test_retired_linkedin_queries_fail_closed_at_the_config_choke_point() -> None:
    for query in RETIRED_LINKEDIN_QUERIES:
        with pytest.raises(ValueError) as excinfo:
            LinkedInSearchConfig(keywords=query)
        message = str(excinfo.value)
        assert query in message
        assert "retired" in message.lower()
        assert "2026-09-29" in message
    # Normalization folds case/whitespace before the equality check.
    with pytest.raises(ValueError):
        LinkedInSearchConfig(keywords="  iam   ENGINEER ")


def test_retirement_allows_longer_and_other_provider_queries() -> None:
    # A longer query that merely contains a retired literal is still allowed.
    assert LinkedInSearchConfig(keywords="Senior Platform Engineer").keywords
    assert LinkedInSearchConfig(keywords="Cloud Security Architect").keywords
    # CP-8 restored these two exact literals on LinkedIn.
    assert LinkedInSearchConfig(keywords="Cloud Security Engineer").keywords
    assert LinkedInSearchConfig(keywords="Platform Engineer").keywords


def test_retired_linkedin_query_fails_a_dry_run_with_a_clear_message(
    tmp_path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    from jobscout.cli import EXIT_ERROR, main

    for query in RETIRED_LINKEDIN_QUERIES:
        assert (
            main(
                [
                    "--db",
                    str(tmp_path / "linkedin.sqlite3"),
                    "search-linkedin",
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
    assert not (tmp_path / "linkedin.sqlite3").exists()


def test_parse_job_cards_extracts_fields() -> None:
    cards = parse_job_cards(fixture_text("linkedin_search_page.html"), fetched_at="2026-09-15T12:00:00Z")
    assert len(cards) == 3
    first = cards[0]
    assert first["source_job_id"] == "3911111111"
    assert first["title"] == "Senior Backend Engineer"
    assert first["company"] == "Acme Data Inc."
    assert first["location"] == "Remote, Argentina"
    assert first["url"] == "https://www.linkedin.com/jobs/view/3911111111/senior-backend-engineer"
    assert first["fetched_at"] == "2026-09-15T12:00:00Z"
    assert first["content_status"] == "partial"

    third = cards[2]
    assert third["source_job_id"] == "3913333333"
    assert third["posted_text"] == "3 days ago"


def test_parse_job_cards_is_resilient_to_noise() -> None:
    assert parse_job_cards("") == []
    assert parse_job_cards("<html><body>no results</body></html>") == []
    broken = "<ol><li data-entity-urn='urn:li:jobPosting:1'><h3 class='base-search-card__title'>Half Card"
    cards = parse_job_cards(broken)
    assert len(cards) == 1
    assert cards[0]["title"] == "Half Card"


def test_parse_job_cards_reads_nested_div_card_roots() -> None:
    """Current responses carry the card data on a nested div, not the outer li."""
    cards = parse_job_cards(
        fixture_text("linkedin_search_page_drift.html"), fetched_at="2026-09-15T12:00:00Z"
    )
    assert len(cards) == 3

    first = cards[0]
    assert first["source_job_id"] == "3921111111"
    assert first["title"] == "Senior Backend Engineer"
    assert first["company"] == "Acme Drift"
    assert first["location"] == "Madrid, Community of Madrid, Spain"
    assert first["posted_at"] == "2026-07-31"
    assert first["url"] == (
        "https://www.linkedin.com/jobs/view/"
        "senior-backend-engineer-at-acme-drift-3921111111"
    )
    assert first["fetched_at"] == "2026-09-15T12:00:00Z"
    assert first["content_status"] == "partial"

    assert [card["source_job_id"] for card in cards] == [
        "3921111111",
        "3922222222",
        "3923333333",
    ]
    assert cards[1]["location"] == "Remote, Argentina"
    assert cards[2]["company"] == "Globex Drift"
    assert cards[2]["posted_at"] == "2026-09-11"


def test_parse_job_cards_accepts_li_and_div_roots_in_one_response() -> None:
    """Both the legacy li root and the current div root must be recognised."""
    html = (
        "<ol>"
        "<li data-entity-urn='urn:li:jobPosting:3931111111' class='result-card'>"
        "<a class='base-card__full-link' href='https://www.linkedin.com/jobs/view/3931111111/li-card'></a>"
        "<h3 class='base-search-card__title'>LI Card</h3>"
        "<h4 class='base-search-card__subtitle'>Li Co</h4>"
        "<span class='job-search-card__location'>Remote</span>"
        "</li>"
        "<li>"
        "<div class='base-card base-search-card job-search-card' data-entity-urn='urn:li:jobPosting:3932222222'>"
        "<a class='base-card__full-link' href='https://www.linkedin.com/jobs/view/3932222222/div-card'></a>"
        "<h3 class='base-search-card__title'>Div Card</h3>"
        "<h4 class='base-search-card__subtitle'>Div Co</h4>"
        "<span class='job-search-card__location'>Madrid</span>"
        "</div>"
        "</li>"
        "</ol>"
    )
    cards = parse_job_cards(html)
    assert [card["source_job_id"] for card in cards] == ["3931111111", "3932222222"]
    assert [card["title"] for card in cards] == ["LI Card", "Div Card"]


def test_parse_job_cards_recovers_from_unclosed_card_wrapper() -> None:
    """A card whose wrapper never closes must not swallow the next card."""
    html = (
        "<ol>"
        "<li><div class='base-card base-search-card job-search-card' data-entity-urn='urn:li:jobPosting:3941111111'>"
        "<h3 class='base-search-card__title'>First</h3>"
        "<li><div class='base-card base-search-card job-search-card' data-entity-urn='urn:li:jobPosting:3942222222'>"
        "<h3 class='base-search-card__title'>Second</h3>"
        "</div></li>"
        "</ol>"
    )
    cards = parse_job_cards(html)
    assert [card["title"] for card in cards] == ["First", "Second"]
    assert [card["source_job_id"] for card in cards] == ["3941111111", "3942222222"]


def test_parse_job_cards_finalizes_truncated_div_card() -> None:
    html = (
        "<li><div class='base-card base-search-card job-search-card' "
        "data-entity-urn='urn:li:jobPosting:3951111111'>"
        "<a class='base-card__full-link' href='https://www.linkedin.com/jobs/view/3951111111/x'></a>"
        "<h3 class='base-search-card__title'>Truncated Role"
    )
    cards = parse_job_cards(html)
    assert len(cards) == 1
    assert cards[0]["title"] == "Truncated Role"
    assert cards[0]["source_job_id"] == "3951111111"


def test_dry_run_plan_makes_no_request(settings, monkeypatch) -> None:
    no_network(monkeypatch)
    config = LinkedInSearchConfig(keywords="python", location="remote", max_pages=2)
    result = search_linkedin(config, settings=settings, dry_run=True)
    assert result.status is SearchRunStatus.DRY_RUN
    assert len(result.planned_urls) == 2
    assert result.postings == []
    assert result.pages_fetched == 0
    assert plan_search_urls(config, settings=settings) == result.planned_urls


def test_collection_is_disabled_without_explicit_opt_in(settings, monkeypatch) -> None:
    no_network(monkeypatch)
    config = LinkedInSearchConfig(keywords="python")
    with pytest.raises(CollectorDisabledError) as excinfo:
        search_linkedin(config, settings=settings, network_enabled=False)
    assert "--confirm-network" in str(excinfo.value)


def test_collection_honours_configured_raw_max_bytes(settings) -> None:
    body = fixture_text("linkedin_search_page.html")
    client = FakeHttpClient(
        [response(200, body, headers={"content-type": "text/html"})]
    )
    tiny = settings.with_overrides(raw_max_bytes=64)
    config = LinkedInSearchConfig(keywords="backend", max_pages=1)
    result = search_linkedin(
        config, settings=tiny, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert len(result.raw_documents) == 1
    document = result.raw_documents[0]
    assert document.byte_size > 64
    assert document.body is None
    assert document.truncated is True
    assert "not stored" in (document.notes or "")


def test_live_search_bounds_the_transport_at_raw_max_bytes(settings, monkeypatch) -> None:
    """The live transport must be created with the configured raw ceiling."""
    captured: dict[str, object] = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return FakeHttpClient(
            [
                response(
                    200,
                    fixture_text("linkedin_search_page.html"),
                    headers={"content-type": "text/html"},
                )
            ]
        )

    monkeypatch.setattr(linkedin, "UrllibHttpClient", factory)
    tiny = settings.with_overrides(raw_max_bytes=1234)
    config = LinkedInSearchConfig(keywords="backend", max_pages=1)
    search_linkedin(config, settings=tiny, network_enabled=True, sleep=lambda _: None)

    assert captured["max_download_bytes"] == 1234


def test_normal_live_search_response_is_parsed_and_stored(settings, monkeypatch) -> None:
    """A normal response through the real bounded transport still parses."""
    config = LinkedInSearchConfig(keywords="backend", max_pages=1)
    url = plan_search_urls(config, settings=settings)[0]
    opener = FakeOpener(
        FakeRawResponse(fixture_text("linkedin_search_page.html").encode("utf-8"), url)
    )
    monkeypatch.setattr(
        linkedin,
        "UrllibHttpClient",
        lambda **kwargs: UrllibHttpClient(opener=opener, **kwargs),
    )
    result = search_linkedin(
        config, settings=settings, network_enabled=True, sleep=lambda _: None
    )

    assert result.postings
    document = result.raw_documents[0]
    assert document.body is not None
    assert document.truncated is False


def test_oversized_live_search_response_is_bounded_and_not_parsed(
    settings, monkeypatch
) -> None:
    """A page over the raw ceiling is bounded in memory and never parsed."""
    limit = 512
    tiny = settings.with_overrides(raw_max_bytes=limit)
    config = LinkedInSearchConfig(keywords="backend", max_pages=1)
    url = plan_search_urls(config, settings=tiny)[0]
    huge = b"<html><body>" + (b"<li class='result-card'></li>" * 200) + b"</body></html>"
    assert len(huge) > limit
    opener = FakeOpener(FakeRawResponse(huge, url))
    monkeypatch.setattr(
        linkedin,
        "UrllibHttpClient",
        lambda **kwargs: UrllibHttpClient(opener=opener, **kwargs),
    )
    result = search_linkedin(
        config, settings=tiny, network_enabled=True, sleep=lambda _: None
    )

    assert result.pages_fetched == 1
    assert result.postings == []
    assert "download limit" in (result.notes or "")
    document = result.raw_documents[0]
    assert document.body is None
    assert document.truncated is True
    assert document.byte_size <= limit


def test_collection_parses_pages_and_respects_item_cap(settings) -> None:
    client = FakeHttpClient(
        [
            response(200, fixture_text("linkedin_search_page.html"), headers={"content-type": "text/html"}),
            response(200, fixture_text("linkedin_search_page.html"), headers={"content-type": "text/html"}),
        ]
    )
    config = LinkedInSearchConfig(keywords="backend", max_pages=2, max_items=10)
    result = search_linkedin(
        config,
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
    )
    assert client.call_count == 2
    assert result.status is SearchRunStatus.COMPLETED
    assert result.pages_fetched == 2
    # Page 2 repeats the same cards, so they dedupe inside the run.
    assert result.items_found == 3
    assert len(result.raw_documents) == 2
    assert result.raw_documents[0].body is not None
    assert result.raw_documents[0].sha256


def test_collection_stops_at_the_item_cap(settings) -> None:
    client = FakeHttpClient(
        [response(200, fixture_text("linkedin_search_page.html"), headers={"content-type": "text/html"})]
    )
    config = LinkedInSearchConfig(keywords="backend", max_pages=3, max_items=2)
    result = search_linkedin(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.items_found == 2
    assert client.call_count == 1


def test_collection_fails_closed_on_access_denied(settings) -> None:
    client = FakeHttpClient([response(403, "<html>denied</html>")])
    config = LinkedInSearchConfig(keywords="backend")
    result = search_linkedin(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert client.call_count == 1
    assert "access denied" in (result.error or "")
    # The refusal is still recorded for provenance.
    assert result.raw_documents and result.raw_documents[0].status_code == 403


def test_collection_fails_closed_on_rate_limit(settings) -> None:
    client = FakeHttpClient([response(429, "slow down")])
    config = LinkedInSearchConfig(keywords="backend")
    result = search_linkedin(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert client.call_count == 1


def test_collection_retries_transient_failures_then_gives_up(settings) -> None:
    client = FakeHttpClient(
        [
            response(503, "busy"),
            response(503, "busy"),
            response(503, "busy"),
            response(503, "busy"),
            response(503, "busy"),
        ]
    )
    jitterless = settings.with_overrides(request_jitter_seconds=0.0)
    config = LinkedInSearchConfig(keywords="backend", max_pages=1)
    sleeps: list[float] = []
    result = search_linkedin(
        config,
        settings=jitterless,
        network_enabled=True,
        client=client,
        sleep=sleeps.append,
    )
    assert result.status is SearchRunStatus.FAILED
    # 1 initial attempt + 2 retries = 3 requests, then the run aborts.
    assert client.call_count == 3
    # Retries apply both the pacing interval and the exponential backoff.
    assert [value for value in sleeps if value < 6.0] == [2.0, 4.0]
    assert any(value >= settings.request_delay_seconds - 0.5 for value in sleeps)


def test_collection_empty_page_stops_early(settings) -> None:
    client = FakeHttpClient([response(200, "<html>nothing here</html>", headers={"content-type": "text/html"})])
    config = LinkedInSearchConfig(keywords="backend", max_pages=3)
    result = search_linkedin(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert client.call_count == 1
    assert result.pages_fetched == 1
    assert result.items_found == 0
    assert "no parsable job cards" in (result.notes or "")


def test_collection_diagnoses_non_empty_body_with_zero_cards(settings) -> None:
    """A 200 with real HTML but no card roots must stay fail-open with a note."""
    body = (
        "<html><body><ol class='jobs-search__results-list'>"
        "<li><span>No matching jobs</span></li>"
        "</ol></body></html>"
    )
    client = FakeHttpClient([response(200, body, headers={"content-type": "text/html"})])
    config = LinkedInSearchConfig(keywords="backend", max_pages=3)
    result = search_linkedin(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert client.call_count == 1
    assert result.status is SearchRunStatus.COMPLETED
    assert result.pages_fetched == 1
    assert result.items_found == 0
    assert "no parsable job cards" in (result.notes or "")
    assert f"{len(body.encode('utf-8'))} byte(s)" in (result.notes or "")


def test_collection_dedupes_repeated_drift_cards(settings) -> None:
    """Cards from the nested-div markup dedupe across pages inside one run."""
    client = FakeHttpClient(
        [
            response(200, fixture_text("linkedin_search_page_drift.html"), headers={"content-type": "text/html"}),
            response(200, fixture_text("linkedin_search_page_drift.html"), headers={"content-type": "text/html"}),
        ]
    )
    config = LinkedInSearchConfig(keywords="cloud", max_pages=2, max_items=10)
    result = search_linkedin(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert client.call_count == 2
    assert result.pages_fetched == 2
    assert result.items_found == 3


def test_drift_cards_flow_through_pipeline_and_dedupe(conn, settings) -> None:
    """Parsed drift cards normalize, store, and re-ingest as duplicates."""
    config = LinkedInSearchConfig(keywords="cloud", max_pages=1, max_items=10)

    def run_once():
        client = FakeHttpClient(
            [response(200, fixture_text("linkedin_search_page_drift.html"), headers={"content-type": "text/html"})]
        )
        return search_linkedin(
            config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
        )

    first = ingest_collector_result(
        conn, run_once(), config=config, settings=settings, now="2026-09-15T12:00:00Z"
    )
    assert first.status is SearchRunStatus.COMPLETED
    assert first.items_found == 3
    assert first.new_jobs == 3
    assert first.duplicate_jobs == 0
    assert count_jobs(conn) == 3

    second = ingest_collector_result(
        conn, run_once(), config=config, settings=settings, now="2026-09-16T12:00:00Z"
    )
    assert second.new_jobs == 0
    assert second.duplicate_jobs == 3
    assert count_jobs(conn) == 3


def test_collection_applies_pacing_between_pages(settings) -> None:
    client = FakeHttpClient(
        [
            response(200, fixture_text("linkedin_search_page.html"), headers={"content-type": "text/html"}),
            response(200, "", headers={"content-type": "text/html"}),
        ]
    )
    config = LinkedInSearchConfig(keywords="backend", max_pages=2)
    sleeps: list[float] = []
    search_linkedin(
        config, settings=settings, network_enabled=True, client=client, sleep=sleeps.append
    )
    # A real pacer sleep of at least the configured delay happens between pages.
    assert sleeps and sleeps[0] >= settings.request_delay_seconds


def test_collection_never_uses_ambient_credentials(settings) -> None:
    client = FakeHttpClient([response(200, "<html></html>")])
    config = LinkedInSearchConfig(keywords="backend", max_pages=1)
    search_linkedin(config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None)
    headers = client.calls[0]["headers"]
    assert "Cookie" not in headers
    assert "Authorization" not in headers
    assert headers["Accept-Language"].startswith("en")
