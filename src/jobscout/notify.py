"""Deterministic, read-only local notification selection and delivery.

This module answers one narrow question: *which already-scored, still-active
offers are worth surfacing to the human right now?* It is deliberately offline
and deterministic:

* selection is a plain read of stored data (no model call, no network, no write
  to SQLite, no schema or notification state of its own);
* an offer is selected when it still has a **stored successful evaluation**
  (``latest_evaluations``), its score is at or above a configurable threshold,
  it optionally matches a configured **preference outcome**, and it has **no
  imported human feedback row at all** (any decision excludes it);
* ordering is stable: best score first, then lowest job id.

Delivery is separated from selection. The default path only prints a preview.
A system notification is sent only when the caller explicitly opts in *and* the
machine has a supported local notifier (``notify-send`` or
``terminal-notifier``); otherwise the output falls back to stdout. There is no
scheduler, daemon, cron hook, background service, automatic run, remote
delivery, arbitrary shell command, or application action anywhere in this
module.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import HARD_MAX_ITEMS
from .db import list_jobs
from .preference import assess_preference

#: Stable schema tag for the serialized notification report.
NOTIFY_SCHEMA = "jobscout.notify.v1"

#: Default number of notifications reported when the CLI passes no ``--limit``.
DEFAULT_NOTIFY_LIMIT = 20

#: Delivery channels. ``stdout`` is always available; ``system`` may fall back.
DELIVERY_STDOUT = "stdout"
DELIVERY_SYSTEM = "system"
DELIVERY_CHOICES: tuple[str, ...] = (DELIVERY_STDOUT, DELIVERY_SYSTEM)

#: Preference outcomes accepted by the optional filter (empty/``None`` = any).
PREFERENCE_CHOICES: tuple[str, ...] = ("pass", "flag", "fail")

#: Local notifier executables discovered on ``PATH``, in preference order.
SYSTEM_NOTIFIERS: tuple[str, ...] = ("notify-send", "terminal-notifier")

#: Hard ceiling for a single local-notification scan. It reuses the project's
#: existing collection cap rather than inventing a new one. Because the scan is
#: ordered by descending score, the top-placed offers are never dropped by it.
SCAN_LIMIT = HARD_MAX_ITEMS


@dataclass(frozen=True)
class NotificationItem:
    """One selected offer, with just enough evidence to be actionable."""

    job_id: int
    title: str
    company: str
    url: str | None
    score: float
    recommendation: str
    preference_outcome: str
    preference_reason: str = ""
    preference_evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "score": self.score,
            "recommendation": self.recommendation,
            "preference": {
                "outcome": self.preference_outcome,
                "reason": self.preference_reason,
                "evidence": list(self.preference_evidence),
            },
        }


@dataclass(frozen=True)
class NotificationSelection:
    """Deterministic selection result (no delivery side effects)."""

    items: tuple[NotificationItem, ...]
    candidates: int = 0
    excluded_feedback: int = 0
    filtered_preference: int = 0


@dataclass(frozen=True)
class NotificationReport:
    """A serializable selection report plus its enabling state."""

    enabled: bool
    threshold: float
    preference_outcome: str | None
    limit: int
    candidates: int = 0
    excluded_feedback: int = 0
    filtered_preference: int = 0
    items: tuple[NotificationItem, ...] = ()
    reason: str = ""
    schema: str = NOTIFY_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "enabled": self.enabled,
            "reason": self.reason,
            "threshold": self.threshold,
            "preference_outcome": self.preference_outcome,
            "limit": self.limit,
            "candidates": self.candidates,
            "excluded_feedback": self.excluded_feedback,
            "filtered_preference": self.filtered_preference,
            "count": len(self.items),
            "items": [item.to_dict() for item in self.items],
            "preview_only": True,
        }

    def to_text(self) -> str:
        if not self.enabled:
            return (
                "notifications are disabled (JOBSCOUT_NOTIFY_ENABLED=false); "
                "nothing was read or sent.\n"
                "Enable with JOBSCOUT_NOTIFY_ENABLED=true, then preview with "
                "'jobscout notify' (add --deliver to send locally)."
            )

        preference = self.preference_outcome or "any"
        lines = [
            (
                f"notification preview: {len(self.items)} high-value offer(s) "
                f"(score >= {self.threshold:g}, preference {preference}, "
                f"limit {self.limit})"
            )
        ]
        if not self.items:
            lines.append("  no high-value offers matched")
        for item in self.items:
            title = item.title or "(untitled)"
            company = item.company or "(unknown company)"
            url = item.url or "(no url)"
            lines.append(
                f"#{item.job_id:<4} {item.score:5.1f}  {item.recommendation:<14} "
                f"{title} @ {company}"
            )
            lines.append(f"      url: {url}")
            if item.preference_outcome != "pass" or item.preference_evidence:
                lines.append(
                    f"      preference: {item.preference_outcome}  "
                    f"{item.preference_reason}"
                )
                for evidence in item.preference_evidence:
                    lines.append(f"        evidence: {evidence}")
        if self.excluded_feedback:
            lines.append(
                f"  excluded {self.excluded_feedback} offer(s) with imported "
                "human feedback"
            )
        if self.filtered_preference:
            lines.append(
                f"  filtered {self.filtered_preference} offer(s) not matching "
                f"preference '{preference}'"
            )
        lines.append(
            "note: preview only; there is no scheduler, daemon, or automatic "
            "run. Pass --deliver to send via the configured local channel."
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one explicit delivery attempt (never a background action)."""

    requested: str
    channel: str | None
    delivered: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "channel": self.channel,
            "delivered": self.delivered,
            "detail": self.detail,
        }


def _normalize_preference_filter(value: str | None) -> str | None:
    """Map the configured value to a concrete outcome or ``None`` (any)."""
    normalized = (value or "").strip().lower()
    return normalized or None


# ---------------------------------------------------------------------------
# Selection (pure read, deterministic)
# ---------------------------------------------------------------------------


def select_notifications(
    conn: Any,
    *,
    threshold: float,
    preference_outcome: str | None = None,
    limit: int = DEFAULT_NOTIFY_LIMIT,
) -> NotificationSelection:
    """Select high-value, unreviewed offers from an existing read-only database.

    ``conn`` must be a read-only connection (for example from
    :func:`connect_readonly`). The function runs ``SELECT`` statements only:
    :func:`jobscout.db.list_jobs` for the latest successful evaluation and one
    batched ``job_feedback`` lookup for human decisions, with
    :func:`jobscout.preference.assess_preference` computed purely in memory.

    An offer with **any** imported feedback row is excluded, whatever the
    decision. Selection is bounded to :data:`SCAN_LIMIT` highest-scoring
    candidates and then capped to ``limit``.
    """
    target_preference = _normalize_preference_filter(preference_outcome)
    rows = list_jobs(
        conn,
        limit=SCAN_LIMIT,
        min_score=float(threshold),
        scored_only=True,
        rank_by_score=True,
    )

    # Fetch all feedback ids in one bounded read instead of issuing one query
    # per candidate. Notifications must stay a single, offline selection pass;
    # in particular, this cannot become a fan-out path as the corpus grows.
    candidate_ids = [int(item.job.id) for item in rows]
    feedback_ids: set[int] = set()
    if candidate_ids:
        placeholders = ",".join("?" for _ in candidate_ids)
        feedback_ids = {
            int(row[0])
            for row in conn.execute(
                f"SELECT job_id FROM job_feedback WHERE job_id IN ({placeholders})",
                candidate_ids,
            ).fetchall()
        }

    selected: list[NotificationItem] = []
    excluded_feedback = 0
    filtered_preference = 0
    for item in rows:
        job = item.job
        job_id = int(job.id)
        if job_id in feedback_ids:
            excluded_feedback += 1
            continue
        assessment = assess_preference(job)
        outcome = str(assessment.outcome)
        if target_preference is not None and outcome != target_preference:
            filtered_preference += 1
            continue
        selected.append(
            NotificationItem(
                job_id=job_id,
                title=job.title or "",
                company=job.company or "",
                url=job.url,
                score=float(item.score if item.score is not None else 0.0),
                recommendation=str(item.recommendation or ""),
                preference_outcome=outcome,
                preference_reason=assessment.reason,
                preference_evidence=tuple(assessment.evidence),
            )
        )

    # ``list_jobs`` already orders by score, but the explicit key makes the
    # result independent of any future tie-break changes in the query.
    selected.sort(key=lambda entry: (-entry.score, entry.job_id))
    return NotificationSelection(
        items=tuple(selected[: max(0, int(limit))]),
        candidates=len(rows),
        excluded_feedback=excluded_feedback,
        filtered_preference=filtered_preference,
    )


def disabled_report(
    settings: Any, *, limit: int = DEFAULT_NOTIFY_LIMIT
) -> NotificationReport:
    """The honest report for the disabled-by-default state (no DB access)."""
    return NotificationReport(
        enabled=False,
        threshold=float(settings.notify_score_threshold),
        preference_outcome=_normalize_preference_filter(
            settings.notify_preference_outcome
        ),
        limit=int(limit),
        reason="disabled",
    )


def build_report(
    conn: Any, settings: Any, *, limit: int = DEFAULT_NOTIFY_LIMIT
) -> NotificationReport:
    """Select notifications from ``conn`` and package a preview report."""
    selection = select_notifications(
        conn,
        threshold=settings.notify_score_threshold,
        preference_outcome=settings.notify_preference_outcome,
        limit=limit,
    )
    return NotificationReport(
        enabled=True,
        threshold=float(settings.notify_score_threshold),
        preference_outcome=_normalize_preference_filter(
            settings.notify_preference_outcome
        ),
        limit=int(limit),
        candidates=selection.candidates,
        excluded_feedback=selection.excluded_feedback,
        filtered_preference=selection.filtered_preference,
        items=selection.items,
        reason="ok",
    )


# ---------------------------------------------------------------------------
# Delivery (explicit opt-in only)
# ---------------------------------------------------------------------------


def find_system_notifier() -> str | None:
    """Return the path of the first supported local notifier, or ``None``.

    Discovery is a plain ``PATH`` lookup (:func:`shutil.which`); no process is
    started here. ``notify-send`` (Linux) and ``terminal-notifier`` (macOS) are
    the only accepted mechanisms.
    """
    for name in SYSTEM_NOTIFIERS:
        found = shutil.which(name)
        if found:
            return found
    return None


def _notification_argv(executable: str, title: str, body: str) -> list[str]:
    """Fixed argument arrays for the two supported notifiers (no shell)."""
    name = Path(executable).name.lower()
    if "terminal-notifier" in name:
        return [executable, "-title", title, "-message", body]
    return [executable, title, body]


def send_system_notification(executable: str, title: str, body: str) -> bool:
    """Run one supported notifier with a fixed argument array.

    Never uses a shell and never takes a caller-supplied command string, so it
    cannot become arbitrary command execution. Returns ``True`` only when the
    notifier exits successfully.
    """
    try:
        completed = subprocess.run(
            _notification_argv(executable, title, body),
            shell=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _notification_summary(report: NotificationReport) -> tuple[str, str]:
    title = f"jobscout: {len(report.items)} high-value offer(s)"
    if not report.items:
        return title, "no high-value offers matched"
    body = "\n".join(
        f"{item.title or '(untitled)'} @ {item.company or '(unknown company)'} "
        f"[{item.score:.1f}]"
        for item in report.items
    )
    return title, body


def deliver_report(
    report: NotificationReport,
    *,
    delivery: str = DELIVERY_STDOUT,
    executable: str | None = None,
    sender: Callable[[str, str, str], bool] | None = None,
) -> DeliveryResult:
    """Deliver ``report`` through the configured local channel.

    ``stdout`` prints nothing on its own: the caller already emits the preview,
    so this reports the stdout channel. ``system`` sends a single notification
    through a discovered local notifier and falls back to stdout when none is
    available or the notifier fails. ``executable``/``sender`` are injectable so
    tests never start a process.
    """
    if not report.items:
        return DeliveryResult(
            requested=delivery,
            channel=None,
            delivered=False,
            detail="nothing to deliver",
        )
    if delivery != DELIVERY_SYSTEM:
        return DeliveryResult(
            requested=DELIVERY_STDOUT,
            channel=DELIVERY_STDOUT,
            delivered=True,
            detail="printed to stdout",
        )

    resolved = executable if executable is not None else find_system_notifier()
    if not resolved:
        return DeliveryResult(
            requested=DELIVERY_SYSTEM,
            channel=DELIVERY_STDOUT,
            delivered=True,
            detail="no system notifier found; printed to stdout",
        )

    title, body = _notification_summary(report)
    send = sender if sender is not None else send_system_notification
    if send(resolved, title, body):
        return DeliveryResult(
            requested=DELIVERY_SYSTEM,
            channel=DELIVERY_SYSTEM,
            delivered=True,
            detail=f"sent via {Path(resolved).name}",
        )
    return DeliveryResult(
        requested=DELIVERY_SYSTEM,
        channel=DELIVERY_STDOUT,
        delivered=True,
        detail="system notifier failed; printed to stdout",
    )


__all__ = [
    "DEFAULT_NOTIFY_LIMIT",
    "DELIVERY_CHOICES",
    "DELIVERY_STDOUT",
    "DELIVERY_SYSTEM",
    "DeliveryResult",
    "NOTIFY_SCHEMA",
    "NotificationItem",
    "NotificationReport",
    "NotificationSelection",
    "PREFERENCE_CHOICES",
    "SCAN_LIMIT",
    "SYSTEM_NOTIFIERS",
    "build_report",
    "deliver_report",
    "disabled_report",
    "find_system_notifier",
    "select_notifications",
    "send_system_notification",
]
