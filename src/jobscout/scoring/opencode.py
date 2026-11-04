"""Optional OpenCode CLI scorer.

This backend shells out to the locally installed ``opencode`` CLI instead of
talking to an HTTP endpoint. Authentication stays entirely inside OpenCode's own
credential store (``opencode auth login``); jobscout never uses
``JOBSCOUT_LLM_API_KEY`` to authenticate this path, never places it in the CLI
argument array or in project files, removes any configured secret value from the
child process environment, and scrubs captured output against it before it could
be persisted.

Design rules:

* **Local CLI, no endpoint.** The scorer runs ``opencode run [--standalone]
  --model <model>[#<variant>] --format json <prompt>`` through a subprocess
  argument array. ``--standalone`` starts a private server instead of the
  background service and is on by default (``JOBSCOUT_OPENCODE_PURE``); the
  optional variant is part of the model id (``JOBSCOUT_OPENCODE_VARIANT``,
  default ``low``). There is no ``shell=True`` and no network client in this
  module.
* **Resolved executable.** The CLI is only ever launched through an absolute
  path from :func:`resolve_executable` (an absolute path, or a
  :func:`shutil.which` hit). A relative path is never resolved against the
  process cwd, so a planted executable cannot hijack the invocation.
* **Bounded while running.** Output is captured with :class:`subprocess.Popen`
  and reader threads that enforce ``JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES`` as the
  bytes arrive; crossing the ceiling kills the child instead of buffering an
  unbounded response in memory. The same supervisor enforces the timeout.
* **Hardened prompt is reused.** The system/user messages from
  :mod:`jobscout.scoring.llm` are composed into one CLI prompt, so the
  untrusted-posting framing and delimiter neutralization are identical to the
  HTTP scorer.
* **Robust extraction.** OpenCode can emit formatted text, a single JSON object,
  or a stream of raw JSON events (``--format json``). The scorer collects the
  assistant's text parts from events, then reuses
  :func:`jobscout.scoring.schema.parse_llm_evaluation` to extract and validate
  exactly one canonical evaluation.
* **Fail closed, never crash a batch.** Missing executable, timeout, non-zero
  exit, and malformed output become evaluations with ``status != ok`` and an
  operator-readable, secret-redacted error.
* **Bounded and conditional provenance.** Captured output is truncated to a
  configured ceiling, and the raw output is persisted only when it is small
  enough, contains no configured secret, and contains no credential-looking
  content.
* **Secrets never reach the child or the database.** The CLI runs with a copy of
  the inherited environment that keeps ``PATH`` and ``HOME``/``APPDATA`` (so
  OpenCode still finds its executable and credentials) but drops any variable
  carrying a configured secret. With ``opencode_pure`` the child additionally
  gets a private ``XDG_CONFIG_HOME`` instead of the user's OpenCode config.
  Persisted output is additionally checked against the configured secret list
  and discarded when one is present.
* **Model-derived text is redacted.** The rationale, dimension text, and the
  schema repair/error diagnostics (which quote model-supplied values) are
  redacted against the configured secrets through
  :func:`jobscout.scoring.schema.parse_llm_evaluation` before they can be
  persisted or printed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import PREFIX, Settings, is_safe_to_store, redact_secrets
from ..db import insert_raw_document
from ..models import (
    Evaluation,
    EvaluationStatus,
    RawDocument,
    RawDocumentKind,
    Recommendation,
    ScorerName,
)
from ..timeutil import utcnow_iso
from .currency import currency_rejection
from .llm import build_messages, sha256_text
from .fingerprint import effective_input_fingerprint
from .profile import Profile
from .schema import parse_llm_evaluation, success_contract_errors

SCORER_NAME = ScorerName.OPENCODE
OPENCODE_PROMPT_VERSION = "opencode-v6-salary-estimate"
OPENCODE_MAX_JOB_TEXT_CHARS = 6_000
MAX_ERROR_CHARS = 500

#: Bytes read per OS pipe read while capturing child output.
_READ_CHUNK_BYTES = 8192
#: How often the supervisor re-checks the child for exit/overflow/timeout.
_POLL_INTERVAL_SECONDS = 0.05
#: Grace period for reader threads to drain after the child is stopped.
_READER_JOIN_SECONDS = 2.0


class OpenCodeError(RuntimeError):
    """Base class for OpenCode CLI scorer failures."""


class OpenCodeNotConfiguredError(OpenCodeError):
    """Raised when the CLI cannot be located or started."""


class OpenCodeTimeoutError(OpenCodeError):
    """Raised when the CLI exceeds the configured timeout."""


class OpenCodeExecutionError(OpenCodeError):
    """Raised when the CLI exits non-zero or returns unusable output."""


@dataclass
class CommandOutcome:
    """Raw result of one CLI invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    #: True when the child exceeded the output ceiling and was terminated.
    overflowed: bool = False


@dataclass
class OpenCodeResponse:
    """An extracted evaluation plus bounded provenance for one CLI call."""

    text: str
    raw: str
    model: str
    truncated: bool = False


# A runner has the same keyword signature as the default and returns a
# :class:`CommandOutcome`. Tests inject one so no real process is ever started.
Runner = Callable[..., CommandOutcome]


# Appended to the hardened CLI prompt. OpenCode sometimes stops after a valid
# JSON prefix without closing the object (observed live: ``reason=stop`` with a
# truncated payload), which the schema correctly rejects as ``parse_error``.
# This contract asks for a completed, compact object so the model does not spend
# its budget on prose, pretty-printing, or list padding. It never weakens the
# security framing or the schema from :mod:`jobscout.scoring.llm`.
OPENCODE_OUTPUT_CONTRACT = (
    "\n\nOPENCODE OUTPUT CONTRACT (applies to this CLI call):\n"
    "- Reply with exactly one JSON object and nothing else. No prose, no\n"
    "  markdown fences, and no commentary before or after the object.\n"
    "- Return only these keys: overall_score, recommendation, confidence,\n"
    "  rationale, salary_estimate_low, salary_estimate_mid,\n"
    "  salary_estimate_high, employer_tier, salary_basis. Do not return\n"
    "  dimensions, blockers, strengths, gaps, unknowns, evidence, or any other\n"
    "  keys.\n"
    "- The salary keys never change overall_score or recommendation.\n"
    "- Use one numeric overall_score from 0 to 100, one recommendation from\n"
    "  apply, consider, or skip, and confidence from 0 to 1. Never answer\n"
    "  need_more_info, and never return an empty or placeholder score: a 0 is\n"
    "  a real incompatibility, not \"unknown\".\n"
    "- Calibrate recommendations for a junior candidate and separate hard\n"
    "  requirements from preferences. A stated preference, or a reasonable\n"
    "  junior or stretch role, should tend to consider or apply even when it\n"
    "  asks for more experience than the candidate has.\n"
    "- Apply the seniority policy: treat 5 or more years of required\n"
    "  experience, an explicit senior, staff, principal, lead, or manager\n"
    "  title, or leadership and architectural ownership as core duties as\n"
    "  strong penalties. Choose skip when one of those clearly defines the\n"
    "  role; a passing mention of seniority in an otherwise junior or stretch\n"
    "  posting is not a hard skip.\n"
    "- Do not skip solely because one or two required technologies are\n"
    "  unfamiliar. Treat them as learnable gaps when the main role is still\n"
    "  Cloud, DevOps, DevSecOps, or Platform engineering.\n"
    "- If most of the required core stack is unfamiliar, treat it as a\n"
    "  material mismatch and lower both the recommendation and the score.\n"
    "- Keep explicit dealbreakers, legal requirements, and the user's\n"
    "  SOC/rotating-shift exclusions as hard limitations.\n"
    "- Keep the JSON compact: single line or minimal whitespace, no\n"
    "  pretty-printing, and no repeating the schema in your answer.\n"
    "- Keep \"rationale\" to one short sentence of at most 160 characters.\n"
    "- Close the JSON object completely before you stop.\n"
)


# The salary estimate is informational and deliberately separated from the
# score. Keeping its rubric in one constant makes the prompt version auditable
# and leaves the scoring rules above untouched.
SALARY_ESTIMATE_GUIDE = """

SALARY ESTIMATE (gross annual EUR, Spain, centred on Madrid):
- Estimate what THIS offer realistically pays for the role and level it describes, not what
  the candidate is worth. Be brutally realistic: most junior offers in Madrid pay 20-28k,
  advertised ranges skew high, and generic portal averages are inflated.
- If the posting states a salary, base the range on it (convert monthly to annual: x14 for
  Spanish monthly pay unless 12 payments are stated).
- First classify employer_tier, then pick the band, then adjust. Approximate weight of each
  factor in the final figure: employer type 30%, tech stack and niche 25%, English and
  international exposure 20%, experience and seniority demanded 15%, on-call and office
  presence 10%.
- employer_tier bands (junior <2y / mid 2-5y / senior 5y+):
  consultora_pequena (small consultancy, staffing "carnica", local SME, IT support company):
    17-24k / 26-35k / 35-45k. N1 helpdesk or 24x7 operator at an SME: 16-21k.
  consultora_grande (Accenture, NTT Data, Capgemini, Devoteam, Sopra Steria, Indra; banks,
    insurers, telcos, utilities such as Mapfre, BBVA, Telefonica): 22-27k / 30-42k / 42-60k.
  startup (national product startup, early or growth stage): 25-32k / 38-50k / 50-70k.
  scaleup_nacional (Series B/C national scale-up: Factorial, Cabify, Glovo, Fever):
    30-38k / 45-60k / 60-90k.
  internacional (international scale-up or global SaaS hiring in Spain or fully remote for a
    foreign company): 38-50k / 55-80k / 90-150k.
  big_tech (Google, Amazon, Microsoft, Meta, OpenAI): base 50-70k junior, 130k+ senior.
- Role anchors in Spain (Manfred salary guide 2026): SRE/DevOps <2y 25-35k, 2-5y 36-45k,
  5-10y 46-60k; SysAdmin <2y 20-25k, 2-5y 26-35k; Security engineer <2y 25-30k, 2-5y 31-40k,
  5-10y 41-55k. Market medians from offers that state pay: DevOps/Platform 50k, Security 50k
  (mostly mid and senior roles). Offers seen with stated pay: junior DevOps at small
  companies 23-30k, junior SRE at a product company 35-45k, mid DevOps 40-52k, senior DevOps
  or Platform 55-80k.
- Adjustments: on-call or 24x7 availability +3-6k; mandatory 100% onsite +10-20%; required
  fluent English or an international team moves the figure to the top of the band;
  Kubernetes, Terraform, cloud security or production on-call responsibility move it up;
  ticket-driven support, L1 operation or "cloud" titles that are really support move it down.
- salary_estimate_low <= salary_estimate_mid <= salary_estimate_high, whole euros. mid is the
  most likely figure. salary_basis: one short sentence (max 160 chars) naming the tier, level
  and the main adjustment.
"""


def build_cli_prompt(job: Any, profile: Profile) -> str:
    """Compose the hardened scoring messages into a single CLI prompt.

    The messages keep the untrusted-posting framing and delimiter
    neutralization from :mod:`jobscout.scoring.llm`; the OpenCode-specific
    output contract is appended without replacing the system schema.
    """
    messages = build_messages(
        job,
        profile,
        max_job_text_chars=OPENCODE_MAX_JOB_TEXT_CHARS,
    )
    composed = "\n\n".join(
        f"{message['role'].upper()}:\n{message['content']}" for message in messages
    )
    return composed + OPENCODE_OUTPUT_CONTRACT + SALARY_ESTIMATE_GUIDE


def effective_prompt_version(settings: Settings) -> str:
    """Return the ``prompt_version`` an OpenCode evaluation must record.

    The configured ``opencode_variant`` is part of the reasoning provenance, so
    two passes with different variants must be distinguishable after the fact.
    A non-empty variant is appended as ``<version>+<variant>``; an empty or
    unset variant keeps the bare historical string byte-identical, so rows
    produced before variants were recorded stay comparable.
    """
    variant = (settings.opencode_variant or "").strip()
    if variant:
        return f"{OPENCODE_PROMPT_VERSION}+{variant}"
    return OPENCODE_PROMPT_VERSION


def input_fingerprint(job: Any, profile: Profile, settings: Settings) -> str:
    """Effective-input fingerprint for the OpenCode CLI scorer.

    OpenCode carries its *own* effective prompt version and model, not the
    shared HTTP-LLM defaults: the scored offer, the profile snapshot, the
    ``opencode`` scorer identity, the configured ``opencode_model`` and the
    variant-bearing :func:`effective_prompt_version` are the inputs that decide
    whether a stored evaluation is still current.
    """
    return effective_input_fingerprint(
        scorer=SCORER_NAME,
        model=settings.opencode_model,
        prompt_version=effective_prompt_version(settings),
        profile=profile,
        job=job,
    )


def build_command(
    settings: Settings,
    prompt: str,
    *,
    command: str | None = None,
) -> list[str]:
    """Build the ``opencode run`` argument array (never a shell string).

    ``command`` overrides the configured executable so the caller can pass a
    fully resolved absolute path (see :func:`resolve_executable`).
    ``--standalone`` is added by default to run a private server instead of the
    background service. The variant is part of the model id in OpenCode 2.x, so
    a non-empty variant is appended as ``<model>#<variant>`` unless the model
    already carries its own ``#``; an empty setting leaves the model untouched.
    """
    argv = [
        command or settings.opencode_command,
        "run",
    ]
    if settings.opencode_pure:
        argv.append("--standalone")
    model = settings.opencode_model
    variant = (settings.opencode_variant or "").strip()
    if variant and "#" not in model:
        model = f"{model}#{variant}"
    argv += [
        "--model",
        model,
    ]
    if settings.opencode_json_events:
        argv += ["--format", "json"]
    argv.append(prompt)
    return argv


def resolve_executable(command: str | None) -> str | None:
    """Resolve ``command`` to an absolute executable path, or return ``None``.

    Only two forms are accepted:

    * an **absolute** path to a real file, or
    * a bare command name resolved through :func:`shutil.which`.

    A *relative* path (``./opencode``, ``bin/opencode``, or a bare name that
    happens to exist in the current working directory) is deliberately rejected.
    Resolving relative paths against the process cwd would let a planted
    ``opencode`` file in whatever directory the scorer is launched from hijack
    the invocation and run arbitrary code.
    """
    if not command:
        return None
    raw = str(command).strip()
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() else None
    # Reject any relative path: it would be resolved against the process cwd.
    if "/" in raw or "\\" in raw or (os.altsep and os.altsep in raw):
        return None
    found = shutil.which(raw)
    if not found:
        return None
    # On Windows, ``shutil.which`` implicitly searches the current directory and
    # returns a *relative* result (``.\name.EXE``) for a planted executable, so
    # an absolute result is required.
    resolved = Path(found)
    if not resolved.is_absolute():
        return None
    return str(resolved)


def bound_output(text: str, *, max_bytes: int) -> tuple[str, bool]:
    """Truncate captured output to ``max_bytes`` UTF-8 bytes."""
    encoded = (text or "").encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text or "", False
    return encoded[:max_bytes].decode("utf-8", errors="replace"), True


# Environment variables that carry jobscout's own LLM credential. They are
# dropped from the child environment by name as well as by value, because a key
# loaded from ``.env`` may not be reflected in ``os.environ``.
_SECRET_ENV_NAMES = (f"{PREFIX}LLM_API_KEY",)

#: Actions denied on every resource. ``shell``/``write``/``edit``/``execute``
#: can change the machine (``execute`` also reaches OpenCode Console admin
#: tools); ``subagent``/``skill`` load more agents or code; ``question``,
#: ``external_directory`` and ``doom_loop`` default to "ask", and any prompt
#: hangs a non-interactive run until the timeout; ``websearch`` is paid
#: (HTTP 402) and only wastes a turn.
_ISOLATED_DENIED_ACTIONS = (
    "shell",
    "write",
    "edit",
    "execute",
    "subagent",
    "skill",
    "question",
    "websearch",
    "external_directory",
    "doom_loop",
)

#: Secret file patterns that must never be read: the model could send their
#: contents out through ``webfetch``. ``read``/``grep``/``glob``/``webfetch``
#: stay allowed on purpose everywhere else.
_ISOLATED_SECRET_RESOURCES = ("*.env", "*.env.*", "*.pem", "*.key", "*auth.json")

#: Minimal config written into the private config home. ``share: disabled``
#: keeps a scoring prompt out of OpenCode's sharing service and ``autoupdate``
#: is off so an isolated run never replaces the executable mid-batch.
#: ``permissions`` is the OpenCode 2.x mechanism that actually removes tools.
_ISOLATED_OPENCODE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "share": "disabled",
    "autoupdate": False,
    "permissions": [
        {"action": action, "resource": "*", "effect": "deny"}
        for action in _ISOLATED_DENIED_ACTIONS
    ]
    + [
        {"action": "read", "resource": resource, "effect": "deny"}
        for resource in _ISOLATED_SECRET_RESOURCES
    ],
}


def _config_is_current(config_file: Path) -> bool:
    """Return whether ``config_file`` already parses to the isolated config."""
    try:
        written = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return written == _ISOLATED_OPENCODE_CONFIG


def isolated_config_home(settings: Settings) -> Path:
    """Return (and initialise) the private ``XDG_CONFIG_HOME`` for OpenCode.

    The directory lives next to the database, inside a user-owned data tree,
    because plugins loaded from a config directory are arbitrary code: a
    predictable shared location such as ``/tmp`` would let another account plant
    one. The child process gets this directory as ``XDG_CONFIG_HOME``, so it
    never reads the user's real OpenCode configuration.

    The config file is rewritten whenever it is missing or does not parse to
    the current constant, so an installation created by an older version picks
    up new deny rules without manual intervention.
    """
    # Resolved here, not in the child: the CLI runs from ``opencode_workdir``
    # (the private directory by default), where a relative ``data/...`` would
    # point into exactly the shared location this function exists to avoid.
    home = Path(settings.db_path).expanduser().resolve().parent / "opencode-config"
    config_file = home / "opencode" / "opencode.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)

    if not _config_is_current(config_file):
        config_file.write_text(
            json.dumps(_ISOLATED_OPENCODE_CONFIG) + "\n", encoding="utf-8"
        )

    return home


def isolated_workdir(settings: Settings) -> Path:
    """Return (and create) the private working directory next to the database.

    ``read``/``grep``/``glob`` are allowed inside the working directory, so it
    must not be a shared place such as the system temp dir where another
    process's files would be visible.
    """
    workdir = Path(settings.db_path).expanduser().resolve().parent / "opencode-work"
    workdir.mkdir(parents=True, exist_ok=True)

    return workdir


def resolved_workdir(settings: Settings) -> str | None:
    """Translate ``opencode_workdir`` into the child process ``cwd``.

    ``None`` means the private directory next to the database, ``""`` means
    "inherit the process cwd" (the library default), and any other value is
    used verbatim.
    """
    if settings.opencode_workdir is None:
        return str(isolated_workdir(settings))

    if settings.opencode_workdir == "":
        return None

    return settings.opencode_workdir


def scrubbed_subprocess_env(
    settings: Settings,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy the inherited environment with configured secrets removed.

    OpenCode keeps its authentication in its own local store
    (``opencode auth login``), so it does not need a jobscout API key. This
    preserves every inherited variable — including ``PATH``,
    ``HOME``/``APPDATA``, and the data-oriented ``XDG_*`` variables, which
    OpenCode uses to locate its executable and credentials — and removes only
    the variables that carry a configured secret (matched by name or by exact
    value). Captured output is still scrubbed against the same secrets before it
    could be persisted.

    When ``opencode_pure`` is set, the environment also isolates the child from
    the user's plugins and project config: ``XDG_CONFIG_HOME`` points at
    :func:`isolated_config_home` and project config, autoupdate, and Claude Code
    skills are disabled. Credentials live in the data dir, so they still work.
    """
    source = os.environ if environ is None else environ
    secrets = tuple(secret for secret in settings.llm_secrets if secret)
    scrubbed: dict[str, str] = {}
    for key, value in source.items():
        if key in _SECRET_ENV_NAMES:
            continue
        candidate = value.strip() if isinstance(value, str) else value
        if candidate and any(candidate == secret for secret in secrets):
            continue
        scrubbed[key] = value

    if settings.opencode_pure:
        scrubbed["XDG_CONFIG_HOME"] = str(isolated_config_home(settings))
        scrubbed["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        scrubbed["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        scrubbed["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] = "1"
    return scrubbed


def _drain_stream(
    stream: Any,
    sink: list[bytes],
    *,
    limit: int | None,
    overflow: threading.Event,
) -> None:
    """Read ``stream`` in chunks into ``sink`` without exceeding ``limit`` bytes.

    Runs on a reader thread. When the ceiling is crossed it stops reading and
    raises ``overflow`` so the supervisor can terminate the child instead of
    letting it keep producing output that would be buffered in memory.
    """
    total = 0
    try:
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            if limit is not None:
                remaining = limit - total
                if remaining <= 0:
                    overflow.set()
                    return
                if len(chunk) > remaining:
                    sink.append(chunk[:remaining])
                    overflow.set()
                    return
            sink.append(chunk)
            total += len(chunk)
    except Exception:  # pragma: no cover - stream closed by termination
        pass
    finally:
        try:
            stream.close()
        except Exception:  # pragma: no cover - best effort
            pass


def run_command(
    argv: list[str],
    *,
    cwd: str | None,
    timeout: float,
    env: Mapping[str, str] | None = None,
    max_output_bytes: int | None = None,
) -> CommandOutcome:
    """Run one child process with the output bounded *while it runs*.

    ``subprocess.run(capture_output=True)`` buffers the entire stdout/stderr in
    memory before returning, so a runaway CLI can exhaust memory before any
    ceiling is applied. This runner uses :class:`subprocess.Popen` plus reader
    threads and enforces ``max_output_bytes`` as the bytes arrive: when either
    stream crosses the ceiling the child is killed and ``overflowed`` is set.
    ``timeout`` is enforced by the same supervisor loop, so a hung child is
    terminated instead of blocking the batch.

    The command is always an argument array with ``shell=False``; ``env`` is the
    deliberately scrubbed environment built by :func:`scrubbed_subprocess_env`.
    """
    limit = int(max_output_bytes) if max_output_bytes and max_output_bytes > 0 else None
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        shell=False,
    )

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    overflow = threading.Event()
    readers = [
        threading.Thread(
            target=_drain_stream,
            args=(process.stdout, stdout_chunks),
            kwargs={"limit": limit, "overflow": overflow},
            name="jobscout-opencode-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr_chunks),
            kwargs={"limit": limit, "overflow": overflow},
            name="jobscout-opencode-stderr",
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if overflow.is_set():
            process.kill()
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            process.kill()
            break
        try:
            process.wait(timeout=min(_POLL_INTERVAL_SECONDS, remaining))
            break
        except subprocess.TimeoutExpired:
            continue

    try:
        process.wait(timeout=_READER_JOIN_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - kill is not ignorable
        pass
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)

    # Release the pipes even if a reader thread is still blocked on a stream
    # that never reached EOF, so the descriptor cannot leak into the next call.
    for stream in (process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:  # pragma: no cover - best effort
            pass

    return CommandOutcome(
        returncode=int(process.returncode if process.returncode is not None else -1),
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        overflowed=overflow.is_set(),
    )


def _iter_json_documents(text: str) -> list[Any]:
    """Decode whole output as one JSON value, else as newline-delimited JSON."""
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        return [json.loads(raw)]
    except (json.JSONDecodeError, ValueError):
        pass
    documents: list[Any] = []
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            documents.append(json.loads(candidate))
        except (json.JSONDecodeError, ValueError):
            continue
    return documents


def _error_event_detail(text: str) -> str | None:
    """Return ``"<type>: <message>"`` from the first JSON error event.

    OpenCode emits a stream of JSON events. When the CLI fails after starting,
    the useful diagnostic is in the first ``{"type": "error", ...}`` event
    instead of the raw event stream. Returns ``None`` when no such event exists,
    so the caller can fall back to the previous raw detail.
    """
    for document in _iter_json_documents(text):
        if not isinstance(document, dict) or document.get("type") != "error":
            continue
        error = document.get("error")
        if not isinstance(error, dict):
            continue
        parts = [
            part.strip()
            for part in (error.get("type"), error.get("message"))
            if isinstance(part, str) and part.strip()
        ]
        if parts:
            return ": ".join(parts)
    return None


def _collect_text_parts(value: Any, out: list[str]) -> None:
    """Collect assistant text from OpenCode JSON events.

    OpenCode emits ``{"type": "text", ..., "part": {"type": "text", "text":
    "..."}}``. Only parts explicitly typed ``text`` are collected, so a stray
    ``text`` field inside an evaluation object cannot shadow the evaluation and
    reasoning/thinking parts are skipped.
    """
    if isinstance(value, dict):
        text = value.get("text")
        node_type = value.get("type")
        if isinstance(text, str) and node_type == "text":
            out.append(text)
            return
        for child in value.values():
            _collect_text_parts(child, out)
    elif isinstance(value, list):
        for child in value:
            _collect_text_parts(child, out)


def extract_evaluation_text(raw_output: str) -> str:
    """Return the text that should contain the evaluation JSON.

    Handles three shapes: a JSON event stream (text parts are joined), a single
    JSON document, and plain prose. When nothing structured is found the raw
    output is returned so the schema extractor can still scan it.
    """
    documents = _iter_json_documents(raw_output)
    if not documents:
        return raw_output or ""
    collected: list[str] = []
    for document in documents:
        _collect_text_parts(document, collected)
    if not collected:
        return raw_output or ""
    return "\n".join(collected)


def missing_cli_message(settings: Settings) -> str:
    """Operator-readable error for a missing OpenCode executable."""
    return (
        f"OpenCode CLI '{settings.opencode_command}' was not found. "
        "Install OpenCode and run 'opencode auth login' once; this scorer "
        "relies on that local authentication and does not use "
        "JOBSCOUT_LLM_API_KEY for authentication. Set "
        "JOBSCOUT_OPENCODE_COMMAND to an absolute path or a name on PATH "
        "(a path relative to the current working directory is never used)."
    )


def run_prompt(
    settings: Settings,
    prompt: str,
    *,
    timeout: float | None = None,
    runner: Runner = run_command,
) -> str:
    """Run one OpenCode CLI prompt and return the assistant's text.

    This is the same transport as :meth:`OpenCodeScorer.complete` without the
    evaluation-specific raw/truncated provenance, so commands that only need
    the answer (such as ``dossier``) reuse the resolved executable, the
    isolated working directory/config, the scrubbed environment, and the
    bounded runner. An injected ``runner`` is a test double and keeps the
    configured command name; a real invocation always resolves an absolute
    path so a cwd-relative planted executable cannot be picked up.
    """
    timeout = settings.effective_opencode_timeout_seconds if timeout is None else timeout
    if runner is run_command:
        command = resolve_executable(settings.opencode_command)
        if command is None:
            raise OpenCodeNotConfiguredError(missing_cli_message(settings))
    else:
        command = None
    argv = build_command(settings, prompt, command=command)
    cwd = resolved_workdir(settings)
    env = scrubbed_subprocess_env(settings)

    try:
        outcome = runner(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            max_output_bytes=settings.opencode_max_output_bytes,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpenCodeTimeoutError(
            f"OpenCode CLI timed out after {timeout:g}s"
        ) from exc
    except FileNotFoundError as exc:
        raise OpenCodeNotConfiguredError(
            f"OpenCode CLI '{settings.opencode_command}' could not be "
            "started. Install it and run 'opencode auth login' once."
        ) from exc
    except OSError as exc:
        raise OpenCodeExecutionError(
            f"OpenCode CLI could not be started: {exc}"
        ) from exc

    if getattr(outcome, "timed_out", False):
        raise OpenCodeTimeoutError(f"OpenCode CLI timed out after {timeout:g}s")

    if getattr(outcome, "overflowed", False):
        raise OpenCodeExecutionError(
            "OpenCode CLI produced more than "
            f"{settings.opencode_max_output_bytes} byte(s) of output; "
            "the process was terminated"
        )

    if outcome.returncode != 0:
        error_event = _error_event_detail(
            outcome.stdout or ""
        ) or _error_event_detail(outcome.stderr or "")
        detail = redact_secrets(
            error_event or (outcome.stderr or outcome.stdout or "").strip(),
            settings.llm_secrets,
        )[:MAX_ERROR_CHARS]
        raise OpenCodeExecutionError(
            f"OpenCode CLI exited with code {outcome.returncode}"
            + (f": {detail}" if detail else "")
        )

    raw, _ = bound_output(
        outcome.stdout or "", max_bytes=settings.opencode_max_output_bytes
    )
    text = extract_evaluation_text(raw)
    if not text.strip():
        raise OpenCodeExecutionError("OpenCode CLI returned no output")
    return text


class OpenCodeScorer:
    """Run the local OpenCode CLI and map its answer to an evaluation."""

    name = SCORER_NAME

    def __init__(self, settings: Settings, *, runner: Runner | None = None) -> None:
        self.settings = settings
        self.runner = runner

    # -- configuration ----------------------------------------------------
    def executable_available(self) -> bool:
        return resolve_executable(self.settings.opencode_command) is not None

    def require_configured(self) -> None:
        """Fail early when no runner is injected and the CLI is missing."""
        if self.runner is not None:
            return
        if not self.executable_available():
            raise OpenCodeNotConfiguredError(self._missing_cli_message())

    def _missing_cli_message(self) -> str:
        return missing_cli_message(self.settings)

    def resolve_command(self) -> str:
        """Resolve the executable for a real invocation.

        Only an absolute path or a :func:`shutil.which` hit is accepted, so a
        relative path can never be resolved against the process cwd.
        """
        resolved = resolve_executable(self.settings.opencode_command)
        if resolved is None:
            raise OpenCodeNotConfiguredError(self._missing_cli_message())
        return resolved

    # -- transport --------------------------------------------------------
    def complete(self, job: Any, profile: Profile) -> OpenCodeResponse:
        """Run one CLI call and return the extracted evaluation text."""
        self.require_configured()
        prompt = build_cli_prompt(job, profile)
        # An injected runner is a test double and keeps whatever command the
        # settings name; a real invocation always uses the resolved absolute
        # path so a cwd-relative planted executable cannot be picked up.
        command = None if self.runner is not None else self.resolve_command()
        argv = build_command(self.settings, prompt, command=command)
        timeout = self.settings.effective_opencode_timeout_seconds
        cwd = resolved_workdir(self.settings)
        env = scrubbed_subprocess_env(self.settings)
        active_runner = self.runner or run_command

        try:
            outcome = active_runner(
                argv,
                cwd=cwd,
                timeout=timeout,
                env=env,
                max_output_bytes=self.settings.opencode_max_output_bytes,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenCodeTimeoutError(
                f"OpenCode CLI timed out after {timeout:g}s"
            ) from exc
        except FileNotFoundError as exc:
            raise OpenCodeNotConfiguredError(
                f"OpenCode CLI '{self.settings.opencode_command}' could not be "
                "started. Install it and run 'opencode auth login' once."
            ) from exc
        except OSError as exc:
            raise OpenCodeExecutionError(
                f"OpenCode CLI could not be started: {exc}"
            ) from exc

        if getattr(outcome, "timed_out", False):
            raise OpenCodeTimeoutError(f"OpenCode CLI timed out after {timeout:g}s")

        if getattr(outcome, "overflowed", False):
            raise OpenCodeExecutionError(
                "OpenCode CLI produced more than "
                f"{self.settings.opencode_max_output_bytes} byte(s) of output; "
                "the process was terminated"
            )

        if outcome.returncode != 0:
            error_event = _error_event_detail(
                outcome.stdout or ""
            ) or _error_event_detail(outcome.stderr or "")
            detail = redact_secrets(
                error_event or (outcome.stderr or outcome.stdout or "").strip(),
                self.settings.llm_secrets,
            )[:MAX_ERROR_CHARS]
            raise OpenCodeExecutionError(
                f"OpenCode CLI exited with code {outcome.returncode}"
                + (f": {detail}" if detail else "")
            )

        raw, truncated = bound_output(
            outcome.stdout or "", max_bytes=self.settings.opencode_max_output_bytes
        )
        text = extract_evaluation_text(raw)
        if not text.strip():
            raise OpenCodeExecutionError("OpenCode CLI returned no output")
        return OpenCodeResponse(
            text=text,
            raw=raw,
            model=self.settings.opencode_model,
            truncated=truncated,
        )

    # -- evaluation -------------------------------------------------------
    def score(
        self,
        job: Any,
        profile: Profile,
        *,
        job_id: int | None = None,
        profile_id: int | None = None,
        conn: Any = None,
        now: Any = None,
    ) -> Evaluation:
        """Evaluate one job, always returning an explainable :class:`Evaluation`.

        Failures are represented as evaluations with ``status != ok`` instead of
        raising, so a batch run can continue. ``now`` exists for interface parity
        with the other scorers and is unused.

        Every outcome — CLI failure, parse error, contract error, and success —
        records the same :func:`effective_prompt_version`, so the reasoning
        variant is part of the provenance even when the call did not complete.

        A posting explicitly quoted in a currency outside USD/EUR hard-rejects
        before the CLI is launched: the deterministic currency policy is enforced
        here, so the provider is never invoked and model text cannot overturn it.
        """
        recorded_prompt_version = effective_prompt_version(self.settings)
        rejection = currency_rejection(
            scorer=SCORER_NAME,
            model=self.settings.opencode_model,
            prompt_version=recorded_prompt_version,
            job=job,
            profile=profile,
            job_id=job_id,
            profile_id=profile_id,
        )
        if rejection is not None:
            return rejection
        try:
            response = self.complete(job, profile)
        except OpenCodeError as exc:
            return Evaluation(
                job_id=int(job_id or 0),
                scorer=SCORER_NAME,
                status=EvaluationStatus.ERROR,
                error=redact_secrets(str(exc), self.settings.llm_secrets)[:MAX_ERROR_CHARS],
                recommendation=Recommendation.NEED_MORE_INFO,
                confidence=0.0,
                model=self.settings.opencode_model,
                prompt_version=recorded_prompt_version,
                profile_id=profile_id,
                input_fingerprint=input_fingerprint(job, profile, self.settings),
                rationale="OpenCode evaluation could not be completed.",
            )

        outcome = parse_llm_evaluation(
            response.text, secrets=self.settings.llm_secrets
        )
        raw_document_id = self._store_raw_response(conn, response, outcome.ok, profile)

        if not outcome.ok or outcome.payload is None:
            return Evaluation(
                job_id=int(job_id or 0),
                scorer=SCORER_NAME,
                status=EvaluationStatus.PARSE_ERROR,
                error="; ".join(outcome.errors) or "unparseable OpenCode response",
                recommendation=Recommendation.NEED_MORE_INFO,
                confidence=0.0,
                model=response.model,
                prompt_version=recorded_prompt_version,
                profile_id=profile_id,
                raw_document_id=raw_document_id,
                input_fingerprint=input_fingerprint(job, profile, self.settings),
                rationale="OpenCode response could not be validated as structured JSON.",
            )

        payload = outcome.payload
        contract_errors = success_contract_errors(payload)
        if contract_errors:
            return Evaluation(
                job_id=int(job_id or 0),
                scorer=SCORER_NAME,
                status=EvaluationStatus.PARSE_ERROR,
                error="; ".join(contract_errors),
                recommendation=Recommendation.NEED_MORE_INFO,
                confidence=0.0,
                model=response.model,
                prompt_version=recorded_prompt_version,
                profile_id=profile_id,
                raw_document_id=raw_document_id,
                input_fingerprint=input_fingerprint(job, profile, self.settings),
                rationale="OpenCode output was structurally valid but not a publishable evaluation.",
            )
        from ..models import DimensionScore

        dimensions = [
            DimensionScore.from_dict(item)
            for item in payload.get("dimensions", [])
            if isinstance(item, dict)
        ]
        rationale = str(payload.get("rationale") or "")
        if outcome.repairs:
            rationale = (rationale + " [repaired: " + "; ".join(outcome.repairs) + "]").strip()
        return Evaluation(
            job_id=int(job_id or 0),
            scorer=SCORER_NAME,
            overall_score=payload.get("overall_score"),
            recommendation=payload.get("recommendation") or Recommendation.NEED_MORE_INFO,
            confidence=float(payload.get("confidence") or 0.0),
            dimensions=dimensions,
            blockers=list(payload.get("blockers") or []),
            strengths=list(payload.get("strengths") or []),
            gaps=list(payload.get("gaps") or []),
            unknowns=list(payload.get("unknowns") or []),
            rationale=rationale,
            salary_estimate_low=payload.get("salary_estimate_low"),
            salary_estimate_mid=payload.get("salary_estimate_mid"),
            salary_estimate_high=payload.get("salary_estimate_high"),
            employer_tier=payload.get("employer_tier"),
            salary_basis=payload.get("salary_basis"),
            status=EvaluationStatus.OK,
            model=response.model,
            prompt_version=recorded_prompt_version,
            profile_id=profile_id,
            raw_document_id=raw_document_id,
            input_fingerprint=input_fingerprint(job, profile, self.settings),
        )

    def _store_raw_response(
        self,
        conn: Any,
        response: OpenCodeResponse,
        parsed_ok: bool,
        profile: Profile,
    ) -> int | None:
        """Persist the bounded CLI output when it is safe and enabled.

        Raw responses are stored under the existing ``llm_response`` kind so the
        schema and provenance queries stay unchanged; ``source`` marks the
        backend. A credential-looking or oversized payload is recorded with
        metadata only.
        """
        if conn is None or not self.settings.llm_store_raw:
            return None
        raw_text = response.raw
        safe, reason = is_safe_to_store(
            raw_text,
            secrets=self.settings.llm_secrets,
            max_bytes=self.settings.raw_max_bytes,
        )
        if safe:
            notes = f"profile={profile.name}; parsed_ok={parsed_ok}; backend=opencode"
            if response.truncated:
                notes += "; output truncated by JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES"
        else:
            notes = (
                f"response not stored: {reason}; parsed_ok={parsed_ok}; backend=opencode"
            )
        document = RawDocument(
            kind=RawDocumentKind.LLM_RESPONSE,
            source="opencode",
            url=None,
            status_code=None,
            content_type="application/x-ndjson",
            sha256=sha256_text(raw_text),
            byte_size=len(raw_text.encode("utf-8", errors="replace")),
            fetched_at=utcnow_iso(),
            stored_at=utcnow_iso(),
            truncated=(not safe) or response.truncated,
            notes=notes,
            body=raw_text if safe else None,
        )
        return insert_raw_document(conn, document)


__all__ = [
    "CommandOutcome",
    "OPENCODE_OUTPUT_CONTRACT",
    "isolated_config_home",
    "isolated_workdir",
    "OpenCodeError",
    "OpenCodeExecutionError",
    "OpenCodeNotConfiguredError",
    "OpenCodeResponse",
    "OpenCodeScorer",
    "OpenCodeTimeoutError",
    "SCORER_NAME",
    "bound_output",
    "build_cli_prompt",
    "build_command",
    "effective_prompt_version",
    "extract_evaluation_text",
    "input_fingerprint",
    "resolve_executable",
    "resolved_workdir",
    "run_command",
    "scrubbed_subprocess_env",
]
