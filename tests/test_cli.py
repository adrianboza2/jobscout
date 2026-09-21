"""CLI smoke tests.

Every command runs against a temporary database and the local fixture. The live
LinkedIn path is only exercised through ``--dry-run`` and the disabled-by-default
refusal, never with a real request.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import FIXTURES, no_network

from jobscout.cli import EXIT_ERROR, EXIT_FAIL_CLOSED, EXIT_OK, main
from jobscout.config import HARD_MAX_ITEMS, HARD_MAX_PAGES

FIXTURE = FIXTURES / "linkedin_search_sample.json"
PROFILE = FIXTURES / "profile_test.yaml"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cli.sqlite3"


def test_init_db_accepts_global_and_subcommand_flags(db_path: Path, capsys) -> None:
    assert main(["--db", str(db_path), "init-db"]) == EXIT_OK
    assert main(["init-db", "--db", str(db_path)]) == EXIT_OK
    assert db_path.is_file()
    assert "initialized" in capsys.readouterr().out


def test_ingest_fixture_then_list_and_rank(db_path: Path, capsys) -> None:
    assert main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "4 new" in output and "2 duplicate" in output

    assert main(["--db", str(db_path), "list"]) == EXIT_OK
    listing = capsys.readouterr().out
    assert "Senior Backend Engineer" in listing
    assert "Platform Engineer" in listing

    assert main(["--db", str(db_path), "rank", "--top", "5"]) == EXIT_OK
    assert "no scored jobs yet" in capsys.readouterr().out


def test_list_json_output_is_machine_readable(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 4
    assert payload["jobs"][0]["job"]["title"]


def test_score_heuristic_then_rank_and_list(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--all",
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert "scorer=heuristic" in output
    assert "recommendation=" in output
    assert "blockers:" in output
    assert "scored 4 job(s)" in output

    assert main(["--db", str(db_path), "rank", "--top", "2", "--json"]) == EXIT_OK
    ranked = json.loads(capsys.readouterr().out)
    assert ranked["count"] == 2
    assert ranked["jobs"][0]["score"] is not None
    assert ranked["jobs"][0]["score"] >= ranked["jobs"][1]["score"]

    assert main(["--db", str(db_path), "list", "--json", "--min-score", "0"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["count"] >= 1


def test_score_is_idempotent_without_rescore(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    args = [
        "--db",
        str(db_path),
        "--profile",
        str(PROFILE),
        "score",
        "--all",
        "--scorer",
        "heuristic",
    ]
    assert main(args) == EXIT_OK
    capsys.readouterr()
    assert main(args) == EXIT_OK
    assert "scored 0 job(s)" in capsys.readouterr().out

    rescore = [
        "--db",
        str(db_path),
        "--profile",
        str(PROFILE),
        "score",
        "--all",
        "--scorer",
        "heuristic",
        "--rescore",
    ]
    assert main(rescore) == EXIT_OK
    assert "scored 4 job(s)" in capsys.readouterr().out


def test_score_specific_job_and_unknown_job(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    job_id = json.loads(capsys.readouterr().out)["jobs"][0]["job"]["id"]

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(PROFILE),
                "score",
                "--job-id",
                str(job_id),
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    assert f"job #{job_id}" in capsys.readouterr().out

    assert main(["--db", str(db_path), "--profile", str(PROFILE), "score", "--job-id", "999999"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "not found" in captured.err


def test_score_with_llm_without_configuration_fails_cleanly(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(["--db", str(db_path), "--profile", str(PROFILE), "score", "--all", "--scorer", "llm"])
    assert exit_code == EXIT_ERROR
    output = capsys.readouterr().out
    assert "JOBSCOUT_LLM_BASE_URL" in output


def test_score_with_opencode_scorer_uses_mocked_subprocess(
    db_path: Path, capsys, monkeypatch, tmp_path: Path
) -> None:
    """The CLI wiring works end to end without starting the real CLI."""
    from conftest import fake_popen_factory
    from jobscout.scoring import opencode as opencode_module

    payload = (FIXTURES / "llm_response_valid.json").read_text(encoding="utf-8")
    event = json.dumps(
        {"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": payload}}
    )

    factory, created = fake_popen_factory(stdout_text=event)
    monkeypatch.setattr(opencode_module.subprocess, "Popen", factory)
    fake_exe = tmp_path / "opencode-fake"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("JOBSCOUT_OPENCODE_COMMAND", str(fake_exe))

    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--all",
            "--scorer",
            "opencode",
        ]
    )
    output = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "scorer=opencode" in output
    assert "scored 4 job(s)" in output
    # The default runner was used: an argument array with no shell.
    assert created and created[0].kwargs.get("shell") is False


def test_score_with_opencode_scorer_missing_cli_fails_cleanly(
    db_path: Path, capsys, monkeypatch
) -> None:
    no_network(monkeypatch)
    monkeypatch.setenv("JOBSCOUT_OPENCODE_COMMAND", "jobscout-definitely-not-installed-xyz")
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(
        [
            "--db",
            str(db_path),
            "--profile",
            str(PROFILE),
            "score",
            "--all",
            "--scorer",
            "opencode",
        ]
    )
    assert exit_code == EXIT_ERROR
    output = capsys.readouterr().out
    assert "OpenCode CLI" in output
    assert "auth login" in output


def test_score_with_a_missing_profile_reports_an_error(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    exit_code = main(
        ["--db", str(db_path), "--profile", "does-not-exist.yaml", "score", "--all"]
    )
    assert exit_code == EXIT_ERROR
    assert "profile error" in capsys.readouterr().err


def test_search_linkedin_refuses_without_confirmation(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    exit_code = main(["--db", str(db_path), "search-linkedin", "--keywords", "python"])
    assert exit_code == EXIT_FAIL_CLOSED
    err = capsys.readouterr().err
    assert "--confirm-network" in err
    assert "--dry-run" in err
    # Nothing was recorded because no run happened.
    assert main(["--db", str(db_path), "runs", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["runs"] == []


def test_search_linkedin_dry_run_needs_no_network(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    exit_code = main(
        [
            "--db",
            str(db_path),
            "search-linkedin",
            "--keywords",
            "senior backend engineer",
            "--location",
            "Buenos Aires",
            "--max-pages",
            "2",
            "--dry-run",
        ]
    )
    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "dry run" in output
    assert output.count("seeMoreJobPostings") == 2
    assert "start=0" in output and "start=25" in output


def test_search_linkedin_dry_run_respects_settings_bounds(db_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("JOBSCOUT_MAX_PAGES", "3")
    monkeypatch.setenv("JOBSCOUT_DB_PATH", str(db_path))
    assert main(["search-linkedin", "--keywords", "python", "--dry-run", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["planned_urls"]) == 3


def test_search_linkedin_dry_run_enforces_hard_caps(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    # Exactly at the cap: the dry run plans that many requests.
    assert (
        main(
            [
                "--db",
                str(db_path),
                "search-linkedin",
                "--keywords",
                "python",
                "--max-pages",
                str(HARD_MAX_PAGES),
                "--dry-run",
                "--json",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["planned_urls"]) == HARD_MAX_PAGES

    # Above the cap: refuse clearly instead of planning an unbounded run.
    assert (
        main(
            [
                "--db",
                str(db_path),
                "search-linkedin",
                "--keywords",
                "python",
                "--max-pages",
                str(HARD_MAX_PAGES + 1),
                "--dry-run",
            ]
        )
        == EXIT_ERROR
    )
    assert "absolute upper bound" in capsys.readouterr().err

    assert (
        main(
            [
                "--db",
                str(db_path),
                "search-linkedin",
                "--keywords",
                "python",
                "--max-items",
                str(HARD_MAX_ITEMS + 1),
                "--dry-run",
            ]
        )
        == EXIT_ERROR
    )
    assert "absolute upper bound" in capsys.readouterr().err


def test_runs_command_reports_counts(db_path: Path, capsys) -> None:
    main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)])
    capsys.readouterr()
    assert main(["--db", str(db_path), "runs", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["jobs"] == 4
    assert payload["evaluations"] == 0
    assert payload["runs"][0]["status"] == "completed"


def test_missing_command_is_a_usage_error(db_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--db", str(db_path)])


def test_score_requires_a_selection(db_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--db", str(db_path), "score"])


def test_notify_is_disabled_by_default_and_creates_no_database(
    db_path: Path, capsys
) -> None:
    """The opt-in default reads nothing, sends nothing, and writes no file."""
    assert main(["--db", str(db_path), "notify"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "disabled" in output
    assert not db_path.exists()

    assert main(["--db", str(db_path), "notify", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is False
    assert payload["count"] == 0
    assert not db_path.exists()
