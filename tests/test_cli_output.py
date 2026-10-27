"""Regression tests for CLI output on a legacy (cp1252/ASCII) console.

A real ``score --scorer opencode`` batch crashed while printing job #71 because
the model-provided rationale contained U+2011 NON-BREAKING HYPHEN, which the
Windows ``cp1252`` stdout code page cannot encode. The batch was lost to a
``UnicodeEncodeError`` inside ``print``.

These tests pin the contract of the CLI print boundary:

* unencodable characters are backslash-escaped instead of raising,
* JSON stays parseable and round-trips to the exact original values,
* a UTF-8 stream still receives the original characters (no escaping),
* non-secret Unicode is never redacted.

Everything runs offline: the OpenCode CLI is replaced by an injected
``subprocess.Popen`` double from :mod:`conftest`.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from conftest import FIXTURES, fake_popen_factory

from jobscout.cli import (
    EXIT_OK,
    _write_line,
    configure_output_streams,
    main,
)

FIXTURE = FIXTURES / "linkedin_search_sample.json"
PROFILE = FIXTURES / "profile_test.yaml"

#: U+2011 NON-BREAKING HYPHEN: seen in model output, absent from cp1252.
UNICODE_HYPHEN = "\u2011"
#: Astral character: exercises the ``\U0001f600`` escaping path in text output.
ASTRAL = "\U0001f600"


class _StrictStream:
    """Text sink that fails like a cp1252 console and cannot be reconfigured.

    Models the worst case: no ``reconfigure`` (so
    :func:`configure_output_streams` cannot relax it) and a strict ``write``.
    """

    def __init__(self, encoding: str = "cp1252") -> None:
        self.encoding = encoding
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        text.encode(self.encoding)  # raises UnicodeEncodeError when unencodable
        self.parts.append(text)
        return len(text)

    @property
    def text(self) -> str:
        return "".join(self.parts)


class _ByteStream:
    """Captures what a text stream actually wrote, as raw bytes."""

    def __init__(self, encoding: str) -> None:
        self.buffer = io.BytesIO()
        # ``newline=""`` disables the platform newline translation so the tests
        # observe exactly the bytes the CLI wrote.
        self.stream = io.TextIOWrapper(self.buffer, encoding=encoding, newline="")

    def raw(self) -> str:
        self.stream.flush()
        return self.buffer.getvalue().decode(self.stream.encoding)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cli-output.sqlite3"


def _prepare_opencode_batch(db_path: Path, tmp_path: Path, monkeypatch) -> None:
    """Ingest the fixture and wire a fake OpenCode CLI with Unicode output."""
    from jobscout.scoring import opencode as opencode_module

    payload = json.loads((FIXTURES / "llm_response_valid.json").read_text(encoding="utf-8"))
    payload["rationale"] = f"senior{UNICODE_HYPHEN}level role {ASTRAL}"
    payload["dimensions"][0]["evidence"] = [f"posting lists Python{UNICODE_HYPHEN}SQL"]
    event = json.dumps(
        {
            "type": "text",
            "sessionID": "ses_test",
            "part": {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
        }
    )

    factory, _created = fake_popen_factory(stdout_text=event)
    monkeypatch.setattr(opencode_module.subprocess, "Popen", factory)
    fake_exe = tmp_path / "opencode-fake"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("JOBSCOUT_OPENCODE_COMMAND", str(fake_exe))

    assert main(["--db", str(db_path), "ingest-fixture", str(FIXTURE)]) == EXIT_OK


def _score_args(db_path: Path, *, as_json: bool = False) -> list[str]:
    args = ["--db", str(db_path), "--profile", str(PROFILE), "score", "--all", "--scorer", "opencode"]
    if as_json:
        args.append("--json")
    return args


def test_cp1252_stream_rejects_the_raw_character_it_cannot_encode() -> None:
    """Documents the failure mode the CLI must survive (not a CLI test)."""
    capture = _ByteStream("cp1252")
    with pytest.raises(UnicodeEncodeError):
        print(f"evidence: {UNICODE_HYPHEN}", file=capture.stream)


def test_write_line_escapes_on_a_stream_that_cannot_be_reconfigured() -> None:
    stream = _StrictStream("cp1252")
    _write_line(f"evidence: a{UNICODE_HYPHEN}b {ASTRAL}", stream=stream)
    assert stream.text == "evidence: a\\u2011b \\U0001f600\n"


def test_write_line_keeps_raw_text_when_the_stream_can_encode_it() -> None:
    capture = _ByteStream("utf-8")
    configure_output_streams([capture.stream])
    _write_line(f"evidence: a{UNICODE_HYPHEN}b {ASTRAL}", stream=capture.stream)
    assert capture.raw() == f"evidence: a{UNICODE_HYPHEN}b {ASTRAL}\n"


def test_configure_output_streams_only_relaxes_the_error_handler() -> None:
    capture = _ByteStream("cp1252")
    configure_output_streams([capture.stream])
    # The encoding is untouched: a UTF-8 terminal still gets valid UTF-8.
    assert capture.stream.encoding == "cp1252"
    assert capture.stream.errors == "backslashreplace"


def test_configure_output_streams_ignores_streams_without_reconfigure() -> None:
    stream = _StrictStream("cp1252")
    configure_output_streams([stream])  # must not raise
    assert stream.encoding == "cp1252"


def test_score_prints_unicode_evidence_on_a_cp1252_stdout(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """The batch completes and the unencodable characters are escaped, not lost."""
    _prepare_opencode_batch(db_path, tmp_path, monkeypatch)
    capture = _ByteStream("cp1252")
    monkeypatch.setattr(sys, "stdout", capture.stream)

    assert main(_score_args(db_path)) == EXIT_OK
    output = capture.raw()

    assert "scored 3 job(s)" in output
    assert "\\u2011" in output  # escaped, so the operator still sees the data
    assert "\\U0001f600" in output
    assert UNICODE_HYPHEN not in output  # cp1252 cannot carry it at all


def test_score_json_output_stays_valid_on_a_cp1252_stdout(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """JSON is escaped as legal ``\\uXXXX`` and round-trips to the same values."""
    _prepare_opencode_batch(db_path, tmp_path, monkeypatch)
    capture = _ByteStream("cp1252")
    monkeypatch.setattr(sys, "stdout", capture.stream)

    assert main(_score_args(db_path, as_json=True)) == EXIT_OK
    output = capture.raw()

    payload = json.loads(output)
    rationale = payload["evaluations"][0]["rationale"]
    evidence = payload["evaluations"][0]["dimensions"][0]["evidence"][0]
    assert UNICODE_HYPHEN in rationale and ASTRAL in rationale
    assert UNICODE_HYPHEN in evidence
    assert output.isascii()  # legal \uXXXX / surrogate-pair escapes only


def test_score_keeps_raw_unicode_on_a_utf8_stdout(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """A UTF-8 stream still receives the original characters, not escapes."""
    _prepare_opencode_batch(db_path, tmp_path, monkeypatch)
    capture = _ByteStream("utf-8")
    monkeypatch.setattr(sys, "stdout", capture.stream)

    assert main(_score_args(db_path)) == EXIT_OK
    output = capture.raw()

    # Non-secret Unicode is displayed verbatim: the boundary must not redact or
    # rewrite it, only escape it when the console cannot encode it.
    assert f"rationale: senior{UNICODE_HYPHEN}level role {ASTRAL}" in output
    assert f"evidence: posting lists Python{UNICODE_HYPHEN}SQL" in output
    assert "\\u2011" not in output
    assert "***REDACTED***" not in output
    assert "[REDACTED-MARKER]" not in output


def test_score_json_keeps_raw_unicode_on_a_utf8_stdout(
    db_path: Path, tmp_path: Path, monkeypatch
) -> None:
    _prepare_opencode_batch(db_path, tmp_path, monkeypatch)
    capture = _ByteStream("utf-8")
    monkeypatch.setattr(sys, "stdout", capture.stream)

    assert main(_score_args(db_path, as_json=True)) == EXIT_OK
    output = capture.raw()

    assert not output.isascii()
    assert json.loads(output)["evaluations"][0]["rationale"].startswith("senior" + UNICODE_HYPHEN)
