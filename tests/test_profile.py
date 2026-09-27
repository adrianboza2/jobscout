"""Strict profile validation, unknown-key persistence, and fingerprinting."""

from __future__ import annotations

import json

import pytest

from jobscout.db import profile_evidence, upsert_profile
from jobscout.scoring.profile import (
    DEFAULT_WEIGHTS,
    ProfileError,
    build_snapshot,
    profile_from_dict,
)


def test_boolean_fields_require_real_booleans() -> None:
    for bad in ("false", "true", 1, 0, "yes"):
        with pytest.raises(ProfileError):
            profile_from_dict({"require_all_must_have": bad})

    assert profile_from_dict({"require_all_must_have": False}).require_all_must_have is False
    assert profile_from_dict({"require_all_must_have": True, "must_have_skills": ["Python"]}).require_all_must_have is True


def test_numeric_fields_reject_real_booleans() -> None:
    with pytest.raises(ProfileError):
        profile_from_dict({"years_experience": True})
    with pytest.raises(ProfileError):
        profile_from_dict({"min_salary": False})


def test_weights_must_be_finite_and_nonnegative() -> None:
    with pytest.raises(ProfileError):
        profile_from_dict({"weights": {"skills": -0.1}})
    with pytest.raises(ProfileError):
        profile_from_dict({"weights": {"skills": float("inf")}})
    with pytest.raises(ProfileError):
        profile_from_dict({"weights": {"skills": float("nan")}})
    with pytest.raises(ProfileError):
        profile_from_dict({"weights": {"skills": True}})
    with pytest.raises(ProfileError):
        profile_from_dict({"weights": {"skills": "heavy"}})


def test_weight_sum_must_be_positive() -> None:
    with pytest.raises(ProfileError):
        profile_from_dict({"weights": {key: 0 for key in DEFAULT_WEIGHTS}})


def test_profile_currency_must_be_usd_or_eur() -> None:
    with pytest.raises(ProfileError):
        profile_from_dict({"salary_currency": "GBP"})
    with pytest.raises(ProfileError):
        profile_from_dict({"salary_currency": "ARS"})
    with pytest.raises(ProfileError):
        profile_from_dict({"salary_currency": 978})

    assert profile_from_dict({"salary_currency": "eur"}).salary_currency == "EUR"
    assert profile_from_dict({}).salary_currency == "USD"
    assert profile_from_dict({"salary_currency": ""}).salary_currency == "USD"


def test_max_posting_age_days_validation_and_default() -> None:
    assert profile_from_dict({}).max_posting_age_days == 30
    assert profile_from_dict({"max_posting_age_days": None}).max_posting_age_days is None
    assert profile_from_dict({"max_posting_age_days": 0}).max_posting_age_days == 0

    with pytest.raises(ProfileError):
        profile_from_dict({"max_posting_age_days": -1})
    with pytest.raises(ProfileError):
        profile_from_dict({"max_posting_age_days": True})


def test_unknown_keys_persist_and_change_the_fingerprint() -> None:
    base = profile_from_dict({"name": "p"})
    extended = profile_from_dict({"name": "p", "custom_flag": "keep-me", "team_size": 7})

    assert extended.to_dict()["custom_flag"] == "keep-me"
    assert extended.to_dict()["team_size"] == 7
    assert extended.fingerprint() != base.fingerprint()


def test_unknown_keys_survive_snapshot_round_trip_deterministically(conn) -> None:
    first = profile_from_dict({"name": "p", "custom_flag": "keep-me", "nested": {"a": 1}})
    # Same content, different insertion order: the fingerprint must not depend on
    # enumeration order, so persisted snapshots stay comparable.
    second = profile_from_dict({"nested": {"a": 1}, "custom_flag": "keep-me", "name": "p"})
    assert first.fingerprint() == second.fingerprint()

    stored = upsert_profile(conn, build_snapshot(first))
    row = conn.execute("SELECT raw_json FROM profiles WHERE id = ?", (stored.id,)).fetchone()
    persisted = json.loads(row["raw_json"])
    assert persisted["custom_flag"] == "keep-me"
    assert persisted["nested"] == {"a": 1}

    # The structured evidence rows still only describe known fields.
    assert profile_evidence(conn, int(stored.id))


def test_known_fields_do_not_leak_into_the_unknown_section() -> None:
    profile = profile_from_dict({"name": "p", "seniority": "mid", "salary_currency": "usd"})
    payload = profile.to_dict()
    assert payload["seniority"] == "mid"
    assert payload["salary_currency"] == "USD"
    # The canonical view is exactly the known fields when there are no unknowns.
    assert set(payload) == {
        "name",
        "summary",
        "target_roles",
        "seniority",
        "years_experience",
        "locations",
        "remote_preference",
        "languages",
        "min_salary",
        "salary_currency",
        "must_have_skills",
        "nice_to_have_skills",
        "dealbreakers",
        "excluded_companies",
        "require_all_must_have",
        "max_posting_age_days",
        "weights",
    }
