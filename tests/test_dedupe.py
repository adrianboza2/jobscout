"""Deduplication policy tests (pure functions, no database)."""

from __future__ import annotations

import pytest

from jobscout.dedupe import decide_duplicate
from jobscout.models import DuplicateKind, JobPosting
from jobscout.normalize import finalize_posting


def make_posting(**overrides) -> JobPosting:
    payload = {
        "source": "linkedin",
        "title": "Senior Backend Engineer",
        "company": "Acme Data Inc.",
        "location": "Remote, Argentina",
        "url": "https://www.linkedin.com/jobs/view/3901234567/",
        "source_job_id": "3901234567",
    }
    payload.update(overrides)
    return finalize_posting(JobPosting(**payload))


def test_new_posting_when_no_identity_matches() -> None:
    decision = decide_duplicate(make_posting(), {}, {})
    assert decision.kind is DuplicateKind.NEW
    assert decision.is_new
    assert decision.existing_job_id is None


def test_exact_duplicate_by_fingerprint() -> None:
    posting = make_posting()
    decision = decide_duplicate(posting, {posting.fingerprint: 7}, {})
    assert decision.kind is DuplicateKind.EXACT_DUPLICATE
    assert decision.existing_job_id == 7


def test_probable_duplicate_by_dedupe_key() -> None:
    existing = make_posting(source_job_id="111", url="https://www.linkedin.com/jobs/view/111/")
    incoming = make_posting(
        source_job_id="222",
        url="https://www.linkedin.com/jobs/view/222/senior-backend-engineer",
    )
    decision = decide_duplicate(
        incoming, {existing.fingerprint: 3}, {incoming.dedupe_key: 3}
    )
    assert decision.kind is DuplicateKind.PROBABLE_DUPLICATE
    assert decision.existing_job_id == 3
    assert "company/title/location" in decision.reason


def test_exact_match_wins_over_probable() -> None:
    posting = make_posting()
    decision = decide_duplicate(posting, {posting.fingerprint: 1}, {posting.dedupe_key: 2})
    assert decision.kind is DuplicateKind.EXACT_DUPLICATE
    assert decision.existing_job_id == 1


def test_unfinalized_posting_is_rejected() -> None:
    with pytest.raises(ValueError):
        decide_duplicate(JobPosting(source="s", title="t", company="c"), {}, {})
