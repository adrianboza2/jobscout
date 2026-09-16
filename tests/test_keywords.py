"""Keyword catalog and ``keyword-plan`` tests.

The catalog is the bounded search surface: these tests prove it stays explicit,
stays within its hard bound, includes the security coverage that was missing, and
is reachable from the existing CLI flow without changing ``--keywords``.
"""

from __future__ import annotations

import json

import pytest

from conftest import no_network

from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.config import ConfigError, Settings
from jobscout.keywords import (
    GROUP_BASELINE,
    GROUP_SECURITY,
    HARD_MAX_QUERIES_PER_FAMILY,
    KEYWORD_FAMILIES,
    UnknownKeywordFamily,
    family_ids,
    get_family,
    planned_queries,
    resolve_families,
    search_command,
)

#: Queries the security coverage work explicitly promised.
REQUIRED_SECURITY_QUERIES = (
    "Cloud Security Engineer",
    "Junior Cloud Security Engineer",
    "DevSecOps Engineer",
    "Junior DevSecOps Engineer",
    "Azure Cloud Security",
    "Azure DevSecOps",
    "IAM Engineer",
    "Security Infrastructure Engineer",
)


def _all_queries() -> list[str]:
    return [query for family in KEYWORD_FAMILIES for query in family.queries]


# ---------------------------------------------------------------------------
# Catalog invariants
# ---------------------------------------------------------------------------


def test_catalog_is_bounded_unique_and_non_empty() -> None:
    assert KEYWORD_FAMILIES, "the catalog must not be empty"
    assert len(family_ids()) == len(set(family_ids())), "family ids must be unique"
    queries = _all_queries()
    assert len(queries) == len(set(queries)), "queries must not repeat across families"
    for family in KEYWORD_FAMILIES:
        assert family.id and family.label and family.focus
        assert family.group in {GROUP_BASELINE, GROUP_SECURITY}
        assert 0 < len(family.queries) <= HARD_MAX_QUERIES_PER_FAMILY
        for query in family.queries:
            assert query.strip() == query and query


def test_catalog_covers_the_requested_security_queries() -> None:
    queries = set(_all_queries())
    missing = [query for query in REQUIRED_SECURITY_QUERIES if query not in queries]
    assert not missing, f"security coverage is missing: {missing}"

    security_queries = {
        query
        for family in KEYWORD_FAMILIES
        if family.group == GROUP_SECURITY
        for query in family.queries
    }
    assert security_queries == set(REQUIRED_SECURITY_QUERIES) | {"Azure Security Engineer", "Identity and Access Management Engineer"}


def test_catalog_keeps_the_baseline_families() -> None:
    baseline = [
        query
        for family in KEYWORD_FAMILIES
        if family.group == GROUP_BASELINE
        for query in family.queries
    ]
    assert baseline == ["Junior Cloud Engineer", "Junior DevOps Engineer", "Platform Engineer"]


def test_total_query_count_stays_bounded() -> None:
    families = len(KEYWORD_FAMILIES)
    assert len(_all_queries()) <= families * HARD_MAX_QUERIES_PER_FAMILY


def test_get_family_rejects_an_unknown_id_with_the_known_list() -> None:
    with pytest.raises(UnknownKeywordFamily) as excinfo:
        get_family("does-not-exist")
    assert "junior-cloud" in str(excinfo.value)


def test_resolve_families_defaults_to_the_whole_catalog_and_deduplicates() -> None:
    assert resolve_families(None) == KEYWORD_FAMILIES
    assert resolve_families([]) == KEYWORD_FAMILIES
    assert [family.id for family in resolve_families(["devsecops"])] == ["devsecops"]
    # A repeated id never plans its queries twice.
    assert [family.id for family in resolve_families(["devsecops", "devsecops"])] == ["devsecops"]
    # Catalog order wins over request order, so the plan is deterministic.
    assert [family.id for family in resolve_families(["iam-security-infra", "junior-cloud"])] == [
        "junior-cloud",
        "iam-security-infra",
    ]


def test_search_command_is_an_explicit_preview() -> None:
    command = search_command("Cloud Security Engineer")
    assert '--keywords "Cloud Security Engineer"' in command
    assert command.endswith("--dry-run")

    with_filters = search_command("Azure DevSecOps", location="Madrid, Spain", remote=True)
    assert '--location "Madrid, Spain"' in with_filters
    assert with_filters.endswith("--remote --dry-run")


def test_planned_queries_include_family_provenance_and_a_command() -> None:
    planned = planned_queries(["cloud-security"])
    assert [item["query"] for item in planned] == [
        "Cloud Security Engineer",
        "Junior Cloud Security Engineer",
    ]
    assert all(item["family"] == "cloud-security" for item in planned)
    assert all("--dry-run" in item["command"] for item in planned)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_keyword_families_default_to_empty_meaning_every_family() -> None:
    settings = Settings.from_env(env={}, dotenv_path=None)
    assert settings.keyword_families == ()


def test_keyword_families_are_read_from_the_environment() -> None:
    settings = Settings.from_env(
        env={"JOBSCOUT_KEYWORD_FAMILIES": "cloud-security, devsecops ,cloud-security"},
        dotenv_path=None,
    )
    assert settings.keyword_families == ("cloud-security", "devsecops")


def test_unknown_keyword_family_fails_at_startup() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env(env={"JOBSCOUT_KEYWORD_FAMILIES": "nope"}, dotenv_path=None)
    assert "unknown keyword family" in str(excinfo.value)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_keyword_plan_lists_every_query_offline(capsys, monkeypatch) -> None:
    no_network(monkeypatch)

    assert main(["keyword-plan"]) == EXIT_OK

    output = capsys.readouterr().out
    assert "no request was performed" in output
    for query in _all_queries():
        assert query in output, query
    assert "--dry-run" in output
    assert "--confirm-network" in output


def test_keyword_plan_filters_one_family(capsys, monkeypatch) -> None:
    no_network(monkeypatch)

    assert main(["keyword-plan", "--family", "devsecops"]) == EXIT_OK

    output = capsys.readouterr().out
    assert "DevSecOps Engineer" in output
    assert "Junior DevSecOps Engineer" in output
    # A family that was not requested stays out of the plan.
    assert "Junior Cloud Engineer" not in output


def test_keyword_plan_honours_location_and_remote(capsys, monkeypatch) -> None:
    no_network(monkeypatch)

    assert (
        main(
            [
                "keyword-plan",
                "--family",
                "cloud-security",
                "--location",
                "Madrid, Spain",
                "--remote",
            ]
        )
        == EXIT_OK
    )

    output = capsys.readouterr().out
    assert '--location "Madrid, Spain"' in output
    assert "--remote --dry-run" in output


def test_keyword_plan_json_payload_is_machine_readable(capsys, monkeypatch) -> None:
    no_network(monkeypatch)

    assert main(["keyword-plan", "--json"]) == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["family_count"] == len(KEYWORD_FAMILIES)
    assert payload["query_count"] == len(_all_queries())
    assert len(payload["planned"]) == payload["query_count"]
    assert payload["planned"][0]["command"].endswith("--dry-run")
    assert payload["families"][0]["id"] == family_ids()[0]


def test_keyword_plan_uses_the_configured_families(capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    monkeypatch.setenv("JOBSCOUT_KEYWORD_FAMILIES", "cloud-security")

    assert main(["keyword-plan", "--json"]) == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["family_count"] == 1
    assert {item["family"] for item in payload["planned"]} == {"cloud-security"}


def test_keyword_plan_rejects_an_unknown_family_cleanly(capsys, monkeypatch) -> None:
    no_network(monkeypatch)

    assert main(["keyword-plan", "--family", "nope"]) == EXIT_ERROR

    err = capsys.readouterr().err
    assert "unknown keyword family" in err
    assert "junior-cloud" in err


def test_every_catalog_query_runs_through_the_existing_search_flow(
    tmp_path, capsys, monkeypatch
) -> None:
    """The catalog is reachable with plain ``--keywords``; no new execution path."""
    no_network(monkeypatch)
    db_path = tmp_path / "keywords.sqlite3"

    for entry in planned_queries():
        assert (
            main(
                [
                    "--db",
                    str(db_path),
                    "search-linkedin",
                    "--keywords",
                    entry["query"],
                    "--dry-run",
                    "--json",
                ]
            )
            == EXIT_OK
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["planned_urls"], entry["query"]
        assert entry["query"].replace(" ", "+") in payload["planned_urls"][0]
        # A dry run never creates the database or records a run.
        assert not db_path.exists()
