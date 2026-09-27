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


# ---------------------------------------------------------------------------
# Wildcard (*) and terminal anchor ($) matching
# ---------------------------------------------------------------------------


def test_wildcard_disallow_and_allow_precedence() -> None:
    text = """
User-agent: *
Disallow: /jobs/*
Allow: /jobs/public/*
"""
    rules = RobotsRules.parse(text)
    # The bare wildcard prefix denies everything under /jobs/...
    assert not rules.is_allowed("/jobs/private/1", user_agent="jobscout/0.1")
    # ...and the longer Allow pattern wins where it matches.
    assert rules.is_allowed("/jobs/public/1", user_agent="jobscout/0.1")


def test_wildcard_matches_across_segments() -> None:
    text = "User-agent: *\nDisallow: /*/apply\n"
    rules = RobotsRules.parse(text)
    assert not rules.is_allowed("/jobs/123/apply", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/a/b/c/apply", user_agent="jobscout/0.1")
    assert rules.is_allowed("/jobs/123/view", user_agent="jobscout/0.1")


def test_terminal_anchor_requires_exact_end() -> None:
    text = "User-agent: *\nDisallow: /secret$\n"
    rules = RobotsRules.parse(text)
    # The trailing '$' anchors the pattern to the end of the path.
    assert not rules.is_allowed("/secret", user_agent="jobscout/0.1")
    # A path that merely starts with the pattern is not anchored.
    assert rules.is_allowed("/secret/more", user_agent="jobscout/0.1")


def test_terminal_anchor_with_wildcard() -> None:
    text = "User-agent: *\nDisallow: /jobs/*/apply$\n"
    rules = RobotsRules.parse(text)
    assert not rules.is_allowed("/jobs/123/apply", user_agent="jobscout/0.1")
    assert rules.is_allowed("/jobs/123/apply/more", user_agent="jobscout/0.1")


def test_anchored_trailing_wildcard_matches_the_remainder() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /a*$\n")
    # `/a*$` requires the path to start with /a and then end anywhere.
    assert not rules.is_allowed("/abc", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/a", user_agent="jobscout/0.1")
    assert rules.is_allowed("/bc", user_agent="jobscout/0.1")


def test_bare_wildcard_anchor_matches_every_path() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /*$\n")
    assert not rules.is_allowed("/", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/anything/at/all", user_agent="jobscout/0.1")


def test_repeated_literal_chunks_backtrack_to_the_anchored_tail() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /a*b$\n")
    # The first ``b`` is not the anchored final one, so matching must backtrack.
    assert not rules.is_allowed("/axbxb", user_agent="jobscout/0.1")
    assert rules.is_allowed("/axbx", user_agent="jobscout/0.1")
    assert rules.is_allowed("/axbxb/more", user_agent="jobscout/0.1")


def test_repeated_asterisks_match_like_a_single_wildcard() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /a***b$\n")
    assert not rules.is_allowed("/ab", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/axxxb", user_agent="jobscout/0.1")
    assert rules.is_allowed("/axxxbc", user_agent="jobscout/0.1")


def test_unanchored_wildcard_is_a_prefix_match() -> None:
    rules = RobotsRules.parse("User-agent: *\nDisallow: /jobs/*\n")
    assert not rules.is_allowed("/jobs/", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/jobs/a/b/c", user_agent="jobscout/0.1")
    assert rules.is_allowed("/other", user_agent="jobscout/0.1")


# ---------------------------------------------------------------------------
# User-agent precedence and group combination
# ---------------------------------------------------------------------------


def test_most_specific_user_agent_group_wins() -> None:
    text = """
User-agent: *
Disallow: /
User-agent: jobscout
Allow: /
User-agent: jobscout/0.1
Disallow: /private/
"""
    rules = RobotsRules.parse(text)
    # The longest matching agent token ("jobscout/0.1") selects its group only.
    assert rules.is_allowed("/public", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/private/x", user_agent="jobscout/0.1")
    # A shorter token selects a different group: "jobscout" allows everything.
    assert rules.is_allowed("/private/x", user_agent="jobscout")
    # A user agent that matches nothing specific falls back to the wildcard.
    assert not rules.is_allowed("/anything", user_agent="other-agent")


def test_agent_groups_with_the_same_token_are_combined() -> None:
    text = """
User-agent: jobscout
Disallow: /a
User-agent: jobscout
Disallow: /b
"""
    rules = RobotsRules.parse(text)
    # Rules from both equally-specific groups apply; neither is silently lost.
    assert not rules.is_allowed("/a", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/b", user_agent="jobscout/0.1")
    assert rules.is_allowed("/c", user_agent="jobscout/0.1")


def test_specific_group_beats_wildcard_regardless_of_order() -> None:
    text = """
User-agent: jobscout
Allow: /
User-agent: *
Disallow: /
"""
    rules = RobotsRules.parse(text)
    assert rules.is_allowed("/anything", user_agent="jobscout/0.1")
    assert not rules.is_allowed("/anything", user_agent="other-agent")


# ---------------------------------------------------------------------------
# Empty and malformed documents
# ---------------------------------------------------------------------------


def test_malformed_lines_are_ignored_not_guessed() -> None:
    text = """
this line has no colon
User-agent: *
: value with no field
Disallow /x
Disallow: /really-blocked
"""
    rules = RobotsRules.parse(text)
    assert not rules.is_allowed("/really-blocked", user_agent="jobscout/0.1")
    # The malformed "Disallow /x" line is ignored, so /x stays allowed.
    assert rules.is_allowed("/x", user_agent="jobscout/0.1")


def test_rules_before_any_user_agent_are_ignored() -> None:
    text = "Disallow: /\nUser-agent: *\nAllow: /\n"
    rules = RobotsRules.parse(text)
    assert rules.is_allowed("/anything", user_agent="jobscout/0.1")


def test_empty_and_missing_documents_allow_everything() -> None:
    assert RobotsRules.parse(None).is_allowed("/x", user_agent="jobscout/0.1")
    assert RobotsRules.parse("").is_allowed("/x", user_agent="jobscout/0.1")
    # A document with only a wildcard group and no rules allows everything.
    assert RobotsRules.parse("User-agent: *\n").is_allowed("/x", user_agent="jobscout/0.1")


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
