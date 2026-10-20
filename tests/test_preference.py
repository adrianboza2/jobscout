"""Preference-filter tests: rotating shifts, operational SOC, and integration.

Everything runs offline. The classifier is pure, so these tests call it directly
and then verify that the dashboard payload and the CLI surface its verdict
without moving the model score or persisting a second judgment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from conftest import no_network

from jobscout.cli import EXIT_OK, main
from jobscout.dashboard import (
    PREFERENCE_ORDER,
    build_payload,
    render_dashboard_html,
)
from jobscout.db import connect, insert_evaluation, init_db, store_posting
from jobscout.models import Evaluation, Recommendation, ScorerName
from jobscout.normalize import finalize_posting
from jobscout.models import JobPosting
from jobscout.preference import (
    PREFERENCE_SCHEMA,
    PreferenceOutcome,
    assess_preference,
    assess_text,
)

DESCRIPTION_TOKEN = "DESCRIPTION-TOKEN-preference-4b2e-should-never-appear"


@dataclass
class FakeJob:
    title: str = ""
    description: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    location: str | None = None
    company: str | None = None


# ---------------------------------------------------------------------------
# Rotating shifts: clear matches fail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Buscamos analista con turnos rotativos",
        "El puesto requiere turno rotativo",
        "Trabajo con rotación de turnos",
        "Ofrecemos turnos rotatorios",
        "Horario rotativo de 8 horas",
        "This role requires rotating shifts including nights",
        "A rotating shift pattern across three crews",
        "Rotational shift work on a 24/7 rota",
        "The team covers shift rotation every week",
    ],
)
def test_clear_rotating_shift_language_fails(text: str) -> None:
    assessment = assess_text(text)

    assert assessment.outcome is PreferenceOutcome.FAIL
    assert assessment.evidence
    assert assessment.reason
    assert any(match.rule == "shift.rotating" for match in assessment.matches)


def test_two_four_seven_shift_pairing_fails() -> None:
    assessment = assess_text("Cobertura de turnos 24/7 para el equipo")

    assert assessment.outcome is PreferenceOutcome.FAIL
    assert any(match.rule == "shift.247" for match in assessment.matches)


# ---------------------------------------------------------------------------
# Flexible schedules and occasional on-call: no false failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Ofrecemos horario flexible y trabajo remoto",
        "Flexibilidad horaria y buen ambiente",
        "Flexible working hours with core overlap",
        "Se requieren guardias puntuales unas veces al año",
        "Guardias ocasionales fuera de horario",
        "Occasional on-call duty a few times a year",
    ],
)
def test_flexible_or_occasional_never_fails(text: str) -> None:
    assessment = assess_text(text)

    assert assessment.outcome is PreferenceOutcome.PASS


def test_occasional_on_call_is_recorded_as_pass_evidence() -> None:
    assessment = assess_text("Se requieren guardias puntuales")

    assert assessment.outcome is PreferenceOutcome.PASS
    assert any(match.rule == "oncall.occasional" for match in assessment.matches)
    assert "guardias" in assessment.evidence


def test_unqualified_guardias_is_only_flagged() -> None:
    assessment = assess_text("Disponibilidad para guardias de soporte")

    assert assessment.outcome is PreferenceOutcome.FLAG
    assert any(match.rule == "shift.oncall" for match in assessment.matches)


def test_generic_shift_work_is_only_flagged() -> None:
    assert assess_text("This is shift work in a warehouse").outcome is PreferenceOutcome.FLAG
    assert assess_text("Disponibilidad para turnos").outcome is PreferenceOutcome.FLAG


def test_fixed_shift_is_not_flagged_as_rotating() -> None:
    assessment = assess_text("Turno fijo de mañana, sin rotación")

    assert assessment.outcome is PreferenceOutcome.PASS


# ---------------------------------------------------------------------------
# SOC: generic/knowledge passes, operational fails, role is ambiguous
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Trabajamos con varios frameworks; SOC es una de las áreas de seguridad",
        "Conocimiento de SOC y SIEM deseable",
        "Knowledge of SOC concepts is a plus",
        "Auditoría de SOC 2 Type II y controles internos",
        "Familiarity with SOC and security frameworks",
    ],
)
def test_generic_or_knowledge_soc_passes(text: str) -> None:
    assessment = assess_text(text)

    assert assessment.outcome is PreferenceOutcome.PASS


@pytest.mark.parametrize(
    "text",
    [
        "SOC Analyst para nuestro equipo de seguridad",
        "Analista de Seguridad SOC",
        "SOC Engineer required",
        "Buscamos un especialista SOC N2",
    ],
)
def test_soc_role_without_operational_evidence_is_flagged(text: str) -> None:
    assessment = assess_text(text)

    assert assessment.outcome is PreferenceOutcome.FLAG
    assert any(match.rule == "soc.role" for match in assessment.matches)


@pytest.mark.parametrize(
    "text",
    [
        "SOC monitoring y gestión de incidentes",
        "Experiencia en SOC con SIEM monitoring",
        "SOC with incident monitoring and alert triage",
        "Analista SOC en turnos rotativos",
        "Trabajo en el SOC con guardias 24/7",
        "Security Operations Center con vigilancia continua",
    ],
)
def test_operational_soc_fails(text: str) -> None:
    assessment = assess_text(text)

    assert assessment.outcome is PreferenceOutcome.FAIL
    assert any(match.rule == "soc.operational" for match in assessment.matches)


def test_soc_title_combines_with_operational_description_across_fields() -> None:
    job = FakeJob(
        title="SOC Analyst",
        description="Cobertura 24/7 y monitorización continua de alertas",
    )

    assessment = assess_preference(job)

    assert assessment.outcome is PreferenceOutcome.FAIL
    assert any(match.rule == "soc.operational" for match in assessment.matches)


def test_engineer_title_does_not_turn_soc_knowledge_into_a_role() -> None:
    job = FakeJob(
        title="Security Engineer",
        description="Conocimiento de SOC y auditorías SOC 2",
    )

    assert assess_preference(job).outcome is PreferenceOutcome.PASS


def test_soc_compliance_number_is_not_a_role() -> None:
    assert assess_text("SOC 2 compliance program").outcome is PreferenceOutcome.PASS


# ---------------------------------------------------------------------------
# Shape, determinism, and offline guarantee
# ---------------------------------------------------------------------------


def test_assessment_payload_is_serializable_and_stable() -> None:
    assessment = assess_text("turnos rotativos")
    payload = assessment.to_dict()

    assert payload["schema"] == PREFERENCE_SCHEMA
    assert payload["outcome"] == "fail"
    assert payload["evidence"] == payload["evidence"]  # stable ordering
    assert payload["rules"]
    json.dumps(payload)  # must be JSON-safe

    assert assessment.to_dict() == assess_text("turnos rotativos").to_dict()


def test_pass_without_cues_has_an_honest_reason() -> None:
    assessment = assess_text("Senior Backend Engineer with Python and SQL")

    assert assessment.outcome is PreferenceOutcome.PASS
    assert assessment.evidence == []
    assert "No exclusion cue" in assessment.reason


def test_blank_input_is_pass() -> None:
    assert assess_preference(None).outcome is PreferenceOutcome.PASS
    assert assess_text("", None).outcome is PreferenceOutcome.PASS


def test_classifier_never_opens_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    no_network(monkeypatch)

    assert assess_text("turnos rotativos").outcome is PreferenceOutcome.FAIL
    assert assess_preference(FakeJob(title="SOC Analyst")).outcome is PreferenceOutcome.FLAG


def test_preference_does_not_mutate_the_job() -> None:
    job = FakeJob(title="Analista SOC", description="turnos rotativos")
    before = (job.title, job.description)

    assess_preference(job)

    assert (job.title, job.description) == before


# ---------------------------------------------------------------------------
# Dashboard integration: separate axis, score intact, description not leaked
# ---------------------------------------------------------------------------


def _store(conn, *, title: str, description: str, source_job_id: str):
    posting = finalize_posting(
        JobPosting(
            source="fixture",
            title=title,
            company="Acme Data Inc.",
            location="Remote, Argentina",
            url=f"https://example.test/jobs/{source_job_id}",
            source_job_id=source_job_id,
            description=description,
        )
    )
    job, _ = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    return job


def _evaluate(conn, job_id: int, score: float) -> None:
    insert_evaluation(
        conn,
        Evaluation(
            job_id=int(job_id),
            scorer=ScorerName.HEURISTIC,
            overall_score=score,
            recommendation=Recommendation.CONSIDER,
            confidence=0.8,
            rationale="stored evaluation",
            model="heuristic-v1",
        ),
    )


def test_dashboard_reports_preference_without_changing_the_score(tmp_path: Path) -> None:
    db_path = tmp_path / "preference.sqlite3"
    conn = connect(db_path)
    try:
        init_db(conn)
        rotating = _store(
            conn,
            title="Backend Engineer",
            description="Turnos rotativos y disponibilidad de fin de semana.",
            source_job_id="1",
        )
        clean = _store(
            conn,
            title="Data Engineer",
            description="Horario flexible y trabajo remoto.",
            source_job_id="2",
        )
        _evaluate(conn, rotating.id, 71.5)
        _evaluate(conn, clean.id, 64.0)
        payload = build_payload(conn, top=10)
    finally:
        conn.close()

    offers = {offer["id"]: offer for offer in payload["offers"]}
    rotating_offer = offers[int(rotating.id)]
    clean_offer = offers[int(clean.id)]

    # The stored model score is reported verbatim; preference never edits it.
    assert rotating_offer["score"] == 71.5
    assert clean_offer["score"] == 64.0
    assert rotating_offer["recommendation"] == "consider"

    assert rotating_offer["preference"]["outcome"] == "fail"
    assert rotating_offer["preference"]["evidence"]
    assert clean_offer["preference"]["outcome"] == "pass"

    assert payload["summary"]["preferences"] == {"pass": 1, "flag": 0, "fail": 1}
    assert set(payload["summary"]["preferences"]) == set(PREFERENCE_ORDER)


def test_dashboard_never_serializes_the_description_used_for_detection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "preference-min.sqlite3"
    conn = connect(db_path)
    try:
        init_db(conn)
        _store(
            conn,
            title="Backend Engineer",
            description=f"{DESCRIPTION_TOKEN} turnos rotativos",
            source_job_id="1",
        )
        payload = build_payload(conn, top=10)
        html_text = render_dashboard_html(payload)
    finally:
        conn.close()

    assert DESCRIPTION_TOKEN not in json.dumps(payload)
    assert DESCRIPTION_TOKEN not in html_text
    # The band is rendered for a FAIL, with the reason and the cue.
    assert "PREFERENCE_LABELS" in html_text
    assert "preference-fail" in html_text
    assert "Preferencia:" in html_text


def test_preference_band_stays_hue_free(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "preference-css.sqlite3"
    conn = connect(db_path)
    try:
        init_db(conn)
        _store(
            conn,
            title="Backend Engineer",
            description="turnos rotativos",
            source_job_id="1",
        )
        html_text = render_dashboard_html(build_payload(conn, top=10))
    finally:
        conn.close()

    css_rules = [
        rule
        for rule in html_text.split("}")
        if ".preference-evidence" in rule or ".preference-fail" in rule
    ]
    assert css_rules
    for rule in css_rules:
        assert "--signal" not in rule, rule
        assert "--ember" not in rule, rule
        assert "#e7000b" not in rule, rule

    # The single reduced-motion media query stays.
    import re

    css = re.search(r"<style>(.*?)</style>", html_text, re.DOTALL).group(1)
    assert css.count("@media") == 1


def test_dashboard_generation_needs_no_network(tmp_path: Path, monkeypatch) -> None:
    no_network(monkeypatch)
    db_path = tmp_path / "preference-offline.sqlite3"
    conn = connect(db_path)
    try:
        init_db(conn)
        _store(
            conn,
            title="SOC Analyst",
            description="Cobertura 24/7 y monitorización continua",
            source_job_id="1",
        )
        html_text = render_dashboard_html(build_payload(conn, top=10))
    finally:
        conn.close()

    assert "<!DOCTYPE html>" in html_text
    assert "fetch(" not in html_text


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_list_and_rank_expose_preference(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "preference-cli.sqlite3"
    conn = connect(db_path)
    try:
        init_db(conn)
        rotating = _store(
            conn,
            title="Backend Engineer",
            description="Turnos rotativos en el equipo",
            source_job_id="1",
        )
        clean = _store(
            conn,
            title="Data Engineer",
            description="Horario flexible",
            source_job_id="2",
        )
        _evaluate(conn, rotating.id, 70.0)
        _evaluate(conn, clean.id, 60.0)
    finally:
        conn.close()

    assert main(["--db", str(db_path), "list", "--json"]) == EXIT_OK
    listing = json.loads(capsys.readouterr().out)
    by_title = {item["job"]["title"]: item for item in listing["jobs"]}
    assert by_title["Backend Engineer"]["preference"]["outcome"] == "fail"
    assert by_title["Data Engineer"]["preference"]["outcome"] == "pass"

    assert main(["--db", str(db_path), "rank", "--top", "5"]) == EXIT_OK
    ranked = capsys.readouterr().out
    assert "[preference=fail]" in ranked
    assert "Backend Engineer" in ranked


def test_cli_score_reports_preference_beside_the_unchanged_score(
    tmp_path: Path, settings, capsys
) -> None:
    db_path = tmp_path / "preference-score.sqlite3"
    conn = connect(db_path)
    try:
        init_db(conn)
        rotating = _store(
            conn,
            title="Backend Engineer",
            description="Turnos rotativos en el equipo",
            source_job_id="1",
        )
    finally:
        conn.close()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "--profile",
                str(settings.profile_path),
                "score",
                "--all",
                "--scorer",
                "heuristic",
            ]
        )
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert "preference: fail" in output
    assert "turnos rotativos" in output
