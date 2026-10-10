"""Deduplication policy.

Kept deliberately pure: the decision function takes lookup tables instead of a
database handle so the policy can be tested without any I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .models import DuplicateKind, JobPosting


@dataclass(frozen=True)
class DedupeDecision:
    """Outcome of a duplicate check for one incoming posting."""

    kind: DuplicateKind
    existing_job_id: int | None = None
    reason: str = ""

    @property
    def is_new(self) -> bool:
        return self.kind is DuplicateKind.NEW


NO_DUPLICATE = DedupeDecision(DuplicateKind.NEW, None, "no matching fingerprint or dedupe key")


def decide_duplicate(
    posting: JobPosting,
    fingerprints: Mapping[str, int],
    dedupe_keys: Mapping[str, int],
) -> DedupeDecision:
    """Classify a posting against existing identity maps.

    * ``fingerprints`` maps ``job.fingerprint`` -> ``job.id`` (strong identity).
    * ``dedupe_keys`` maps ``job.dedupe_key`` -> ``job.id`` (company+title+location).

    An exact fingerprint match means the same posting was already stored. A
    dedupe-key match means a different URL/id describes what is very likely the
    same role; it is recorded as a sighting, not as a new job.
    """
    if not posting.fingerprint:
        raise ValueError("posting must be finalized before deduplication")

    existing_id = fingerprints.get(posting.fingerprint)
    if existing_id is not None:
        return DedupeDecision(
            DuplicateKind.EXACT_DUPLICATE,
            existing_id,
            "identical fingerprint (same source id or canonical URL)",
        )

    probable_id = dedupe_keys.get(posting.dedupe_key)
    if probable_id is not None:
        return DedupeDecision(
            DuplicateKind.PROBABLE_DUPLICATE,
            probable_id,
            "same normalized company/title/location as an existing job",
        )

    return NO_DUPLICATE


