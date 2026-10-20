"""GetManfred sitemap inventory tests: parsing, gating, and provenance.

No real network request is ever made. The transport is the offline fake client
and the sitemap bodies are local fixtures copied from the public format.
"""

from __future__ import annotations

import pytest

from conftest import FakeHttpClient, FIXTURES, fixture_text, no_network, response

from jobscout.collectors.base import CollectorDisabledError
from jobscout.collectors.base import TRUNCATION_HEADER
from jobscout.collectors.getmanfred import (
    SITEMAP_KIND_INDEX,
    SITEMAP_KIND_UNKNOWN,
    SITEMAP_KIND_URLSET,
    GetManfredSitemapConfig,
    classify_sitemap,
    extract_getmanfred_job_id,
    is_offer_url,
    parse_index_entries,
    parse_url_entries,
    plan_sitemap_urls,
    postings_from_sitemap,
    search_getmanfred,
    title_from_offer_slug,
)
from jobscout.collectors.keyword_match import keyword_matches, matching_keywords
from jobscout.db import count_jobs, get_search_run, list_search_runs
from jobscout.models import ContentStatus, PositionKind, SearchRunStatus
from jobscout.pipeline import ingest_collector_result

SITEMAP_FIXTURE = FIXTURES / "getmanfred_sitemap_offers.xml"
INDEX_FIXTURE = FIXTURES / "getmanfred_sitemap_index.xml"
OFFER_PREFIX = "/ofertas-empleo/"


def test_classify_and_parse_a_real_urlset() -> None:
    text = fixture_text("getmanfred_sitemap_offers.xml")
    assert classify_sitemap(text) == SITEMAP_KIND_URLSET
    entries = parse_url_entries(text)
    assert len(entries) >= 6
    assert entries[0]["loc"].startswith("https://www.getmanfred.com/ofertas-empleo/")
    assert entries[0]["lastmod"] == "2026-09-18T07:48:11.546Z"


def test_parse_index_and_classify_index() -> None:
    text = fixture_text("getmanfred_sitemap_index.xml")
    assert classify_sitemap(text) == SITEMAP_KIND_INDEX
    children = parse_index_entries(text)
    assert "https://www.getmanfred.com/sitemap-offers.xml" in children
    assert parse_url_entries(text) == []


def test_classify_rejects_non_sitemap_bodies() -> None:
    assert classify_sitemap("<html><body>nope</body></html>") == SITEMAP_KIND_UNKNOWN
    assert classify_sitemap("") == SITEMAP_KIND_UNKNOWN
    assert classify_sitemap("<urlset>") == SITEMAP_KIND_UNKNOWN


def test_offer_url_and_id_extraction() -> None:
    url = "https://www.getmanfred.com/ofertas-empleo/8476/datcisions-backend-engineer-php-sept26"
    assert extract_getmanfred_job_id(url) == "8476"
    assert is_offer_url(url, offer_path_prefix=OFFER_PREFIX)
    assert not is_offer_url(
        "https://www.getmanfred.com/ofertas-empleo", offer_path_prefix=OFFER_PREFIX
    )
    assert not is_offer_url(
        "https://evil.test/ofertas-empleo/1/x", offer_path_prefix=OFFER_PREFIX
    )


def test_title_from_slug_is_display_only_and_never_invents_company() -> None:
    title = title_from_offer_slug(
        "https://www.getmanfred.com/ofertas-empleo/8476/datcisions-backend-engineer-php-sept26"
    )
    assert title == "Datcisions backend engineer php sept26"


# ---------------------------------------------------------------------------
# Keyword filtering (local, no fan-out)
# ---------------------------------------------------------------------------


MIXED_FIXTURE = "getmanfred_sitemap_mixed.xml"


def test_keyword_match_is_accent_and_case_insensitive_token_subset() -> None:
    assert keyword_matches("Platform Engineer", "acme platform engineer azure")
    assert keyword_matches("azure devsecops", "Azure DevSecOps Engineer")
    assert keyword_matches("Cloud Security", "cloud-security-engineer")
    assert not keyword_matches("Platform", "frontend developer")
    # Token matching, never substring: "front" must not match "frontend".
    assert not keyword_matches("front", "frontend react developer")
    assert not keyword_matches("", "anything")
    assert matching_keywords("platform engineer", "platform engineer") == [
        "platform engineer"
    ]
    assert matching_keywords("platform engineer", "backend engineer") == []
    # A comma-separated catalogue is matched locally without another sitemap request.
    assert matching_keywords("cloud, security", "cloud engineer") == ["cloud"]


def test_postings_from_sitemap_filters_backend_frontend_when_not_approved() -> None:
    # Approved keywords are platform/cloud/security, so backend and frontend
    # offers must be dropped — the regression this fix targets.
    platform = postings_from_sitemap(
        fixture_text(MIXED_FIXTURE),
        offer_path_prefix=OFFER_PREFIX,
        keywords="Platform Engineer",
    )
    assert [p.source_job_id for p in platform] == ["9001"]
    assert platform[0].extra["matched_keywords"] == ["Platform Engineer"]

    frontend = postings_from_sitemap(
        fixture_text(MIXED_FIXTURE),
        offer_path_prefix=OFFER_PREFIX,
        keywords="Frontend Developer",
    )
    # The keyword is approved here, and backend still does not leak in.
    assert [p.source_job_id for p in frontend] == ["9003"]

    backend = postings_from_sitemap(
        fixture_text(MIXED_FIXTURE),
        offer_path_prefix=OFFER_PREFIX,
        keywords="Platform Engineer",
    )
    assert all("php" not in (p.url or "") for p in backend)


def test_postings_from_sitemap_keeps_offer_under_match_and_records_keyword() -> None:
    postings = postings_from_sitemap(
        fixture_text(MIXED_FIXTURE),
        offer_path_prefix=OFFER_PREFIX,
        keywords="Azure DevSecOps",
    )
    assert len(postings) == 1
    assert postings[0].source_job_id == "9005"
    assert postings[0].extra["matched_keywords"] == ["Azure DevSecOps"]
    # No keyword means a plain inventory keeping every offer entry.
    unfiltered = postings_from_sitemap(
        fixture_text(MIXED_FIXTURE), offer_path_prefix=OFFER_PREFIX
    )
    assert len(unfiltered) == 5
    assert "matched_keywords" not in unfiltered[0].extra


def test_search_getmanfred_applies_keyword_with_single_request(settings) -> None:
    client = FakeHttpClient(
        [response(200, fixture_text(MIXED_FIXTURE), headers={"content-type": "application/xml"})]
    )
    config = GetManfredSitemapConfig(keywords="Cloud Engineer", max_items=10)
    result = search_getmanfred(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert client.call_count == 1  # local filter, no fan-out
    assert [p.source_job_id for p in result.postings] == ["9002"]
    assert "Cloud Engineer" in (result.notes or "")


def test_search_getmanfred_keyword_records_run_params(conn, settings) -> None:
    client = FakeHttpClient(
        [response(200, fixture_text(MIXED_FIXTURE), headers={"content-type": "application/xml"})]
    )
    config = GetManfredSitemapConfig(keywords="Platform Engineer", max_items=10)
    result = search_getmanfred(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    outcome = ingest_collector_result(
        conn, result, config=config, settings=settings, cycle_id="c1"
    )
    run = get_search_run(conn, outcome.run_id)
    assert run.params["keywords"] == "Platform Engineer"


def test_keyword_provenance_is_stored_without_mixing_platforms(conn, settings) -> None:
    """A GetManfred offer keeps its matched keyword and never joins a LinkedIn series."""
    from jobscout.collectors.linkedin import LinkedInSearchConfig, search_linkedin

    # One GetManfred offer matched by the approved "Cloud Engineer" keyword.
    gm_client = FakeHttpClient(
        [response(200, fixture_text(MIXED_FIXTURE), headers={"content-type": "application/xml"})]
    )
    gm_config = GetManfredSitemapConfig(keywords="Cloud Engineer", max_items=10)
    gm_result = search_getmanfred(
        gm_config, settings=settings, network_enabled=True, client=gm_client, sleep=lambda _: None
    )
    ingest_collector_result(
        conn, gm_result, config=gm_config, settings=settings, cycle_id="cycle-1"
    )

    # A LinkedIn run that used the same literal keyword is a separate series.
    li_client = FakeHttpClient(
        [response(200, fixture_text("linkedin_search_page_drift.html"), headers={"content-type": "text/html"})]
    )
    li_config = LinkedInSearchConfig(keywords="Cloud Engineer", max_pages=1, max_items=10)
    li_result = search_linkedin(
        li_config, settings=settings, network_enabled=True, client=li_client, sleep=lambda _: None
    )
    ingest_collector_result(
        conn, li_result, config=li_config, settings=settings, cycle_id="cycle-1"
    )

    from jobscout.dashboard import build_payload

    payload = build_payload(conn, top=10, now=None)
    series = {(item["provider"], item["keyword"]) for item in payload["keywords"]}
    assert ("getmanfred", "Cloud Engineer") in series
    assert ("linkedin", "Cloud Engineer") in series
    # Every stored keyword observation is scoped to its provider; none is None.
    for offer in payload["offers"]:
        for key in offer["keyword_keys"]:
            assert key.split("|", 1)[0] in {"getmanfred", "linkedin"}
    # The GetManfred offer carries the keyword it matched as its series key. The
    # payload no longer serializes the offer's provider, so identify it by key.
    gm_offer = next(
        offer
        for offer in payload["offers"]
        if any(key.startswith("getmanfred|") for key in offer["keyword_keys"])
    )
    assert gm_offer["keyword_keys"] == ["getmanfred|Cloud Engineer"]


def test_postings_from_sitemap_keep_contract() -> None:
    postings = postings_from_sitemap(
        fixture_text("getmanfred_sitemap_offers.xml"),
        offer_path_prefix=OFFER_PREFIX,
        fetched_at="2026-09-18T10:00:00Z",
        max_items=10,
    )
    # The duplicate <loc> is present in the fixture; postings_from_sitemap keeps
    # both, and the run-level dedupe removes it. At least the three real offers.
    assert len(postings) >= 3
    first = postings[0]
    assert first.source == "getmanfred"
    assert first.source_job_id == "8476"
    assert first.content_status is ContentStatus.PARTIAL
    assert first.company == ""
    assert first.fingerprint.startswith("v1:id:getmanfred:8476")
    assert first.extra["sitemap_lastmod"] == "2026-09-18T07:48:11.546Z"


def test_config_validates_host_and_caps() -> None:
    with pytest.raises(ValueError):
        GetManfredSitemapConfig(sitemap_url="https://evil.test/sitemap.xml")
    with pytest.raises(ValueError):
        GetManfredSitemapConfig(max_items=0)
    with pytest.raises(ValueError):
        GetManfredSitemapConfig(offer_path_prefix="ofertas/")


def test_dry_run_makes_no_request(settings, monkeypatch) -> None:
    no_network(monkeypatch)
    config = GetManfredSitemapConfig()
    result = search_getmanfred(config, settings=settings, dry_run=True)
    assert result.status is SearchRunStatus.DRY_RUN
    assert result.planned_urls == plan_sitemap_urls(config)
    assert result.postings == []


def test_collection_is_disabled_without_opt_in(settings, monkeypatch) -> None:
    no_network(monkeypatch)
    with pytest.raises(CollectorDisabledError) as excinfo:
        search_getmanfred(GetManfredSitemapConfig(), settings=settings, network_enabled=False)
    assert "--confirm-network" in str(excinfo.value)


def test_live_inventory_parses_and_records_raw_document(settings) -> None:
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text("getmanfred_sitemap_offers.xml"),
                headers={"content-type": "application/xml"},
            )
        ]
    )
    result = search_getmanfred(
        GetManfredSitemapConfig(max_items=10),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert result.pages_fetched == 1
    assert len(result.postings) == 3
    assert result.items_found == 3
    assert result.raw_documents[0].body is not None
    assert "offer(s) discovered" in (result.notes or "")


def test_getmanfred_body_just_below_provider_limit_is_parsed(settings) -> None:
    base = fixture_text("getmanfred_sitemap_offers.xml")
    limit = len(base.encode("utf-8")) + 100
    settings = settings.with_overrides(getmanfred_max_bytes=limit)
    padding = "x" * (limit - len(base.encode("utf-8")) - 30)
    body = base.replace("</urlset>", f"<!-- {padding} --></urlset>")
    assert len(body.encode("utf-8")) < settings.getmanfred_max_bytes
    result = search_getmanfred(
        GetManfredSitemapConfig(max_items=10),
        settings=settings,
        network_enabled=True,
        client=FakeHttpClient([response(200, body, headers={"content-type": "application/xml"})]),
        sleep=lambda _: None,
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert len(result.postings) == 3


def test_getmanfred_body_over_provider_limit_is_rejected_with_note(settings) -> None:
    settings = settings.with_overrides(getmanfred_max_bytes=1024)
    body = fixture_text("getmanfred_sitemap_offers.xml") + (" " * 1024)
    result = search_getmanfred(
        GetManfredSitemapConfig(max_items=10),
        settings=settings,
        network_enabled=True,
        client=FakeHttpClient(
            [response(200, body, headers={TRUNCATION_HEADER: "1"})]
        ),
        sleep=lambda _: None,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert result.postings == []
    assert result.notes == (
        "https://www.getmanfred.com/sitemap-offers.xml exceeded the "
        "1024-byte download limit; content not parsed"
    )
    assert result.raw_documents[0].body is None
    assert result.raw_documents[0].truncated is True


def test_sitemap_index_follows_only_allow_listed_children(settings) -> None:
    client = FakeHttpClient(
        [
            response(200, fixture_text("getmanfred_sitemap_index.xml"), headers={"content-type": "application/xml"}),
            response(200, fixture_text("getmanfred_sitemap_offers.xml"), headers={"content-type": "application/xml"}),
        ]
    )
    config = GetManfredSitemapConfig(
        sitemap_url="https://www.getmanfred.com/sitemap.xml",
        max_documents=2,
        max_items=10,
    )
    result = search_getmanfred(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    assert result.status is SearchRunStatus.COMPLETED
    assert len(result.postings) == 3
    called = [call["url"] for call in client.calls]
    assert called[0] == "https://www.getmanfred.com/sitemap.xml"
    assert called[1] == "https://www.getmanfred.com/sitemap-offers.xml"
    assert all("evil.test" not in url for url in called)


def test_unsupported_body_fails_closed(settings) -> None:
    client = FakeHttpClient([response(200, "<html>not a sitemap</html>")])
    result = search_getmanfred(
        GetManfredSitemapConfig(),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert "unsupported sitemap" in (result.error or "")


def test_access_denied_fails_closed(settings) -> None:
    client = FakeHttpClient([response(403, "denied")])
    result = search_getmanfred(
        GetManfredSitemapConfig(),
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
    )
    assert result.status is SearchRunStatus.FAILED_CLOSED
    assert result.raw_documents[0].status_code == 403


def test_ingestion_records_provider_cycle_and_none_position(conn, settings) -> None:
    client = FakeHttpClient(
        [response(200, fixture_text("getmanfred_sitemap_offers.xml"), headers={"content-type": "application/xml"})]
    )
    config = GetManfredSitemapConfig(max_items=10)
    result = search_getmanfred(
        config, settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    outcome = ingest_collector_result(
        conn, result, config=config, settings=settings, cycle_id="cycle-2026-09"
    )

    run = get_search_run(conn, outcome.run_id)
    assert run is not None
    assert run.provider == "getmanfred"
    assert run.cycle_id == "cycle-2026-09"
    assert run.query is None
    assert run.params["sitemap_url"].endswith("/sitemap-offers.xml")
    rows = conn.execute(
        "SELECT position_kind, rank_in_run FROM search_run_items ORDER BY id"
    ).fetchall()
    assert rows
    assert all(row["position_kind"] == "none" for row in rows)
    assert all(row["rank_in_run"] == 0 for row in rows)
    assert count_jobs(conn) == 3


def test_inventory_reingest_is_idempotent(conn, settings) -> None:
    def run_once():
        client = FakeHttpClient(
            [response(200, fixture_text("getmanfred_sitemap_offers.xml"), headers={"content-type": "application/xml"})]
        )
        return search_getmanfred(
            GetManfredSitemapConfig(max_items=10),
            settings=settings,
            network_enabled=True,
            client=client,
            sleep=lambda _: None,
        )

    first = ingest_collector_result(conn, run_once(), config=GetManfredSitemapConfig(max_items=10), settings=settings)
    second = ingest_collector_result(conn, run_once(), config=GetManfredSitemapConfig(max_items=10), settings=settings)
    assert first.new_jobs == 3
    assert second.new_jobs == 0
    assert count_jobs(conn) == 3
    assert [run.provider for run in list_search_runs(conn)] == ["getmanfred", "getmanfred"]
