"""How much a selector is worth keeping, once it is known to work.

The four selectors below are the four a real Zara build verified and froze for
one field. Two of them read the right paragraph and neither survives an element
being inserted above it -- and the positional one does not fail when that
happens, it resolves to whatever now sits at that position and reports it as
data.

`test_the_port_agrees_with_the_picker` is the pin. `is_unstable_class` is a port
of `isUnstableClass` from `frontend/src/lib/picker/vendor/shared/selectors/
stability.ts`, and two halves of one system that disagree about what a stable
class is would grade the same page differently depending on who authored it --
the same reason `tests/test_recipe_v2_capture.py` pins `REVEALING_OPS` against
`fromPick.ts`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentpilot.recipe.v2.selector_quality import (
    filter_by_grade,
    is_hash_class,
    is_unstable_class,
    selector_grade,
)

# Verbatim from the build this module was written against.
ZARA_SHORT = ".expandable-text__inner-content p"
ZARA_CHAIN = (
    "article > div.product-detail-view__main-content > "
    "div.product-detail-view__main-info > div.product-detail-info > "
    "section.product-detail-description > div.expandable-text > "
    "div.expandable-text__content > div.expandable-text__inner-content > p"
)
ZARA_XPATH = (
    '//*[@id="main"]/div[1]/div[1]/div[1]/div[1]/article[1]/div[1]/div[2]/'
    "div[1]/section[1]/div[1]/div[1]/div[1]/p[1]"
)


@pytest.mark.parametrize(
    ("kind", "selector", "expected"),
    [
        # Named by what it is.
        ("css", "#specs", "strong"),
        ("css", "[itemprop=name]", "strong"),
        ("css", "[data-testid='composition-panel']", "strong"),
        ("css", "[aria-label='Care']", "strong"),
        ("css", "link[rel='canonical']", "strong"),
        ("css", "dl:has(dt)", "strong"),
        # Ordinary and fine.
        ("css", "h1", "ok"),
        ("css", ZARA_SHORT, "ok"),
        ("css", "article > h1", "ok"),
        # Described by where it sits.
        ("css", ZARA_CHAIN, "weak"),
        ("css", "li:nth-child(3)", "weak"),
        ("css", ".sc-a1b2c3 .css-dy3i81", "weak"),
        ("css", "div > div > div > div > div > p", "weak"),
        # xpath.
        ("xpath", ZARA_XPATH, "weak"),
        ("xpath", ".//tr[th[normalize-space()='Item Weight']]/td", "strong"),
        ("xpath", './/*[@itemprop="name"]', "strong"),
        ("xpath", ".//ul/li", "ok"),
        # Structured locators are ranked by SOURCE_PRIORITY, not graded here.
        ("json_ld", None, "ok"),
        ("hydration", None, "ok"),
        ("ax_role", None, "ok"),
    ],
)
def test_selector_grade(kind: str, selector: str | None, expected: str) -> None:
    assert selector_grade(kind, selector) == expected


def test_a_selector_list_is_as_good_as_its_best_branch() -> None:
    """The browser matches any of them, so one strong alternative carries it --
    and a build that wrote `.a, #b` meant the `#b`."""

    assert selector_grade("css", ".product-detail-color, #color-name") == "strong"
    assert selector_grade("css", "div > div > div > div > p, .sc-a1b2c3") == "weak"


# --- the comparative rule ---------------------------------------------------


def test_a_brittle_selector_loses_to_a_good_one() -> None:
    kept = filter_by_grade([("weak", "chain"), ("strong", "#id"), ("ok", "h1")])
    assert kept == ["#id", "h1"]


def test_a_brittle_selector_beats_nothing_at_all() -> None:
    """A field with no binding collects nothing on every run, which is strictly
    worse than a selector that might rot -- and the rot is visible, because a
    chain falling through to its last resort shows up as `candidate N of N` in
    the run's provenance."""

    kept = filter_by_grade([("weak", ZARA_CHAIN), ("weak", ZARA_XPATH)])
    assert kept == [ZARA_CHAIN, ZARA_XPATH]


def test_filtering_an_empty_chain_is_not_an_error() -> None:
    assert filter_by_grade([]) == []


# --- the pin ----------------------------------------------------------------

# One table, asserted here and readable from the TypeScript. Every entry is a
# class name seen on a real page.
UNSTABLE = [
    "flex", "grid", "hidden", "absolute", "w-4", "h-8", "px-2", "mt-1",
    "text-sm", "font-bold", "bg-white", "border-gray", "rounded-lg",
    "transition-all", "duration-300", "cursor-pointer", "sr-only",
    "sc-a1b2c3", "css-dy3i81", "items-center", "justify-between",
    "gap-3", "shadow-md", "opacity-50", "active", "selected", "open",
    "layout-main", "zds-layout-desktop", "card-desktop", "w-[100px]",
    # A versioned slug needs its separator to be one: the picker's rule is
    # `[_-][a-z]{1,4}\d{2,}`, so `product-ss26` is caught and a bare `ss26` is
    # not. Kept as written rather than widened -- a two-letter-plus-digits class
    # with no separator is as likely to be a real name as a build stamp.
    "hover:bg-red", "product-ss26", "a",
]

STABLE = [
    "product-detail-description", "expandable-text__inner-content",
    "care-list", "price", "product-title", "composition-panel",
    "breadcrumb", "review-count", "swatch-name",
]


@pytest.mark.parametrize("name", UNSTABLE)
def test_unstable_classes(name: str) -> None:
    assert is_unstable_class(name)


@pytest.mark.parametrize("name", STABLE)
def test_stable_classes(name: str) -> None:
    assert not is_unstable_class(name)


@pytest.mark.parametrize(
    "name", ["css-dy3i81", "sc-1mbp38s", "_75228706", "e6d1c6b3"]
)
def test_hash_classes(name: str) -> None:
    assert is_hash_class(name)


@pytest.mark.parametrize("name", ["facade", "decade", "price", "care-list"])
def test_a_word_spelled_in_hex_letters_is_not_a_hash(name: str) -> None:
    """`facade` and `decade` are valid hex. Requiring a digit is what keeps a
    real class name from being mistaken for a build artefact."""

    assert not is_hash_class(name)


_STABILITY_TS = (
    Path(__file__).parent.parent
    / "frontend/src/lib/picker/vendor/shared/selectors/stability.ts"
)


def test_the_port_agrees_with_the_picker() -> None:
    """The two halves author recipes for the same pages. A class one calls
    stable and the other calls generated would grade the same selector
    differently depending on who wrote it.

    Checked by running the TypeScript's own regexes here, rather than by
    re-listing them -- a copy of the rules would drift in exactly the way this
    test exists to catch.
    """

    source = _STABILITY_TS.read_text()
    body = source.split("export function isUnstableClass", 1)[1].split(
        "export function", 1
    )[0]

    # Every `/.../ .test(c)` literal in the TypeScript, translated to Python.
    # The two dialects agree on everything these rules use.
    patterns = [
        re.compile(m.group(1))
        for m in re.finditer(r"if \(/(.+?)/\.test\(c\)\) return true;", body)
    ]
    assert len(patterns) > 15, "the TypeScript rules did not parse -- has it moved?"

    for name in UNSTABLE + STABLE:
        ts_says = any(p.search(name) for p in patterns)
        py_says = is_unstable_class(name)
        if ts_says:
            # Everything the picker calls unstable, the port must too. The
            # reverse is not asserted: the port also folds in the length and
            # prefix guards the TypeScript writes outside these literals.
            assert py_says, f"{name!r}: picker says unstable, port says stable"
