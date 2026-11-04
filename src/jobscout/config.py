"""Runtime configuration.

Precedence (highest first): explicit CLI arguments > environment variables >
``.env`` file > built-in conservative defaults.

Every knob is configurable through ``JOBSCOUT_*`` environment variables so the
same code runs offline in tests and against a live endpoint in manual runs.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .keywords import UnknownKeywordFamily, get_family
from .url_policy import (
    UrlPolicyError,
    validate_getmanfred_url,
    validate_linkedin_url,
    validate_llm_base_url,
    validate_tecnoempleo_url,
)

PREFIX = "JOBSCOUT_"

DEFAULT_USER_AGENT = "jobscout/0.1 (local research tool)"
DEFAULT_LINKEDIN_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
DEFAULT_LINKEDIN_DETAIL_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
)
#: GetManfred advertises this sitemap in its public ``robots.txt`` (which also
#: allows ``/``), and its entries are individual offer detail URLs. Inventory
#: mode reads it without any keyword fan-out.
DEFAULT_GETMANFRED_SITEMAP_URL = "https://www.getmanfred.com/sitemap-offers.xml"
DEFAULT_GETMANFRED_OFFER_PATH_PREFIX = "/ofertas-empleo/"
#: GetManfred's public ``robots.txt``. Re-fetched before the first detail
#: request; the current document allows ``/`` for every user agent.
DEFAULT_GETMANFRED_ROBOTS_URL = "https://www.getmanfred.com/robots.txt"
#: Bounded item cap for the opt-in GetManfred detail-enrichment run. Kept
#: separate from the sitemap inventory cap so a sweep and an enrichment run can
#: be budgeted independently.
DEFAULT_GETMANFRED_DETAIL_MAX_ITEMS = 20
# The public offers sitemap is larger than the generic raw-document ceiling.
# Keep this provider-specific allowance bounded instead of raising the global
# limit used by every other collector.
DEFAULT_GETMANFRED_MAX_BYTES = 1_048_576
HARD_MAX_GETMANFRED_BYTES = 1_048_576
#: Default recency window for the sitemap inventory. GetManfred's sitemap keeps
#: offers from years ago, so entries whose ``lastmod`` is older are skipped;
#: ``0`` disables the cut.
DEFAULT_GETMANFRED_MAX_AGE_DAYS = 120
HARD_MAX_GETMANFRED_AGE_DAYS = 3650
#: Tecnoempleo's public, server-rendered keyword search lives under this path.
#: Its ``robots.txt`` allows it (it only disallows private/API/feed paths such
#: as ``/alertas-empleo-rss.php`` and ``/profesionales/``), which the collector
#: re-checks at runtime before any request.
DEFAULT_TECNOEMPLEO_SEARCH_URL = "https://www.tecnoempleo.com/ofertas-trabajo/"
DEFAULT_TECNOEMPLEO_ROBOTS_URL = "https://www.tecnoempleo.com/robots.txt"
#: Tecnoempleo offer detail links carry this stable path fragment; the search
#: page parser uses it to recognise an offer anchor without relying on volatile
#: CSS class names.
DEFAULT_TECNOEMPLEO_OFFER_PATH_MARKER = "rf-"
DEFAULT_LLM_MODEL = "deepseek-v4.1-flash"

# OpenCode CLI scorer. The model id matches the locally configured OpenCode
# provider; authentication is handled entirely by the CLI's own credential
# store, so no API key ever flows through jobscout.
# Default to the model documented in .env.example. The ``opencode-go/`` prefix
# keeps scoring inside the owner's included OpenCode Go subscription; the
# ``opencode/`` prefix would be pay-per-use.
DEFAULT_OPENCODE_MODEL = "opencode-go/deepseek-v4.1-flash"
DEFAULT_OPENCODE_COMMAND = "opencode"
# Low-reasoning variant appended to the model as ``<model>#<variant>``. A
# smaller reasoning budget makes the CLI more likely to close the JSON object
# instead of stopping mid-object; ``low`` is the smallest variant OpenCode 2.x
# still offers for the default model (``minimal`` was removed). Set
# JOBSCOUT_OPENCODE_VARIANT to empty to omit the variant entirely.
DEFAULT_OPENCODE_VARIANT = "low"
#: Optional model (possibly ``<model>#<variant>``) used when the primary model
#: fails with a technical error during a ``score`` run.
DEFAULT_OPENCODE_FALLBACK_MODEL: str | None = None
# Run from a private, empty directory next to the database by default so the
# CLI does not pick up this repository's AGENTS.md/opencode.json instructions
# and cannot see another process's files. Override with
# JOBSCOUT_OPENCODE_WORKDIR; a library caller can pass "" to inherit the cwd.
DEFAULT_OPENCODE_WORKDIR: str | None = None
DEFAULT_OPENCODE_TIMEOUT_SECONDS = 180.0
# Per-offer timeout by reasoning effort when no explicit timeout is set. A
# higher effort reasons longer per offer; 180 s was sized for ``low``.
EFFORT_TIMEOUT_SECONDS = {
    "minimal": 180.0,
    "low": 180.0,
    "medium": 240.0,
    "high": 360.0,
    "max": 600.0,
}
DEFAULT_OPENCODE_MAX_OUTPUT_BYTES = 1_048_576
# Isolate the CLI from the user's plugins and project config by default. Plugins
# are arbitrary code loaded from the local OpenCode config, so an isolated
# ``--standalone`` invocation with a private config home is the most restrictive
# option available without editing global config. See the README for the residual
# tool-permission risk this does not remove.
DEFAULT_OPENCODE_PURE = True

# Absolute ceilings for LinkedIn collection volume. These are true hard upper
# bounds: no CLI flag, environment variable, or library caller can exceed them.
# They are deliberately conservative for a low-volume, good-citizen collector.
HARD_MAX_PAGES = 10
HARD_MAX_ITEMS = 250

# Local notifications (opt-in, disabled by default). Selection is offline and
# read-only; delivery is stdout unless the operator explicitly opts into a
# discovered local system notifier.
DEFAULT_NOTIFY_SCORE_THRESHOLD = 80.0
DEFAULT_NOTIFY_DELIVERY = "stdout"
NOTIFY_DELIVERY_CHOICES: tuple[str, ...] = ("stdout", "system")
NOTIFY_PREFERENCE_OUTCOMES: tuple[str, ...] = ("pass", "flag", "fail")


class ConfigError(ValueError):
    """Raised when an environment variable cannot be interpreted."""


def validate_bounded_int(
    value: int,
    *,
    name: str,
    hard_max: int,
    minimum: int = 1,
) -> int:
    """Return ``value`` if within ``[minimum, hard_max]``, else raise.

    ``ConfigError`` subclasses :class:`ValueError`, so library callers that
    already validate with ``ValueError`` keep working while the CLI can present
    a clean message instead of a traceback.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if value > hard_max:
        raise ConfigError(
            f"{name} must be <= {hard_max} (absolute upper bound), got {value}"
        )
    return value


#: Optional quoting/escaping around a JSON-ish label or value.
#:
#: Covers a plain quote (``"password"``), a single-escaped quote as it appears
#: when a JSON document is embedded in a JSON string (``\"password\"``), and the
#: multiply-escaped form (``\\\"password\\\"``) produced when that document is
#: nested again — which is how model and API payloads carry credentials. Used
#: around both the label and the value; the mandatory separator that follows
#: keeps a bare quoted label (``"token"``) from matching on its own.
_QUOTE = r'(?:\\+["\']?|["\'])?'


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Provider API keys ("sk-", "sk-proj-", "sk-ant-", Stripe-style "sk_live_").
    # The leading boundary keeps ordinary hyphenated words such as
    # "risk-assessment", "task-management", and "desk-organization" from
    # matching: the "sk" there is preceded by a letter, so the token cannot be
    # a credential.
    re.compile(
        r"(?<![A-Za-z0-9])sk[_-](?:proj[_-]|ant[_-]|live[_-]|test[_-])?"
        r"[A-Za-z0-9][A-Za-z0-9_\-]{15,}"
    ),
    # JWTs (base64url header.payload.signature).
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
    # Labelled credentials require an explicit separator and a long token, so
    # prose such as "API key management" is not treated as a secret. The optional
    # quotes around the label and the value cover JSON/object forms such as
    # ``{"api_key": "0123456789abcdef"}`` and ``{"authorization": "Bearer ..."}``
    # without matching a bare quoted label followed by a non-separator.
    re.compile(
        r"(?i)\b(api[_-]?key|x-api-key|authorization)\b"
        + _QUOTE + r"\s*[:=]\s*"
        + _QUOTE + r"(?:bearer\s+)?[A-Za-z0-9_\-\.]{16,}"
    ),
    # A bare bearer token, long enough to be a credential rather than prose.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{20,}"),
    # PEM private-key blocks (RSA/EC/DSA/OPENSSH/PGP and unlabelled).
    re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----"),
    # LinkedIn session cookies, always as a labelled value. The 10-character
    # minimum avoids prose such as "li_at: not set". The optional quote after the
    # label covers the JSON form ``{"li_at": "AQED..."}``.
    re.compile(
        r"(?i)\b(li_at|jsessionid)\b"
        + _QUOTE + r"\s*[:=]\s*"
        + _QUOTE + r"[A-Za-z0-9_\-\.%:]{10,}"
    ),
    # Labelled token/secret/password assignments, including ``*_TOKEN`` and
    # ``*_SECRET`` env-style names and OAuth ``access_token``/``refresh_token``.
    # A 12-character token-shaped value is required so prose such as
    # "token: missing", "password: required", or "secret: none" stays clean.
    # The optional quotes around the label and value cover JSON/object forms such
    # as ``{"password": "correct-horse..."}`` and ``{"client_secret": "..."}``,
    # which is how model and API payloads label credentials.
    re.compile(
        r"(?i)\b(?:[a-z0-9]+[_-])*(?:access[_-]?token|refresh[_-]?token|"
        r"auth[_-]?token|id[_-]?token|session[_-]?token|client[_-]?secret|"
        r"api[_-]?secret|token|secret|password|passwd|pwd)\b"
        + _QUOTE + r"\s*[:=]\s*"
        + _QUOTE + r"[A-Za-z0-9_\-\./+]{12,}"
    ),
    # Cloud/provider prefixed keys: AWS access key ids, GitHub tokens, NVIDIA.
    re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{20,}"),
)


#: The public Cloudflare beacon script (``data-cf-beacon``) publishes a 32-hex
#: site token in its JSON ``token`` field. Cloudflare ships that value to every
#: visitor, so it is not a credential; without an exception the generic
#: labelled-token pattern rejects the whole page body and Tecnoempleo detail
#: provenance is lost. Only this exact attribute context with a 32-hex value is
#: exempted. A real ``token``/``password``/``secret`` form field, a JWT, or a
#: beacon token that is not exactly 32 hex characters still fails closed.
_CF_BEACON_ATTRIBUTE_RE = re.compile(
    r"""data-cf-beacon\s*=\s*(?P<quote>["'])(?P<value>.*?)(?P=quote)""",
    re.IGNORECASE | re.DOTALL,
)
_CF_BEACON_TOKEN_RE = re.compile(
    r"""["']?token["']?\s*:\s*["'][0-9a-fA-F]{32}["']""",
    re.IGNORECASE,
)
#: Neutral replacement for the exempted key/value pair before pattern matching.
#: It carries no credential-shaped label and no long token value.
_CF_BEACON_MASK = '"cf-beacon-public": ""'


def _without_cloudflare_beacon_tokens(text: str) -> str:
    """Return a copy with the public Cloudflare beacon token neutralized.

    The exception is deliberately narrow: it only applies inside a
    ``data-cf-beacon`` attribute and only to a JSON ``token`` whose value is
    exactly 32 hex characters. Everything else is returned unchanged, so the
    generic credential patterns keep seeing real form fields and JWT-like
    values.
    """
    if "cf-beacon" not in text.lower():
        return text

    def _neutralize_attribute(attribute: re.Match[str]) -> str:
        value = _CF_BEACON_TOKEN_RE.sub(_CF_BEACON_MASK, attribute.group("value"))
        return (
            f"data-cf-beacon={attribute.group('quote')}{value}"
            f"{attribute.group('quote')}"
        )

    return _CF_BEACON_ATTRIBUTE_RE.sub(_neutralize_attribute, text)


def redact_secrets(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Redact anything that looks like a credential.

    Used both for display and for the model-derived evaluation text that is
    persisted and printed (rationale, dimension text, and schema repair/error
    diagnostics), which is sanitized in place before it can reach the database
    or the console. Raw payloads are still guarded separately by
    :func:`is_safe_to_store`, which drops the whole body when it is too large or
    credential-looking rather than rewriting it in place.
    """
    if not text:
        return text
    # The public Cloudflare beacon token is exempted before any pattern runs, so
    # it is not rewritten as a credential while everything else still is.
    redacted = _without_cloudflare_beacon_tokens(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "***REDACTED***")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("***REDACTED***", redacted)
    return redacted


def is_safe_to_store(
    text: str | None,
    *,
    secrets: tuple[str, ...] = (),
    max_bytes: int = 262_144,
) -> tuple[bool, str]:
    """Decide whether a raw payload may be persisted verbatim.

    Returns ``(safe, reason)``. Any doubt resolves to "not safe": we would
    rather lose provenance than write credentials or unbounded blobs to disk.
    """
    if text is None:
        return False, "empty payload"
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        return False, f"payload exceeds {max_bytes} bytes"
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in text:
            return False, "payload contains a configured secret"
    analyzed = _without_cloudflare_beacon_tokens(text)
    for pattern in _SECRET_PATTERNS:
        if pattern.search(analyzed):
            return False, "payload looks like it contains a credential"
    return True, "ok"


def load_dotenv(path: str | Path) -> dict[str, str]:
    """Minimal ``.env`` reader.

    Only ``KEY=value`` lines are supported. Existing process environment
    variables always win; this function never mutates the environment.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _merged_env(env: dict[str, str] | None, dotenv_path: str | Path | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    if dotenv_path is not None:
        merged.update(load_dotenv(dotenv_path))
    merged.update(dict(os.environ if env is None else env))
    return merged


def _raw(env: dict[str, str], name: str) -> str | None:
    value = env.get(PREFIX + name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_str(env: dict[str, str], name: str, default: str) -> str:
    return _raw(env, name) or default


def _env_opt_str(env: dict[str, str], name: str, default: str | None = None) -> str | None:
    return _raw(env, name) or default


def _env_opt_str_allow_empty_none(
    env: dict[str, str], name: str, default: str | None
) -> str | None:
    """Return ``default`` when the variable is unset, ``None`` when it is empty.

    Unlike :func:`_env_opt_str`, an explicitly empty value disables the option
    instead of falling back to the default. This lets an operator write
    ``JOBSCOUT_OPENCODE_VARIANT=`` to omit the variant from the model id while
    leaving the variable unset keeps the conservative default.
    """
    key = PREFIX + name
    if key not in env:
        return default
    value = (env.get(key) or "").strip()
    return value or None


def _env_int(env: dict[str, str], name: str, default: int) -> int:
    raw = _raw(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{PREFIX}{name} must be an integer, got {raw!r}") from exc


def _env_float(env: dict[str, str], name: str, default: float | None) -> float | None:
    raw = _raw(env, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{PREFIX}{name} must be a number, got {raw!r}") from exc


def _env_str_list(env: dict[str, str], name: str) -> tuple[str, ...]:
    """Parse a comma-separated list, dropping blanks and duplicates.

    Order is preserved so a documented family order survives a round trip
    through the environment; an unset or empty variable yields an empty tuple.
    """
    raw = _raw(env, name)
    if raw is None:
        return ()
    items: list[str] = []
    for chunk in raw.split(","):
        value = chunk.strip()
        if value and value not in items:
            items.append(value)
    return tuple(items)


_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = _raw(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{PREFIX}{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    # Storage
    db_path: Path = Path("data/jobscout.sqlite3")
    profile_path: Path = Path("profile.example.yaml")

    # HTTP / collector behaviour (conservative by default)
    http_user_agent: str = DEFAULT_USER_AGENT
    contact_email: str | None = None
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 2
    request_delay_seconds: float = 8.0
    request_jitter_seconds: float = 2.0
    max_pages: int = 2
    max_items: int = 40
    #: Default families for ``keyword-plan``. Empty means "every family in the
    #: catalog". Ids are validated against the catalog so a typo fails at
    #: startup instead of silently planning nothing.
    keyword_families: tuple[str, ...] = ()
    linkedin_search_url: str = DEFAULT_LINKEDIN_SEARCH_URL
    # Download ceiling for a live search response as well as the limit above
    # which a raw body is not stored verbatim: the search transport reads at most
    # this many bytes, and a body that hits the ceiling is recorded as metadata
    # only (see ``collectors.linkedin.search_linkedin``).
    raw_max_bytes: int = 262_144

    # Opt-in job-detail enrichment (bounded like the search collector)
    linkedin_detail_url: str = DEFAULT_LINKEDIN_DETAIL_URL
    detail_max_items: int = 20
    detail_timeout_seconds: float = 20.0
    # Download ceiling for a detail response. The fetcher also caps what it
    # persists with ``raw_max_bytes``, so a body above either ceiling is never
    # stored verbatim (see ``collectors.linkedin_details._record_detail_response``).
    detail_max_bytes: int = 524_288

    # GetManfred public sitemap inventory (opt-in, disabled by default).
    getmanfred_sitemap_url: str = DEFAULT_GETMANFRED_SITEMAP_URL
    getmanfred_robots_url: str = DEFAULT_GETMANFRED_ROBOTS_URL
    getmanfred_offer_path_prefix: str = DEFAULT_GETMANFRED_OFFER_PATH_PREFIX
    getmanfred_max_bytes: int = DEFAULT_GETMANFRED_MAX_BYTES
    #: Bounded number of child sitemaps to follow when the configured URL is a
    #: sitemap index rather than a urlset.
    getmanfred_max_documents: int = 1
    getmanfred_max_items: int = 40
    getmanfred_detail_max_items: int = DEFAULT_GETMANFRED_DETAIL_MAX_ITEMS
    #: Skip sitemap offers whose ``lastmod`` is older than this many days
    #: (``0`` = no cut); the sitemap keeps offers from years ago.
    getmanfred_max_age_days: int = DEFAULT_GETMANFRED_MAX_AGE_DAYS

    # Tecnoempleo public keyword search (opt-in, disabled by default).
    tecnoempleo_search_url: str = DEFAULT_TECNOEMPLEO_SEARCH_URL
    tecnoempleo_robots_url: str = DEFAULT_TECNOEMPLEO_ROBOTS_URL
    tecnoempleo_offer_path_marker: str = DEFAULT_TECNOEMPLEO_OFFER_PATH_MARKER
    tecnoempleo_max_pages: int = 2
    tecnoempleo_max_items: int = 40
    #: Bounded item cap for the opt-in Tecnoempleo detail-enrichment run. Kept
    #: separate from the search cap so a sweep and an enrichment run can be
    #: budgeted independently.
    tecnoempleo_detail_max_items: int = 20

    # Local notifications (opt-in, disabled by default). ``notify_enabled``
    # gates the whole ``notify`` command: the disabled default reads nothing.
    # ``notify_score_threshold`` must be a finite number in ``0..100``;
    # ``notify_preference_outcome`` is ``None``/empty (no filter) or one of
    # ``pass``/``flag``/``fail``; ``notify_delivery`` is ``stdout`` or
    # ``system`` (with a stdout fallback when no local notifier exists).
    notify_enabled: bool = False
    notify_score_threshold: float = DEFAULT_NOTIFY_SCORE_THRESHOLD
    notify_preference_outcome: str | None = None
    notify_delivery: str = DEFAULT_NOTIFY_DELIVERY

    # LLM scorer (optional; never required for tests)
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = DEFAULT_LLM_MODEL
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1200
    llm_store_raw: bool = True
    llm_json_mode: bool = True

    # OpenCode CLI scorer (optional; uses the CLI's own local authentication and
    # never an API key). ``opencode_workdir`` defaults to a private, empty
    # directory next to the database so the CLI cannot load this repository's
    # agent instructions or read another process's files; a library caller can
    # pass ``""`` to inherit the process working directory.
    opencode_command: str = DEFAULT_OPENCODE_COMMAND
    opencode_model: str = DEFAULT_OPENCODE_MODEL
    opencode_variant: str | None = DEFAULT_OPENCODE_VARIANT
    #: ``None`` derives the timeout from the effort (see
    #: :attr:`effective_opencode_timeout_seconds`).
    opencode_timeout_seconds: float | None = None
    opencode_workdir: str | None = DEFAULT_OPENCODE_WORKDIR
    opencode_max_output_bytes: int = DEFAULT_OPENCODE_MAX_OUTPUT_BYTES
    opencode_json_events: bool = True
    #: Run the CLI in isolation (``--standalone`` plus a private config home)
    #: so it loads no user plugins or project config.
    opencode_pure: bool = DEFAULT_OPENCODE_PURE
    #: Optional model used when the primary model fails with a technical error.
    opencode_fallback_model: str | None = DEFAULT_OPENCODE_FALLBACK_MODEL

    extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Enforce the absolute caps here as well so direct library callers and
        # ``with_overrides`` cannot bypass the CLI/environment checks.
        validate_bounded_int(self.max_pages, name="max_pages", hard_max=HARD_MAX_PAGES)
        validate_bounded_int(self.max_items, name="max_items", hard_max=HARD_MAX_ITEMS)
        validate_bounded_int(
            self.detail_max_items, name="detail_max_items", hard_max=HARD_MAX_ITEMS
        )
        validate_bounded_int(
            self.getmanfred_max_items, name="getmanfred_max_items", hard_max=HARD_MAX_ITEMS
        )
        validate_bounded_int(
            self.getmanfred_detail_max_items,
            name="getmanfred_detail_max_items",
            hard_max=HARD_MAX_ITEMS,
        )
        validate_bounded_int(
            self.getmanfred_max_documents, name="getmanfred_max_documents", hard_max=HARD_MAX_PAGES
        )
        validate_bounded_int(
            self.getmanfred_max_bytes,
            name="getmanfred_max_bytes",
            hard_max=HARD_MAX_GETMANFRED_BYTES,
        )
        validate_bounded_int(
            self.getmanfred_max_age_days,
            name="getmanfred_max_age_days",
            hard_max=HARD_MAX_GETMANFRED_AGE_DAYS,
            minimum=0,
        )
        validate_bounded_int(
            self.tecnoempleo_max_items, name="tecnoempleo_max_items", hard_max=HARD_MAX_ITEMS
        )
        validate_bounded_int(
            self.tecnoempleo_max_pages, name="tecnoempleo_max_pages", hard_max=HARD_MAX_PAGES
        )
        validate_bounded_int(
            self.tecnoempleo_detail_max_items,
            name="tecnoempleo_detail_max_items",
            hard_max=HARD_MAX_ITEMS,
        )
        if not self.getmanfred_offer_path_prefix.startswith("/"):
            raise ConfigError("getmanfred_offer_path_prefix must start with '/'")
        if not self.tecnoempleo_offer_path_marker.strip():
            raise ConfigError("tecnoempleo_offer_path_marker must be non-empty")
        # Configured endpoints are validated here so no CLI flag, environment
        # variable, or library caller can point the collectors or the LLM
        # scorer at a non-HTTPS scheme, an unrelated host, or a URL carrying
        # embedded credentials.
        try:
            validate_linkedin_url(self.linkedin_search_url, name="linkedin_search_url")
            validate_linkedin_url(self.linkedin_detail_url, name="linkedin_detail_url")
            validate_getmanfred_url(self.getmanfred_sitemap_url, name="getmanfred_sitemap_url")
            validate_getmanfred_url(self.getmanfred_robots_url, name="getmanfred_robots_url")
            validate_tecnoempleo_url(self.tecnoempleo_search_url, name="tecnoempleo_search_url")
            validate_tecnoempleo_url(self.tecnoempleo_robots_url, name="tecnoempleo_robots_url")
            if self.llm_base_url:
                validate_llm_base_url(self.llm_base_url, name="llm_base_url")
        except UrlPolicyError as exc:
            raise ConfigError(str(exc)) from exc
        if self.detail_timeout_seconds <= 0:
            raise ConfigError("detail_timeout_seconds must be > 0")
        if self.detail_max_bytes < 1:
            raise ConfigError("detail_max_bytes must be >= 1")
        # Keyword families are a closed catalog: an unknown id is a
        # configuration error, never a silently empty plan.
        for family_id in self.keyword_families:
            try:
                get_family(family_id)
            except UnknownKeywordFamily as exc:
                raise ConfigError(str(exc)) from exc
        if not self.opencode_command or not self.opencode_command.strip():
            raise ConfigError("opencode_command must be a non-empty executable name or path")
        if not self.opencode_model or not self.opencode_model.strip():
            raise ConfigError("opencode_model must be a non-empty provider/model string")
        if self.opencode_timeout_seconds is not None and self.opencode_timeout_seconds <= 0:
            raise ConfigError("opencode_timeout_seconds must be > 0")
        if self.opencode_max_output_bytes < 1:
            raise ConfigError("opencode_max_output_bytes must be >= 1")
        # Local-notification knobs are validated here as well, so a direct
        # library caller or ``with_overrides`` cannot bypass the CLI/env checks.
        if isinstance(self.notify_score_threshold, bool) or not isinstance(
            self.notify_score_threshold, (int, float)
        ):
            raise ConfigError("notify_score_threshold must be a number between 0 and 100")
        threshold = float(self.notify_score_threshold)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 100.0:
            raise ConfigError("notify_score_threshold must be a finite number between 0 and 100")
        if self.notify_preference_outcome not in (None, "") and (
            self.notify_preference_outcome not in NOTIFY_PREFERENCE_OUTCOMES
        ):
            raise ConfigError(
                "notify_preference_outcome must be one of "
                f"{', '.join(NOTIFY_PREFERENCE_OUTCOMES)} or empty"
            )
        if self.notify_delivery not in NOTIFY_DELIVERY_CHOICES:
            raise ConfigError(
                "notify_delivery must be one of " f"{', '.join(NOTIFY_DELIVERY_CHOICES)}"
            )

    # -- derived helpers ------------------------------------------------
    @property
    def user_agent(self) -> str:
        """Honest, non-spoofing user agent including an optional contact."""
        if self.contact_email:
            return f"{self.http_user_agent}; contact: {self.contact_email}"
        return self.http_user_agent

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key)

    @property
    def llm_secrets(self) -> tuple[str, ...]:
        return tuple(secret for secret in (self.llm_api_key,) if secret)

    @property
    def effective_opencode_timeout_seconds(self) -> float:
        """Per-offer timeout: the explicit value, else sized by the effort."""
        if self.opencode_timeout_seconds is not None:
            return self.opencode_timeout_seconds
        effort = self.opencode_variant
        if "#" in self.opencode_model:
            # ``build_command`` leaves a model that already carries a variant
            # untouched, so that suffix is the effort actually sent.
            effort = self.opencode_model.split("#", 1)[1]
        return EFFORT_TIMEOUT_SECONDS.get(
            (effort or "").strip().lower(), DEFAULT_OPENCODE_TIMEOUT_SECONDS
        )

    def with_overrides(self, **overrides: object) -> "Settings":
        """Return a copy with non-``None`` overrides applied."""
        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **clean)  # type: ignore[arg-type]

    def llm_endpoint(self) -> str:
        """Normalized ``/chat/completions`` URL for an OpenAI-compatible API."""
        if not self.llm_base_url:
            raise ConfigError(
                "JOBSCOUT_LLM_BASE_URL is not configured; the LLM scorer is optional."
            )
        base = self.llm_base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    # -- construction ---------------------------------------------------
    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        dotenv_path: str | Path | None = ".env",
    ) -> "Settings":
        merged = _merged_env(env, dotenv_path)
        return cls(
            db_path=Path(_env_str(merged, "DB_PATH", "data/jobscout.sqlite3")),
            profile_path=Path(_env_str(merged, "PROFILE_PATH", "profile.example.yaml")),
            http_user_agent=_env_str(merged, "HTTP_USER_AGENT", DEFAULT_USER_AGENT),
            contact_email=_env_opt_str(merged, "CONTACT_EMAIL"),
            http_timeout_seconds=_env_float(merged, "HTTP_TIMEOUT_SECONDS", 20.0),
            http_max_retries=_env_int(merged, "HTTP_MAX_RETRIES", 2),
            request_delay_seconds=_env_float(merged, "REQUEST_DELAY_SECONDS", 8.0),
            request_jitter_seconds=_env_float(merged, "REQUEST_JITTER_SECONDS", 2.0),
            max_pages=_env_int(merged, "MAX_PAGES", 2),
            max_items=_env_int(merged, "MAX_ITEMS", 40),
            keyword_families=_env_str_list(merged, "KEYWORD_FAMILIES"),
            linkedin_search_url=_env_str(
                merged, "LINKEDIN_SEARCH_URL", DEFAULT_LINKEDIN_SEARCH_URL
            ),
            raw_max_bytes=_env_int(merged, "RAW_MAX_BYTES", 262_144),
            linkedin_detail_url=_env_str(
                merged, "LINKEDIN_DETAIL_URL", DEFAULT_LINKEDIN_DETAIL_URL
            ),
            detail_max_items=_env_int(merged, "DETAIL_MAX_ITEMS", 20),
            detail_timeout_seconds=_env_float(merged, "DETAIL_TIMEOUT_SECONDS", 20.0),
            detail_max_bytes=_env_int(merged, "DETAIL_MAX_BYTES", 524_288),
            getmanfred_sitemap_url=_env_str(
                merged, "GETMANFRED_SITEMAP_URL", DEFAULT_GETMANFRED_SITEMAP_URL
            ),
            getmanfred_robots_url=_env_str(
                merged, "GETMANFRED_ROBOTS_URL", DEFAULT_GETMANFRED_ROBOTS_URL
            ),
            getmanfred_offer_path_prefix=_env_str(
                merged, "GETMANFRED_OFFER_PATH_PREFIX", DEFAULT_GETMANFRED_OFFER_PATH_PREFIX
            ),
            getmanfred_max_bytes=_env_int(
                merged, "GETMANFRED_MAX_BYTES", DEFAULT_GETMANFRED_MAX_BYTES
            ),
            getmanfred_max_documents=_env_int(merged, "GETMANFRED_MAX_DOCUMENTS", 1),
            getmanfred_max_items=_env_int(merged, "GETMANFRED_MAX_ITEMS", 40),
            getmanfred_detail_max_items=_env_int(
                merged, "GETMANFRED_DETAIL_MAX_ITEMS", DEFAULT_GETMANFRED_DETAIL_MAX_ITEMS
            ),
            getmanfred_max_age_days=_env_int(
                merged, "GETMANFRED_MAX_AGE_DAYS", DEFAULT_GETMANFRED_MAX_AGE_DAYS
            ),
            tecnoempleo_search_url=_env_str(
                merged, "TECNOEMPLEO_SEARCH_URL", DEFAULT_TECNOEMPLEO_SEARCH_URL
            ),
            tecnoempleo_robots_url=_env_str(
                merged, "TECNOEMPLEO_ROBOTS_URL", DEFAULT_TECNOEMPLEO_ROBOTS_URL
            ),
            tecnoempleo_offer_path_marker=_env_str(
                merged, "TECNOEMPLEO_OFFER_PATH_MARKER", DEFAULT_TECNOEMPLEO_OFFER_PATH_MARKER
            ),
            tecnoempleo_max_pages=_env_int(merged, "TECNOEMPLEO_MAX_PAGES", 2),
            tecnoempleo_max_items=_env_int(merged, "TECNOEMPLEO_MAX_ITEMS", 40),
            tecnoempleo_detail_max_items=_env_int(
                merged, "TECNOEMPLEO_DETAIL_MAX_ITEMS", 20
            ),
            notify_enabled=_env_bool(merged, "NOTIFY_ENABLED", False),
            notify_score_threshold=_env_float(
                merged, "NOTIFY_SCORE_THRESHOLD", DEFAULT_NOTIFY_SCORE_THRESHOLD
            ),
            notify_preference_outcome=_env_opt_str(
                merged, "NOTIFY_PREFERENCE_OUTCOME"
            ),
            notify_delivery=_env_str(merged, "NOTIFY_DELIVERY", DEFAULT_NOTIFY_DELIVERY),
            llm_base_url=_env_opt_str(merged, "LLM_BASE_URL"),
            llm_api_key=_env_opt_str(merged, "LLM_API_KEY"),
            llm_model=_env_str(merged, "LLM_MODEL", DEFAULT_LLM_MODEL),
            llm_timeout_seconds=_env_float(merged, "LLM_TIMEOUT_SECONDS", 60.0),
            llm_max_retries=_env_int(merged, "LLM_MAX_RETRIES", 2),
            llm_temperature=_env_float(merged, "LLM_TEMPERATURE", 0.0),
            llm_max_tokens=_env_int(merged, "LLM_MAX_TOKENS", 1200),
            llm_store_raw=_env_bool(merged, "LLM_STORE_RAW", True),
            llm_json_mode=_env_bool(merged, "LLM_JSON_MODE", True),
            opencode_command=_env_str(merged, "OPENCODE_COMMAND", DEFAULT_OPENCODE_COMMAND),
            opencode_model=_env_str(merged, "OPENCODE_MODEL", DEFAULT_OPENCODE_MODEL),
            opencode_variant=_env_opt_str_allow_empty_none(
                merged, "OPENCODE_VARIANT", DEFAULT_OPENCODE_VARIANT
            ),
            opencode_timeout_seconds=_env_float(
                merged, "OPENCODE_TIMEOUT_SECONDS", None
            ),
            opencode_workdir=_env_opt_str(
                merged, "OPENCODE_WORKDIR", DEFAULT_OPENCODE_WORKDIR
            ),
            opencode_max_output_bytes=_env_int(
                merged, "OPENCODE_MAX_OUTPUT_BYTES", DEFAULT_OPENCODE_MAX_OUTPUT_BYTES
            ),
            opencode_json_events=_env_bool(merged, "OPENCODE_JSON_EVENTS", True),
            opencode_pure=_env_bool(merged, "OPENCODE_PURE", DEFAULT_OPENCODE_PURE),
            opencode_fallback_model=_env_opt_str(
                merged, "OPENCODE_FALLBACK_MODEL", DEFAULT_OPENCODE_FALLBACK_MODEL
            ),
        )
