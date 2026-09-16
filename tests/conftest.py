"""Shared pytest fixtures.

Every test runs fully offline: no test creates a real socket, and the HTTP
transport is always replaced by :class:`FakeHttpClient`.
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pytest

from jobscout.collectors.base import HttpClient, HttpResponse, TransportError
from jobscout.config import Settings
from jobscout.db import connect, init_db
from jobscout.models import Job, JobPosting
from jobscout.normalize import finalize_posting
from jobscout.scoring.opencode import CommandOutcome
from jobscout.scoring.profile import Profile, load_profile

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ambient JOBSCOUT_* variables from leaking into tests."""
    for key in list(os.environ):
        if key.startswith("JOBSCOUT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(Path(__file__).parent)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(env={}, dotenv_path=None).with_overrides(
        db_path=tmp_path / "jobscout-test.sqlite3",
        profile_path=FIXTURES / "profile_test.yaml",
    )


@pytest.fixture
def llm_settings(settings: Settings) -> Settings:
    return settings.with_overrides(
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="test-key-not-a-real-secret",
    )


@pytest.fixture
def opencode_settings(settings: Settings) -> Settings:
    """Settings for the OpenCode CLI scorer (defaults, but explicit)."""
    return settings.with_overrides(
        opencode_command="opencode",
        opencode_model="opencode-go/deepseek-v4.1-flash",
    )


@pytest.fixture
def conn():
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def profile() -> Profile:
    return load_profile(FIXTURES / "profile_test.yaml")


@pytest.fixture
def strong_posting() -> JobPosting:
    posting = JobPosting(
        source="fixture",
        title="Senior Backend Engineer",
        company="Acme Data Inc.",
        url="https://example.test/jobs/senior-backend-engineer",
        location="Remote, Argentina",
        description=(
            "We need Python, SQL and PostgreSQL. FastAPI and Docker are a plus. "
            "Remote friendly team with English communication."
        ),
        source_job_id="job-1",
        employment_type="full_time",
        seniority="senior",
        salary="USD 6000 per month",
        posted_at=(FIXED_NOW - timedelta(days=2)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        fetched_at="2026-09-15T10:00:00Z",
    )
    return finalize_posting(posting)


@pytest.fixture
def weak_posting() -> JobPosting:
    posting = JobPosting(
        source="fixture",
        title="Junior Frontend Developer",
        company="Umbrella Labs",
        url="https://example.test/jobs/junior-frontend",
        location="Cordoba, Argentina",
        description="TypeScript and CSS only. No backend work. On-site in Cordoba.",
        source_job_id="job-2",
        seniority="junior",
        posted_at=(FIXED_NOW - timedelta(days=90)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    return finalize_posting(posting)


@pytest.fixture
def stored_job(conn, strong_posting) -> Job:
    from jobscout.db import store_posting

    job, _ = store_posting(conn, strong_posting, now="2026-09-15T10:00:00Z")
    return job


class FakeHttpClient(HttpClient):
    """Deterministic transport double.

    ``responses`` is consumed in order; an ``Exception`` instance is raised
    instead of returned. Every call is recorded for assertions.
    """

    def __init__(self, responses: Iterable[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def fetch_once(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        self.calls.append({"url": url, "method": method, "headers": dict(headers or {}), "body": body})
        if not self.responses:
            raise AssertionError(f"FakeHttpClient received an unexpected request: {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeRawResponse:
    """Minimal ``http.client``-shaped response for the real transport.

    Only the surface ``UrllibHttpClient.fetch_once`` touches is implemented:
    context-manager entry/exit, ``read``, ``geturl``, ``status`` and ``headers``.
    """

    def __init__(
        self,
        body: bytes,
        url: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._stream = io.BytesIO(body)
        self._url = url
        self.status = status
        self.headers = dict(headers or {"content-type": "text/html; charset=utf-8"})

    def read(self, amount: int = -1) -> bytes:
        return self._stream.read(amount)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "FakeRawResponse":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class FakeOpener:
    """Opener double for :class:`UrllibHttpClient` (``open(request, timeout=)``).

    Injected through ``UrllibHttpClient(opener=...)`` so the real bounded-read
    logic runs without opening a socket.
    """

    def __init__(self, *responses: FakeRawResponse) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: float | None = None) -> FakeRawResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeOpener received an unexpected request")
        return self.responses.pop(0)


class FakeRunner:
    """Deterministic OpenCode CLI double.

    ``outcomes`` is consumed in order and an ``Exception`` instance is raised
    instead of returned, so timeout and missing-executable paths are testable
    without ever starting a process.
    """

    def __init__(self, outcomes: Iterable[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        max_output_bytes: int | None = None,
    ) -> CommandOutcome:
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "timeout": timeout,
                "env": env,
                "max_output_bytes": max_output_bytes,
            }
        )
        if not self.outcomes:
            raise AssertionError("FakeRunner received an unexpected opencode invocation")
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def command_outcome(returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandOutcome:
    return CommandOutcome(returncode=returncode, stdout=stdout, stderr=stderr)


class FakePopen:
    """Minimal ``subprocess.Popen`` double for the bounded runner.

    Exposes only the surface ``run_command`` uses: ``stdout``/``stderr`` byte
    streams, ``wait``, ``poll``, ``kill``, ``terminate``, and ``returncode``.
    Pipe keyword arguments (``stdout``/``stderr``/``stdin``) are separated from
    the rest so tests can assert ``shell``/``env`` were passed correctly.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        returncode: int = 0,
        **kwargs: Any,
    ) -> None:
        self.argv = list(argv)
        self.pipe_kwargs = {
            key: kwargs[key] for key in ("stdout", "stderr", "stdin") if key in kwargs
        }
        self.kwargs = {
            key: value
            for key, value in kwargs.items()
            if key not in ("stdout", "stderr", "stdin")
        }
        self.stdout = io.BytesIO(stdout_text.encode("utf-8"))
        self.stderr = io.BytesIO(stderr_text.encode("utf-8"))
        self.returncode = returncode
        self.killed = False
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        return int(self.returncode)

    def poll(self) -> int:
        return int(self.returncode)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


def fake_popen_factory(
    *, stdout_text: str = "", stderr_text: str = "", returncode: int = 0
) -> tuple[Any, list[FakePopen]]:
    """Return ``(factory, created)`` for monkeypatching ``subprocess.Popen``."""
    created: list[FakePopen] = []

    def factory(argv: list[str], **kwargs: Any) -> FakePopen:
        process = FakePopen(
            argv,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            returncode=returncode,
            **kwargs,
        )
        created.append(process)
        return process

    return factory, created


def response(status: int, body: str = "", headers: dict[str, str] | None = None, url: str = "https://example.test/") -> HttpResponse:
    return HttpResponse(
        url=url,
        status_code=status,
        body=body,
        headers=headers or {},
        fetched_at="2026-09-15T12:00:00Z",
    )


def llm_envelope(content: str, *, model: str = "deepseek-v4.1-flash") -> str:
    import json

    return json.dumps(
        {
            "id": "chatcmpl-test",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
    )


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any code opens a socket."""
    import socket

    def _blocked(*args: Any, **kwargs: Any):  # pragma: no cover - only on failure
        raise AssertionError("a test attempted real network access")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


__all__ = [
    "FIXED_NOW",
    "FIXTURES",
    "FakeHttpClient",
    "FakeOpener",
    "FakePopen",
    "FakeRawResponse",
    "FakeRunner",
    "command_outcome",
    "fake_popen_factory",
    "fixture_text",
    "llm_envelope",
    "no_network",
    "response",
    "TransportError",
]
