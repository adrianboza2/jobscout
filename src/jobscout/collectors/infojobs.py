"""InfoJobs adapter — intentionally blocked.

InfoJobs is **not implemented** in this work unit, and no live path is wired.

Why the adapter stays blocked:

* The only candidate-side URL previously proposed,
  ``https://www.infojobs.net/candidate/my-infojobs.xhtml``, is a **private,
  authenticated page**. It exposes the signed-in candidate's own area, so
  fetching it would require a login session or reused credentials. That is
  exactly what this project's collection policy forbids (no cookies, no private
  sessions, no credential reuse).
* InfoJobs' public search is protected by an application API that requires an
  authorized developer/app registration and explicit credentials. Until that
  authorization exists, any scraping of search results would be an unauthorized
  use of a private/unsupported endpoint.

Therefore this module exposes no fetch, no parser, and no CLI command. The only
behavior is a hard, fail-closed refusal, so a future caller cannot accidentally
turn it on without replacing this file.

Adding InfoJobs later requires **all** of the following, in order:

1. An official InfoJobs API authorization for this project.
2. Explicit, app-scoped credentials supplied by the operator (never reused
   account cookies).
3. A reviewed adapter that uses only the authorized API, keeps the same
   normalized :class:`jobscout.models.JobPosting` contract, dedupe rules, and
   fail-closed statuses, and is covered by offline tests with fixtures.

Until then the decision is a deliberate product/authorization blocker, not an
unsolved implementation detail.
"""

from __future__ import annotations

from typing import Any

from .base import CollectorDisabledError

PROVIDER = "infojobs"

#: The private candidate URL that must never be fetched by this project.
BLOCKED_PRIVATE_URL = "https://www.infojobs.net/candidate/my-infojobs.xhtml"

BLOCKED_REASON = (
    "InfoJobs collection is intentionally blocked: the only known candidate URL "
    f"({BLOCKED_PRIVATE_URL}) is private and requires an authenticated session. "
    "InfoJobs may only be added after an official API authorization and explicit "
    "app credentials exist."
)


def search_infojobs(*_args: Any, **_kwargs: Any) -> None:
    """Always refuse. Kept as a named entry point so the block is explicit."""
    raise CollectorDisabledError(BLOCKED_REASON)


__all__ = [
    "BLOCKED_PRIVATE_URL",
    "BLOCKED_REASON",
    "PROVIDER",
    "search_infojobs",
]
