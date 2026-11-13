"""Tests for the read-time seniority title pre-filter."""

from __future__ import annotations

import pytest

from jobscout.seniority import seniority_filter_reason


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Platform Engineer", "title: senior"),
        ("Sr. Cloud Engineer", "title: sr"),
        ("SRE Engineer", None),
        ("Cloud Security Architect", None),
        ("Arquitecto Cloud Azure", None),
        ("Senior Cloud Architect", "title: senior"),
        ("Jefe de Proyecto IT", "title: jefe"),
        ("Engineering Manager", "title: manager"),
        ("Staff Engineer", "title: staff"),
        ("Junior Lead Developer", None),
        ("Beca Arquitecto de Sistemas", None),
        ("Prácticas Senior Analyst", None),
        ("Senior Intern Program", None),
        ("Técnico de Sistemas", None),
        ("Staffing Coordinator", None),
        ("Headquarters IT Support", None),
        ("Team Leader Helpdesk", None),
        ("", None),
    ],
)
def test_seniority_filter_reason_matches_the_spec_table(
    title: str, expected: str | None
) -> None:
    assert seniority_filter_reason(title) == expected


def test_seniority_filter_reason_handles_none_titles() -> None:
    assert seniority_filter_reason(None) is None


def test_seniority_filter_reason_is_case_insensitive_and_keeps_the_list_spelling() -> None:
    assert seniority_filter_reason("SENIOR DATA ENGINEER") == "title: senior"
    assert seniority_filter_reason("Principal Architect") == "title: principal"


def test_seniority_filter_reason_accepts_practicas_without_the_accent() -> None:
    assert seniority_filter_reason("Practicas Senior Analyst") is None
