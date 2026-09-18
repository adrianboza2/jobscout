"""Minimal ``robots.txt`` policy parser.

Pure and offline-testable: :func:`RobotsRules.parse` takes the text a server
returned and the collector applies the verdict before sending any request. The
rules implemented are the ones that matter for a single, well-behaved client:

* ``User-agent`` groups, matched case-insensitively as a substring of our own
  user agent. A specific matching group wins over the ``*`` group; when no
  specific group matches, the ``*`` group applies.
* ``Allow``/``Disallow`` prefixes. The longest matching path wins and ``Allow``
  wins ties, which is the behaviour crawlers are expected to implement.
* An empty ``Disallow:`` (or ``Allow: /``) means "nothing is disallowed".

Wildcards inside a rule are treated literally; that errs toward *denying* only
when the literal path matches, so it never grants broader access than intended.
An unparseable line is ignored rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

#: A single parsed rule: ``allow`` is True for an Allow line, False for Disallow.
Rule = tuple[bool, str]


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
        ua = (user_agent or "").lower()
        wildcard: tuple[Rule, ...] = ()
        for agents, rules in self.groups:
            for agent in agents:
                if agent == "*":
                    wildcard = rules
                    break
        for agents, rules in self.groups:
            for agent in agents:
                if agent and agent != "*" and agent in ua:
                    # A specific matching group takes precedence over ``*``.
                    return rules
        return wildcard

    def is_allowed(self, path: str, *, user_agent: str = "") -> bool:
        """Return whether ``path`` may be requested for ``user_agent``."""
        target = path or "/"
        if not target.startswith("/"):
            target = "/" + target
        winner: tuple[int, bool] | None = None
        for allow, rule_path in self._rules_for(user_agent):
            if rule_path == "":
                # ``Disallow:`` with no path means nothing is disallowed.
                continue
            candidate = rule_path if rule_path.startswith("/") else "/" + rule_path
            if target.startswith(candidate):
                length = len(candidate)
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
