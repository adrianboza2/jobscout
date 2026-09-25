"""Configuration hardening tests.

Covers the credential-detection patterns (false positives on ordinary words,
true positives on realistic secret formats) and the absolute hard caps on
LinkedIn collection volume.
"""

from __future__ import annotations

import pytest

from jobscout.config import (
    DEFAULT_NOTIFY_DELIVERY,
    DEFAULT_NOTIFY_SCORE_THRESHOLD,
    HARD_MAX_ITEMS,
    HARD_MAX_PAGES,
    ConfigError,
    Settings,
    is_safe_to_store,
    redact_secrets,
)

ORDINARY_HYPHENATED = (
    "risk-assessment",
    "task-management",
    "desk-organization",
    "disk-usage",
    "risk-mitigation plan",
    "task-management dashboard",
    "desk-organization tips",
)


@pytest.mark.parametrize("text", ORDINARY_HYPHENATED)
def test_ordinary_hyphenated_words_are_not_credentials(text: str) -> None:
    safe, reason = is_safe_to_store(text)
    assert safe, f"{text!r} was treated as a secret: {reason}"
    assert redact_secrets(text) == text


REALISTIC_SECRETS = (
    "sk-abcdef0123456789",
    "sk-proj-abcdefghijklmnopqrstuvwx",
    "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    "sk_live_0123456789abcdef",
    "Authorization: Bearer abcdefghijklmnopqrstuvwx",
    "api_key=0123456789abcdef",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
)

# Realistic secrets for the formats added by the session-cookie, PEM, labelled,
# and provider-prefixed patterns.
EXTENDED_SECRETS = (
    # LinkedIn session cookies.
    'li_at=AQEDATq5m2xQz8kL_abcdefghijklmnopqrstuvwxyz0123456789',
    'JSESSIONID="ajax:1234567890123456789"',
    # PEM private-key headers.
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    # Labelled *_TOKEN / *_SECRET / password / OAuth credentials.
    "MY_SERVICE_TOKEN=abcdef1234567890",
    "SERVICE_SECRET: abcdef1234567890",
    "password: correct-horse-battery-staple",
    "client_secret=abcdef1234567890",
    "refresh_token=abcdef1234567890",
    "access_token: abcdef1234567890",
    # Provider-prefixed tokens.
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789",
    "nvapi-abcdefghijklmnopqrstuvwxyz0123456789",
)


@pytest.mark.parametrize("text", REALISTIC_SECRETS)
def test_realistic_secret_formats_are_detected(text: str) -> None:
    safe, _ = is_safe_to_store(text)
    assert not safe, f"{text!r} was not detected as a credential"
    assert "***REDACTED***" in redact_secrets(text)


@pytest.mark.parametrize("text", EXTENDED_SECRETS)
def test_extended_secret_formats_are_detected(text: str) -> None:
    safe, reason = is_safe_to_store(text)
    assert not safe, f"{text!r} was not detected as a credential"
    assert "***REDACTED***" in redact_secrets(text, ())
    assert reason


# JSON/object-encoded credentials: label and value are quoted, as they are in
# model payloads, API error bodies, and configuration dumps. Each entry is
# ``(payload, secret_value)`` so the test asserts the value itself never
# survives redaction.
QUOTED_SECRETS = (
    ('{"password": "correct-horse-battery-staple"}', "correct-horse-battery-staple"),
    ('{"token": "abcdef1234567890"}', "abcdef1234567890"),
    ('{"client_secret": "abcdef1234567890"}', "abcdef1234567890"),
    ('{"api_key": "0123456789abcdef"}', "0123456789abcdef"),
    ('{"access_token": "abcdef1234567890"}', "abcdef1234567890"),
    ('{"refresh_token": "abcdef1234567890"}', "abcdef1234567890"),
    (
        '{"Authorization": "Bearer abcdefghijklmnopqrstuvwx"}',
        "abcdefghijklmnopqrstuvwx",
    ),
    (
        '{"li_at": "AQEDATq5m2xQz8kL_abcdefghijklmnopqrstuvwxyz0123456789"}',
        "AQEDATq5m2xQz8kL_abcdefghijklmnopqrstuvwxyz0123456789",
    ),
    ("{'password': 'abcdef1234567890'}", "abcdef1234567890"),
    ('{"password" : "abcdef1234567890"}', "abcdef1234567890"),
    # Escaped forms: a JSON document nested inside a JSON string, as model and
    # API transport payloads carry it. Detection must survive the escaping.
    (r'{\"password\": \"abcdefghijklmnop\"}', "abcdefghijklmnop"),
    (r'{\\\"client_secret\\\": \\\"abcdefghijklmnop\\\"}', "abcdefghijklmnop"),
)


@pytest.mark.parametrize("text,value", QUOTED_SECRETS)
def test_json_quoted_secret_formats_are_detected(text: str, value: str) -> None:
    safe, reason = is_safe_to_store(text)
    assert not safe, f"{text!r} was not detected as a credential"
    assert reason
    redacted = redact_secrets(text)
    assert value not in redacted
    assert "***REDACTED***" in redacted


@pytest.mark.parametrize(
    "label,value",
    (
        ("password", "correct-horse-battery-staple"),
        ("token", "abcdef1234567890"),
        ("client_secret", "abcdef1234567890"),
        ("api_key", "0123456789abcdef"),
    ),
)
def test_quoted_and_unquoted_forms_are_detected_alike(label: str, value: str) -> None:
    """Quoting the label and value must not change detection behaviour."""
    payloads = (f"{label}={value}", f"{label}: {value}", f'{{"{label}": "{value}"}}')
    for payload in payloads:
        safe, reason = is_safe_to_store(payload)
        assert not safe, f"{payload!r} was not detected as a credential"
        assert "***REDACTED***" in redact_secrets(payload)


def test_json_quoted_credential_is_refused_by_the_persistence_guard() -> None:
    """A model/exporter payload with a quoted label is never stored verbatim."""
    payload = '{"model": "x", "settings": {"password": "abcdefghijklmnop"}}'
    safe, reason = is_safe_to_store(payload, max_bytes=4096)
    assert not safe
    assert "credential" in reason


# Benign text that must not be mistaken for a credential: short placeholder
# values, prose without an assignment, and ordinary technical vocabulary.
BENIGN_TEXTS = (
    "token: missing",
    "token: not set",
    "password: required",
    "password: changeme",
    "secret: none",
    "secret: hidden",
    "client secret rotation policy",
    "refresh token lifetime is 30 days",
    "access token expired, please log in again",
    "the secret to success is practice",
    "private key management",
    "li_at: not set",
    "JSESSIONID=abc",
    "session cookie handling",
    "Our token budget is small.",
    "The password policy requires 12 characters.",
    # Quoted JSON keys with short/placeholder values stay benign, exactly like
    # their unquoted counterparts.
    '{"token": "missing"}',
    '{"password": "required"}',
    '{"secret": "none"}',
    '{"client_secret": "changeme"}',
    '{"api_key": "not set"}',
    '{"li_at": "not set"}',
    '{"note": "token", "value": "abc"}',
)


@pytest.mark.parametrize("text", BENIGN_TEXTS)
def test_benign_text_is_not_treated_as_a_credential(text: str) -> None:
    safe, reason = is_safe_to_store(text)
    assert safe, f"{text!r} was wrongly treated as a secret: {reason}"
    assert redact_secrets(text) == text


def test_configured_secret_redaction_still_takes_precedence() -> None:
    configured = "zzq7-local-configured-credential"
    text = f"note: {configured} and MY_SERVICE_TOKEN=abcdef1234567890"
    redacted = redact_secrets(text, (configured,))
    assert configured not in redacted
    assert "abcdef1234567890" not in redacted
    assert redacted.count("***REDACTED***") == 2

    safe, reason = is_safe_to_store(text, secrets=(configured,))
    assert not safe
    assert "configured secret" in reason


@pytest.mark.parametrize(
    "text",
    ("API key management", "authorization is required", "bearer of good news"),
)
def test_prose_labels_are_not_treated_as_credentials(text: str) -> None:
    safe, reason = is_safe_to_store(text)
    assert safe, f"{text!r} was treated as a secret: {reason}"


@pytest.mark.parametrize(
    "env",
    [
        {"JOBSCOUT_MAX_PAGES": str(HARD_MAX_PAGES + 1)},
        {"JOBSCOUT_MAX_ITEMS": str(HARD_MAX_ITEMS + 1)},
        {"JOBSCOUT_MAX_PAGES": "0"},
        {"JOBSCOUT_MAX_ITEMS": "-5"},
        {"JOBSCOUT_DETAIL_MAX_ITEMS": str(HARD_MAX_ITEMS + 1)},
        {"JOBSCOUT_DETAIL_MAX_ITEMS": "0"},
    ],
)
def test_settings_rejects_out_of_range_bounds(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, dotenv_path=None)


def test_settings_accepts_the_hard_caps() -> None:
    settings = Settings.from_env(
        env={
            "JOBSCOUT_MAX_PAGES": str(HARD_MAX_PAGES),
            "JOBSCOUT_MAX_ITEMS": str(HARD_MAX_ITEMS),
        },
        dotenv_path=None,
    )
    assert settings.max_pages == HARD_MAX_PAGES
    assert settings.max_items == HARD_MAX_ITEMS


def test_getmanfred_byte_limit_is_provider_specific_and_configurable() -> None:
    settings = Settings.from_env(
        env={"JOBSCOUT_GETMANFRED_MAX_BYTES": "524288"}, dotenv_path=None
    )
    assert settings.raw_max_bytes == 262_144
    assert settings.getmanfred_max_bytes == 524_288
    with pytest.raises(ConfigError):
        Settings.from_env(env={"JOBSCOUT_GETMANFRED_MAX_BYTES": "1048577"}, dotenv_path=None)


def test_with_overrides_cannot_bypass_the_hard_caps() -> None:
    settings = Settings.from_env(env={}, dotenv_path=None)
    with pytest.raises(ConfigError):
        settings.with_overrides(max_pages=HARD_MAX_PAGES + 1)
    with pytest.raises(ConfigError):
        settings.with_overrides(max_items=HARD_MAX_ITEMS + 1)


# ---------------------------------------------------------------------------
# Local notifications (opt-in, disabled by default)
# ---------------------------------------------------------------------------


def test_notify_settings_default_to_disabled() -> None:
    settings = Settings.from_env(env={}, dotenv_path=None)
    assert settings.notify_enabled is False
    assert settings.notify_score_threshold == DEFAULT_NOTIFY_SCORE_THRESHOLD
    assert settings.notify_preference_outcome is None
    assert settings.notify_delivery == DEFAULT_NOTIFY_DELIVERY


def test_notify_settings_parse_environment() -> None:
    settings = Settings.from_env(
        env={
            "JOBSCOUT_NOTIFY_ENABLED": "yes",
            "JOBSCOUT_NOTIFY_SCORE_THRESHOLD": "55.5",
            "JOBSCOUT_NOTIFY_PREFERENCE_OUTCOME": "flag",
            "JOBSCOUT_NOTIFY_DELIVERY": "system",
        },
        dotenv_path=None,
    )
    assert settings.notify_enabled is True
    assert settings.notify_score_threshold == 55.5
    assert settings.notify_preference_outcome == "flag"
    assert settings.notify_delivery == "system"


def test_notify_preference_outcome_empty_means_no_filter() -> None:
    settings = Settings.from_env(
        env={"JOBSCOUT_NOTIFY_PREFERENCE_OUTCOME": ""}, dotenv_path=None
    )
    assert settings.notify_preference_outcome is None


@pytest.mark.parametrize(
    "env",
    [
        {"JOBSCOUT_NOTIFY_SCORE_THRESHOLD": "-1"},
        {"JOBSCOUT_NOTIFY_SCORE_THRESHOLD": "100.1"},
        {"JOBSCOUT_NOTIFY_SCORE_THRESHOLD": "nan"},
        {"JOBSCOUT_NOTIFY_SCORE_THRESHOLD": "inf"},
        {"JOBSCOUT_NOTIFY_SCORE_THRESHOLD": "not-a-number"},
        {"JOBSCOUT_NOTIFY_PREFERENCE_OUTCOME": "maybe"},
        {"JOBSCOUT_NOTIFY_DELIVERY": "email"},
        {"JOBSCOUT_NOTIFY_ENABLED": "sometimes"},
    ],
)
def test_notify_settings_reject_invalid_values(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env=env, dotenv_path=None)


def test_notify_settings_accept_the_full_threshold_range() -> None:
    for value in ("0", "0.0", "100", "100.0", "80"):
        settings = Settings.from_env(
            env={"JOBSCOUT_NOTIFY_SCORE_THRESHOLD": value}, dotenv_path=None
        )
        assert settings.notify_score_threshold == float(value)


def test_with_overrides_cannot_bypass_notify_validation() -> None:
    settings = Settings.from_env(env={}, dotenv_path=None)
    with pytest.raises(ConfigError):
        settings.with_overrides(notify_score_threshold=101.0)
    with pytest.raises(ConfigError):
        settings.with_overrides(notify_score_threshold=float("nan"))
    with pytest.raises(ConfigError):
        settings.with_overrides(notify_preference_outcome="maybe")
    with pytest.raises(ConfigError):
        settings.with_overrides(notify_delivery="email")
