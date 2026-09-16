"""Time helpers.

All persisted timestamps are UTC ISO-8601 strings with a ``Z`` suffix so that
SQLite text comparisons and lexicographic ordering stay meaningful.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_ISO_Z_RE = re.compile(r"Z$", re.IGNORECASE)
_RELATIVE_RE = re.compile(
    r"(?P<count>\d+)\s*(?P<unit>minute|minutes|min|hour|hours|h|day|days|d|week|weeks|w|month|months|mo|year|years|y)"
    r"|(?P<word>yesterday|today|just\s+now|now|recién|recien|hoy|ayer)",
    re.IGNORECASE,
)
_UNIT_DAYS = {
    "minute": 1 / 1440,
    "minutes": 1 / 1440,
    "min": 1 / 1440,
    "hour": 1 / 24,
    "hours": 1 / 24,
    "h": 1 / 24,
    "day": 1,
    "days": 1,
    "d": 1,
    "week": 7,
    "weeks": 7,
    "w": 7,
    "month": 30,
    "months": 30,
    "mo": 30,
    "year": 365,
    "years": 365,
    "y": 365,
}
_WORD_DAYS = {
    "yesterday": 1,
    "ayer": 1,
    "today": 0,
    "hoy": 0,
    "just now": 0,
    "now": 0,
    "recién": 0,
    "recien": 0,
}


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_iso(moment: datetime | None) -> str | None:
    """Serialize a datetime to a UTC ISO-8601 string ending in ``Z``."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return to_iso(utcnow())


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601-ish timestamp, returning ``None`` when unusable."""
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    candidate = _ISO_Z_RE.sub("+00:00", candidate)
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_relative_date(text: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse LinkedIn-style relative dates such as ``"3 days ago"``.

    Returns ``None`` for unrecognized input instead of guessing.
    """
    if not text:
        return None
    match = _RELATIVE_RE.search(text)
    if match is None:
        return None
    reference = now or utcnow()
    word = (match.group("word") or "").lower().replace("  ", " ")
    if word:
        days = _WORD_DAYS.get(word)
        if days is None:
            return None
        return reference - timedelta(days=days)
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    days = _UNIT_DAYS.get(unit)
    if days is None:
        return None
    return reference - timedelta(days=count * days)


def parse_flexible_date(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse either an absolute timestamp or a relative date string."""
    return parse_datetime(value) or parse_relative_date(value, now=now)


def age_days(value: str | None, *, now: datetime | None = None) -> float | None:
    """Age in days for an ISO timestamp, or ``None`` when it cannot be parsed."""
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    reference = now or utcnow()
    return (reference - parsed).total_seconds() / 86400.0
