"""Normalization and fingerprinting.

Everything here is pure and deterministic so it can be unit tested offline.
Normalization serves two purposes:

1. Make stored records comparable (URLs, company names, locations).
2. Make deduplication stable across tracking parameters, casing, and
   whitespace noise between fetches of the same posting.
"""

from __future__ import annotations

import hashlib
import html as html_module
import re
import unicodedata
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import ContentStatus, JobPosting
from .timeutil import parse_flexible_date, to_iso

FINGERPRINT_VERSION = "v1"

_WHITESPACE_RE = re.compile(r"[ \t\u00a0]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_BREAK_RE = re.compile(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|li|p|div|h[1-6])\b[^>]*>")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Query parameters used for analytics/attribution; they never change identity.
_TRACKING_PARAMS = {
    "trk",
    "trackingid",
    "refid",
    "ref",
    "referer",
    "original_referer",
    "originalsubdomain",
    "position",
    "pagenum",
    "searchid",
    "lipi",
    "midsig",
    "ebp",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "source",
    "src",
    "s",
    "t",
    "trkinfo",
}
_TRACKING_PREFIXES = ("utm_", "trk_", "pk_")

_COMPANY_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "gmbh",
    "ag",
    "sa",
    "s.a.",
    "s.a",
    "sl",
    "s.l.",
    "s.l",
    "srl",
    "s.r.l.",
    "bv",
    "nv",
    "plc",
    "pty",
    "co",
    "company",
    "group",
}

_SENIORITY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("internship", ("intern", "internship", "trainee", "pasant")),
    ("junior", ("junior", "jr.", "jr", "entry level", "entry-level", "graduate", "ssr junior")),
    # "mid" must be checked before "senior" so that "semi senior" / "SSR" do not
    # match the senior pattern via the shared substring.
    ("mid", ("semi senior", "semisenior", "semi-senior", "ssr", "mid-level", "mid level", "intermediate")),
    ("senior", ("senior", "sr.", "sr)", "snr", "advanced")),
    ("lead", ("lead", "staff", "principal", "arquitect", "architect", "head of")),
    ("manager", ("manager", "manager)", "gerente", "supervisor")),
    ("director", ("director", "vp", "vice president", "chief", "cto", "ceo", "cfo")),
)

_EMPLOYMENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("full_time", ("full-time", "full time", "fulltime", "jornada completa")),
    ("part_time", ("part-time", "part time", "medio tiempo", "media jornada")),
    ("contract", ("contract", "contractor", "freelance", "b2b", "por contrato")),
    ("temporary", ("temporary", "temp ", "temporal")),
    ("internship", ("internship", "intern ", "practica", "práctica", "pasantia", "pasantía")),
)


def normalize_text(value: Any) -> str:
    """Collapse all whitespace (including newlines) and apply NFKC normalization.

    Use this for single-line fields such as titles, companies, and URLs.
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalize_multiline(value: Any) -> str:
    """Like :func:`normalize_text` but preserves meaningful line breaks."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANKLINES_RE.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(value: Any) -> str:
    """Convert an HTML fragment to plain text without third-party parsers.

    Used for LinkedIn guest-API fragments and HTML job descriptions. The result
    is treated strictly as data.
    """
    if value is None:
        return ""
    text = str(value)
    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    return normalize_multiline(text)


def slugify(value: Any) -> str:
    """Lowercase ASCII slug used as part of dedupe keys."""
    normalized = unicodedata.normalize("NFKD", normalize_text(value))
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return _SLUG_RE.sub(" ", ascii_only).strip()


def _strip_tracking(query: str) -> list[tuple[str, str]]:
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES):
            continue
        kept.append((key, value))
    return sorted(kept)


def canonicalize_url(url: Any) -> str:
    """Return a canonical, tracking-free URL.

    Unparseable input is returned as a normalized bare string so callers can
    still fingerprint it deterministically.
    """
    raw = normalize_text(url)
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = urlencode(_strip_tracking(parts.query))
    return urlunsplit((scheme, netloc, path, query, ""))


def extract_linkedin_job_id(url: Any) -> str | None:
    """Extract a LinkedIn job id from a job URL, URN, or query parameter."""
    raw = normalize_text(url)
    if not raw:
        return None
    patterns = (
        r"/jobs/view/(\d{4,})",
        r"currentJobId=(\d{4,})",
        r"urn:li:jobPosting:(\d{4,})",
        r"jobPosting[:\"](\d{4,})",
        # Public guest detail endpoint: /jobPosting/<id>
        r"jobPosting/(\d{4,})",
        r"jobs/view/(\d{4,})",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def normalize_company(value: Any) -> str:
    """Normalize a company name for comparison."""
    slug = slugify(value)
    if not slug:
        return ""
    tokens = slug.split()
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_location(value: Any) -> str:
    """Normalize a location string for comparison."""
    slug = slugify(value)
    return re.sub(r"\s+", " ", slug).strip()


def content_hash(text: Any) -> str:
    """SHA-256 of normalized content, used to detect changed postings."""
    return hashlib.sha256(normalize_multiline(text).encode("utf-8")).hexdigest()


def _hash(*parts: str) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_fingerprint(posting: JobPosting) -> str:
    """Strong identity for a posting.

    Preference order: source job id > canonical URL > normalized
    company/title/location triple. The version prefix allows future migration.
    """
    if posting.source_job_id:
        return f"{FINGERPRINT_VERSION}:id:{posting.source}:{posting.source_job_id}"
    canonical = canonicalize_url(posting.url)
    if canonical:
        return f"{FINGERPRINT_VERSION}:url:{canonical}"
    return f"{FINGERPRINT_VERSION}:triple:" + _hash(
        posting.source, normalize_company(posting.company), slugify(posting.title), normalize_location(posting.location)
    )


def compute_dedupe_key(posting: JobPosting) -> str:
    """Weaker cross-source key: company + title + location."""
    return f"{FINGERPRINT_VERSION}:dedupe:" + _hash(
        normalize_company(posting.company),
        slugify(posting.title),
        normalize_location(posting.location),
    )


def normalize_seniority(value: Any) -> str:
    """Map free-text seniority to a canonical bucket."""
    text = normalize_text(value).lower()
    if not text:
        return "unknown"
    for bucket, needles in _SENIORITY_PATTERNS:
        if any(needle in text for needle in needles):
            return bucket
    return "unknown"


def normalize_employment_type(value: Any) -> str:
    """Map free-text employment type to a canonical bucket."""
    text = normalize_text(value).lower()
    if not text:
        return "unknown"
    for bucket, needles in _EMPLOYMENT_PATTERNS:
        if any(needle in text for needle in needles):
            return bucket
    return "unknown"


def _first(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


_LIKE_LIST_KEYS = (
    "description",
    "jobDescription",
    "job_description",
    "snippet",
    "summary",
    "details",
)


def normalize_job_posting(raw: dict[str, Any], *, source: str = "unknown") -> JobPosting:
    """Convert a raw collector payload into a :class:`JobPosting`.

    Tolerant of common field aliases because fixtures, the LinkedIn guest API,
    and any future provider all use different names for the same concepts.
    """
    if not isinstance(raw, dict):
        raise TypeError("raw job payload must be a mapping")

    source_name = normalize_text(_first(raw, "source", "provider") or source) or source
    url_raw = _first(raw, "url", "jobUrl", "applyUrl", "link", "href", "job_url")
    url = canonicalize_url(url_raw) or None
    title = normalize_text(_first(raw, "title", "jobTitle", "job_title", "name"))
    company = normalize_text(
        _first(raw, "company", "companyName", "company_name", "employer", "organization")
    )
    location = normalize_text(
        _first(raw, "location", "jobLocation", "formattedLocation", "place", "city")
    )

    description_value = _first(raw, *_LIKE_LIST_KEYS)
    if description_value is not None and re.search(r"<[a-zA-Z/][^>]*>", str(description_value)):
        description = html_to_text(description_value)
    else:
        description = normalize_multiline(description_value)

    source_job_id = normalize_text(_first(raw, "source_job_id", "jobId", "job_id", "id")) or None
    urn = _first(raw, "urn", "entityUrn", "data_entity_urn")
    if not source_job_id and urn:
        source_job_id = extract_linkedin_job_id(urn)
    if not source_job_id and url:
        source_job_id = extract_linkedin_job_id(url)

    posted_raw = _first(
        raw, "posted_at", "postedAt", "listedAt", "listed_at", "datePosted", "datetime"
    )
    posted_iso = to_iso(parse_flexible_date(posted_raw)) if posted_raw else None
    if posted_iso is None:
        posted_text = _first(raw, "posted_text", "postedTime", "listDate", "relativeDate")
        posted_iso = to_iso(parse_flexible_date(posted_text)) if posted_text else None

    fetched_at = normalize_text(_first(raw, "fetched_at", "fetchedAt", "crawled_at")) or None

    employment_raw = _first(raw, "employment_type", "employmentType", "contractType", "job_type")
    seniority_raw = _first(raw, "seniority", "seniorityLevel", "experienceLevel", "level")
    salary_raw = _first(raw, "salary", "salaryRange", "compensation", "pay")
    language_raw = _first(raw, "language", "lang")

    if not title and not company and not url:
        status = ContentStatus.EMPTY
    elif not description:
        status = ContentStatus.PARTIAL
    else:
        status = ContentStatus.OK

    status_raw = _first(raw, "content_status")
    if status_raw:
        try:
            status = ContentStatus(str(status_raw))
        except ValueError:
            pass

    extra: dict[str, Any] = {}
    for key in ("posted_text", "company_logo", "benefits", "industries", "applicants"):
        value = raw.get(key)
        if value not in (None, ""):
            extra[key] = value
    if not title and raw.get("link_title"):
        title = normalize_text(raw["link_title"])
    known = {
        "source",
        "provider",
        "url",
        "jobUrl",
        "applyUrl",
        "link",
        "href",
        "job_url",
        "title",
        "jobTitle",
        "job_title",
        "name",
        "company",
        "companyName",
        "company_name",
        "employer",
        "organization",
        "location",
        "jobLocation",
        "formattedLocation",
        "place",
        "city",
        "source_job_id",
        "jobId",
        "job_id",
        "id",
        "urn",
        "entityUrn",
        "data_entity_urn",
        "posted_at",
        "postedAt",
        "listedAt",
        "listed_at",
        "datePosted",
        "datetime",
        "posted_text",
        "postedTime",
        "listDate",
        "relativeDate",
        "fetched_at",
        "fetchedAt",
        "crawled_at",
        "employment_type",
        "employmentType",
        "contractType",
        "job_type",
        "seniority",
        "seniorityLevel",
        "experienceLevel",
        "level",
        "salary",
        "salaryRange",
        "compensation",
        "pay",
        "language",
        "lang",
        "content_status",
        "link_title",
        *_LIKE_LIST_KEYS,
    }
    for key, value in raw.items():
        if key not in known and value not in (None, "", [], {}):
            extra[key] = value

    posting = JobPosting(
        source=source_name,
        title=title,
        company=company,
        url=url,
        location=location or None,
        description=description or None,
        source_job_id=source_job_id,
        employment_type=normalize_employment_type(employment_raw),
        seniority=normalize_seniority(seniority_raw or title),
        salary=normalize_text(salary_raw) or None,
        language=normalize_text(language_raw) or None,
        posted_at=posted_iso,
        fetched_at=fetched_at,
        content_status=status,
        extra=extra,
    )
    return finalize_posting(posting)


def finalize_posting(posting: JobPosting) -> JobPosting:
    """Compute derived identity fields in place and return the posting."""
    posting.fingerprint = compute_fingerprint(posting)
    posting.dedupe_key = compute_dedupe_key(posting)
    posting.content_hash = content_hash(
        "\n".join(
            [
                posting.title,
                posting.company,
                posting.location or "",
                posting.description or "",
            ]
        )
    )
    return posting


def normalize_postings(
    raws: Iterable[dict[str, Any]], *, source: str = "unknown"
) -> list[JobPosting]:
    """Normalize a batch, skipping payloads that normalize to empty records."""
    postings: list[JobPosting] = []
    for raw in raws:
        posting = normalize_job_posting(raw, source=source)
        if posting.content_status is ContentStatus.EMPTY:
            continue
        postings.append(posting)
    return postings
