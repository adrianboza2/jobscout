"""Salary-currency policy shared by every scorer.

User policy: only USD and EUR are comparable, ``$`` means USD, and there is no FX
conversion. An offer explicitly quoted in any other currency is a known,
non-comparable amount and must hard-reject — deterministically, before any model
call. Truly ambiguous or missing evidence stays unknown and is never compared.

``$`` only counts as USD when it is not the symbol of another currency prefix
(``R$``, ``A$``, ...); ``US$`` still reads as USD through its code pattern. That
is what makes ``R$ 5.000`` a BRL offer instead of a USD/BRL ambiguity.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Evaluation, EvaluationStatus, Recommendation
from .fingerprint import effective_input_fingerprint
from .profile import Profile

_USD_CODE_RE = re.compile(r"(?<![A-Za-z])(?:usd|us\$)(?![A-Za-z])", re.IGNORECASE)
#: A bare symbol is USD only when no letter precedes it, so a currency prefix
#: such as ``R$`` is left to its own pattern instead of also reading as USD.
_USD_SYMBOL_RE = re.compile(r"(?<![A-Za-z])\$")
_EUR_CODE_RE = re.compile(r"(?<![A-Za-z])eur(?![A-Za-z])", re.IGNORECASE)
_EUR_SYMBOL_RE = re.compile(r"€")

#: Explicit currencies outside the supported set (USD/EUR). A posting priced only
#: in one of these is a known, non-comparable amount and must be hard-rejected
#: instead of silently treated as an unknown. Bare words that double as common
#: English (for example "pesos", "cop", "try") are deliberately absent so an
#: ambiguous word never produces a false rejection. Well-known ``X$`` prefixes
#: are included so a symbolic form (``R$``, ``A$``, ...) is not read as USD.
_UNSUPPORTED_CURRENCY_PATTERNS = (
    ("GBP", re.compile(r"(?<![A-Za-z])gbp(?![A-Za-z])|£", re.IGNORECASE)),
    ("ARS", re.compile(r"(?<![A-Za-z])ars(?![A-Za-z])", re.IGNORECASE)),
    ("BRL", re.compile(r"(?<![A-Za-z])(?:brl|r\$)(?![A-Za-z])", re.IGNORECASE)),
    ("MXN", re.compile(r"(?<![A-Za-z])(?:mxn|mx\$)(?![A-Za-z])", re.IGNORECASE)),
    ("CLP", re.compile(r"(?<![A-Za-z])clp(?![A-Za-z])", re.IGNORECASE)),
    ("UYU", re.compile(r"(?<![A-Za-z])uyu(?![A-Za-z])", re.IGNORECASE)),
    ("CHF", re.compile(r"(?<![A-Za-z])chf(?![A-Za-z])", re.IGNORECASE)),
    ("CAD", re.compile(r"(?<![A-Za-z])(?:cad|c\$|ca\$)(?![A-Za-z])", re.IGNORECASE)),
    ("AUD", re.compile(r"(?<![A-Za-z])(?:aud|au?\$)(?![A-Za-z])", re.IGNORECASE)),
    ("NZD", re.compile(r"(?<![A-Za-z])(?:nzd|nz\$)(?![A-Za-z])", re.IGNORECASE)),
    ("JPY", re.compile(r"(?<![A-Za-z])jpy(?![A-Za-z])|¥", re.IGNORECASE)),
    ("CNY", re.compile(r"(?<![A-Za-z])(?:cny|rmb)(?![A-Za-z])", re.IGNORECASE)),
    ("INR", re.compile(r"(?<![A-Za-z])inr(?![A-Za-z])|₹", re.IGNORECASE)),
    ("PLN", re.compile(r"(?<![A-Za-z])pln(?![A-Za-z])", re.IGNORECASE)),
    ("RUB", re.compile(r"(?<![A-Za-z])rub(?![A-Za-z])|₽", re.IGNORECASE)),
    ("SEK", re.compile(r"(?<![A-Za-z])sek(?![A-Za-z])", re.IGNORECASE)),
    ("NOK", re.compile(r"(?<![A-Za-z])nok(?![A-Za-z])", re.IGNORECASE)),
    ("DKK", re.compile(r"(?<![A-Za-z])dkk(?![A-Za-z])", re.IGNORECASE)),
    ("ZAR", re.compile(r"(?<![A-Za-z])zar(?![A-Za-z])", re.IGNORECASE)),
)


def salary_currencies(salary_text: str | None) -> tuple[set[str], set[str]]:
    """Return the explicitly signalled ``(supported, unsupported)`` currencies."""
    text = salary_text or ""
    supported: set[str] = set()
    if _USD_CODE_RE.search(text) or _USD_SYMBOL_RE.search(text):
        supported.add("USD")
    if _EUR_CODE_RE.search(text) or _EUR_SYMBOL_RE.search(text):
        supported.add("EUR")
    unsupported = {
        code
        for code, pattern in _UNSUPPORTED_CURRENCY_PATTERNS
        if pattern.search(text)
    }
    return supported, unsupported


def unsupported_currency_codes(salary_text: str | None) -> tuple[str, ...]:
    """Return the hard-reject currencies, or ``()`` when not clear-cut.

    Only a text whose sole explicit currency is unsupported qualifies. A text
    that mixes supported and unsupported signals, or that names only an unknown
    currency, stays unknown/ambiguous and is never compared.
    """
    supported, unsupported = salary_currencies(salary_text)
    if unsupported and not supported:
        return tuple(sorted(unsupported))
    return ()


def currency_blocker(salary_text: str | None) -> str | None:
    """Return the deterministic hard-blocker for an unsupported offer, else ``None``."""
    codes = unsupported_currency_codes(salary_text)
    if not codes:
        return None
    joined = ", ".join(codes)
    return (
        f"salary is quoted in an unsupported currency ({joined}); "
        "only USD and EUR are accepted"
    )


def currency_rejection(
    *,
    scorer: Any,
    model: str | None,
    prompt_version: str | None,
    job: Any,
    profile: Profile,
    job_id: int | None,
    profile_id: int | None,
) -> Evaluation | None:
    """Build a deterministic SKIP evaluation for an unsupported-currency offer.

    Returns ``None`` when the offer is not clearly in an unsupported currency, so
    callers fall through to their normal scoring path. A non-``None`` result is a
    publishable evaluation (numeric score + public recommendation) and lets a
    scorer reject before any external model call.
    """
    blocker = currency_blocker(getattr(job, "salary", None))
    if blocker is None:
        return None
    return Evaluation(
        job_id=int(job_id or 0),
        scorer=scorer,
        overall_score=0.0,
        recommendation=Recommendation.SKIP,
        confidence=1.0,
        blockers=[blocker],
        rationale=(
            "Deterministic currency-policy rejection: the posting is quoted in a "
            "currency the profile does not accept, so no model call was made."
        ),
        status=EvaluationStatus.OK,
        model=model,
        prompt_version=prompt_version,
        profile_id=profile_id,
        input_fingerprint=effective_input_fingerprint(
            scorer=scorer,
            model=model,
            prompt_version=prompt_version,
            profile=profile,
            job=job,
        ),
    )


__all__ = [
    "currency_blocker",
    "currency_rejection",
    "salary_currencies",
    "unsupported_currency_codes",
]
