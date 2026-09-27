"""Minimal ``robots.txt`` policy parser.

Pure and offline-testable: :func:`RobotsRules.parse` takes the text a server
returned and the collector applies the verdict before sending any request. The
rules implemented are the subset a single, well-behaved crawler needs:

* ``User-agent`` groups. The most specific group that matches our user agent
  wins: an agent token matching as a case-insensitive substring beats ``*``, and
  among matching tokens the longest one wins. Every group carrying the winning
  token is combined, so the same crawler split across several ``User-agent``
  blocks cannot silently lose rules. The ``*`` group applies only when no
  specific token matches.
* ``Allow``/``Disallow`` path patterns. ``*`` matches any character sequence and
  a trailing ``$`` anchors the pattern to the end of the path (the standard
  crawler extensions). The most specific matching rule wins and ``Allow`` wins
  ties, which is the behaviour crawlers are expected to implement.
* An empty ``Disallow:`` (or ``Allow:`` with no path) matches nothing, so it
  does not disallow anything.

An unparseable line is ignored rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: A single parsed rule: ``allow`` is True for an Allow line, False for Disallow.
Rule = tuple[bool, str]


def _pattern_matches(pattern: str, path: str) -> bool:
    """Return whether a robots path pattern matches ``path``.

    ``*`` matches any run of characters (including none, and including a run
    that spans ``/``) and a trailing ``$`` anchors the match to the end of
    ``path``. Without ``$`` the pattern is a prefix match. An empty pattern
    never matches.

    The pattern is compiled to a regular expression so any sequence of ``*``
    backtracks correctly instead of greedily consuming the first occurrence of a
    later literal chunk. For example ``/a*$`` matches ``/abc``, ``/*$`` matches
    any path, and ``/a*b$`` matches ``/axbxb``.
    """
    if pattern == "":
        return False
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    translated = ".*".join(re.escape(chunk) for chunk in core.split("*"))
    expression = "^" + translated + ("$" if anchored else "")
    return re.match(expression, path) is not None


def _pattern_specificity(pattern: str) -> int:
    """Return a pattern's specificity: its literal length, wildcards excluded."""
    return len(pattern.replace("*", "").replace("$", ""))


@dataclass(frozen=True)
class RobotsRules:
    """The parsed groups of one ``robots.txt`` document."""

    #: ``((agent_tokens, rules), ...)`` in file order.
    groups: tuple[tuple[tuple[str, ...], tuple[Rule, ...]], ...] = ()

    @classmethod
    def parse(cls, text: str | None) -> "RobotsRules":
        """Parse ``robots.txt`` text. Missing/empty text means allow all."""
        if not text:
            return cls(())
        groups: list[tuple[list[str], list[Rule]]] = []
        current_agents: list[str] = []
        current_rules: list[Rule] = []
        seen_rule = False

        def flush() -> None:
            if current_agents or current_rules:
                groups.append((list(current_agents), list(current_rules)))

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field = field.strip().lower()
            value = value.strip()
            if field == "user-agent":
                if seen_rule:
                    flush()
                    current_agents = []
                    current_rules = []
                    seen_rule = False
                current_agents.append(value.lower())
            elif field in ("allow", "disallow"):
                if not current_agents:
                    continue
                seen_rule = True
                current_rules.append((field == "allow", value))
        flush()

        normalized: list[tuple[tuple[str, ...], tuple[Rule, ...]]] = [
            (tuple(agents), tuple(rules)) for agents, rules in groups if agents
        ]
        return cls(tuple(normalized))

    def _rules_for(self, user_agent: str) -> tuple[Rule, ...]:
        """Return the rules that apply to ``user_agent``.

        Specific groups win over the wildcard group, and the longest matching
        agent token wins among them. Groups sharing that token are combined in
        file order; the wildcard group is used only when nothing specific
        matches.
        """
        ua = (user_agent or "").lower()
        best_length = -1
        selected: list[Rule] = []
        wildcard: list[Rule] = []

        for agents, rules in self.groups:
            specificity = -1
            has_wildcard = False
            for agent in agents:
                if agent == "*":
                    has_wildcard = True
                elif agent and agent in ua:
                    specificity = max(specificity, len(agent))

            if specificity >= 0:
                if specificity > best_length:
                    best_length = specificity
                    selected = list(rules)
                elif specificity == best_length:
                    selected.extend(rules)
            elif has_wildcard:
                # A group's rules count once, even if it names ``*`` too.
                wildcard.extend(rules)

        if best_length >= 0:
            return tuple(selected)
        return tuple(wildcard)

    def is_allowed(self, path: str, *, user_agent: str = "") -> bool:
        """Return whether ``path`` may be requested for ``user_agent``."""
        target = path or "/"
        if not target.startswith("/"):
            target = "/" + target
        winner: tuple[int, bool] | None = None
        for allow, pattern in self._rules_for(user_agent):
            if not _pattern_matches(pattern, target):
                continue
            length = _pattern_specificity(pattern)
            if winner is None or length > winner[0] or (
                length == winner[0] and allow
            ):
                winner = (length, allow)
        return True if winner is None else winner[1]


def path_of(url: str) -> str:
    """Return the request path (plus query) a robots rule is matched against."""
    parts = urlsplit(str(url))
    path = parts.path or "/"
    if parts.query:
        return f"{path}?{parts.query}"
    return path


def check_robots(
    text: str | None,
    url: str,
    *,
    user_agent: str = "",
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for requesting ``url`` under ``text``."""
    rules = RobotsRules.parse(text)
    path = path_of(url)
    if rules.is_allowed(path, user_agent=user_agent):
        return True, f"robots.txt allows {path!r}"
    return False, f"robots.txt disallows {path!r}"


__all__ = [
    "RobotsRules",
    "check_robots",
    "path_of",
]
