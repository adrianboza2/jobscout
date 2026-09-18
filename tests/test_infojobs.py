"""InfoJobs must stay a documented, hard-blocked adapter."""

from __future__ import annotations

import pytest

from jobscout.cli import build_parser
from jobscout.collectors.base import CollectorDisabledError
from jobscout.collectors.infojobs import (
    BLOCKED_PRIVATE_URL,
    BLOCKED_REASON,
    PROVIDER,
    search_infojobs,
)


def test_infojobs_entry_point_always_refuses() -> None:
    with pytest.raises(CollectorDisabledError) as excinfo:
        search_infojobs(keywords="cloud")
    assert BLOCKED_REASON in str(excinfo.value)
    assert "official API authorization" in str(excinfo.value)


def test_blocked_private_url_is_named_and_not_a_public_endpoint() -> None:
    assert PROVIDER == "infojobs"
    assert BLOCKED_PRIVATE_URL == "https://www.infojobs.net/candidate/my-infojobs.xhtml"
    assert "my-infojobs.xhtml" in BLOCKED_REASON
    assert "private" in BLOCKED_REASON.lower()


def test_no_infojobs_cli_command_is_registered() -> None:
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices  # type: ignore[attr-defined]
    assert not any("infojobs" in name for name in choices)
