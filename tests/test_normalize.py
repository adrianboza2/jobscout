"""Normalization and fingerprinting tests (pure functions, offline)."""

from __future__ import annotations

from jobscout.models import ContentStatus, JobPosting
from jobscout.normalize import (
    canonicalize_url,
    compute_dedupe_key,
    compute_fingerprint,
    content_hash,
    extract_linkedin_job_id,
    finalize_posting,
    html_to_text,
    normalize_company,
    normalize_employment_type,
    normalize_job_posting,
    normalize_location,
    normalize_postings,
    normalize_seniority,
    normalize_text,
)


def test_normalize_text_collapses_whitespace_and_unicode() -> None:
    assert normalize_text("  Senior\t Backend\nEngineer  ") == "Senior Backend Engineer"
    assert normalize_text("ＡＢＣ") == "ABC"
    assert normalize_text(None) == ""


def test_html_to_text_strips_markup_and_keeps_list_lines() -> None:
    html = "<p>Hello <b>world</b></p><ul><li>Python</li><li>SQL</li></ul><script>alert(1)</script>"
    text = html_to_text(html)
    assert "Hello world" in text
    assert "Python" in text and "SQL" in text
    assert "alert" not in text
    assert "<" not in text


def test_canonicalize_url_strips_tracking_and_normalizes() -> None:
    raw = "HTTPS://WWW.LinkedIn.com/jobs/view/3901234567/?trk=x&position=2&refId=abc#frag"
    assert canonicalize_url(raw) == "https://www.linkedin.com/jobs/view/3901234567"
    assert canonicalize_url("https://example.test/a/?b=2&a=1") == "https://example.test/a?a=1&b=2"
    assert canonicalize_url("example.test/path/") == "https://example.test/path"


def test_extract_linkedin_job_id_accepts_multiple_shapes() -> None:
    assert extract_linkedin_job_id("https://www.linkedin.com/jobs/view/3901234567/title") == "3901234567"
    assert extract_linkedin_job_id("https://x.test/?currentJobId=3901234567") == "3901234567"
    assert extract_linkedin_job_id("urn:li:jobPosting:3901234567") == "3901234567"
    assert extract_linkedin_job_id("https://example.test/jobs/123") is None
    assert extract_linkedin_job_id(None) is None


def test_company_and_location_normalization() -> None:
    assert normalize_company("Acme Data, Inc.") == "acme data"
    assert normalize_company("ACME DATA LLC") == normalize_company("Acme Data")
    assert normalize_location("Buenos Aires, Argentina") == "buenos aires argentina"


def test_seniority_and_employment_type_buckets() -> None:
    assert normalize_seniority("Senior level") == "senior"
    assert normalize_seniority("Sr. Engineer") == "senior"
    assert normalize_seniority("Semi Senior") == "mid"
    assert normalize_seniority(None) == "unknown"
    assert normalize_employment_type("Full-time") == "full_time"
    assert normalize_employment_type("Part time") == "part_time"
    assert normalize_employment_type("") == "unknown"


def test_fingerprint_is_stable_across_tracking_and_casing() -> None:
    first = finalize_posting(
        JobPosting(
            source="linkedin",
            title="Senior Backend Engineer",
            company="Acme Data Inc.",
            url="https://www.linkedin.com/jobs/view/3901234567/?trk=a&position=1",
            location="Remote, Argentina",
            source_job_id="3901234567",
        )
    )
    second = finalize_posting(
        JobPosting(
            source="linkedin",
            title="senior backend engineer",
            company="ACME DATA, INC",
            url="https://www.linkedin.com/jobs/view/3901234567/senior-backend-engineer?refId=zzz",
            location="remote argentina",
            source_job_id="3901234567",
        )
    )
    assert first.fingerprint == second.fingerprint
    assert first.dedupe_key == second.dedupe_key


def test_fingerprint_falls_back_to_url_then_triple() -> None:
    with_url = finalize_posting(
        JobPosting(
            source="fixture",
            title="Data Engineer",
            company="Globex",
            url="https://example.test/jobs/1",
        )
    )
    without_url = finalize_posting(
        JobPosting(source="fixture", title="Data Engineer", company="Globex")
    )
    assert with_url.fingerprint.startswith("v1:url:")
    assert without_url.fingerprint.startswith("v1:triple:")
    assert with_url.fingerprint != without_url.fingerprint


def test_content_hash_ignores_whitespace_noise() -> None:
    assert content_hash("a  b\n\nc") == content_hash("a b\n\nc")
    assert content_hash("a") != content_hash("b")


def test_normalize_job_posting_handles_aliases_and_markup() -> None:
    posting = normalize_job_posting(
        {
            "jobTitle": "Senior Backend Engineer",
            "companyName": "Acme Data Inc.",
            "jobUrl": "https://www.linkedin.com/jobs/view/3901234567/",
            "jobId": "3901234567",
            "location": "Remote, Argentina",
            "description": "<p>Python and <b>SQL</b></p>",
            "employmentType": "Full-time",
            "seniorityLevel": "Senior",
            "posted_text": "2 days ago",
        },
        source="linkedin",
    )
    assert posting.title == "Senior Backend Engineer"
    assert posting.company == "Acme Data Inc."
    assert posting.source_job_id == "3901234567"
    assert posting.description == "Python and SQL"
    assert posting.employment_type == "full_time"
    assert posting.seniority == "senior"
    assert posting.posted_at is not None
    assert posting.fingerprint.startswith("v1:id:linkedin:3901234567")


def test_normalize_job_posting_marks_incomplete_content() -> None:
    partial = normalize_job_posting({"title": "Platform Engineer", "company": "Northwind"}, source="fixture")
    assert partial.content_status is ContentStatus.PARTIAL
    empty = normalize_job_posting({}, source="fixture")
    assert empty.content_status is ContentStatus.EMPTY


def test_normalize_postings_drops_empty_payloads() -> None:
    postings = normalize_postings([{}, {"title": "Backend Engineer", "company": "Acme"}], source="fixture")
    assert len(postings) == 1
    assert postings[0].title == "Backend Engineer"


def test_normalize_job_posting_derives_job_id_from_url() -> None:
    posting = normalize_job_posting(
        {"title": "Data Engineer", "url": "https://www.linkedin.com/jobs/view/3999999999/data-engineer"}
    )
    assert posting.source_job_id == "3999999999"


def test_compute_helpers_are_pure() -> None:
    posting = finalize_posting(
        JobPosting(source="s", title="t", company="c", url="https://example.test/j")
    )
    assert compute_fingerprint(posting) == posting.fingerprint
    assert compute_dedupe_key(posting) == posting.dedupe_key
