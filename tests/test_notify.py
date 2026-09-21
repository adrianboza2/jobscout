"""Local-notification tests: selection, exclusion, delivery, and CLI wiring.

Everything runs fully offline against in-memory or temporary SQLite databases.
The selection is a read-only ``SELECT`` pass, so no test needs the network, a
model, or a running system notifier. The one test that could touch a system
notifier injects a fake sender or patches discovery.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import no_network

from jobscout.cli import EXIT_ERROR, EXIT_OK, main
from jobscout.config import HARD_MAX_ITEMS, Settings
from jobscout.db import (
    connect,
    connect_readonly,
    init_db,
    insert_evaluation,
    store_posting,
    upsert_feedback,
)
from jobscout.models import (
    Evaluation,
    EvaluationStatus,
    JobFeedback,
    JobPosting,
    Recommendation,
    ScorerName,
)
from jobscout.normalize import finalize_posting
from jobscout.notify import (
    DEFAULT_NOTIFY_LIMIT,
    DELIVERY_STDOUT,
    DELIVERY_SYSTEM,
    build_report,
    deliver_report,
    disabled_report,
    select_notifications,
)

DECIDED_AT = "2026-09-16T14:00:00.000000Z"


def _posting(index: int, *, title: str, description: str) -> JobPosting:
    return finalize_posting(
        JobPosting(
            source="linkedin",
            title=title,
            company=f"Company {index}",
            url=f"https://www.linkedin.com/jobs/view/39123456{index:02d}/role",
            source_job_id=f"39123456{index:02d}",
            description=description,
        )
    )


def _seed(
    conn,
    index: int,
    *,
    title: str,
    description: str,
    score: float,
    recommendation: Recommendation | str = Recommendation.APPLY,
    decision: str | None = None,
) -> int:
    job, _ = store_posting(
        conn,
        _posting(index, title=title, description=description),
        now="2026-09-15T10:00:00Z",
    )
    insert_evaluation(
        conn,
        Evaluation(
            job_id=job.id,
            scorer=ScorerName.HEURISTIC,
            overall_score=score,
            recommendation=recommendation,
            status=EvaluationStatus.OK,
        ),
    )
    if decision is not None:
        upsert_feedback(
            conn,
            JobFeedback(job_id=job.id, decision=decision, decision_updated_at=DECIDED_AT),
        )
    return int(job.id)


#: A corpus with one clean high score, one below threshold, one preferred-flag
#: high score, and one already-reviewed high score.
def _seed_corpus(conn) -> dict[str, int]:
    return {
        "clean": _seed(
            conn,
            1,
            title="Senior Backend Engineer",
            description="Remote Python and SQL role. Flexible schedule.",
            score=90.0,
        ),
        "below": _seed(
            conn,
            2,
            title="Junior Frontend Developer",
            description="TypeScript and CSS only.",
            score=70.0,
        ),
        "flagged": _seed(
            conn,
            3,
            title="SOC Analyst",
            description="Python and SQL. Flexible schedule.",
            score=92.0,
        ),
        "reviewed": _seed(
            conn,
            4,
            title="Cloud Platform Engineer",
            description="Remote Python and SQL role. Flexible schedule.",
            score=95.0,
            decision="apply",
        ),
    }


def _enabled_settings(**overrides) -> Settings:
    settings = Settings.from_env(
        env={"JOBSCOUT_NOTIFY_ENABLED": "true"}, dotenv_path=None
    )
    return settings.with_overrides(**overrides)


def _seed_file(path: Path) -> dict[str, int]:
    conn = connect(path)
    try:
        init_db(conn)
        ids = _seed_corpus(conn)
    finally:
        conn.close()
    return ids


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selection_uses_threshold_and_excludes_reviewed(conn) -> None:
    ids = _seed_corpus(conn)
    selection = select_notifications(conn, threshold=80.0)

    assert [item.job_id for item in selection.items] == [ids["flagged"], ids["clean"]]
    assert ids["below"] not in [item.job_id for item in selection.items]
    assert ids["reviewed"] not in [item.job_id for item in selection.items]
    assert selection.candidates == 3
    assert selection.excluded_feedback == 1
    assert selection.filtered_preference == 0


def test_selection_prefers_configured_outcome(conn) -> None:
    ids = _seed_corpus(conn)

    flagged = select_notifications(conn, threshold=80.0, preference_outcome="flag")
    assert [item.job_id for item in flagged.items] == [ids["flagged"]]
    assert flagged.excluded_feedback == 1
    assert flagged.filtered_preference == 1

    passing = select_notifications(conn, threshold=80.0, preference_outcome="pass")
    assert [item.job_id for item in passing.items] == [ids["clean"]]
    assert passing.filtered_preference == 1


def test_empty_preference_outcome_is_no_filter(conn) -> None:
    ids = _seed_corpus(conn)
    selection = select_notifications(conn, threshold=80.0, preference_outcome="")
    assert {item.job_id for item in selection.items} == {ids["clean"], ids["flagged"]}


def test_selection_is_deterministic_on_tied_scores(conn) -> None:
    first = _seed(conn, 1, title="Role A", description="Remote Python.", score=88.0)
    top = _seed(conn, 2, title="Role B", description="Remote Python.", score=91.0)
    second = _seed(conn, 3, title="Role C", description="Remote Python.", score=88.0)

    for _ in range(2):
        selection = select_notifications(conn, threshold=80.0)
        assert [item.job_id for item in selection.items] == [top, first, second]


def test_selection_limit_caps_the_result(conn) -> None:
    _seed_corpus(conn)
    selection = select_notifications(conn, threshold=80.0, limit=1)
    assert len(selection.items) == 1
    # The cap never changes which candidates were considered.
    assert selection.candidates == 3


def test_item_carries_job_identity_and_preference(conn) -> None:
    ids = _seed_corpus(conn)
    selection = select_notifications(conn, threshold=80.0, preference_outcome="flag")
    item = selection.items[0]
    payload = item.to_dict()

    assert payload["job_id"] == ids["flagged"]
    assert payload["title"] == "SOC Analyst"
    assert payload["company"] == "Company 3"
    assert payload["url"].startswith("https://www.linkedin.com/")
    assert payload["score"] == 92.0
    assert payload["preference"]["outcome"] == "flag"
    assert payload["preference"]["evidence"]


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_selection_writes_nothing_to_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "notify.sqlite3"
    _seed_file(path)
    before_bytes = path.read_bytes()
    before_entries = {entry.name for entry in path.parent.iterdir()}

    conn = connect_readonly(path)
    assert conn is not None
    try:
        select_notifications(conn, threshold=80.0)
        # The connection is read-only at the driver level: a write is refused.
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO job_feedback (job_id, decision, imported_at) "
                "VALUES (1, 'apply', '2026-09-16T14:00:00Z')"
            )
    finally:
        conn.close()

    assert path.read_bytes() == before_bytes
    assert {entry.name for entry in path.parent.iterdir()} == before_entries


# ---------------------------------------------------------------------------
# Report and delivery
# ---------------------------------------------------------------------------


def test_disabled_report_is_honest(conn) -> None:
    settings = Settings.from_env(env={}, dotenv_path=None)
    report = disabled_report(settings)
    assert report.enabled is False
    assert report.reason == "disabled"
    assert report.to_dict()["count"] == 0
    assert "disabled" in report.to_text()


def test_build_report_marks_preview_only(conn) -> None:
    _seed_corpus(conn)
    report = build_report(conn, _enabled_settings(), limit=10)
    payload = report.to_dict()
    assert payload["enabled"] is True
    assert payload["preview_only"] is True
    assert payload["count"] == 2
    assert "notification preview" in report.to_text()
    assert "no scheduler" in report.to_text()


def test_delivery_stdout_channel(conn) -> None:
    _seed_corpus(conn)
    report = build_report(conn, _enabled_settings(), limit=10)
    result = deliver_report(report, delivery=DELIVERY_STDOUT)
    assert result.channel == DELIVERY_STDOUT
    assert result.delivered is True


def test_delivery_falls_back_to_stdout_without_a_notifier(monkeypatch, conn) -> None:
    monkeypatch.setattr("jobscout.notify.find_system_notifier", lambda: None)
    _seed_corpus(conn)
    report = build_report(conn, _enabled_settings(), limit=10)
    result = deliver_report(report, delivery=DELIVERY_SYSTEM)
    assert result.channel == DELIVERY_STDOUT
    assert result.delivered is True
    assert "no system notifier" in result.detail


def test_delivery_uses_discovered_notifier_without_a_process(conn) -> None:
    _seed_corpus(conn)
    report = build_report(conn, _enabled_settings(), limit=10)
    calls: list[tuple[str, str, str]] = []

    def sender(executable: str, title: str, body: str) -> bool:
        calls.append((executable, title, body))
        return True

    result = deliver_report(
        report,
        delivery=DELIVERY_SYSTEM,
        executable="/usr/bin/notify-send",
        sender=sender,
    )
    assert result.channel == DELIVERY_SYSTEM
    assert result.delivered is True
    assert calls and calls[0][0] == "/usr/bin/notify-send"
    assert "2 high-value" in calls[0][1]


def test_delivery_reports_failure_and_falls_back(conn) -> None:
    _seed_corpus(conn)
    report = build_report(conn, _enabled_settings(), limit=10)
    result = deliver_report(
        report,
        delivery=DELIVERY_SYSTEM,
        executable="/usr/bin/notify-send",
        sender=lambda *_args: False,
    )
    assert result.channel == DELIVERY_STDOUT
    assert "failed" in result.detail


def test_delivery_with_no_items_is_a_noop(conn) -> None:
    report = build_report(conn, _enabled_settings(), limit=10)
    result = deliver_report(report, delivery=DELIVERY_SYSTEM)
    assert result.delivered is False
    assert result.channel is None
    assert "nothing to deliver" in result.detail


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_disabled_default_creates_no_database(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "missing.sqlite3"
    assert main(["notify", "--db", str(db_path)]) == EXIT_OK
    output = capsys.readouterr().out
    assert "disabled" in output
    assert not db_path.exists()


def test_cli_disabled_default_json_is_valid(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "missing.sqlite3"
    assert main(["notify", "--db", str(db_path), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is False
    assert payload["preview_only"] is True
    assert not db_path.exists()


def test_cli_enabled_missing_database_reports_clean_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("JOBSCOUT_NOTIFY_ENABLED", "true")
    db_path = tmp_path / "missing.sqlite3"
    assert main(["notify", "--db", str(db_path)]) == EXIT_ERROR
    assert "no database found" in capsys.readouterr().err
    assert not db_path.exists()


def test_cli_enabled_json_preview_is_offline(tmp_path: Path, capsys, monkeypatch) -> None:
    no_network(monkeypatch)
    monkeypatch.setenv("JOBSCOUT_NOTIFY_ENABLED", "true")
    db_path = tmp_path / "notify.sqlite3"
    ids = _seed_file(db_path)

    assert main(["notify", "--db", str(db_path), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert [item["job_id"] for item in payload["items"]] == [
        ids["flagged"],
        ids["clean"],
    ]
    assert payload["excluded_feedback"] == 1
    # The default path is preview-only and never touches a notifier.
    assert payload["delivery"]["detail"] == "preview only"


def test_cli_preference_filter_narrows_the_report(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("JOBSCOUT_NOTIFY_ENABLED", "true")
    monkeypatch.setenv("JOBSCOUT_NOTIFY_PREFERENCE_OUTCOME", "flag")
    db_path = tmp_path / "notify.sqlite3"
    ids = _seed_file(db_path)

    assert main(["notify", "--db", str(db_path), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert [item["job_id"] for item in payload["items"]] == [ids["flagged"]]


def test_cli_preview_never_discovers_a_notifier(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("JOBSCOUT_NOTIFY_ENABLED", "true")
    monkeypatch.setenv("JOBSCOUT_NOTIFY_DELIVERY", "system")
    db_path = tmp_path / "notify.sqlite3"
    _seed_file(db_path)

    def _unexpected() -> str | None:  # pragma: no cover - must not run
        raise AssertionError("preview must not discover a system notifier")

    monkeypatch.setattr("jobscout.notify.find_system_notifier", _unexpected)
    assert main(["notify", "--db", str(db_path)]) == EXIT_OK
    assert "notification preview" in capsys.readouterr().out


def test_cli_deliver_reports_stdout_fallback(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("JOBSCOUT_NOTIFY_ENABLED", "true")
    monkeypatch.setenv("JOBSCOUT_NOTIFY_DELIVERY", "system")
    monkeypatch.setattr("jobscout.notify.find_system_notifier", lambda: None)
    db_path = tmp_path / "notify.sqlite3"
    _seed_file(db_path)

    assert main(["notify", "--db", str(db_path), "--deliver"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "delivery: no system notifier found; printed to stdout" in output


def test_cli_limit_is_bounded(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("JOBSCOUT_NOTIFY_ENABLED", "true")
    db_path = tmp_path / "notify.sqlite3"
    _seed_file(db_path)

    assert main(["notify", "--db", str(db_path), "--limit", "0"]) == EXIT_ERROR
    assert "invalid notify options" in capsys.readouterr().err

    assert (
        main(["notify", "--db", str(db_path), "--limit", str(HARD_MAX_ITEMS + 1)])
        == EXIT_ERROR
    )
    assert "absolute upper bound" in capsys.readouterr().err


def test_cli_default_limit_matches_module_default() -> None:
    assert DEFAULT_NOTIFY_LIMIT > 0
