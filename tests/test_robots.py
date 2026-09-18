"""robots.txt policy tests (pure, offline)."""

from __future__ import annotations

import pytest

from jobscout.collectors.robots import RobotsRules, check_robots, path_of


def test_path_of_includes_query() -> None:
    assert path_of("https://www.tecnoempleo.com/ofertas-trabajo/x?pagina=2") == (
        "/ofertas-trabajo/x?pagina=2"
    )
    assert path_of("https://www.tecnoempleo.com") == "/"


def test_parse_matches_wildcard_group() -> None:
    text = """
User-agent: *
Disallow: /private/
Allow: /private/ok/
Disallow: /alertas-empleo-rss.php
"""
    rules = RobotsRules.parse(text)
    assert rules.is_allowed("/ofertas-trabajo/python", user_agent="jobscout/0.1")
    assert rules.is_allowed("/private/ok/page", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/private/page", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/alertas-empleo-rss.php", user_agent="jobscout/0.1")


def test_specific_group_wins_over_wildcard() -> None:
    text = """
User-agent: *
Disallow: /
User-agent: jobscout
Allow: /
"""
    rules = RobotsRules.parse(text)
    assert rules.is_allowed("/anything", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/anything", user_agent="other-agent")


def test_empty_disallow_allows_everything() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow:\n")
    assert rules.is_allowed("/anything", user_agent="jobscout/0.1")


def test_missing_text_allows_everything() -> None:
    assert RobotsRules.parse(None).is_allowed("/x")
    assert RobotsRules.parse("").is_allowed("/x")


def test_longest_prefix_wins_and_allow_ties() -> None:
    text = """
User-agent: *
Disallow: /ofertas/
Allow: /ofertas/public/
Disallow: /ofertas/public/secret
"""
    rules = RobotsRules.parse(text)
    assert not rules.is_allowed("/ofertas/x")
    assert rules.is_allowed("/ofertas/public/x")
    # A longer Disallow prefix beats a shorter Allow prefix.
    assert not rules.is_allowed("/ofertas/public/secret")


def test_check_robots_returns_reason() -> None:
    allowed, reason = check_robots("User-agent: *\nDisallow: /x\n", "https://h.test/y")
    assert allowed and "allows" in reason
    allowed, reason = check_robots("User-agent: *\nDisallow: /x\n", "https://h.test/x")
    assert not allowed and "disallows" in reason


def test_real_tecnoempleo_ruleset_allows_the_search_path() -> None:
    """The documented public robots rules must not block the search path."""
    text = """
User-agent: *
Disallow: /graficos_estadisticas/
Disallow: /imagenes/
Disallow: /profesionales/
Disallow: /alertas-empleo-rss.php
Disallow: /accesoempresa.php
"""
    assert check_robots(text, "https://www.tecnoempleo.com/ofertas-trabajo/python")[0]
    assert not check_robots(text, "https://www.tecnoempleo.com/alertas-empleo-rss.php")[0]
