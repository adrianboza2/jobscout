"""Shared HTML-detail plumbing used by more than one collector adapter.

Only genuinely identical pieces live here: the void-element set every mini
HTML parser needs, and the redirect-sanitizing core the LinkedIn, Tecnoempleo
and GetManfred detail fetchers all run before persisting a ``final_url``.
Provider-specific policy (allowed host, https requirement) stays in each
adapter and is passed in, so a provider's edge cases are not silently unified.
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit

from ..url_policy import is_http_scheme

#: HTML void elements: a start tag for one of these never opens a container.
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def sanitize_redirect(
    requested_url: str | None,
    response_url: str | None,
    *,
    allow_host: Callable[[str], bool] | None = None,
    require_https: bool = False,
) -> str | None:
    """Return the safe final URL of an allowed redirect, else ``None``.

    ``None`` is the honest "no redirect was observed" value, so callers persist
    ``NULL`` instead of echoing the requested URL. It is also returned when the
    target is not http(s), embeds credentials, fails ``require_https``, or is
    rejected by ``allow_host``. The transport's redirect handler already enforced
    host policy; ``allow_host`` only narrows what may reach the ``final_url``
    column.
    """
    requested = str(requested_url or "").strip()
    candidate = str(response_url or "").strip()
    if not candidate or candidate == requested:
        return None
    if not is_http_scheme(candidate):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if require_https and parts.scheme.lower() != "https":
        return None
    if "@" in parts.netloc:
        return None
    if allow_host is not None and not allow_host(parts.hostname or ""):
        return None
    return candidate
