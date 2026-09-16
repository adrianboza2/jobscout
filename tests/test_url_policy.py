"""URL, redirect, and endpoint-policy tests.

The collector may only ever talk to HTTPS LinkedIn hosts, and the optional LLM
scorer may only ever talk to an HTTPS endpoint. These tests pin that policy
directly (pure functions) and through :class:`jobscout.config.Settings`, so no
CLI flag, environment variable, or library caller can bypass it.
"""

from __future__ import annotations

import pytest

from jobscout.config import ConfigError, Settings
from jobscout.url_policy import (
    UrlPolicyError,
    is_http_scheme,
    is_linkedin_host,
    redirect_allowed,
    validate_linkedin_url,
    validate_llm_base_url,
)

# ---------------------------------------------------------------------------
# LinkedIn host predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    (
        "www.linkedin.com",
        "linkedin.com",
        "es.linkedin.com",
        "uk.linkedin.com",
        "jobs.linkedin.com",
        "WWW.LINKEDIN.COM",
        "www.linkedin.com.",
    ),
)
def test_linkedin_hosts_are_recognized(host: str) -> None:
    assert is_linkedin_host(host) is True


@pytest.mark.parametrize(
    "host",
    (
        "evil.test",
        "notlinkedin.com",
        "evil-linkedin.com",
        "linkedin.com.evil.test",
        "linkedin.com.attacker.io",
        "www.linkedin.com.evil.test",
        "xlinkedin.com",
        "",
        None,
    ),
)
def test_lookalike_hosts_are_rejected(host) -> None:
    assert is_linkedin_host(host) is False


# ---------------------------------------------------------------------------
# LinkedIn URL validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    (
        "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
        "https://es.linkedin.com/jobs/view/1234567890",
        "https://uk.linkedin.com/jobs-guest/jobs/api/jobPosting/1234567890",
        "https://linkedin.com/jobs-guest/jobs/api/jobPosting/1234567890",
        "https://www.linkedin.com:443/jobs/view/123",
    ),
)
def test_valid_linkedin_urls_are_accepted(url: str) -> None:
    assert validate_linkedin_url(url) == url


@pytest.mark.parametrize(
    "url",
    (
        # The prior `file://` probe: local file disclosure.
        "file:///etc/passwd",
        "file:///C:/Windows/win.ini",
        "ftp://www.linkedin.com/x",
        "data:text/html,<h1>x</h1>",
        # Plain HTTP and other schemes.
        "http://www.linkedin.com/jobs",
        # Unrelated or lookalike hosts.
        "https://evil.test/jobs",
        "https://notlinkedin.com/jobs",
        "https://evil-linkedin.com/jobs",
        "https://linkedin.com.evil.test/jobs",
        # Credentials embedded in the URL.
        "https://user:pass@www.linkedin.com/jobs",
        "https://@www.linkedin.com/jobs",
        # Non-default port.
        "https://www.linkedin.com:8443/jobs",
        # No host at all.
        "https:///jobs",
        "not a url at all",
    ),
)
def test_unsafe_linkedin_urls_are_rejected(url: str) -> None:
    with pytest.raises(UrlPolicyError):
        validate_linkedin_url(url)


def test_linkedin_url_error_message_names_the_problem() -> None:
    with pytest.raises(UrlPolicyError) as excinfo:
        validate_linkedin_url("file:///etc/passwd", name="linkedin_search_url")
    message = str(excinfo.value)
    assert "linkedin_search_url" in message
    assert "https" in message


# ---------------------------------------------------------------------------
# LLM base-URL validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    (
        "https://api.example.com/v1",
        "https://llm.internal:8443/v1",
        "https://x.test/v1/chat/completions",
    ),
)
def test_valid_llm_base_urls_are_accepted(url: str) -> None:
    assert validate_llm_base_url(url) == url


@pytest.mark.parametrize(
    "url",
    (
        "http://localhost:11434/v1",
        "http://api.example.com/v1",
        "file:///etc/passwd",
        "data:text/plain,hello",
        "ftp://api.example.com/v1",
        "https://user:key@api.example.com/v1",
        "https:///v1",
    ),
)
def test_unsafe_llm_base_urls_are_rejected(url: str) -> None:
    with pytest.raises(UrlPolicyError):
        validate_llm_base_url(url)


# ---------------------------------------------------------------------------
# Redirect policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,target",
    (
        ("www.linkedin.com", "www.linkedin.com"),
        # Real LinkedIn behaviour: www -> locale subdomain.
        ("www.linkedin.com", "es.linkedin.com"),
        ("es.linkedin.com", "www.linkedin.com"),
        ("uk.linkedin.com", "jobs.linkedin.com"),
        ("api.example.com", "api.example.com"),
    ),
)
def test_allowed_redirects(source: str, target: str) -> None:
    assert redirect_allowed(source, target) is True


@pytest.mark.parametrize(
    "source,target",
    (
        # Cross-host redirect to an unrelated host.
        ("www.linkedin.com", "evil.test"),
        ("www.linkedin.com", "linkedin.com.evil.test"),
        ("api.example.com", "evil.test"),
        ("api.example.com", "other.example.com"),
        # Lookalike host is not "the same LinkedIn parent domain".
        ("www.linkedin.com", "evil-linkedin.com"),
        ("evil-linkedin.com", "www.linkedin.com"),
        ("", "www.linkedin.com"),
        ("www.linkedin.com", ""),
    ),
)
def test_rejected_redirects(source: str, target: str) -> None:
    assert redirect_allowed(source, target) is False


def test_is_http_scheme() -> None:
    assert is_http_scheme("https://www.linkedin.com/x") is True
    assert is_http_scheme("http://localhost/x") is True
    assert is_http_scheme("file:///etc/passwd") is False
    assert is_http_scheme("data:text/plain,x") is False


# ---------------------------------------------------------------------------
# Settings integration
# ---------------------------------------------------------------------------


def test_default_settings_pass_url_policy() -> None:
    settings = Settings.from_env(env={}, dotenv_path=None)
    assert settings.linkedin_search_url.startswith("https://www.linkedin.com/")


@pytest.mark.parametrize(
    "env",
    (
        {"JOBSCOUT_LINKEDIN_SEARCH_URL": "file:///etc/passwd"},
        {"JOBSCOUT_LINKEDIN_SEARCH_URL": "http://www.linkedin.com/jobs"},
        {"JOBSCOUT_LINKEDIN_SEARCH_URL": "https://evil.test/jobs"},
        {"JOBSCOUT_LINKEDIN_SEARCH_URL": "https://user:pw@www.linkedin.com/jobs"},
        {"JOBSCOUT_LINKEDIN_DETAIL_URL": "file:///etc/passwd"},
        {"JOBSCOUT_LINKEDIN_DETAIL_URL": "ftp://www.linkedin.com/x"},
        {"JOBSCOUT_LINKEDIN_DETAIL_URL": "https://evil-linkedin.com/x"},
    ),
)
def test_settings_reject_unsafe_linkedin_urls(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, dotenv_path=None)


@pytest.mark.parametrize(
    "env",
    (
        {"JOBSCOUT_LLM_BASE_URL": "http://localhost:11434/v1"},
        {"JOBSCOUT_LLM_BASE_URL": "file:///etc/passwd"},
        {"JOBSCOUT_LLM_BASE_URL": "data:text/plain,hello"},
    ),
)
def test_settings_reject_unsafe_llm_urls(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, dotenv_path=None)


def test_with_overrides_cannot_bypass_url_policy() -> None:
    settings = Settings.from_env(env={}, dotenv_path=None)
    with pytest.raises(ConfigError):
        settings.with_overrides(linkedin_search_url="file:///etc/passwd")
    with pytest.raises(ConfigError):
        settings.with_overrides(linkedin_detail_url="https://evil.test/x")
    with pytest.raises(ConfigError):
        settings.with_overrides(llm_base_url="http://localhost:1234/v1")


def test_locale_linkedin_urls_are_accepted_by_settings() -> None:
    settings = Settings.from_env(
        env={
            "JOBSCOUT_LINKEDIN_SEARCH_URL": "https://es.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
            "JOBSCOUT_LINKEDIN_DETAIL_URL": "https://uk.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
            "JOBSCOUT_LLM_BASE_URL": "https://api.example.com/v1",
        },
        dotenv_path=None,
    )
    assert settings.linkedin_search_url.startswith("https://es.linkedin.com/")
    assert settings.llm_configured is False  # no API key configured
