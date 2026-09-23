"""How much a selector is worth keeping, once it is known to work.

Verification answers "does this read the right value *now*". It cannot answer
"will it still, after the next deploy", and those are different questions with
different evidence. A Zara build verified all four of these for one field:

    .expandable-text__inner-content p
    .expandable-text__inner-content > p
    article > div.product-detail-view__main-content > div.product-detail-view__main-info
      > div.product-detail-info > section.product-detail-description > div.expandable-text
      > div.expandable-text__content > div.expandable-text__inner-content > p
    //*[@id="main"]/div[1]/div[1]/div[1]/div[1]/article[1]/div[1]/div[2]/div[1]/section[1]/…

The last two are a six-level class chain and a pure positional path. Both read
the right paragraph today; neither survives a single element being inserted
above it, and the positional one will not *fail* when that happens -- it will
resolve to whatever now sits at that position and report it as data.

**The rules are ported, not invented.** `frontend/src/lib/picker/vendor/shared/
selectors/stability.ts` has been accumulating them against real pages --
Tailwind utilities, styled-components and Emotion hashes, versioned layout
slugs, state words -- and it was unreachable from the build because it is
TypeScript. `tests/test_recipe_v2_selector_quality.py` pins the two against a
shared table, the way `tests/test_recipe_v2_capture.py` already pins
`REVEALING_OPS` against `fromPick.ts`.

**Grading is comparative, and that is the whole design.** A `weak` selector is
dropped only when the same field has a better one that also verified;
`filter_by_grade` keeps a weak chain intact when it is all there is, exactly as
`filterPersistableChain` does on the other side. A brittle selector loses to a
good one and beats nothing at all, because a field with no binding collects
nothing on every run.
"""

from __future__ import annotations

import re
from typing import Literal

Grade = Literal["strong", "ok", "weak"]

# --- ported from stability.ts -----------------------------------------------

_UNSTABLE_CLASS_RULES: tuple[re.Pattern[str], ...] = (
    # Tailwind JIT / arbitrary values: w-[100px], max-[calc(100%-1rem)]
    re.compile(r"[\[\]()]"),
    # Purely numeric, or leading digit
    re.compile(r"^\d"),
    # Versioned layout slugs: ss26, ab12, -v2-, -v3-
    re.compile(r"[_-][a-z]{1,4}\d{2,}([_-]|$)"),
    re.compile(r"[_-]v\d+[_-]"),
    re.compile(r"^layout-"),
    # Short sizing utilities: w-4, h-8, p-2, gap-3
    re.compile(r"^[a-z]{1,3}-\d"),
    # Tailwind spacing / sizing
    re.compile(r"^[mp][xytrblse]?-"),
    re.compile(r"^(w|h|min-w|min-h|max-w|max-h|size)-"),
    # Tailwind layout keywords
    re.compile(
        r"^(flex|grid|block|inline|hidden|visible|float|clear|contents|flow|box"
        r"|table|columns|break|isolate|object|overscroll|overflow|static|fixed"
        r"|absolute|relative|sticky)(-|$)"
    ),
    # Tailwind flex/grid children
    re.compile(
        r"^(grow|shrink|basis|order|justify|items|content|self|gap|space|place)(-|$)"
    ),
    # Tailwind typography
    re.compile(
        r"^(text-[a-z]|font-|leading-|tracking-|indent-|whitespace-|uppercase"
        r"|lowercase|capitalize|truncate|antialiased)"
    ),
    # Tailwind colours
    re.compile(r"^(bg-|border-[a-z]|from-|via-|to-|ring-|shadow-[a-z]|fill-|stroke-)"),
    # Tailwind borders & effects
    re.compile(
        r"^(border|rounded|ring|divide|outline|shadow|opacity|blur|brightness"
        r"|contrast|grayscale|invert|saturate|backdrop)(-|$)"
    ),
    # Tailwind transitions / transforms
    re.compile(
        r"^(transition|duration|ease|delay|animate|scale|rotate|translate|skew"
        r"|transform|will-change)(-|$)"
    ),
    # Tailwind misc
    re.compile(
        r"^(cursor|select|resize|scroll|snap|touch|appearance|pointer|sr-|list-"
        r"|decoration-)"
    ),
    # Generated: styled-components, CSS modules
    re.compile(r"^(sc-|css-|_[A-Z])"),
    # Pure state words
    re.compile(
        r"^(active|hover|selected|focus|current|visible|hidden|open|closed"
        r"|disabled|loading)$"
    ),
    # Design-system namespace prefixes: zds-layout-desktop, mds-grid-main
    re.compile(
        r"^[a-z]{2,4}-(layout|container|wrapper|main|content|page|section|view"
        r"|header|footer|sidebar|panel|grid)"
    ),
    # Responsive/variant suffixes: card-desktop, layout-compact
    re.compile(
        r"-(desktop|mobile|tablet|std|alt|wide|narrow|full|compact|primary"
        r"|secondary|small|large)(-|$)"
    ),
)

_HASH_CLASS_RE = re.compile(r"^(css|sc)-[a-z0-9]{4,10}$")
_HEX_HASH_RE = re.compile(r"^_?[0-9a-f]{6,12}$")


def is_unstable_class(name: str) -> bool:
    """A class that describes how something looks, or which build produced it,
    rather than what it is. Mirrors `isUnstableClass`."""

    if not name or len(name) < 2:
        return True
    # Responsive / state variant prefixes (sm:, hover:, dark:)
    if ":" in name:
        return True
    if name[0] in "[(*":
        return True
    return any(rule.search(name) for rule in _UNSTABLE_CLASS_RULES)


def is_hash_class(name: str) -> bool:
    """A build-generated content hash. Deterministic per build, and different
    after every redeploy. Mirrors `isHashClass`."""

    if _HASH_CLASS_RE.match(name):
        return True
    # A bare hex hash must carry a digit, so real words spelled in hex letters
    # ("facade", "decade") never match.
    return bool(_HEX_HASH_RE.match(name)) and any(c.isdigit() for c in name)


# --- grading ----------------------------------------------------------------

# Attributes a page author set on purpose, for something to find this element by.
_STRONG_ATTR_RE = re.compile(
    r"\[\s*(id|data-testid|data-test-id|data-test|data-cy|data-qa|data-automation-id"
    r"|itemprop|role|name|rel|type)\s*[~|^$*]?=",
    re.IGNORECASE,
)
_STRONG_PREFIX_RE = re.compile(r"\[\s*(data-|aria-)", re.IGNORECASE)
_POSITIONAL_RE = re.compile(r":nth-(child|of-type|last-child|last-of-type)\b")
_CLASS_TOKEN_RE = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")
_ID_TOKEN_RE = re.compile(r"#(-?[_a-zA-Z][\w-]*)")

# How many combinators before a CSS chain is describing a path rather than a
# thing. Three is where the Zara chain crosses over -- `article > div > div >
# div > section > div > div > div > p` is a route to a paragraph, not a name for
# it, and every element on that route is a chance to break.
_MAX_CHAIN_DEPTH = 3

# Positional steps in an xpath: `/div[1]/div[2]`. A couple is ordinary; a run of
# them is a recorded path through a DOM that will not stay that shape.
_XPATH_POSITIONAL_RE = re.compile(r"/[a-zA-Z*][\w:-]*\[\d+\]")
_MAX_XPATH_POSITIONAL = 2

# Selecting by a sibling's text -- the one thing CSS cannot express, and what
# the prompts teach xpath for.
_XPATH_RELATIONSHIP_RE = re.compile(
    r"(normalize-space|contains|text\(\)|following-sibling|preceding-sibling)"
)


def _split_combinators(selector: str) -> list[str]:
    """Top-level compound selectors, ignoring anything inside brackets or
    parentheses -- `:has(a > b)` is one step, not three."""

    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in selector:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth = max(0, depth - 1)
        if depth == 0 and (char.isspace() or char in ">+~"):
            if current:
                parts.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def _css_grade(selector: str) -> Grade:
    # A selector list is only as good as its best branch: the browser matches
    # any of them, so one strong alternative carries it.
    if "," in selector:
        return _best(_css_grade(part.strip()) for part in selector.split(",") if part.strip())

    ids = _ID_TOKEN_RE.findall(selector)
    classes = _CLASS_TOKEN_RE.findall(selector)

    if _POSITIONAL_RE.search(selector):
        return "weak"
    if any(is_hash_class(c) for c in classes):
        return "weak"

    steps = _split_combinators(selector)
    if len(steps) - 1 > _MAX_CHAIN_DEPTH:
        return "weak"

    if ids and not all(is_hash_class(i) for i in ids):
        return "strong"
    if _STRONG_ATTR_RE.search(selector) or _STRONG_PREFIX_RE.search(selector):
        return "strong"
    if ":has(" in selector:
        return "strong"

    if classes:
        # A chain built only out of presentational class names says nothing
        # about what the element is, and every one of them can be renamed by a
        # change that was only ever meant to be visual.
        if all(is_unstable_class(c) for c in classes):
            return "weak"
        return "ok"

    # Bare tags. One or two is a semantic selector (`h1`, `article > h1`); a long
    # run of them is a path.
    return "ok" if len(steps) <= 2 else "weak"


def _xpath_grade(selector: str) -> Grade:
    positional = len(_XPATH_POSITIONAL_RE.findall(selector))
    if positional > _MAX_XPATH_POSITIONAL:
        return "weak"
    if _XPATH_RELATIONSHIP_RE.search(selector):
        return "strong"
    if "@id" in selector or "@data-" in selector or "@itemprop" in selector:
        return "strong"
    return "weak" if positional else "ok"


def selector_grade(kind: str, selector: str | None) -> Grade:
    """How much this selector is worth keeping, given that it works.

    Structured locators are not graded here: a `json_ld` or `hydration` path has
    no selector, and `SOURCE_PRIORITY` already ranks it above every DOM locator
    for the reason this module exists -- a value in JSON survives a rewrite that
    destroys every class name on the page.
    """

    if kind not in ("css", "xpath") or not selector:
        return "ok"
    return _css_grade(selector) if kind == "css" else _xpath_grade(selector)


_RANK: dict[Grade, int] = {"strong": 0, "ok": 1, "weak": 2}


def _best(grades) -> Grade:
    return min(grades, key=lambda g: _RANK[g], default="ok")


def filter_by_grade(graded: list[tuple[Grade, object]]) -> list[object]:
    """Drop the weak candidates, unless weak is all there is.

    The comparative rule, and the reason it is comparative: a brittle selector
    loses to a good one and beats nothing at all. A field left with no binding
    collects nothing on every run, which is strictly worse than a selector that
    might rot -- and the rot is visible, because a chain that falls through to
    its last resort shows up as `candidate N of N` in the run's provenance.
    """

    keep = [item for grade, item in graded if grade != "weak"]
    return keep if keep else [item for _grade, item in graded]
