"""Offline contract tests for the shared detail-flow helpers.

The LinkedIn, GetManfred, and Tecnoempleo adapters keep their historical alias
names but must delegate the strict final-URL policy and raw-document recording to
the single shared implementations. No test opens a socket.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

from jobscout.collectors import (
    getmanfred_details,
    linkedin_details,
    tecnoempleo_details,
)
from jobscout.collectors.base import record_detail_response
from jobscout.url_policy import sanitize_final_url

REQUESTED = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/3911111111"
REDIRECTED = "https://es.linkedin.com/jobs-guest/jobs/api/jobPosting/3911111111"


class _Item:
    raw_document_index: int | None = None


def _result() -> SimpleNamespace:
    return SimpleNamespace(raw_documents=[])


def _response(body: str = "<html>detail</html>", **overrides) -> SimpleNamespace:
    payload = {
        "body": body,
        "status_code": 200,
        "headers": {"content-type": "text/html; charset=utf-8"},
        "fetched_at": "2026-09-15T12:00:00Z",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


# ---------------------------------------------------------------------------
# Shared final-URL sanitizer
# ---------------------------------------------------------------------------


def test_shared_sanitize_final_url_keeps_only_safe_redirects() -> None:
    assert sanitize_final_url(REQUESTED, REDIRECTED) == REDIRECTED
    # No redirect observed.
    assert sanitize_final_url(REQUESTED, REQUESTED) is None
    assert sanitize_final_url(REQUESTED, "") is None
    assert sanitize_final_url(REQUESTED, None) is None
    assert sanitize_final_url(None, "") is None
    for unsafe in (
        "http://www.linkedin.com/jobs",
        "https://www.linkedin.com:8443/jobs",
        "https://user:pass@www.linkedin.com/jobs",
        "https://evil.test/jobs",
        "https://linkedin.com.evil.test/jobs",
        "file:///etc/passwd",
    ):
        assert sanitize_final_url(REQUESTED, unsafe) is None


def test_detail_adapters_delegate_to_the_shared_sanitizer() -> None:
    # The adapters keep their historical alias names but must apply the exact
    # shared policy, so a value one refuses is refused by all.
    cases = (
        (REQUESTED, REDIRECTED),
        (REQUESTED, REQUESTED),
        (REQUESTED, "http://www.linkedin.com/jobs"),
        (REQUESTED, "https://www.linkedin.com:8443/jobs"),
        (REQUESTED, "https://user:pass@www.linkedin.com/jobs"),
        (REQUESTED, "https://evil.test/jobs"),
        (REQUESTED, "file:///etc/passwd"),
    )
    for requested, response in cases:
        expected = sanitize_final_url(requested, response)
        assert linkedin_details.sanitize_final_url(requested, response) == expected
        assert getmanfred_details._sanitize_final_url(requested, response) == expected
        assert tecnoempleo_details._sanitize_final_url(requested, response) == expected


# ---------------------------------------------------------------------------
# Shared detail-response recorder
# ---------------------------------------------------------------------------


def test_shared_record_detail_response_contract() -> None:
    result = _result()
    item = _Item()
    url = "https://www.getmanfred.com/ofertas-empleo/8476/x"
    record_detail_response(
        result,
        _response(),
        url,
        source="getmanfred",
        max_bytes=1024,
        item=item,
    )

    assert item.raw_document_index == 0
    assert len(result.raw_documents) == 1
    document = result.raw_documents[0]
    assert str(document.kind) == "job_detail"
    assert document.source == "getmanfred"
    assert document.url == url
    assert document.status_code == 200
    assert document.content_type == "text/html; charset=utf-8"
    assert document.fetched_at == "2026-09-15T12:00:00Z"
    assert document.body == "<html>detail</html>"
    assert document.truncated is False
    assert document.sha256 == hashlib.sha256(b"<html>detail</html>").hexdigest()
    assert document.byte_size == len(b"<html>detail</html>")
    assert document.stored_at  # stamped at record time


def test_shared_record_detail_response_metadata_only_and_none() -> None:
    result = _result()
    item = _Item()
    record_detail_response(
        result,
        _response(body="x" * 100),
        "u",
        source="linkedin",
        max_bytes=10,
        item=item,
    )
    document = result.raw_documents[0]
    assert document.body is None
    assert document.truncated is True
    assert document.notes and "download limit" in document.notes

    empty = _result()
    record_detail_response(
        empty, None, "u", source="linkedin", max_bytes=10, item=_Item()
    )
    assert empty.raw_documents == []


def test_shared_record_detail_response_indexes_consecutive_documents() -> None:
    result = _result()
    first, second = _Item(), _Item()
    record_detail_response(
        result, _response(body="a"), "u1", source="linkedin", max_bytes=10, item=first
    )
    record_detail_response(
        result, _response(body="b"), "u2", source="linkedin", max_bytes=10, item=second
    )
    assert (first.raw_document_index, second.raw_document_index) == (0, 1)
    assert [document.url for document in result.raw_documents] == ["u1", "u2"]
