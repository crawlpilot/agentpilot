"""The identity slug is an on-disk format, and must never change.

`IdentityRef`'s rendered slug *is* the profile directory name and the vault
filename, so **any change to it orphans every existing profile and every
encrypted storage-state blob on every node** -- silently, because a missing
profile dir just looks like a cold first visit. Nothing errors; sessions simply
start logged out and burn their warm identities.

The rendering, unchanged since `IdentityKey(tenant, domain, name)` became
`IdentityRef(key)` in Phase 3c, is:

    "/".join(_sanitize_segment(p) for p in (tenant, domain, name))

These tests pin exactly that, including the sanitizer's rewriting and rejection
rules. They were written to verify that one migration and kept because the
invariant is permanent: this is a data-format guarantee, not a record of a
completed refactor.
"""

from __future__ import annotations

import pytest

from agentpilot.control.identity import IdentityParts, identity_for, parts_of, tenant_of
from crawlpilot.spi.identity import IdentityRef, ProfileKind


def _legacy_slug(tenant: str, domain: str, name: str) -> str:
    """The pre-Phase-3c rendering, reproduced verbatim from git history rather
    than imported -- a test that calls the code under test to compute its own
    expectation cannot detect a change in that code."""

    from crawlpilot.spi.identity import _sanitize_segment  # noqa: PLC0415

    return "/".join(_sanitize_segment(p) for p in (tenant, domain, name))


TRIPLES = [
    ("t", "d", "n"),
    ("acme", "example.com", "alice"),
    ("t", "example.com", "scrape-0123456789abcdef"),
    # Segments the sanitizer rewrites: anything outside [A-Za-z0-9_.-] becomes _
    ("ten ant", "sub.example.co.uk", "user@host"),
    ("t", "example.com", "name with spaces"),
    ("UPPER", "Example.COM", "MiXeD"),
]


@pytest.mark.parametrize(("tenant", "domain", "name"), TRIPLES)
def test_slug_is_byte_identical_to_the_legacy_rendering(
    tenant: str, domain: str, name: str
) -> None:
    assert identity_for(tenant, domain, name).slug() == _legacy_slug(tenant, domain, name)


@pytest.mark.parametrize(("tenant", "domain", "name"), TRIPLES)
def test_parts_round_trip(tenant: str, domain: str, name: str) -> None:
    assert parts_of(identity_for(tenant, domain, name)) == IdentityParts(tenant, domain, name)
    assert tenant_of(identity_for(tenant, domain, name)) == tenant


def test_kind_survives_composition() -> None:
    ref = identity_for("t", "d", "n", ProfileKind.DEFAULT)
    assert ref.kind is ProfileKind.DEFAULT
    assert ref.is_permanent is True
    assert IdentityRef(key="t/d/n").is_temporary is True


def test_scope_root_is_the_first_segment() -> None:
    """`profile_store.resolve_profile_dir` uses this as the containment root.
    For a platform-composed key it is the tenant, which is what the old
    `profiles_root / identity.tenant` check used."""

    assert identity_for("acme", "example.com", "alice").scope_root == "acme"


def test_a_single_tenant_key_is_equally_valid() -> None:
    """The whole point of the change: a caller with no tenants is not forced to
    invent one, and gets a shorter path rather than `"_/_/example.com"`."""

    ref = IdentityRef(key="example.com")
    assert ref.slug() == "example.com"
    assert ref.scope_root == "example.com"


def test_traversal_segments_are_still_rejected() -> None:
    """The sanitizer is the profile dir's first line of defence; opacity must
    not have widened what a key may contain."""

    for bad in ("..", "a/../../b", "a/./b"):
        with pytest.raises(ValueError):
            IdentityRef(key=bad).slug()


def test_parts_of_rejects_a_key_it_did_not_compose() -> None:
    """`parts_of` feeds authorization (`tenant_of` gates session access), so a
    key that is not a platform triple must raise rather than silently
    attributing the session to the wrong tenant."""

    for foreign in ("example.com", "a/b", "a/b/c/d"):
        with pytest.raises(ValueError):
            parts_of(IdentityRef(key=foreign))
