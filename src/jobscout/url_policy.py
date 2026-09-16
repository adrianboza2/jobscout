"""URL and redirect policy.

The collectors only ever talk to the public LinkedIn guest endpoints, and the
optional LLM scorer only ever talks to an HTTPS endpoint. This module makes
that a *validated* property instead of an assumption:

* A configured LinkedIn URL must be ``https`` on ``linkedin.com`` or a
  ``*.linkedin.com`` locale/regional subdomain. Credentials in the URL
  (``https://user:pass@host``), other schemes (``file:``, ``ftp:``, ``data:``),
  other hosts, and non-standard ports are rejected.
* A configured LLM base URL must be ``https`` with no embedded credentials.
  Local/file schemes (``http://``, ``file:``, ``data:``, ``ftp:``) are refused;
  tests and offline runs substitute an injected transport instead of relaxing
  this rule.
* Redirects are followed only when the target host is the same host or, for
  LinkedIn, a host under the same ``linkedin.com`` parent domain. A cross-host
  redirect is refused, so a compromised or misconfigured endpoint cannot bounce
  the collector to an unrelated host.

The functions here are pure: no I/O, no network, no global state. The transport
in :mod:`jobscout.collectors.base` applies the redirect rule, and
:class:`jobscout.config.Settings` applies the configured-URL rules.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: Parent domain that owns every LinkedIn host we accept.
LINKEDIN_PARENT_DOMAIN = "linkedin.com"

#: Only the default HTTPS port is accepted for LinkedIn endpoints.
_HTTPS_PORT = 443

_ALLOWED_HTTP_SCHEMES = frozenset({"http", "https"})


class UrlPolicyError(ValueError):
    """Raised when a configured URL violates the collection policy."""


def _parse(url: str, *, name: str) -> tuple[str, str, str, int | None]:
    """Return ``(scheme, host, netloc, port)`` for ``url`` or raise."""
    if url is None:
        raise UrlPolicyError(f"{name} must be a non-empty URL")
    text = str(url).strip()
    if not text:
        raise UrlPolicyError(f"{name} must be a non-empty URL")
    try:
        parts = urlsplit(text)
        port = parts.port  # raises ValueError for a malformed port
    except ValueError as exc:
        raise UrlPolicyError(f"{name} is not a valid URL: {exc}") from exc
    host = (parts.hostname or "").strip().lower()
    return parts.scheme.lower(), host, parts.netloc, port


def _reject_embedded_credentials(netloc: str, *, name: str) -> None:
    # ``urlsplit`` extracts userinfo, but an empty user (`https://@host`) leaves
    # it empty, so check the raw netloc for the separator instead.
    if "@" in netloc:
        raise UrlPolicyError(
            f"{name} must not embed credentials (user:password@host)"
        )


def is_linkedin_host(host: str | None) -> bool:
    """Return whether ``host`` is ``linkedin.com`` or a ``*.linkedin.com`` host."""
    if not host:
        return False
    candidate = host.strip().lower().rstrip(".")
    return candidate == LINKEDIN_PARENT_DOMAIN or candidate.endswith(
        f".{LINKEDIN_PARENT_DOMAIN}"
    )


def validate_linkedin_url(url: str, *, name: str = "url") -> str:
    """Validate a configured LinkedIn endpoint and return it unchanged.

    Rejects non-HTTPS schemes, other hosts, embedded credentials, and any port
    other than the default HTTPS port.
    """
    scheme, host, netloc, port = _parse(url, name=name)
    if scheme != "https":
        raise UrlPolicyError(
            f"{name} must use https, got {scheme or 'no'} scheme: {url!r}"
        )
    _reject_embedded_credentials(netloc, name=name)
    if not host:
        raise UrlPolicyError(f"{name} must include a hostname: {url!r}")
    if not is_linkedin_host(host):
        raise UrlPolicyError(
            f"{name} must point at linkedin.com or a *.linkedin.com host, got {host!r}"
        )
    if port not in (None, _HTTPS_PORT):
        raise UrlPolicyError(
            f"{name} must use the default HTTPS port, got port {port}"
        )
    return url


def validate_llm_base_url(url: str, *, name: str = "llm_base_url") -> str:
    """Validate an optional LLM base URL and return it unchanged.

    The scorer posts an API key to this endpoint, so it must be HTTPS with no
    embedded credentials. ``http://`` (including ``http://localhost``), ``file:``,
    ``data:``, and ``ftp:`` are refused; offline tests inject a transport rather
    than lowering this requirement.
    """
    scheme, host, netloc, _port = _parse(url, name=name)
    if scheme != "https":
        raise UrlPolicyError(
            f"{name} must use https, got {scheme or 'no'} scheme: {url!r}"
        )
    _reject_embedded_credentials(netloc, name=name)
    if not host:
        raise UrlPolicyError(f"{name} must include a hostname: {url!r}")
    return url


def is_http_scheme(url: str) -> bool:
    """Return whether ``url`` uses an ``http``/``https`` scheme."""
    try:
        scheme = urlsplit(str(url)).scheme.lower()
    except ValueError:
        return False
    return scheme in _ALLOWED_HTTP_SCHEMES


def redirect_allowed(source_host: str | None, target_host: str | None) -> bool:
    """Return whether a redirect from ``source_host`` to ``target_host`` is safe.

    Same-host redirects are always allowed (LinkedIn serves ``www`` and locale
    hosts interchangeably). Cross-host redirects are allowed only when both
    hosts belong to the same ``linkedin.com`` parent domain, which preserves the
    real ``www.linkedin.com`` -> ``es.linkedin.com`` behaviour without opening a
    path to an unrelated host.
    """
    source = (source_host or "").strip().lower().rstrip(".")
    target = (target_host or "").strip().lower().rstrip(".")
    if not source or not target:
        return False
    if source == target:
        return True
    if is_linkedin_host(source) and is_linkedin_host(target):
        return True
    return False


__all__ = [
    "LINKEDIN_PARENT_DOMAIN",
    "UrlPolicyError",
    "is_http_scheme",
    "is_linkedin_host",
    "redirect_allowed",
    "validate_linkedin_url",
    "validate_llm_base_url",
]
