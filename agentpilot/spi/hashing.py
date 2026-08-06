"""Structural element identity hashing -- the stable cross-step handle the DOM
fusion pipeline and change-diff key on, ported from browser-use's
`dom/views.py` (`element_hash`, `compute_stable_hash`, `filter_dynamic_classes`,
`parent_branch_hash`) and Browser4's `HashUtils`.

Deliberately *pure*: every function takes primitives (a parent-branch tag-name
path, a static-attribute dict, an accessible name) rather than a live node, so
it is unit-testable against synthetic inputs with no CDP session and reusable by
both the enriched-node model (`spi.dom_tree`) and the diff (`agent.dom_diff`).

Two hashes, mirroring the references' EXACT vs STABLE distinction:

- `element_hash` (EXACT) folds in *all* static attributes -- two snapshots of a
  literally-unchanged element hash identically.
- `stable_hash` (STABLE) filters transient CSS-state classes (focus, hover,
  open, expanded, loading, ...) out of `class` first, so it survives the class
  churn a re-render sprays on otherwise-identical elements. This is the key the
  change-diff and the MatchLevel replay cascade prefer.

Both intentionally hash *structure + identifying attributes + accessible name*,
never the re-minted `aria-ref` / `backendNodeId`, so the identity is portable
across snapshots (a single DOM insertion must not shift it).
"""

from __future__ import annotations

import hashlib
from enum import IntEnum

# Attributes that identify an element rather than describe transient state --
# the only ones folded into the identity hash. Superset of browser-use's
# STATIC_ATTRIBUTES so ported serializer/replay logic stays behavior-compatible.
STATIC_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "class",
        "id",
        "name",
        "type",
        "placeholder",
        "aria-label",
        "title",
        "role",
        "data-testid",
        "data-test",
        "data-cy",
        "data-selenium",
        "for",
        "required",
        "disabled",
        "readonly",
        "checked",
        "selected",
        "multiple",
        "accept",
        "href",
        "target",
        "rel",
        "aria-describedby",
        "aria-labelledby",
        "aria-controls",
        "aria-owns",
        "aria-live",
        "aria-atomic",
        "aria-busy",
        "aria-disabled",
        "aria-hidden",
        "aria-pressed",
        "aria-autocomplete",
        "aria-checked",
        "aria-selected",
        "list",
        "tabindex",
        "alt",
        "src",
        "lang",
        "itemscope",
        "itemtype",
        "itemprop",
        "pseudo",
        "aria-valuemin",
        "aria-valuemax",
        "aria-valuenow",
        "aria-placeholder",
    }
)

# Substrings marking a class as dynamic/transient UI state -- stripped before
# the STABLE hash so a hover/open/loading class flip doesn't change identity.
DYNAMIC_CLASS_PATTERNS: frozenset[str] = frozenset(
    {
        "focus",
        "hover",
        "active",
        "selected",
        "disabled",
        "animation",
        "transition",
        "loading",
        "open",
        "closed",
        "expanded",
        "collapsed",
        "visible",
        "hidden",
        "pressed",
        "checked",
        "highlighted",
        "current",
        "entering",
        "leaving",
    }
)


class MatchLevel(IntEnum):
    """Element matching strictness for cross-step history replay, tried in
    ascending order (see `driver.ref_cache` / `agent.history_element`). Lower =
    stricter/more-certain."""

    EXACT = 1  # full hash over all static attributes
    STABLE = 2  # hash with dynamic classes filtered out
    XPATH = 3  # xpath string comparison
    AX_NAME = 4  # tag + accessible name from the AX tree
    ATTRIBUTE = 5  # a single unique attribute (name / id / aria-label)


def filter_dynamic_classes(class_str: str | None) -> str:
    """Drop transient state classes, keep semantic/identifying ones, and return
    them sorted so the result is deterministic for hashing. Empty string when
    nothing (or nothing stable) remains."""

    if not class_str:
        return ""
    stable = [
        cls
        for cls in class_str.split()
        if not any(pattern in cls.lower() for pattern in DYNAMIC_CLASS_PATTERNS)
    ]
    return " ".join(sorted(stable))


def _attributes_string(attributes: dict[str, str], *, filter_classes: bool) -> str:
    """The `k=v` join over static attributes only, sorted for determinism.
    When `filter_classes`, the `class` value is run through
    `filter_dynamic_classes` first and dropped entirely if empty (the STABLE
    variant); otherwise every static attribute is included verbatim (EXACT)."""

    filtered: dict[str, str] = {}
    for key, value in attributes.items():
        if key not in STATIC_ATTRIBUTES:
            continue
        if filter_classes and key == "class":
            value = filter_dynamic_classes(value)
            if not value:
                continue
        filtered[key] = value
    return "".join(f"{k}={v}" for k, v in sorted(filtered.items()))


def _hash16(text: str) -> int:
    """First 16 hex chars of SHA-256 as an int -- the reference projects'
    truncated element-hash convention (fits comfortably, collision-safe for a
    single page's node set)."""

    return int(hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16], 16)


def element_hash(parent_branch_path: list[str], attributes: dict[str, str], ax_name: str) -> int:
    """EXACT identity: parent tag-name branch + all static attributes +
    accessible name. Distinguishes structurally-identical elements by their
    visible/accessible text."""

    return _combined_hash(parent_branch_path, attributes, ax_name, filter_classes=False)


def stable_hash(parent_branch_path: list[str], attributes: dict[str, str], ax_name: str) -> int:
    """STABLE identity: same as `element_hash` but with transient CSS-state
    classes filtered out of `class` -- survives the class churn a re-render adds
    to otherwise-unchanged elements. Preferred key for the change-diff."""

    return _combined_hash(parent_branch_path, attributes, ax_name, filter_classes=True)


def _combined_hash(
    parent_branch_path: list[str],
    attributes: dict[str, str],
    ax_name: str,
    *,
    filter_classes: bool,
) -> int:
    branch = "/".join(parent_branch_path)
    attrs = _attributes_string(attributes, filter_classes=filter_classes)
    ax_part = f"|ax_name={ax_name}" if ax_name else ""
    return _hash16(f"{branch}|{attrs}{ax_part}")


def parent_branch_hash(parent_branch_path: list[str]) -> int:
    """Hash of just the ancestor tag-name chain (no attributes/name) -- a
    coarse structural-position identity used to detect MOVED elements (same
    element hash, different branch) and as a replay fallback."""

    return _hash16("/".join(parent_branch_path))
