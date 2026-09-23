"""An xpath expression must stay inside the thing it is scoped to.

`paths.py` already states this rule for the JSON dialects and explains what it
costs to break it:

    Measured on that same Walmart page, an unanchored `$..name` matches 203
    nodes, and among the first distinct strings it returns is a *sponsored
    competitor's* product name. An extraction language whose shortest
    expression silently scrapes the wrong brand is the wrong default, so
    descent is simply not offered.

The identical rule was never applied to xpath, and the identical failure
followed. A Zara build bound `care` to

    //*[contains(translate(text(),'CARE','care'),'care')]/following::ul[1]/li

which collected the site header and the nav menu -- "Bag0", "LOG IN", "Help" --
with the five real washing instructions behind them.

**Scoping alone cannot fix this, which is why it is a lint and not a rewrite.**
`_READ_JS` passes the scope root to `document.evaluate` as its *context node*,
and a context node constrains a **relative** expression only. `//x` is absolute
and `following::x` is a document-order walk; both ignore the root entirely. So a
`within` on the locator above would have changed nothing at all, and the
expression has to be refused rather than contained.

What is *not* banned matters as much. `following-sibling::` and
`preceding-sibling::` are bounded by the parent element, and they are how the
prompts teach the one thing CSS cannot express -- selecting a cell by its
sibling's text, `.//tr[th[normalize-space()='Item Weight']]/td`. Banning the
whole `following` family because `following::` is dangerous would take the
useful half with it.
"""

from __future__ import annotations

import re

# Axes that walk the document rather than the subtree. `following::` and
# `preceding::` are defined over document order, and the `ancestor` family goes
# up past the scope root by definition -- none of them can be contained by a
# context node.
_ESCAPING_AXES = (
    "following::",
    "preceding::",
    "ancestor::",
    "ancestor-or-self::",
)

# `following-sibling::`/`preceding-sibling::` are bounded by the parent and stay.
# Matched with a lookbehind for `-` so `following::` does not also match inside
# `following-sibling::`.
_AXIS_RE = re.compile(
    r"(?<![-\w])(" + "|".join(re.escape(a) for a in _ESCAPING_AXES) + r")"
)

# The full-alphabet case-fold. A model reaching for `translate()` reliably
# writes a short form -- `translate(text(),'CARE','care')` maps exactly those
# four letters, so it matches "Customer care" and "Careful" as readily as the
# heading it was aiming at. That is how the Zara `care` locator found the
# header.
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_TRANSLATE_RE = re.compile(
    r"translate\s*\([^,]+,\s*(['\"])([^'\"]*)\1\s*,\s*(['\"])([^'\"]*)\3\s*\)"
)


def xpath_escape_reason(selector: str, *, scoped: bool) -> str | None:
    """Why this expression cannot be trusted inside a scope, or None.

    `scoped` says whether the locator carries a `within`. An absolute path is
    only *wrong* when something was supposed to contain it; on an unscoped
    locator `//` is the ordinary way to write a document query and refusing it
    would refuse most correct xpath.

    A returned string is written to be read by a person and fed back to the
    model, the same discipline `verify_locators` and `_problems_with` follow.
    """

    if not selector:
        return None

    axis = _AXIS_RE.search(selector)
    if axis is not None:
        found = axis.group(1)
        return (
            f"the {found} axis walks the whole document in order, so it leaves any "
            "container it is scoped to -- that is how a selector aimed at a "
            "section ends up collecting the site header. Use a path that stays "
            "inside the element you mean, or following-sibling::/"
            "preceding-sibling:: which are bounded by the parent"
        )

    if scoped and selector.startswith("/"):
        return (
            "an absolute expression ignores the scope entirely and searches the "
            f"whole page -- write it relative to the container, {_relative(selector)!r}"
        )

    return None


def partial_case_fold_reason(selector: str) -> str | None:
    """Whether a `translate()` in this expression folds only part of the alphabet.

    Not fatal on its own -- a short `translate()` can be deliberate -- so this is
    reported as a warning and fed back as advice rather than refused. The Zara
    build's `translate(text(),'CARE','care')` is the case it exists for.
    """

    for match in _TRANSLATE_RE.finditer(selector):
        source, target = match.group(2), match.group(4)
        if not source or source == _UPPER:
            continue
        if source.upper() == source and target.lower() == target and len(source) < 26:
            return (
                f"translate(..., {source!r}, {target!r}) lower-cases only those "
                f"{len(source)} letters, so it matches any text containing them in "
                "any other word. Fold the whole alphabet: "
                f"translate(., '{_UPPER}', '{_LOWER}')"
            )
    return None


def _relative(selector: str) -> str:
    return "." + selector if selector.startswith("/") else selector


def relativize_xpath(selector: str) -> str | None:
    """The expression rewritten to run inside a scope, or None if it already does.

    Only the leading anchor is touched. Rewriting anything deeper would be
    guessing at what the author meant, and an expression carrying an escaping
    axis is refused by `xpath_escape_reason` before it can reach this.
    """

    if not selector or not selector.startswith("/"):
        return None
    return _relative(selector)


def scoped_xpath(selector: str) -> str:
    """`relativize_xpath` with the no-op case folded in, for call sites that just
    want the string to use."""

    return relativize_xpath(selector) or selector
