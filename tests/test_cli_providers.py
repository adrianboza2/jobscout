"""CLI tests for the new public provider commands.

Every command runs against a temporary database. The live paths are only
exercised through ``--dry-run`` and the disabled-by-default refusal, never with
a real request (``no_network`` blocks sockets).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import no_network

from jobscout.cli import EXIT_FAIL_CLOSED, EXIT_OK, main


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cli-providers.sqlite3"


def test_search_getmanfred_refuses_without_confirmation(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    exit_code = main(["--db", str(db_path), "search-getmanfred"])
    assert exit_code == EXIT_FAIL_CLOSED
    err = capsys.readouterr().err
    assert "--confirm-network" in err
    assert "--dry-run" in err
    # No run was recorded.
    assert main(["--db", str(db_path), "runs", "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["runs"] == []


def test_search_getmanfred_dry_run_needs_no_network(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    exit_code = main(["--db", str(db_path), "search-getmanfred", "--dry-run", "--json"])
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["planned_urls"] == ["https://www.getmanfred.com/sitemap-offers.xml"]


def test_search_tecnoempleo_refuses_without_confirmation(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    exit_code = main(
        ["--db", str(db_path), "search-tecnoempleo", "--keywords", "python"]
    )
    assert exit_code == EXIT_FAIL_CLOSED
    err = capsys.readouterr().err
    assert "--confirm-network" in err


def test_search_tecnoempleo_dry_run_needs_no_network(db_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    exit_code = main(
        [
            "--db",
            str(db_path),
            "search-tecnoempleo",
            "--keywords",
            "cloud security",
            "--location",
            "madrid",
            "--max-pages",
            "2",
            "--dry-run",
            "--json",
        ]
    )
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["planned_urls"] == [
        "https://www.tecnoempleo.com/ofertas-trabajo/madrid/?te=cloud+security",
        "https://www.tecnoempleo.com/ofertas-trabajo/madrid/?te=cloud+security&pagina=2",
    ]


def test_new_provider_commands_enforce_hard_item_caps(db_path: Path, capsys, monkeypatch) -> None:
    from jobscout.config import HARD_MAX_ITEMS

    no_network(monkeypatch)
    exit_code = main(
        [
            "--db",
            str(db_path),
            "search-getmanfred",
            "--max-items",
            str(HARD_MAX_ITEMS + 1),
            "--dry-run",
        ]
    )
    assert exit_code != EXIT_OK
    assert "must be" in capsys.readouterr().err
