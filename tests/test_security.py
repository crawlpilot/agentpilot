"""Unit tests for `agentpilot.agent.security` -- the navigation allowlist and
placeholder-based secret substitution/redaction."""

from __future__ import annotations

from agentpilot.agent.security import is_url_allowed, redact_secrets, substitute_secrets


def test_empty_allowlist_is_unrestricted() -> None:
    assert is_url_allowed("https://anything.example/", ())


def test_bare_domain_matches_host_and_subdomains() -> None:
    allow = ("example.com",)
    assert is_url_allowed("https://example.com/path", allow)
    assert is_url_allowed("https://app.example.com/x", allow)
    assert not is_url_allowed("https://evil.com/", allow)
    # Must not match a domain that merely ends with the string.
    assert not is_url_allowed("https://notexample.com/", allow)


def test_glob_pattern_matches_subdomains() -> None:
    allow = ("*.example.com",)
    assert is_url_allowed("https://app.example.com/", allow)
    assert is_url_allowed("https://example.com/", allow)


def test_subdomain_pattern_scopes_below_it() -> None:
    allow = ("sub.example.com",)
    assert is_url_allowed("https://sub.example.com/", allow)
    assert is_url_allowed("https://deep.sub.example.com/", allow)
    assert not is_url_allowed("https://example.com/", allow)


def test_substitute_and_redact_round_trip() -> None:
    secrets = {"<pw>": "hunter2"}
    filled = substitute_secrets("password is <pw> now", secrets)
    assert filled == "password is hunter2 now"
    # A read-back that surfaced the real secret is scrubbed back to placeholder.
    assert redact_secrets("field now contains 'hunter2'", secrets) == "field now contains '<pw>'"


def test_substitute_noop_without_secrets() -> None:
    assert substitute_secrets("plain", None) == "plain"
    assert redact_secrets("plain", {}) == "plain"
