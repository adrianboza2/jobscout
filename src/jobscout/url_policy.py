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

#: Parent domains for the other public providers. A configured endpoint must be
#: ``https`` on one of these hosts (or a subdomain), with no embedded
#: credentials and the default HTTPS port, exactly like LinkedIn.
GETMANFRED_PARENT_DOMAIN = "getmanfred.com"
TECNOEMPLEO_PARENT_DOMAIN = "tecnoempleo.com"

#: Every parent domain the collectors are allowed to talk to. Redirects may only
#: cross hosts *within* one of these parent domains, never between them.
PROVIDER_PARENT_DOMAINS: tuple[str, ...] = (
    LINKEDIN_PARENT_DOMAIN,
    GETMANFRED_PARENT_DOMAIN,
    TECNOEMPLEO_PARENT_DOMAIN,
)

#: Only the default HTTPS port is accepted for provider endpoints.
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


def _is_host_under(host: str | None, parent: str) -> bool:
    if not host:
        return False
    candidate = host.strip().lower().rstrip(".")
    return candidate == parent or candidate.endswith(f".{parent}")


def is_getmanfred_host(host: str | None) -> bool:
    """Return whether ``host`` is ``getmanfred.com`` or a ``*.getmanfred.com`` host."""
    return _is_host_under(host, GETMANFRED_PARENT_DOMAIN)


def is_tecnoempleo_host(host: str | None) -> bool:
    """Return whether ``host`` is ``tecnoempleo.com`` or a ``*.tecnoempleo.com`` host."""
    return _is_host_under(host, TECNOEMPLEO_PARENT_DOMAIN)


def _validate_provider_url(
    url: str,
    *,
    name: str,
    host_predicate,
    allowed_hosts: str,
) -> str:
    """Shared https/host/credentials/port validation for a provider endpoint."""
    scheme, host, netloc, port = _parse(url, name=name)
    if scheme != "https":
        raise UrlPolicyError(
            f"{name} must use https, got {scheme or 'no'} scheme: {url!r}"
        )
    _reject_embedded_credentials(netloc, name=name)
    if not host:
        raise UrlPolicyError(f"{name} must include a hostname: {url!r}")
    if not host_predicate(host):
        raise UrlPolicyError(
            f"{name} must point at {allowed_hosts}, got {host!r}"
        )
    if port not in (None, _HTTPS_PORT):
        raise UrlPolicyError(
            f"{name} must use the default HTTPS port, got port {port}"
        )
    return url


def validate_linkedin_url(url: str, *, name: str = "url") -> str:
    """Validate a configured LinkedIn endpoint and return it unchanged.

    Rejects non-HTTPS schemes, other hosts, embedded credentials, and any port
    other than the default HTTPS port.
    """
    return _validate_provider_url(
        url,
        name=name,
        host_predicate=is_linkedin_host,
        allowed_hosts="linkedin.com or a *.linkedin.com host",
    )


def validate_getmanfred_url(url: str, *, name: str = "url") -> str:
    """Validate a configured GetManfred endpoint (host allow-list)."""
    return _validate_provider_url(
        url,
        name=name,
        host_predicate=is_getmanfred_host,
        allowed_hosts="getmanfred.com or a *.getmanfred.com host",
    )


def validate_tecnoempleo_url(url: str, *, name: str = "url") -> str:
    """Validate a configured Tecnoempleo endpoint (host allow-list)."""
    return _validate_provider_url(
        url,
        name=name,
        host_predicate=is_tecnoempleo_host,
        allowed_hosts="tecnoempleo.com or a *.tecnoempleo.com host",
    )


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


def is_safe_https_url(url: str) -> bool:
    """Return whether ``url`` is a well-shaped HTTPS URL.

    Requires the ``https`` scheme, a host, no embedded credentials, and either
    no explicit port or the default HTTPS port. Used to vet discovery targets
    (for example sitemap children) whose only safe shape is the same one the
    configured provider endpoints require.
    """
    try:
        scheme, host, netloc, port = _parse(url, name="url")
    except UrlPolicyError:
        return False
    if scheme != "https" or not host:
        return False
    if "@" in netloc:
        return False
    return port in (None, _HTTPS_PORT)


def redirect_target_violation(source_url: str, target_url: str) -> str | None:
    """Return why following a redirect to ``target_url`` is unsafe, else ``None``.

    The check is strict at *every* hop: the target must keep ``https`` (a
    same-host downgrade to ``http`` is refused), must not embed credentials, must
    use the default HTTPS port, must have a host, and its host must satisfy
    :func:`redirect_allowed` relative to the source. This is the single policy
    used by the transport redirect handler and by the final-URL sanitizer, so a
    value that reaches storage can never be one the transport would refuse.
    """
    try:
        scheme, host, netloc, port = _parse(target_url, name="redirect target")
    except UrlPolicyError as exc:
        return str(exc)
    if scheme != "https":
        return f"non-HTTPS scheme {scheme or 'none'!r}"
    if "@" in netloc:
        return "target embeds credentials"
    if not host:
        return "target has no hostname"
    if port not in (None, _HTTPS_PORT):
        return f"non-default port {port}"
    try:
        source_host = urlsplit(str(source_url)).hostname or ""
    except ValueError:
        source_host = ""
    if not redirect_allowed(source_host, host):
        return f"cross-host redirect from {source_host!r} to {host!r}"
    return None


def _normalized_https_port(port: int | None) -> int:
    """Return ``443`` for both an omitted port and an explicit ``:443``."""
    return _HTTPS_PORT if port is None else port


def response_url_violation(request_url: str, response_url: str) -> str | None:
    """Return why a response URL is unsafe relative to its request, else ``None``.

    Applied to the URL a transport's opener actually resolved, which must hold
    even when no redirect handler ran (for example an injected opener). It
    requires HTTPS, no embedded credentials, a host allowed relative to the
    request, and the *same* HTTPS port the request used. A provider request on
    the default port therefore never accepts a downgraded or non-default-port
    response, while a caller deliberately configured on a custom HTTPS port (an
    LLM endpoint) is not silently moved off it.
    """
    try:
        _req_scheme, req_host, _req_netloc, req_port = _parse(
            request_url, name="request URL"
        )
    except UrlPolicyError as exc:
        return str(exc)
    try:
        scheme, host, netloc, port = _parse(response_url, name="response URL")
    except UrlPolicyError as exc:
        return str(exc)
    if scheme != "https":
        return f"non-HTTPS scheme {scheme or 'none'!r}"
    if "@" in netloc:
        return "response URL embeds credentials"
    if not host:
        return "response URL has no hostname"
    if not redirect_allowed(req_host, host):
        return f"cross-host response from {req_host!r} to {host!r}"
    if _normalized_https_port(port) != _normalized_https_port(req_port):
        return (
            "response changed the HTTPS port from "
            f"{_normalized_https_port(req_port)} to {_normalized_https_port(port)}"
        )
    return None


def is_safe_redirect_target(source_url: str, target_url: str) -> bool:
    """Return whether following a redirect from source to target is allowed."""
    return redirect_target_violation(source_url, target_url) is None


def sanitize_final_url(requested_url: str | None, response_url: str | None) -> str | None:
    """Return the safe final URL of an allowed redirect, else ``None``.

    The single strict policy shared by every detail adapter and the transport.
    A value is kept only when it differs from the requested URL and satisfies
    :func:`redirect_target_violation` (HTTPS only, no embedded credentials,
    default HTTPS port, host allowed relative to the request). ``None`` is the
    honest "no redirect was observed" value, so a refused target never reaches
    ``job_detail_fetches.final_url``.
    """
    requested = str(requested_url or "").strip()
    candidate = str(response_url or "").strip()
    if not candidate or candidate == requested:
        return None
    if redirect_target_violation(requested, candidate) is not None:
        return None
    return candidate


def redirect_allowed(source_host: str | None, target_host: str | None) -> bool:
    """Return whether a redirect from ``source_host`` to ``target_host`` is safe.

    Same-host redirects are always allowed (a provider may serve ``www`` and
    locale hosts interchangeably). Cross-host redirects are allowed only when
    both hosts belong to the same known provider parent domain, which preserves
    the real ``www.linkedin.com`` -> ``es.linkedin.com`` behaviour for LinkedIn
    and the equivalent ``www``/``locale`` pattern for GetManfred and Tecnoempleo,
    without ever opening a path to an unrelated host or across providers.
    """
    source = (source_host or "").strip().lower().rstrip(".")
    target = (target_host or "").strip().lower().rstrip(".")
    if not source or not target:
        return False
    if source == target:
        return True
    for parent in PROVIDER_PARENT_DOMAINS:
        if _is_host_under(source, parent) and _is_host_under(target, parent):
            return True
    return False


__all__ = [
    "GETMANFRED_PARENT_DOMAIN",
    "LINKEDIN_PARENT_DOMAIN",
    "PROVIDER_PARENT_DOMAINS",
    "TECNOEMPLEO_PARENT_DOMAIN",
    "UrlPolicyError",
    "is_getmanfred_host",
    "is_http_scheme",
    "is_linkedin_host",
    "is_safe_https_url",
    "is_safe_redirect_target",
    "is_tecnoempleo_host",
    "redirect_allowed",
    "redirect_target_violation",
    "response_url_violation",
    "sanitize_final_url",
    "validate_getmanfred_url",
    "validate_linkedin_url",
    "validate_llm_base_url",
    "validate_tecnoempleo_url",
]
