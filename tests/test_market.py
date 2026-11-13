"""Market tab aggregates: repeated companies, technologies and salaries.

Every test runs offline against a temporary SQLite database. The market block
is also checked to carry aggregates only, never a description or a per-offer
row.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from jobscout.dashboard import build_payload, generate_dashboard
from jobscout.db import connect, init_db, insert_evaluation
from jobscout.market import (
    MARKET_MIN_SCORE,
    build_market,
    is_senior_offer,
)
from jobscout.models import Evaluation, Recommendation, ScorerName
from jobscout.scoring.profile import Profile

DESCRIPTION_MARKER = "DESCRIPTION-MARKER-should-stay-out-of-the-payload"
_SERIAL = 0


@pytest.fixture
def market_db(tmp_path: Path) -> Path:
    path = tmp_path / "market.sqlite3"
    conn = connect(path)
    try:
        init_db(conn)
    finally:
        conn.close()
    return path


def _add_job(
    conn: sqlite3.Connection,
    *,
    title: str = "Cloud Engineer",
    company: str = "Acme",
    description: str = "",
    seniority: str | None = None,
    salary: str | None = None,
    is_active: int = 1,
    last_seen: str = "2026-09-15T10:00:00Z",
) -> int:
    global _SERIAL
    _SERIAL += 1
    serial = _SERIAL
    cursor = conn.execute(
        """
        INSERT INTO jobs (
            fingerprint, dedupe_key, source, source_job_id, title, company,
            description, seniority, salary, first_seen_at, last_seen_at, is_active
        ) VALUES (?, ?, 'linkedin', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"fp-{serial}",
            f"dk-{serial}",
            f"serial-{serial}",
            title,
            company,
            description,
            seniority,
            salary,
            "2026-09-10T10:00:00Z",
            last_seen,
            is_active,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _evaluate(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    score: float = 70.0,
    employer_tier: str | None = None,
    mid: int | None = None,
) -> None:
    low = mid - 2000 if mid is not None else None
    high = mid + 2000 if mid is not None else None
    insert_evaluation(
        conn,
        Evaluation(
            job_id=job_id,
            scorer=ScorerName.OPENCODE,
            overall_score=score,
            recommendation=Recommendation.APPLY,
            confidence=0.7,
            employer_tier=employer_tier,
            salary_estimate_low=low,
            salary_estimate_mid=mid,
            salary_estimate_high=high,
        ),
    )


def _market(path: Path, profile: Profile | None = None) -> dict:
    conn = connect(path)
    try:
        return build_market(conn, profile)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


def test_repeated_company_counts_closed_offers_and_skips_senior(market_db: Path) -> None:
    conn = connect(market_db)
    try:
        _add_job(conn, title="DevOps Engineer", company="Acme Cloud")
        _add_job(
            conn,
            title="Cloud Engineer",
            company="Acme Cloud",
            is_active=0,
            last_seen="2026-09-20T10:00:00Z",
        )
        # Normalized spelling joins the same group; the most frequent label wins.
        _add_job(conn, title="Cloud Support", company="acme  cloud")
        # Excluded by title and by the stored seniority column.
        _add_job(conn, title="Senior Cloud Engineer", company="Acme Cloud")
        _add_job(
            conn,
            title="Platform Engineer",
            company="Acme Cloud",
            seniority="senior",
        )
        # A single offer company never reaches the table.
        _add_job(conn, title="Cloud Engineer", company="Solo Ltd")
    finally:
        conn.close()

    rows = _market(market_db)["companies"]

    assert [row["company"] for row in rows] == ["Acme Cloud"]
    row = rows[0]
    assert row["offers"] == 3
    assert row["active"] == 2
    assert row["last_seen"] == "2026-09-20"


def test_company_rows_sort_by_offers_then_last_seen_and_keep_best_score(
    market_db: Path,
) -> None:
    conn = connect(market_db)
    try:
        first = _add_job(conn, company="Beta", title="Cloud Engineer")
        second = _add_job(
            conn, company="Beta", title="DevOps Engineer", last_seen="2026-09-18T10:00:00Z"
        )
        for index in range(2):
            job = _add_job(
                conn,
                company="Alpha",
                title="Cloud Engineer",
                last_seen=f"2026-09-1{index}T10:00:00Z",
            )
            _evaluate(conn, job, score=61.0 + index)
        _evaluate(conn, first, score=80.0)
        _evaluate(conn, second, score=55.0)
    finally:
        conn.close()

    rows = _market(market_db)["companies"]

    # Beta sorts first: more offers.
    assert [row["company"] for row in rows] == ["Beta", "Alpha"]
    assert rows[0]["score"] == 80.0
    assert rows[0]["last_seen"] == "2026-09-18"
    assert all(row["score"] is not None for row in rows)


def test_company_without_evaluation_shows_no_score(market_db: Path) -> None:
    conn = connect(market_db)
    try:
        _add_job(conn, company="Gamma")
        _add_job(conn, company="Gamma", title="DevOps Engineer")
    finally:
        conn.close()

    row = _market(market_db)["companies"][0]

    assert row["score"] is None


# ---------------------------------------------------------------------------
# Technologies
# ---------------------------------------------------------------------------


def test_technology_alias_counts_one_offer_once(market_db: Path) -> None:
    conn = connect(market_db)
    try:
        job = _add_job(
            conn,
            description="Kubernetes, K8s and Kubernetes again. AWS and Amazon Web Services.",
        )
        _evaluate(conn, job, score=70.0)
        # Below the threshold: its technologies never reach the table.
        low = _add_job(conn, description="Terraform and Docker")
        _evaluate(conn, low, score=MARKET_MIN_SCORE - 1)
    finally:
        conn.close()

    technologies = _market(market_db)["technologies"]
    counts = {row["name"]: row["offers"] for row in technologies["rows"]}

    assert technologies["scored_offers"] == 1
    assert counts["Kubernetes"] == 1
    assert counts["AWS"] == 1
    assert "Terraform" not in counts


def test_technology_percentage_uses_the_scored_offer_total(market_db: Path) -> None:
    conn = connect(market_db)
    try:
        docker = _add_job(conn, description="Docker in production")
        _evaluate(conn, docker, score=90.0)
        other = _add_job(conn, description="A role with no listed tools")
        _evaluate(conn, other, score=65.0)
        # A 60+ offer with no text cannot mention a technology: it stays out of
        # the denominator, and so does the "N ofertas de 60+" line.
        empty = _add_job(conn, title="", description="")
        _evaluate(conn, empty, score=65.0)
    finally:
        conn.close()

    technologies = _market(market_db)["technologies"]
    docker_row = next(row for row in technologies["rows"] if row["name"] == "Docker")

    assert technologies["scored_offers"] == 2
    assert docker_row["offers"] == 1
    assert docker_row["pct"] == 50


def test_technology_aliases_do_not_match_false_friends(market_db: Path) -> None:
    conn = connect(market_db)
    try:
        trap = _add_job(
            conn,
            description=(
                "Join the rest of the team, write to acme.net, and take the helm."
            ),
        )
        _evaluate(conn, trap, score=80.0)
    finally:
        conn.close()

    names = {row["name"] for row in _market(market_db)["technologies"]["rows"]}

    # "rest" is not REST, "acme.net" is not .NET, "the helm" is not Helm, and a
    # lone Vault or Mongo mention no longer counts either.
    assert "REST API" not in names
    assert ".NET" not in names
    assert "Helm" not in names
    assert "Vault" not in names
    assert "MongoDB" not in names


def test_technology_aliases_match_real_terms(market_db: Path) -> None:
    conn = connect(market_db)
    try:
        job = _add_job(
            conn,
            description=(
                "Helm charts, HashiCorp Vault, MongoDB and .NET behind a REST API"
            ),
        )
        _evaluate(conn, job, score=80.0)
    finally:
        conn.close()

    names = {row["name"] for row in _market(market_db)["technologies"]["rows"]}

    assert {"Helm", "Vault", "MongoDB", ".NET", "REST API"} <= names


def test_profile_skills_mark_technologies_without_leaking_the_list(
    market_db: Path,
) -> None:
    conn = connect(market_db)
    try:
        job = _add_job(conn, description="Python and Docker on Linux")
        _evaluate(conn, job, score=80.0)
    finally:
        conn.close()

    profile = Profile(
        must_have_skills=["Python"],
        nice_to_have_skills=["Linux", "Kubernetes"],
    )
    market = _market(market_db, profile)
    flags = {row["name"]: row["in_profile"] for row in market["technologies"]["rows"]}

    assert market["profile_available"] is True
    assert flags["Python"] is True
    assert flags["Linux"] is True
    # Kubernetes is in the profile but no offer asks for it: no row at all.
    assert "Kubernetes" not in flags
    assert flags["Docker"] is False

    no_profile = _market(market_db)
    assert no_profile["profile_available"] is False
    assert all(row["in_profile"] is None for row in no_profile["technologies"]["rows"])
    # Only the booleans travel, never the profile skill list.
    assert "must_have_skills" not in json.dumps(no_profile)


def test_technology_rows_sort_by_offer_count(market_db: Path) -> None:
    conn = connect(market_db)
    try:
        for _ in range(2):
            job = _add_job(conn, description="AWS and Python")
            _evaluate(conn, job)
        only = _add_job(conn, description="Ansible")
        _evaluate(conn, only)
    finally:
        conn.close()

    names = [row["name"] for row in _market(market_db)["technologies"]["rows"]]

    assert names[:2] == ["AWS", "Python"]
    assert names.index("Ansible") > names.index("Python")


# ---------------------------------------------------------------------------
# Salaries
# ---------------------------------------------------------------------------


def test_salary_median_needs_three_estimates_and_counts_stated_salaries(
    market_db: Path,
) -> None:
    conn = connect(market_db)
    try:
        mids = (20000, 24000, 28000)
        for index, mid in enumerate(mids):
            job = _add_job(
                conn,
                title=f"Cloud Engineer {index}",
                salary="30.000 € - 40.000 €" if index == 0 else None,
            )
            _evaluate(conn, job, employer_tier="consultora_grande", mid=mid)
        # Two offers in another tier: no median, but the row and stated count stay.
        for index in range(2):
            job = _add_job(conn, title=f"Platform {index}", salary="35k")
            _evaluate(conn, job, employer_tier="startup", mid=30000)
        # Three offers but only two estimates: the median gate is the estimate
        # count, so this cell still shows no median.
        for index, mid in enumerate((20000, 26000, None)):
            job = _add_job(conn, title=f"Support {index}")
            _evaluate(conn, job, employer_tier="consultora_pequena", mid=mid)
        # Evaluated without a tier: unclassified, never mixed with unscored offers.
        tierless = _add_job(conn, title="Cloud Engineer odd")
        _evaluate(conn, tierless, employer_tier=None, mid=25000)
        # Unknown tier is shown as stored.
        job = _add_job(conn, title="Cloud Engineer raro")
        _evaluate(conn, job, employer_tier="raro", mid=25000)
        # No successful evaluation: excluded from the table, counted separately.
        _add_job(conn, title="Intern Cloud", salary="18k")
    finally:
        conn.close()

    market = _market(market_db)
    rows = market["salaries"]
    by_key = {(row["tier"], row["level"]): row for row in rows}

    grande = by_key[("consultora_grande", "junior")]
    assert grande["offers"] == 3
    assert grande["median"] == 24000
    assert grande["stated"] == 1

    startup = by_key[("startup", "junior")]
    assert startup["offers"] == 2
    assert startup["median"] is None
    assert startup["stated"] == 2

    pequena = by_key[("consultora_pequena", "junior")]
    assert pequena["offers"] == 3
    assert pequena["median"] is None

    unclassified = by_key[("sin clasificar", "junior")]
    assert unclassified["offers"] == 1
    assert unclassified["median"] is None

    assert by_key[("raro", "junior")]["offers"] == 1
    # The unclassified tier sorts last.
    assert rows[-1]["tier"] == "sin clasificar"
    # The unscored offer is not in any cell; it has its own count.
    assert market["unscored_offers"] == 1


def test_salary_level_splits_on_the_same_seniority_rule(market_db: Path) -> None:
    conn = connect(market_db)
    try:
        junior = _add_job(conn, title="Cloud Engineer")
        _evaluate(conn, junior, employer_tier="startup", mid=20000)
        senior = _add_job(conn, title="Senior Cloud Engineer")
        _evaluate(conn, senior, employer_tier="startup", mid=50000)
        stored = _add_job(conn, title="Platform Engineer", seniority="lead")
        _evaluate(conn, stored, employer_tier="startup", mid=60000)
    finally:
        conn.close()

    levels = {
        row["level"]: row for row in _market(market_db)["salaries"]
    }

    assert levels["junior"]["offers"] == 1
    assert levels["senior"]["offers"] == 2


def test_is_senior_offer_combines_title_and_stored_seniority() -> None:
    assert is_senior_offer("Senior DevOps Engineer", None) is True
    assert is_senior_offer("DevOps Engineer", "senior") is True
    assert is_senior_offer("DevOps Engineer", None) is False
    # A junior marker cancels the title filter, as in the offers tab.
    assert is_senior_offer("Junior Lead Developer", None) is False


# ---------------------------------------------------------------------------
# Empty database and payload shape
# ---------------------------------------------------------------------------


def test_empty_database_produces_empty_market_blocks(market_db: Path) -> None:
    market = _market(market_db)

    assert market["companies"] == []
    assert market["technologies"] == {"scored_offers": 0, "rows": []}
    assert market["salaries"] == []
    assert market["unscored_offers"] == 0
    assert market["profile_available"] is False


def test_empty_market_still_renders_and_explains_itself(market_db: Path, tmp_path: Path) -> None:
    output = tmp_path / "report.html"
    generate_dashboard(market_db, output)
    document = output.read_text(encoding="utf-8")

    assert 'id="panel-market" role="tabpanel" aria-labelledby="tab-market"' in document
    assert 'aria-controls="panel-market"' in document
    # The score threshold is painted from the payload, never hard-coded twice.
    assert "Aún no hay ofertas de 60+ puntos" in document
    assert "llega a 60 puntos" in document
    assert 'EMPLOYER_TIER_LABELS[item.tier] || item.tier' in document
    assert 'item.level === "junior" ? "no senior" : item.level' in document
    assert "ofertas aún sin puntuar" in document


def test_payload_carries_market_aggregates_only(market_db: Path) -> None:
    conn = connect(market_db)
    try:
        job = _add_job(conn, company="Acme", description=DESCRIPTION_MARKER)
        _evaluate(conn, job, score=90.0)
        _add_job(conn, company="Acme")
    finally:
        conn.close()

    conn = connect(market_db)
    try:
        payload = build_payload(conn, top=None)
    finally:
        conn.close()

    market = payload["market"]
    assert set(market) == {
        "min_score",
        "companies",
        "technologies",
        "salaries",
        "unscored_offers",
        "profile_available",
    }
    assert market["min_score"] == 60
    assert market["unscored_offers"] == 1
    assert [row["company"] for row in market["companies"]] == ["Acme"]
    assert DESCRIPTION_MARKER not in json.dumps(market)
    # No per-offer row or description key ever appears in the block.
    for row in market["companies"] + market["technologies"]["rows"] + market["salaries"]:
        assert "description" not in row
