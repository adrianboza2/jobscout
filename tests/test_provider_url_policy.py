"""Host allow-lists and redirect policy for the new public providers."""

from __future__ import annotations

import pytest

from jobscout.config import ConfigError, Settings
from jobscout.url_policy import (
    UrlPolicyError,
    is_getmanfred_host,
    is_tecnoempleo_host,
    redirect_allowed,
    validate_getmanfred_url,
    validate_tecnoempleo_url,
)


@pytest.mark.parametrize(
    "host",
    ("www.getmanfred.com", "getmanfred.com", "es.getmanfred.com", "GETMANFRED.COM"),
)
def test_getmanfred_hosts_are_recognized(host: str) -> None:
    assert is_getmanfred_host(host) is True


@pytest.mark.parametrize(
    "host",
    ("evil.test", "notgetmanfred.com", "getmanfred.com.evil.test", "xgetmanfred.com", "", None),
)
def test_getmanfred_lookalikes_are_rejected(host) -> None:
    assert is_getmanfred_host(host) is False


@pytest.mark.parametrize(
    "host",
    ("www.tecnoempleo.com", "tecnoempleo.com", "es.tecnoempleo.com"),
)
def test_tecnoempleo_hosts_are_recognized(host: str) -> None:
    assert is_tecnoempleo_host(host) is True


@pytest.mark.parametrize(
    "host",
    ("evil.test", "nottecnoempleo.com", "tecnoempleo.com.evil.test", "", None),
)
def test_tecnoempleo_lookalikes_are_rejected(host) -> None:
    assert is_tecnoempleo_host(host) is False


@pytest.mark.parametrize(
    "url",
    (
        "https://www.getmanfred.com/sitemap-offers.xml",
        "https://getmanfred.com/sitemap.xml",
    ),
)
def test_valid_getmanfred_urls_are_accepted(url: str) -> None:
    assert validate_getmanfred_url(url) == url


@pytest.mark.parametrize(
    "url",
    (
        "http://www.getmanfred.com/sitemap.xml",
        "file:///etc/passwd",
        "https://evil.test/sitemap.xml",
        "https://user:pw@www.getmanfred.com/sitemap.xml",
        "https://www.getmanfred.com:8443/sitemap.xml",
    ),
)
def test_unsafe_getmanfred_urls_are_rejected(url: str) -> None:
    with pytest.raises(UrlPolicyError):
        validate_getmanfred_url(url)


@pytest.mark.parametrize(
    "url",
    (
        "http://www.tecnoempleo.com/ofertas-trabajo/",
        "https://evil.test/ofertas-trabajo/",
        "https://www.tecnoempleo.com:8443/ofertas-trabajo/",
    ),
)
def test_unsafe_tecnoempleo_urls_are_rejected(url: str) -> None:
    with pytest.raises(UrlPolicyError):
        validate_tecnoempleo_url(url)


def test_cross_host_redirect_inside_each_provider_is_allowed() -> None:
    assert redirect_allowed("getmanfred.com", "www.getmanfred.com") is True
    assert redirect_allowed("www.tecnoempleo.com", "es.tecnoempleo.com") is True
    assert redirect_allowed("www.linkedin.com", "es.linkedin.com") is True


def test_redirect_across_providers_or_to_unrelated_hosts_is_refused() -> None:
    assert redirect_allowed("www.getmanfred.com", "www.tecnoempleo.com") is False
    assert redirect_allowed("www.tecnoempleo.com", "evil.test") is False
    assert redirect_allowed("getmanfred.com.evil.test", "getmanfred.com") is False


def test_settings_validate_provider_endpoints() -> None:
    settings = Settings.from_env(env={}, dotenv_path=None)
    assert settings.getmanfred_sitemap_url == "https://www.getmanfred.com/sitemap-offers.xml"
    assert settings.tecnoempleo_search_url == "https://www.tecnoempleo.com/ofertas-trabajo/"

    with pytest.raises(ConfigError):
        Settings.from_env(
            env={"JOBSCOUT_GETMANFRED_SITEMAP_URL": "https://evil.test/sitemap.xml"},
            dotenv_path=None,
        )
    with pytest.raises(ConfigError):
        Settings.from_env(
            env={"JOBSCOUT_TECNOEMPLEO_SEARCH_URL": "file:///etc/passwd"},
            dotenv_path=None,
        )
    with pytest.raises(ConfigError):
        Settings.from_env(env={"JOBSCOUT_GETMANFRED_MAX_ITEMS": "9999"}, dotenv_path=None)


def test_settings_reject_non_slash_offer_prefix() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(
            env={"JOBSCOUT_GETMANFRED_OFFER_PATH_PREFIX": "ofertas/"}, dotenv_path=None
        )
