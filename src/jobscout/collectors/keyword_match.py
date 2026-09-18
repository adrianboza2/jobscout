"""Keyword matching for providers that inventory a whole catalog.

GetManfred publishes a sitemap of *every* offer, with no search endpoint and no
keyword in the URL. The adapter must therefore take the operator's approved
keywords and decide, locally and deterministically, which sitemap entries match
them — without query fan-out and without ever calling a private endpoint.

Matching is deliberately conservative:

* **Normalized, accent- and case-insensitive.** ``"Azure DevSecOps"`` matches
  ``azure-devsecops`` and ``"Platform Engineer"`` matches ``platform engineer``.
* **Token subset.** A keyword matches when every one of its tokens appears as a
  whole token in the offer's normalized ``title + slug`` text. A keyword never
  matches by substring, so ``"front"`` does not match ``"frontend"`` and
  ``"backend"`` does not match a URL that merely contains unrelated words.
* **Provenance per keyword.** A posting that matches several approved keywords
  records all of them, so the observation is never collapsed into one term.

The helper is pure and offline-testable: no I/O, no network, no global state.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

#: Separators that split a keyword or offer text into comparable tokens. Any
#: character outside ``[a-z0-9]`` (after ASCII-folding) becomes a space.
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _fold(value: Any) -> str:
    """Lowercase, strip accents, and collapse punctuation to single spaces."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    ascii_only = text.encode("ascii", "ignore").decode("ascii").lower()
    return _WHITESPACE_RE.sub(" ", _TOKEN_SPLIT_RE.sub(" ", ascii_only)).strip()


def normalize_tokens(value: Any) -> tuple[str, ...]:
    """Return the normalized, order-preserving tokens of ``value``.

    Duplicate tokens are dropped so ``"engineer engineer"`` behaves like
    ``"engineer"`` and a repeated keyword can never look like a different one.
    """
    tokens: list[str] = []
    for token in _fold(value).split(" "):
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def keyword_matches(keyword: Any, text: Any) -> bool:
    """Whether every token of ``keyword`` appears as a token of ``text``."""
    keyword_tokens = normalize_tokens(keyword)
    if not keyword_tokens:
        return False
    text_tokens = set(normalize_tokens(text))
    return all(token in text_tokens for token in keyword_tokens)


def matching_keywords(keyword_text: Any, text: Any) -> list[str]:
    """Return the approved keywords that match ``text``, in the configured order.

    ``keyword_text`` may contain one or more approved keyword groups separated by
    commas. This lets one sitemap request serve the small GetManfred catalogue;
    commas are not expected inside an approved keyword itself.
    """
    keywords = [item.strip() for item in str(keyword_text or "").split(",")]
    return [keyword for keyword in keywords if keyword and keyword_matches(keyword, text)]


__all__ = [
    "keyword_matches",
    "matching_keywords",
    "normalize_tokens",
]
