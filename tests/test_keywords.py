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
    RETIRED_SEARCHES,
    UnknownKeywordFamily,
    family_ids,
    get_family,
    planned_queries,
    resolve_families,
    retired_search,
    search_command,
)

#: Queries the security coverage work promised that stay active after the
#: 2026-09-29 LinkedIn retirement (`Cloud Security Engineer` and `IAM Engineer`
#: were removed from the LinkedIn catalog by that owner decision).
REQUIRED_SECURITY_QUERIES = (
    "Junior Cloud Security Engineer",
    "DevSecOps Engineer",
    "Junior DevSecOps Engineer",
    "Azure Cloud Security",
    "Azure DevSecOps",
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
    assert security_queries == set(REQUIRED_SECURITY_QUERIES) | {
        "Azure Security Engineer",
        "Cloud Security Engineer",
    }


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


def test_planned_queries_report_retired_providers() -> None:
    planned = {item["query"]: item for item in planned_queries()}
    assert planned["Platform Engineer"]["retired_for"] == ["tecnoempleo"]
    assert planned["Junior Cloud Security Engineer"]["retired_for"] == ["tecnoempleo"]
    assert planned["Junior Cloud Engineer"]["retired_for"] == []
    assert all("retired_for" in item for item in planned.values())


# ---------------------------------------------------------------------------
# Retired searches (provider-keyed history)
# ---------------------------------------------------------------------------


RETIRED_TECNO_QUERIES = (
    "Platform Engineer",
    "Azure Security Engineer",
    "IAM Engineer",
    "Junior Cloud Security Engineer",
    "Security Infrastructure Engineer",
)
RETIRED_LINKEDIN_QUERIES = (
    "Identity and Access Management Engineer",
    "IAM Engineer",
)
#: The exact owner-authorized retirement set, provider by provider.
RETIRED_PAIRS = (
    *(("tecnoempleo", query) for query in RETIRED_TECNO_QUERIES),
    *(("linkedin", query) for query in RETIRED_LINKEDIN_QUERIES),
)
#: New CP-8 retirements carry the 2026-09-30 decision date.
CP8_RETIRED_PAIRS = (
    ("tecnoempleo", "Junior Cloud Security Engineer"),
    ("tecnoempleo", "Security Infrastructure Engineer"),
)


def test_retirement_registry_records_provider_query_date_and_reason() -> None:
    assert len(RETIRED_SEARCHES) == 7
    observed = {(entry.provider, entry.query) for entry in RETIRED_SEARCHES}
    assert observed == set(RETIRED_PAIRS)
    for entry in RETIRED_SEARCHES:
        assert entry.reason.strip()
        assert entry.provider in {"tecnoempleo", "linkedin"}
        expected_on = "2026-09-30" if (entry.provider, entry.query) in CP8_RETIRED_PAIRS else "2026-09-29"
        assert entry.retired_on == expected_on


def test_retired_search_matches_only_the_exact_normalized_pair() -> None:
    for provider, query in RETIRED_PAIRS:
        match = retired_search(provider, query)
        assert match is not None
        assert (match.provider, match.query) == (provider, query)


def test_retired_search_folds_case_whitespace_and_diacritics() -> None:
    match = retired_search("  TecnoEmpleo  ", "  platform   ENGINEER ")
    assert match is not None and match.query == "Platform Engineer"
    match = retired_search("LinkedIn", "  identity   and   access   management   engineer ")
    assert match is not None and match.query == "Identity and Access Management Engineer"
    # Diacritics fold through the project normalizer.
    assert retired_search("tecnoempleo", "IAM Engine\u00e9r") is not None
    assert retired_search("linkedin", "IAM Engine\u00e9r") is not None


def test_retired_search_never_substring_matches() -> None:
    assert retired_search("tecnoempleo", "Senior Platform Engineer") is None
    assert retired_search("tecnoempleo", "Platform Engineering") is None
    assert retired_search("linkedin", "Senior Platform Engineer") is None
    assert retired_search("linkedin", "Cloud Security Architect") is None


def test_retired_search_is_provider_scoped() -> None:
    # A provider that never retired a literal keeps it. Some literals are retired
    # on both listed providers, so those are asserted per-pair above; here every
    # pair stays clear on the providers that did not retire it.
    for provider in ("getmanfred", "infojobs"):
        for _, query in RETIRED_PAIRS:
            assert retired_search(provider, query) is None, (provider, query)
    # Provider-exclusive retirements do not leak to the other listed provider.
    assert retired_search("linkedin", "Azure Security Engineer") is None
    assert retired_search("linkedin", "Junior Cloud Security Engineer") is None
    # CP-8 kept these Tecnoempleo literals active (they cost good offers).
    assert retired_search("tecnoempleo", "Cloud Security Engineer") is None
    assert retired_search("tecnoempleo", "Identity and Access Management Engineer") is None


def test_active_catalog_restores_the_kept_linkedin_queries() -> None:
    # CP-8 restored these two LinkedIn terms: the audit showed they cost good
    # offers (Platform Engineer) or zero unique offers (Cloud Security Engineer).
    active = set(_all_queries())
    assert "Cloud Security Engineer" in active
    assert "Platform Engineer" in active
    assert active.isdisjoint(RETIRED_LINKEDIN_QUERIES)
    # ``Azure Security Engineer`` is Tecnoempleo-only and stays in the LinkedIn
    # catalog.
    assert "Azure Security Engineer" in active


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


def test_keyword_plan_json_reports_retired_providers(capsys, monkeypatch) -> None:
    no_network(monkeypatch)

    assert main(["keyword-plan", "--json"]) == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    retired = {item["query"]: item["retired_for"] for item in payload["planned"]}
    assert retired["Platform Engineer"] == ["tecnoempleo"]
    assert retired["Junior Cloud Engineer"] == []
    assert all("retired_for" in item for item in payload["planned"])


def test_keyword_plan_text_marks_retired_queries(capsys, monkeypatch) -> None:
    no_network(monkeypatch)

    assert main(["keyword-plan"]) == EXIT_OK

    output = capsys.readouterr().out
    assert "- Platform Engineer (retired for: tecnoempleo)" in output
    assert "- Junior Cloud Security Engineer (retired for: tecnoempleo)" in output
    assert "- Junior Cloud Engineer" in output
    assert "- Junior Cloud Engineer (retired for:" not in output


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
