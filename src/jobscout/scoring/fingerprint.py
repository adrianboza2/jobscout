"""Effective-input fingerprint for the scoring pipeline.

Every scorer must persist the same shape of provenance: a hash of the exact
inputs that determined the evaluation. This module is the single place that
defines what "the inputs" are, so the heuristic, the HTTP LLM scorer, the
OpenCode CLI scorer, and the input-aware selection query cannot drift apart.

Effective inputs
----------------

* the profile fingerprint (which itself covers the normalized profile and any
  preserved unknown keys);
* the job's scoring view: identity, stored content hash, and every field a
  scorer reads (title, company, location, description, seniority, salary,
  posted date, employment type, language, content status);
* the scorer name;
* the configured model; and
* the effective prompt/policy version.

The fingerprint deliberately excludes model output: it is computed *before* a
call and must be reproducible offline, so the normal scoring path can decide
whether an offer's inputs changed without contacting any model.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .profile import Profile

#: Contract version for the fingerprint material. Bump it whenever the set of
#: effective inputs or their serialization changes, so a schema/behaviour change
#: invalidates every previously stored fingerprint instead of silently reusing
#: it. Historical rows are never rewritten; they simply stop matching.
FINGERPRINT_VERSION = "scoring-input-v2"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def job_scoring_view(job: Any) -> dict[str, str]:
    """Canonical, JSON-serializable view of everything a scorer reads.

    The view is built defensively with ``getattr`` so it accepts both a
    persisted :class:`~jobscout.models.Job` and an in-memory
    :class:`~jobscout.models.JobPosting`.
    """
    return {
        "identity": _text(getattr(job, "fingerprint", "")),
        "content_hash": _text(getattr(job, "content_hash", "")),
        "title": _text(getattr(job, "title", "")),
        "company": _text(getattr(job, "company", "")),
        "location": _text(getattr(job, "location", "")),
        "description": _text(getattr(job, "description", "")),
        "employment_type": _text(getattr(job, "employment_type", "")),
        "seniority": _text(getattr(job, "seniority", "")),
        "salary": _text(getattr(job, "salary", "")),
        "posted_at": _text(getattr(job, "posted_at", "")),
        "language": _text(getattr(job, "language", "")),
        "content_status": _text(getattr(job, "content_status", "")),
    }


def effective_input_fingerprint(
    *,
    scorer: str,
    model: str | None,
    prompt_version: str | None,
    profile: Profile,
    job: Any,
) -> str:
    """SHA-256 over the full, versioned set of effective scoring inputs."""
    material = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "scorer": _text(scorer),
        "model": _text(model),
        "prompt_version": _text(prompt_version),
        "profile": profile.fingerprint(),
        "job": job_scoring_view(job),
    }
    payload = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "FINGERPRINT_VERSION",
    "effective_input_fingerprint",
    "job_scoring_view",
]
