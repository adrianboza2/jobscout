"""Offers a provider marks as closed while still answering HTTP 200.

LinkedIn keeps serving a closed posting with its description plus a "No longer
accepting applications" caption; GetManfred keeps the full offer page and says
``status: CLOSED`` in its Next.js page data. Markup below mirrors live pages
(captured 2026-11-12), trimmed to the parts the parsers read.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeHttpClient, fixture_text, response

from jobscout.cli import EXIT_OK, main
from jobscout.collectors.base import CLOSED_NOTE
from jobscout.collectors.getmanfred_details import fetch_getmanfred_details
from jobscout.collectors.getmanfred_details import parse_detail_closed as getmanfred_closed
from jobscout.collectors.linkedin_details import fetch_linkedin_details
from jobscout.collectors.linkedin_details import parse_detail_closed as linkedin_closed
from jobscout.db import (
    connect,
    ensure_job_variant,
    get_job,
    init_db,
    insert_evaluation,
    latest_detail_fetch,
    latest_evaluation,
    store_posting,
)
from jobscout.models import ContentStatus, DetailFetchStatus, Evaluation, ScorerName
from jobscout.normalize import normalize_job_posting
from jobscout.pipeline import closed_listings, ingest_detail_results

LINKEDIN_CLOSED_CAPTION = (
    '<figure class="closed-job closed-job__flavor topcard__flavor-row">'
    '<span class="closed-job__icon closed-job__icon--error-pebble lazy-load"></span>'
    '<figcaption class="closed-job__flavor--closed">No longer accepting applications'
    "</figcaption></figure>"
)
GETMANFRED_URL = "https://www.getmanfred.com/ofertas-empleo/826/nubosas-junior-devops-sept22"
ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"


def _linkedin_page(*, closed: bool) -> str:
    page = fixture_text("linkedin_detail_page.html")
    if closed:
        page = page.replace('<section class="show-more-less-html">', LINKEDIN_CLOSED_CAPTION + '<section class="show-more-less-html">')
    return page


def _next_data(offer: dict) -> str:
    payload = {"props": {"pageProps": {"offer": offer}}, "page": "/job-offers/[id]/[slug]"}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


def _getmanfred_page(*, status: str, active: bool) -> str:
    page = fixture_text("getmanfred_detail_page_jobposting.html")
    return page + _next_data({"id": 826, "status": status, "active": active})


def _store_linkedin(conn, source_job_id: str = "4476740910"):
    posting = normalize_job_posting(
        {
            "source": "linkedin",
            "source_job_id": source_job_id,
            "jobUrl": f"https://www.linkedin.com/jobs/view/{source_job_id}/role",
            "jobTitle": "Cloud Security Engineer Junior",
            "companyName": "Acme",
            "location": f"Madrid ({source_job_id})",
        },
        source="linkedin",
    )
    job, _ = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    return get_job(conn, int(job.id))


def _store_getmanfred(conn):
    posting = normalize_job_posting(
        {
            "source": "getmanfred",
            "source_job_id": "826",
            "url": GETMANFRED_URL,
            "title": "Nubosas junior devops sept22",
            "company": "",
            "content_status": str(ContentStatus.PARTIAL),
        },
        source="getmanfred",
    )
    job, _ = store_posting(conn, posting)
    return get_job(conn, int(job.id))


def _fetch_linkedin(job, settings, page: str):
    client = FakeHttpClient([response(200, page, headers={"content-type": "text/html"})])
    return fetch_linkedin_details(
        [job], settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )


# -- parsing (pure) ----------------------------------------------------------


def test_linkedin_closed_caption_is_recognised() -> None:
    assert linkedin_closed(_linkedin_page(closed=True)) is True
    assert linkedin_closed(_linkedin_page(closed=False)) is False
    # The words alone, without LinkedIn's class, are not a closure signal.
    assert linkedin_closed("<p>No longer accepting applications</p>") is False
    assert linkedin_closed("") is False


@pytest.mark.parametrize(
    ("offer", "expected"),
    [
        ({"status": "CLOSED", "active": False}, True),
        ({"status": "ACTIVE", "active": True}, False),
        ({"status": "CLOSED"}, True),
        ({"active": False}, True),
        ({}, False),
    ],
)
def test_getmanfred_page_data_status(offer, expected) -> None:
    assert getmanfred_closed("<html><body>" + _next_data(offer) + "</body></html>") is expected


@pytest.mark.parametrize(
    "page",
    [
        "",
        fixture_text("getmanfred_detail_page_jobposting.html"),
        '<script id="__NEXT_DATA__">{not json</script>',
        '<script id="__NEXT_DATA__">["CLOSED"]</script>',
        # A truncated page loses the closing tag: unknown, so not closed.
        '<script id="__NEXT_DATA__">{"props": {"pageProps": {"offer": {"status": "CLOSED"',
    ],
)
def test_getmanfred_unreadable_page_data_is_not_closed(page) -> None:
    assert getmanfred_closed(page) is False


# -- fetch + ingest ------------------------------------------------------------


def test_closed_linkedin_page_deactivates_and_keeps_history(conn, settings) -> None:
    job = _store_linkedin(conn)
    insert_evaluation(
        conn,
        Evaluation(job_id=job.id, scorer=ScorerName.HEURISTIC, overall_score=82, recommendation="apply"),
    )

    result = _fetch_linkedin(job, settings, _linkedin_page(closed=True))
    item = result.items[0]
    assert item.closed is True
    # The description is still read: closure does not discard the posting text.
    assert item.status is DetailFetchStatus.OK
    assert CLOSED_NOTE in item.notes

    outcome = ingest_detail_results(conn, result, settings=settings)
    assert outcome.deactivated == 1
    assert get_job(conn, job.id).is_active is False
    assert latest_evaluation(conn, job.id).overall_score == 82
    assert CLOSED_NOTE in latest_detail_fetch(conn, job.id).notes


def test_open_linkedin_page_stays_active(conn, settings) -> None:
    job = _store_linkedin(conn)
    result = _fetch_linkedin(job, settings, _linkedin_page(closed=False))
    assert result.items[0].closed is False
    outcome = ingest_detail_results(conn, result, settings=settings)
    assert outcome.deactivated == 0
    assert get_job(conn, job.id).is_active is True


def test_closed_listing_keeps_job_active_while_a_repost_is_live(conn, settings) -> None:
    job = _store_linkedin(conn)
    ensure_job_variant(
        conn,
        int(job.id),
        source="linkedin",
        source_job_id="4480639924",
        url="https://www.linkedin.com/jobs/view/4480639924/role",
        timestamp="2026-10-01T10:00:00Z",
    )

    outcome = ingest_detail_results(conn, _fetch_linkedin(job, settings, _linkedin_page(closed=True)), settings=settings)

    assert outcome.deactivated == 0
    assert get_job(conn, job.id).is_active is True


@pytest.mark.parametrize(("status", "active", "expected_active"), [("CLOSED", False, False), ("ACTIVE", True, True)])
def test_getmanfred_page_status_drives_deactivation(conn, settings, status, active, expected_active) -> None:
    job = _store_getmanfred(conn)
    client = FakeHttpClient(
        [response(200, _getmanfred_page(status=status, active=active), headers={"content-type": "text/html"}, url=GETMANFRED_URL)]
    )
    result = fetch_getmanfred_details(
        [job], settings=settings, network_enabled=True, client=client, sleep=lambda _: None, robots_text=ROBOTS_ALLOW
    )
    assert result.items[0].closed is (not expected_active)
    assert result.items[0].status is DetailFetchStatus.OK

    ingest_detail_results(conn, result, settings=settings, provider="getmanfred")
    assert get_job(conn, job.id).is_active is expected_active


# -- offline backfill ------------------------------------------------------------


def _ingest_as_old_adapter(conn, job, settings, page: str) -> None:
    """Store a closed page the way the adapter did before it recognised closure."""
    result = _fetch_linkedin(job, settings, page)
    result.items[0].closed = False
    ingest_detail_results(conn, result, settings=settings)


def test_backfill_selects_only_listings_whose_stored_page_is_closed(conn, settings) -> None:
    closed_job = _store_linkedin(conn, "4476740910")
    open_job = _store_linkedin(conn, "4476740911")
    _ingest_as_old_adapter(conn, closed_job, settings, _linkedin_page(closed=True))
    _ingest_as_old_adapter(conn, open_job, settings, _linkedin_page(closed=False))
    assert get_job(conn, closed_job.id).is_active is True

    assert [item["job_id"] for item in closed_listings(conn)] == [closed_job.id]


def test_deactivate_closed_command_previews_then_applies(settings, capsys) -> None:
    db_path = settings.db_path
    conn = connect(db_path)
    init_db(conn)
    job = _store_linkedin(conn)
    _ingest_as_old_adapter(conn, job, settings, _linkedin_page(closed=True))
    conn.close()

    assert main(["--db", str(db_path), "deactivate-closed"]) == EXIT_OK
    assert "1 active listing(s)" in capsys.readouterr().out
    conn = connect(db_path)
    assert get_job(conn, job.id).is_active is True
    conn.close()

    assert main(["--db", str(db_path), "deactivate-closed", "--apply", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True and payload["changed"] == 1
    conn = connect(db_path)
    assert get_job(conn, job.id).is_active is False
    assert closed_listings(conn) == []
    conn.close()


def test_backfill_never_blames_a_repost_for_an_unattributed_old_page(conn, settings) -> None:
    job = _store_linkedin(conn)
    _ingest_as_old_adapter(conn, job, settings, _linkedin_page(closed=True))
    # Simulate a fetch stored before variants existed, then a live repost.
    conn.execute("UPDATE job_detail_fetches SET variant_id = NULL")
    conn.commit()
    ensure_job_variant(
        conn,
        int(job.id),
        source="linkedin",
        source_job_id="4480639924",
        url="https://www.linkedin.com/jobs/view/4480639924/role",
        timestamp="2026-10-01T10:00:00Z",
    )

    assert closed_listings(conn) == []


def test_resighting_does_not_revive_a_closed_listing(conn, settings) -> None:
    job = _store_linkedin(conn)
    ingest_detail_results(conn, _fetch_linkedin(job, settings, _linkedin_page(closed=True)), settings=settings)
    assert get_job(conn, job.id).is_active is False

    # GetManfred's sitemap keeps listing closed offers; a search can re-surface one.
    _store_linkedin(conn)

    assert get_job(conn, job.id).is_active is False


def test_resighting_still_revives_a_404(conn, settings) -> None:
    job = _store_linkedin(conn)
    client = FakeHttpClient([response(404, "gone")])
    result = fetch_linkedin_details(
        [job], settings=settings, network_enabled=True, client=client, sleep=lambda _: None
    )
    ingest_detail_results(conn, result, settings=settings)
    assert get_job(conn, job.id).is_active is False

    _store_linkedin(conn)

    assert get_job(conn, job.id).is_active is True


def test_backfilled_closure_also_survives_a_resighting(settings) -> None:
    db_path = settings.db_path
    conn = connect(db_path)
    init_db(conn)
    job = _store_linkedin(conn)
    _ingest_as_old_adapter(conn, job, settings, _linkedin_page(closed=True))
    conn.close()
    assert main(["--db", str(db_path), "deactivate-closed", "--apply"]) == EXIT_OK

    conn = connect(db_path)
    _store_linkedin(conn)
    assert get_job(conn, job.id).is_active is False
    conn.close()
