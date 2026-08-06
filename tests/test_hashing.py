"""Unit tests for `agentpilot.spi.hashing` -- pure structural identity hashes.
No browser. Mirrors the EXACT vs STABLE guarantees the change-diff relies on."""

from __future__ import annotations

from agentpilot.spi.hashing import (
    MatchLevel,
    element_hash,
    filter_dynamic_classes,
    parent_branch_hash,
    stable_hash,
)

_BRANCH = ["html", "body", "div", "button"]


def test_filter_dynamic_classes_drops_transient_and_sorts() -> None:
    # "btn" and "primary" are semantic; "is-hover"/"open"/"loading" are transient.
    assert filter_dynamic_classes("primary btn is-hover open loading") == "btn primary"
    # Order-independent + deterministic.
    assert filter_dynamic_classes("btn primary") == filter_dynamic_classes("primary btn")
    assert filter_dynamic_classes(None) == ""
    assert filter_dynamic_classes("hover active") == ""  # nothing stable remains


def test_element_hash_is_deterministic_and_int() -> None:
    h1 = element_hash(_BRANCH, {"id": "cart", "class": "btn"}, "Add to cart")
    h2 = element_hash(_BRANCH, {"class": "btn", "id": "cart"}, "Add to cart")
    assert isinstance(h1, int)
    assert h1 == h2  # attribute order must not matter


def test_stable_hash_invariant_under_dynamic_class_churn() -> None:
    """The whole point: a re-render that adds hover/focus/open classes must not
    change the STABLE identity, even though the EXACT hash does change."""
    before = {"id": "cart", "class": "btn primary"}
    after = {"id": "cart", "class": "btn primary is-hover open focus-visible"}

    assert stable_hash(_BRANCH, before, "Add") == stable_hash(_BRANCH, after, "Add")
    assert element_hash(_BRANCH, before, "Add") != element_hash(_BRANCH, after, "Add")


def test_hash_sensitive_to_structural_and_name_change() -> None:
    base = stable_hash(_BRANCH, {"id": "cart"}, "Add to cart")
    # Different ancestry -> different identity (MOVED signal upstream).
    assert stable_hash([*_BRANCH[:-1], "a"], {"id": "cart"}, "Add to cart") != base
    # Different accessible name -> different identity.
    assert stable_hash(_BRANCH, {"id": "cart"}, "Remove") != base
    # Different identifying attribute -> different identity.
    assert stable_hash(_BRANCH, {"id": "wishlist"}, "Add to cart") != base


def test_non_static_attributes_ignored() -> None:
    # style/data-reactid aren't in STATIC_ATTRIBUTES -> must not affect identity.
    a = element_hash(_BRANCH, {"id": "x"}, "")
    b = element_hash(_BRANCH, {"id": "x", "style": "color:red", "data-reactid": "42"}, "")
    assert a == b


def test_parent_branch_hash_position_only() -> None:
    a = parent_branch_hash(_BRANCH)
    assert a == parent_branch_hash(["html", "body", "div", "button"])
    assert a != parent_branch_hash(["html", "body", "span", "button"])
    # Independent of attributes/name by construction.
    assert isinstance(a, int)


def test_match_level_ordering() -> None:
    assert MatchLevel.EXACT < MatchLevel.STABLE < MatchLevel.XPATH < MatchLevel.AX_NAME
    assert MatchLevel.AX_NAME < MatchLevel.ATTRIBUTE
