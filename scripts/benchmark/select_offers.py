"""Build the benchmark offer selection from the local database.

Reads the real database read-only (``mode=ro`` URI) and writes a small JSON
with the three groups bench.py expects: A (your apply offers), B (your
reject/consider offers) and C (active senior/lead offers you have not judged).
Sampling is seeded so a rerun on the same database picks the same offers.
"""
import argparse
import json
import random
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open an existing database without any write capability."""
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _ids(rows) -> list[int]:
    return [int(row[0]) for row in rows]


def select(db_path: Path) -> dict[str, list[int]]:
    """Return the A/B/C job ids for the benchmark."""
    conn = _connect_ro(db_path)
    try:
        # A: active, enriched offers you marked as apply. The seed keeps the
        # selection stable across reruns while still spreading it over ids.
        a_rows = conn.execute(
            """
            SELECT j.id FROM jobs j
            JOIN job_feedback f ON f.job_id = j.id
            WHERE j.is_active = 1
              AND LENGTH(COALESCE(j.description, '')) > 800
              AND f.decision = 'apply'
            ORDER BY j.id
            """
        ).fetchall()
        rng_a = random.Random(20261020)
        a = sorted(rng_a.sample(_ids(a_rows), min(8, len(a_rows))))

        # B: offers you already rejected or put on hold, as negative examples.
        b_rows = conn.execute(
            """
            SELECT j.id FROM jobs j
            JOIN job_feedback f ON f.job_id = j.id
            WHERE j.is_active = 1
              AND LENGTH(COALESCE(j.description, '')) > 800
              AND f.decision IN ('reject', 'consider')
            ORDER BY j.id
            """
        ).fetchall()
        rng_b = random.Random(7)
        b = sorted(rng_b.sample(_ids(b_rows), min(6, len(b_rows))))

        # C: active senior/lead roles you have not judged. Ordered by their
        # mean successful score ascending so the group leans towards offers the
        # model itself considered a poor fit. A mean requires a scored
        # evaluation, so unscored offers are not eligible.
        c_rows = conn.execute(
            """
            SELECT j.id
            FROM jobs j
            JOIN evaluations e ON e.job_id = j.id
            WHERE j.is_active = 1
              AND LENGTH(COALESCE(j.description, '')) > 800
              AND NOT EXISTS (
                  SELECT 1 FROM job_feedback f WHERE f.job_id = j.id
              )
              AND e.status = 'ok'
              AND e.overall_score IS NOT NULL
              AND (
                  LOWER(j.title) LIKE '%senior%'
                  OR LOWER(j.title) LIKE '%lead%'
                  OR LOWER(j.title) LIKE '%architect%'
                  OR LOWER(j.title) LIKE '%principal%'
                  OR LOWER(j.title) LIKE '%manager%'
              )
            GROUP BY j.id
            ORDER BY AVG(e.overall_score) ASC, j.id ASC
            LIMIT 4
            """
        ).fetchall()
        c = _ids(c_rows)
    finally:
        conn.close()

    return {"A": a, "B": b, "C": c}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=REPO / "data" / "jobscout.sqlite3")
    parser.add_argument("--out", type=Path, default=HERE / "selection.json")
    args = parser.parse_args()

    selection = select(args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
    print("wrote", args.out)


if __name__ == "__main__":
    main()
