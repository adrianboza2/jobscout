"""Salary extraction from LinkedIn descriptions and the offline backfill.

Every path here is offline: the fetch tests serve a captured fixture through the
fake transport, and the backfill only reads stored descriptions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeHttpClient, FIXTURES, fixture_text, response

from jobscout.cli import EXIT_OK, main
from jobscout.collectors.linkedin_details import (
    fetch_linkedin_details,
    parse_description_salary,
    parse_detail_description,
)
from jobscout.db import connect, get_job, init_db, insert_raw_document, store_posting
from jobscout.models import RawDocument, RawDocumentKind
from jobscout.normalize import normalize_job_posting
from jobscout.pipeline import ingest_detail_results

REAL_SALARY_PAGE = FIXTURES / "linkedin_detail_page_real_salary.html"
PLAIN_PAGE = FIXTURES / "linkedin_detail_page.html"
TECNOEMPLEO_SALARY_PAGE = FIXTURES / "tecnoempleo_detail_page_salary_real.html"


def _store_job(conn, source_job_id: str, **overrides):
    payload = {
        "source": "linkedin",
        "source_job_id": source_job_id,
        "jobUrl": f"https://www.linkedin.com/jobs/view/{source_job_id}/role",
        "jobTitle": "Senior Backend Engineer",
        "companyName": "Acme Data Inc.",
        # Unique per job id so the weak dedupe key does not merge distinct cards.
        "location": f"Remote, Argentina ({source_job_id})",
        "description": "A stored description.",
    }
    payload.update(overrides)
    posting = normalize_job_posting(payload, source=payload["source"])
    job, _ = store_posting(conn, posting, now="2026-09-15T10:00:00Z")
    return job


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


def test_real_capture_yields_the_stated_range() -> None:
    html = REAL_SALARY_PAGE.read_text(encoding="utf-8")
    description = parse_detail_description(html)

    assert parse_description_salary(description) == "€25,000 – €35,000"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        (
            "Salario entre 32.000 € y 38.000 € brutos anuales.",
            "32.000 € y 38.000 €",
        ),
        ("€62.800—€82.400 EUR", "€62.800—€82.400 EUR"),
        ("Salary (to €120,000 negotiable)", "to €120,000"),
        ("Sueldo bruto / año 42.000€ en 12 pagas", "42.000€"),
        ("Rango salarial: 36 000€ - 42 000€ BA", "36 000€ - 42 000€"),
        (
            "SALARIO: En función de experiencia, máximo 25.000 euros bruto/año.",
            "máximo 25.000 euros",
        ),
        (
            "El salario bruto anual fijo para esta posición se sitúa entre "
            "€ 40.000,00 y € 50.000,00, en función de tu experiencia",
            "€ 40.000,00 y € 50.000,00",
        ),
        (
            "Salary: We are actively working to align our budgets with market "
            "standards, offering highly competitive salary packages. For this "
            "position we estimate between 55,000€ and 65,000€ (annually).",
            "55,000€ and 65,000€",
        ),
        ("Salario: Hasta 50.000 € brutos anuales", "Hasta 50.000 €"),
    ],
)
def test_real_description_lines(description: str, expected: str) -> None:
    assert parse_description_salary(description) == expected


@pytest.mark.parametrize(
    "description",
    [
        "1150€ gross per month.",
        "🤸 Up to 25 EUR per month for a gym subscription.",
        "With a turnover of €320 million and a staff of 1,200, it exports "
        "approximately 40%",
        "📙 Grow your skills We offer up to €2000/year in training",
        "Ayuda comida (1200€ b/a).",
        "Además, de forma extra contarás con 1.100€ anuales para que puedas "
        "ampliar aún más tu expertise.",
        "",
        None,
    ],
)
def test_non_salary_lines_are_ignored(description) -> None:
    assert parse_description_salary(description) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Verbatim from a stored Tecnoempleo description.
        ("Banda Salarial de 39.000 €BA a 42.000 €BA", "39.000 €BA a 42.000 €"),
        ("Salario 40.000€B/A - 50.000€B/A", "40.000€B/A - 50.000€"),
        ("Salario: 40.000 € brutos a 50.000 € brutos anuales", "40.000 € brutos a 50.000 €"),
    ],
)
def test_gross_marker_glued_to_the_left_amount_keeps_the_range(text, expected) -> None:
    assert parse_description_salary(text) == expected


def test_first_qualifying_line_wins() -> None:
    description = "Junior: €28,000 – €41,000\nSenior: €42,000 – €52,000"

    assert parse_description_salary(description) == "€28,000 – €41,000"


# ---------------------------------------------------------------------------
# Fetch path
# ---------------------------------------------------------------------------


def test_fetch_ingests_salary_from_description(conn, settings) -> None:
    job = _store_job(conn, "3911111111")
    body = REAL_SALARY_PAGE.read_text(encoding="utf-8")
    client = FakeHttpClient(
        [response(200, body, headers={"content-type": "text/html"})]
    )
    result = fetch_linkedin_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
    )

    assert result.items[0].salary == "€25,000 – €35,000"
    ingest_detail_results(conn, result, settings=settings)
    assert get_job(conn, job.id).salary == "€25,000 – €35,000"


def test_fetch_without_a_pay_range_leaves_salary_empty(conn, settings) -> None:
    job = _store_job(conn, "3911111111")
    client = FakeHttpClient(
        [
            response(
                200,
                fixture_text("linkedin_detail_page.html"),
                headers={"content-type": "text/html"},
            )
        ]
    )
    result = fetch_linkedin_details(
        [job],
        settings=settings,
        network_enabled=True,
        client=client,
        sleep=lambda _: None,
    )

    assert result.items[0].salary is None
    ingest_detail_results(conn, result, settings=settings)
    assert get_job(conn, job.id).salary is None


# ---------------------------------------------------------------------------
# Offline backfill
# ---------------------------------------------------------------------------


def test_backfill_salary_fills_only_missing_salaries(settings, capsys) -> None:
    conn = connect(settings.db_path)
    init_db(conn)
    to_fill = _store_job(
        conn,
        "3611111111",
        description="Rango salarial: 36 000€ - 42 000€ BA",
    )
    kept = _store_job(
        conn,
        "3622222222",
        description="Salario: Hasta 50.000 € brutos anuales",
        salary="existing value",
    )
    foreign = _store_job(
        conn,
        "36-foreign",
        source="tecnoempleo",
        description="Sueldo bruto / año 42.000€ en 12 pagas",
    )
    conn.close()

    # A dry run reports the match and writes nothing.
    assert (
        main(["--db", str(settings.db_path), "backfill-salary", "--dry-run"])
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert (
        "examined 2 offer(s) (LinkedIn 1, Tecnoempleo 1), 2 would get a salary"
        in output
    )
    assert f"#{to_fill.id} 36 000€ - 42 000€" in output
    assert f"#{foreign.id} 42.000€" in output
    conn = connect(settings.db_path)
    assert get_job(conn, to_fill.id).salary is None
    conn.close()

    assert main(["--db", str(settings.db_path), "backfill-salary"]) == EXIT_OK
    output = capsys.readouterr().out
    assert (
        "examined 2 offer(s) (LinkedIn 1, Tecnoempleo 1), 2 got a salary" in output
    )

    conn = connect(settings.db_path)
    assert get_job(conn, to_fill.id).salary == "36 000€ - 42 000€"
    assert get_job(conn, kept.id).salary == "existing value"
    assert get_job(conn, foreign.id).salary == "42.000€"
    conn.close()


def test_backfill_tecnoempleo_falls_back_to_the_stored_detail_page(settings, capsys) -> None:
    conn = connect(settings.db_path)
    init_db(conn)
    from_page_url = "https://www.tecnoempleo.com/backend-python/rf-aaaaaaaaaaaaaaaaaaaa"
    from_page = _store_job(
        conn,
        "rf-aaaaaaaaaaaaaaaaaaaa",
        source="tecnoempleo",
        jobUrl=from_page_url,
        description="Backend Python en Madrid, equipo pequeño y producto propio.",
    )
    insert_raw_document(
        conn,
        RawDocument(
            kind=RawDocumentKind.JOB_DETAIL,
            source="tecnoempleo",
            url=from_page_url,
            sha256="tecnoempleo-detail-body",
            byte_size=1,
            stored_at="2026-09-15T10:00:00Z",
            body=TECNOEMPLEO_SALARY_PAGE.read_text(encoding="utf-8"),
        ),
    )
    from_description = _store_job(
        conn,
        "rf-bbbbbbbbbbbbbbbbbbbb",
        source="tecnoempleo",
        jobUrl="https://www.tecnoempleo.com/backend-python/rf-bbbbbbbbbbbbbbbbbbbb",
        description="Sueldo bruto / año 42.000€ en 12 pagas",
    )
    kept = _store_job(
        conn,
        "rf-cccccccccccccccccccc",
        source="tecnoempleo",
        jobUrl="https://www.tecnoempleo.com/backend-python/rf-cccccccccccccccccccc",
        description="Salario: Hasta 50.000 € brutos anuales",
        salary="existing value",
    )
    conn.close()

    assert (
        main(["--db", str(settings.db_path), "backfill-salary", "--dry-run"])
        == EXIT_OK
    )
    output = capsys.readouterr().out
    assert (
        "examined 2 offer(s) (LinkedIn 0, Tecnoempleo 2), 2 would get a salary"
        in output
    )
    assert f"#{from_page.id} 39.000 € - 45.000 € Bruto/año" in output
    assert f"#{from_description.id} 42.000€" in output
    conn = connect(settings.db_path)
    assert get_job(conn, from_page.id).salary is None
    conn.close()

    assert main(["--db", str(settings.db_path), "backfill-salary"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "examined 2 offer(s) (LinkedIn 0, Tecnoempleo 2), 2 got a salary" in output

    conn = connect(settings.db_path)
    assert get_job(conn, from_page.id).salary == "39.000 € - 45.000 € Bruto/año"
    assert get_job(conn, from_description.id).salary == "42.000€"
    assert get_job(conn, kept.id).salary == "existing value"
    conn.close()
