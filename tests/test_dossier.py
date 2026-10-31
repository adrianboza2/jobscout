"""Company dossier tests.

Every test is offline: the OpenCode CLI is replaced by a ``FakeRunner`` or by
monkeypatching the module-level ``run_prompt``. No process, network call, or
model is used.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from conftest import FakeRunner, command_outcome

from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.config import Settings
from jobscout.dashboard import build_payload, render_dashboard_html
from jobscout.db import connect, init_db, store_posting, upsert_feedback
from jobscout.dossier import (
    DESCRIPTION_CHARS,
    build_dossier_prompt,
    company_slug,
    dossier_path,
    generate_dossier,
    load_dossiers,
    render_dossier,
    sanitize_dossier,
)
from jobscout.models import Job, JobFeedback, JobPosting
from jobscout.normalize import finalize_posting
from jobscout.scoring.llm import UNTRUSTED_BEGIN
from jobscout.scoring.opencode import (
    OpenCodeExecutionError,
    isolated_config_home,
    isolated_workdir,
)

TODAY = date(2026, 10, 31)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "dossier.sqlite3"


def make_job(**overrides) -> Job:
    payload = {
        "id": 7,
        "fingerprint": "fp-dossier",
        "dedupe_key": "dk-dossier",
        "source": "fixture",
        "title": "Senior Backend Engineer",
        "company": "Acme Data Inc.",
        "url": "https://example.test/jobs/7",
        "location": "Remote, Argentina",
        "description": "Python, SQL and PostgreSQL.",
    }
    payload.update(overrides)
    return Job(**payload)


def make_posting(serial: str, company: str, **overrides) -> JobPosting:
    payload = {
        "source": "fixture",
        "source_job_id": serial,
        "title": f"Role {serial}",
        "company": company,
        "url": f"https://example.test/jobs/{serial}",
        "location": "Remote",
        "description": "Python and SQL.",
    }
    payload.update(overrides)
    return finalize_posting(JobPosting(**payload))


def store_job(path: Path, serial: str, company: str) -> int:
    conn = connect(path)
    try:
        init_db(conn)
        job, _ = store_posting(
            conn, make_posting(serial, company), now="2026-10-31T10:00:00Z"
        )
        return int(job.id)
    finally:
        conn.close()


def write_feedback(path: Path, job_id: int, decision: str) -> None:
    conn = connect(path)
    try:
        upsert_feedback(
            conn,
            JobFeedback(
                job_id=job_id,
                decision=decision,
                decision_updated_at="2026-10-31T11:00:00Z",
                imported_at="2026-10-31T11:00:00Z",
            ),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Slug and prompt
# ---------------------------------------------------------------------------


def test_company_slug_folds_accents_and_punctuation() -> None:
    assert company_slug("Acme Data Inc.") == "acme-data-inc"
    assert company_slug("Ñandú & Cía.") == "nandu-cia"
    assert company_slug("  --Acme__Data--  ") == "acme-data"


def test_company_slug_rejects_an_empty_name() -> None:
    with pytest.raises(ValueError):
        company_slug("")
    with pytest.raises(ValueError):
        company_slug("!!!")


def test_prompt_contains_company_date_sections_and_the_linkedin_rule() -> None:
    prompt = build_dossier_prompt(make_job(), TODAY)

    assert "Acme Data Inc." in prompt
    assert "https://example.test/jobs/7" in prompt
    assert "2026-10-31" in prompt
    assert UNTRUSTED_BEGIN in prompt
    assert "DATOS NO CONFIABLES" in prompt
    assert "NUNCA consultes ni cites linkedin.com" in prompt
    assert "No encontrado" in prompt
    for heading in (
        "## Sector",
        "## Tamaño",
        "## Stack",
        "## Política de remoto",
        "## Contratación junior",
        "## Fuentes",
    ):
        assert heading in prompt


def test_prompt_caps_the_description() -> None:
    job = make_job(description="x" * (DESCRIPTION_CHARS + 500))
    prompt = build_dossier_prompt(job, TODAY)

    assert "x" * DESCRIPTION_CHARS in prompt
    assert "x" * (DESCRIPTION_CHARS + 1) not in prompt


# ---------------------------------------------------------------------------
# Sanitizing and rendering
# ---------------------------------------------------------------------------


def test_sanitize_strips_a_whole_answer_fence_and_linkedin_lines() -> None:
    answer = (
        "```markdown\n"
        "## Sector\n"
        "SaaS.\n"
        "- https://www.linkedin.com/company/acme (consultada 2026-10-31)\n"
        "## Fuentes\n"
        "- https://acme.test (consultada 2026-10-31)\n"
        "```\n"
    )

    text, removed = sanitize_dossier(answer)

    assert removed == 1
    assert "```" not in text
    assert "linkedin.com" not in text.lower()
    assert text.startswith("## Sector")
    assert "https://acme.test" in text


def test_sanitize_counts_linkedin_lines_case_insensitively() -> None:
    text, removed = sanitize_dossier(
        "## Sector\nSaaS.\n## Fuentes\n- https://LINKEDIN.com/jobs/1\n- https://acme.test\n"
    )

    assert removed == 1
    assert "acme.test" in text


def test_sanitize_keeps_only_the_final_answer_after_fetched_pages() -> None:
    answer = (
        "I'll research Acme.\n"
        "# Acme home\n## Sector\nfetched page noise\n"
        "- [Jobs](https://www.linkedin.com/company/acme)\n"
        "```markdown\n## Sector\nSaaS B2B.\n## Fuentes\n"
        "- https://acme.test (consultada 2026-10-31)\n```\n"
    )

    text, removed = sanitize_dossier(answer)

    assert text.startswith("## Sector\nSaaS B2B.")
    assert "noise" not in text and "I'll research" not in text
    assert "```" not in text
    assert removed == 0


def test_sanitize_rejects_an_answer_without_sections() -> None:
    with pytest.raises(ValueError, match="Sector"):
        sanitize_dossier("I could not finish the research.")


def test_render_dossier_writes_the_code_header() -> None:
    rendered = render_dossier(
        "Acme Data Inc.",
        "## Sector\nSaaS.",
        job_id=7,
        model="opencode-go/deepseek-v4.1-flash#low",
        generated_on=TODAY,
        job_title="Senior Backend Engineer",
    )

    lines = rendered.splitlines()
    assert lines[0] == "# Acme Data Inc."
    assert lines[1] == "- Generado: 2026-10-31"
    assert lines[2] == "- Modelo: opencode-go/deepseek-v4.1-flash#low"
    assert lines[3] == "- Oferta de origen: #7 Senior Backend Engineer"
    assert lines[4] == ""
    assert rendered.endswith("## Sector\nSaaS.\n")


# ---------------------------------------------------------------------------
# generate_dossier uses the isolated scorer transport
# ---------------------------------------------------------------------------


def test_generate_dossier_writes_the_file_and_uses_the_isolated_invocation(
    tmp_path: Path, opencode_settings: Settings
) -> None:
    settings = opencode_settings.with_overrides(
        db_path=tmp_path / "jobscout.sqlite3"
    )
    runner = FakeRunner(
        [command_outcome(stdout="```markdown\n## Sector\nSaaS.\n```\n")]
    )

    result = generate_dossier(
        settings, make_job(), today=TODAY, timeout=600, runner=runner
    )

    assert result.company == "Acme Data Inc."
    assert result.path == tmp_path / "dossiers" / "acme-data-inc.md"
    assert result.removed_linkedin_lines == 0
    written = result.path.read_text(encoding="utf-8")
    assert written.startswith("# Acme Data Inc.\n")
    assert "- Generado: 2026-10-31" in written
    assert "## Sector" in written
    assert "```" not in written

    call = runner.calls[0]
    assert call["argv"][0] == "opencode"
    assert call["argv"][1] == "run"
    assert call["cwd"] == str(isolated_workdir(settings))
    assert call["cwd"] == str((tmp_path / "opencode-work").resolve())
    assert call["timeout"] == 600
    env = call["env"]
    assert env["XDG_CONFIG_HOME"] == str(isolated_config_home(settings))
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"


def test_load_dossiers_reads_the_directory_keyed_by_slug(tmp_path: Path) -> None:
    assert load_dossiers(tmp_path / "missing.sqlite3") == {}

    directory = tmp_path / "dossiers"
    directory.mkdir()
    (directory / "acme-data-inc.md").write_text("# Acme", encoding="utf-8")

    assert load_dossiers(tmp_path / "jobscout.sqlite3") == {
        "acme-data-inc": "# Acme"
    }
    assert dossier_path(
        Settings.from_env(env={}, dotenv_path=None).with_overrides(
            db_path=tmp_path / "jobscout.sqlite3"
        ),
        "Acme Data Inc.",
    ) == directory / "acme-data-inc.md"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_dossier_without_selection_is_an_error(db_path: Path, capsys) -> None:
    assert main(["--db", str(db_path), "dossier"]) == EXIT_ERROR
    assert "pass --job-id and/or --apply" in capsys.readouterr().err


def test_apply_selects_only_apply_feedback_and_dedupes_companies(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    first = store_job(db_path, "1", "Acme Data Inc.")
    second = store_job(db_path, "2", "Acme Data Inc.")
    rejected = store_job(db_path, "3", "Rejected Corp")
    other = store_job(db_path, "4", "Beta Corp")
    write_feedback(db_path, first, "apply")
    write_feedback(db_path, second, "apply")
    write_feedback(db_path, rejected, "reject")
    write_feedback(db_path, other, "apply")

    prompts: list[str] = []

    def fake_run_prompt(settings, prompt, *, timeout=None, runner=None):
        prompts.append(prompt)
        return "## Sector\nSaaS.\n"

    monkeypatch.setattr("jobscout.dossier.run_prompt", fake_run_prompt)

    assert main(["--db", str(db_path), "dossier", "--apply"]) == EXIT_OK

    output = capsys.readouterr().out
    assert len(prompts) == 2  # one per company, first offer wins
    assert "wrote" in output
    assert (tmp_path / "dossiers" / "acme-data-inc.md").is_file()
    assert (tmp_path / "dossiers" / "beta-corp.md").is_file()
    assert not (tmp_path / "dossiers" / "rejected-corp.md").exists()


def test_existing_dossier_is_skipped_unless_refresh(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    job_id = store_job(db_path, "1", "Acme Data Inc.")
    target = tmp_path / "dossiers" / "acme-data-inc.md"
    target.parent.mkdir()
    target.write_text("# Old\n", encoding="utf-8")
    calls: list[str] = []

    def fake_run_prompt(settings, prompt, *, timeout=None, runner=None):
        calls.append(prompt)
        return "## Sector\nNew.\n"

    monkeypatch.setattr("jobscout.dossier.run_prompt", fake_run_prompt)

    assert main(["--db", str(db_path), "dossier", "--job-id", str(job_id)]) == EXIT_OK
    assert "skipped acme-data-inc (exists)" in capsys.readouterr().out
    assert calls == []
    assert target.read_text(encoding="utf-8") == "# Old\n"

    assert (
        main(
            ["--db", str(db_path), "dossier", "--job-id", str(job_id), "--refresh"]
        )
        == EXIT_OK
    )
    assert len(calls) == 1
    assert target.read_text(encoding="utf-8").startswith("# Acme Data Inc.")
    assert "New." in target.read_text(encoding="utf-8")


def test_one_failure_does_not_stop_the_others_and_exits_1(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    broken = store_job(db_path, "1", "Broken Corp")
    working = store_job(db_path, "2", "Working Corp")
    write_feedback(db_path, broken, "apply")
    write_feedback(db_path, working, "apply")

    def fake_run_prompt(settings, prompt, *, timeout=None, runner=None):
        if "Broken Corp" in prompt:
            raise OpenCodeExecutionError("OpenCode CLI exited with code 1")
        return "## Sector\nSaaS.\n"

    monkeypatch.setattr("jobscout.dossier.run_prompt", fake_run_prompt)

    assert main(["--db", str(db_path), "dossier", "--apply"]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert "failed broken-corp: OpenCode CLI exited with code 1" in captured.out
    assert (tmp_path / "dossiers" / "working-corp.md").is_file()


def test_cli_json_output_reports_each_company(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    job_id = store_job(db_path, "1", "Acme Data Inc.")

    def fake_run_prompt(settings, prompt, *, timeout=None, runner=None):
        return "## Sector\nSaaS.\n- https://linkedin.com/x\n"

    monkeypatch.setattr("jobscout.dossier.run_prompt", fake_run_prompt)

    assert (
        main(["--db", str(db_path), "dossier", "--job-id", str(job_id), "--json"])
        == EXIT_OK
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] == 1
    assert payload["failed"] == 0
    assert payload["dossiers"][0]["slug"] == "acme-data-inc"
    assert payload["dossiers"][0]["removed_linkedin_lines"] == 1
    written = (tmp_path / "dossiers" / "acme-data-inc.md").read_text(
        encoding="utf-8"
    )
    assert "linkedin.com" not in written.lower()


# ---------------------------------------------------------------------------
# Dashboard embedding
# ---------------------------------------------------------------------------


def test_dashboard_embeds_the_company_dossier_escaped(tmp_path: Path) -> None:
    db_file = tmp_path / "dash" / "jobscout.sqlite3"
    conn = connect(db_file)
    try:
        init_db(conn)
        store_posting(
            conn, make_posting("1", "Acme Data Inc."), now="2026-10-31T10:00:00Z"
        )
        store_posting(
            conn, make_posting("2", "No Dossier Corp"), now="2026-10-31T10:00:00Z"
        )
    finally:
        conn.close()

    directory = db_file.parent / "dossiers"
    directory.mkdir()
    hostile = "## Sector\n<script>alert('x')</script>\n"
    (directory / "acme-data-inc.md").write_text(hostile, encoding="utf-8")

    conn = connect(db_file)
    try:
        payload = build_payload(conn, top=None, db_path=db_file)
    finally:
        conn.close()

    by_company = {offer["company"]: offer for offer in payload["offers"]}
    assert by_company["Acme Data Inc."]["dossier"] == hostile
    assert by_company["No Dossier Corp"]["dossier"] is None

    html_text = render_dashboard_html(payload)
    assert "\\u003cscript\\u003ealert" in html_text
    assert "<script>alert('x')</script>" not in html_text
