"""Stage 1b of the extraction pipeline: density-based pruning of the sanitized
tree.

`sanitizer.py` removes the boilerplate it can *name* -- a fixed list of
selectors (`header`, `.sidebar`, `#cookie`, ...). That list is worth a great
deal on sites built out of semantic tags and worth nothing on the ones that
aren't: a utility-class page whose navigation is `<div class="flex gap-4">` and
whose article body is `<div class="flex flex-col">` offers no selector to
match, so the whole page survives stage 1 and the markdown comes out half
chrome.

This module scores instead of matching. Every element gets a composite score
from five signals -- text density, link density, tag identity, negative
class/id hints and absolute text length -- and subtrees scoring below a
threshold are dropped. Nothing here needs to know any one site's markup, which
is the point: it is the half of main-content extraction that generalizes.

Runs *after* `sanitize()` and *before* `markdown_converter.to_markdown()`, over
the same tree, so it composes with the selector pass rather than replacing it.

Adapted from crawl4ai's `PruningContentFilter` / `PruningContentFilterLXML`
(Apache-2.0; crawl4ai 0.9.4, `crawl4ai/content_filter_strategy{,_lxml}.py`):
the five metrics, their weights, the tag-weight table and the
dynamic-threshold adjustments are theirs. Three deliberate deviations, each
marked at its site below:

1. **The class/id signal actually applies.** Upstream folds it in as
   `max(0, class_id_score)` where that score is only ever `0` or negative --
   so the metric cannot do anything except dilute the denominator by its own
   weight. Here the penalty is subtracted, which is what makes
   `<div class="social-share">` score below `<div class="prose">`.
2. **Link text is the anchor's whole subtree.** Upstream reads BeautifulSoup's
   `a.string`, which is `None` the moment an anchor wraps any markup, so
   `<a><span>Home</span></a>` contributes zero link text -- and modern nav
   markup is made of exactly that. Here it is the anchor's full text, so
   link-density does its job on the pages that need it most.
3. **No BeautifulSoup-parity arithmetic.** Upstream reproduces BS4's exact
   serialized byte lengths so its two engines agree node-for-node. That
   constraint does not exist here, so inner length is computed straight off
   the lxml tree.
"""

from __future__ import annotations

import math
import re
from typing import Literal

from lxml.html import HtmlElement

from crawlpilot.extraction import selectors

ThresholdType = Literal["fixed", "dynamic"]

DEFAULT_THRESHOLD = 0.48
"""crawl4ai's own default, kept -- the metric weights below are tuned around
it, so the two numbers are not independently adjustable."""

# Weight of each signal in the composite score. Sums to 1.0, so a score is
# directly comparable to `threshold`.
_METRIC_WEIGHTS = {
    "text_density": 0.4,
    "link_density": 0.2,
    "tag_weight": 0.2,
    "class_id_weight": 0.1,
    "text_length": 0.1,
}

# How much an element's tag alone says about it carrying body content.
_TAG_WEIGHTS = {
    "article": 1.5,
    "p": 1.0,
    "section": 1.0,
    "div": 0.5,
    "span": 0.3,
    "li": 0.5,
    "ul": 0.5,
    "ol": 0.5,
    "h1": 1.2,
    "h2": 1.1,
    "h3": 1.0,
    "h4": 0.9,
    "h5": 0.8,
    "h6": 0.7,
}
_DEFAULT_TAG_WEIGHT = 0.5

# Used only by the dynamic threshold: an element of "important" tag gets a
# more forgiving bar than a bare div.
_TAG_IMPORTANCE = {
    "article": 1.5,
    "main": 1.4,
    "section": 1.3,
    "p": 1.2,
    "h1": 1.4,
    "h2": 1.3,
    "h3": 1.2,
    "div": 0.7,
    "span": 0.6,
}
_DEFAULT_TAG_IMPORTANCE = 0.7

_NEGATIVE_CLASS_RE = re.compile(
    r"nav|footer|header|sidebar|ads?\b|comment|promo|advert|social|share|cookie|banner",
    re.IGNORECASE,
)

_PRESERVE_TAGS = frozenset({"table", "pre", "figure", "dl", "blockquote"})
"""Never scored, never descended into. Each is a structure whose value is not
in its text-to-markup ratio: a data table is mostly markup by construction, and
a code block's whole point is that nobody reformats it. Scoring them means
losing them."""

# HTML void elements -- they serialize self-closed, with no end tag, which
# `_outer_len` has to account for to keep text density honest on image-heavy
# markup.
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }
)  # fmt: skip


class _Metrics:
    """Per-element metrics, computed exactly once each in one bottom-up pass.

    The naive form of this algorithm asks each element for its subtree text and
    serialized inner HTML while scoring it, which re-walks every subtree once
    per ancestor -- super-linear on wide pages, and the reason crawl4ai wrote a
    second engine. Caching here keeps the whole pass O(nodes + text).
    """

    __slots__ = ("text_len", "inner_len", "link_text_len", "word_count")

    def __init__(self, text_len: int, inner_len: int, link_text_len: int, word_count: int) -> None:
        self.text_len = text_len
        self.inner_len = inner_len
        self.link_text_len = link_text_len
        self.word_count = word_count


def prune(
    root: HtmlElement,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    threshold_type: ThresholdType = "dynamic",
    min_word_threshold: int | None = None,
) -> HtmlElement:
    """Drop low-scoring subtrees from `root`, in place, and return it.

    `root` itself is never dropped -- pruning starts at its children (or at
    `<body>`'s children when `root` is a whole document), so the caller always
    gets a usable tree back even on a page where every block scores badly.

    `threshold_type="dynamic"` (the default here, `"fixed"` upstream) adjusts
    the bar per element using tag importance and the text/link ratios, which is
    what keeps a long link-free `<p>` inside a low-scoring wrapper from going
    down with it.
    """

    start = _start_node(root)
    metrics = _compute_metrics(start)
    for child in list(start):
        _prune_subtree(
            child,
            metrics,
            threshold=threshold,
            threshold_type=threshold_type,
            min_word_threshold=min_word_threshold,
        )
    return root


def _start_node(root: HtmlElement) -> HtmlElement:
    """`<body>` when `root` is a full document, else `root`. Pruning from
    `<html>` would score `<body>` as one element and could drop the entire
    page on a single bad ratio."""

    for body in root.iter("body"):
        return body
    return root


def _is_element(el: HtmlElement) -> bool:
    """Comments and processing instructions are in the tree but carry a
    non-string `tag`; every tag lookup below would raise on them."""

    return isinstance(el.tag, str)


def _compute_metrics(start: HtmlElement) -> dict[HtmlElement, _Metrics]:
    """One bottom-up pass over `start` and its descendants.

    Pre-order collection then `reversed` iteration is what guarantees every
    element sees its children's metrics already cached -- a parent is always
    appended before its children.
    """

    nodes: list[HtmlElement] = []
    stack: list[HtmlElement] = [start]
    while stack:
        el = stack.pop()
        nodes.append(el)
        if el.tag in _PRESERVE_TAGS:
            # Preserved subtrees are kept whole, so their interiors are never
            # scored and never need metrics of their own.
            continue
        stack.extend(child for child in el if _is_element(child))

    metrics: dict[HtmlElement, _Metrics] = {}
    for el in reversed(nodes):
        text_len = 0
        inner_len = 0
        word_count = 0

        own_text = el.text
        if own_text:
            stripped = own_text.strip()
            text_len += len(stripped)
            inner_len += len(own_text)
            word_count += len(stripped.split())

        link_text_len = 0
        for child in el:
            if _is_element(child):
                child_metrics = metrics.get(child)
                if child_metrics is not None:
                    text_len += child_metrics.text_len
                    inner_len += _outer_len(child, child_metrics.inner_len)
                    word_count += child_metrics.word_count
                    if child.tag == "a":
                        # Deviation 2: the anchor's whole subtree text, not
                        # BeautifulSoup's `.string`, which nested markup nulls.
                        link_text_len += child_metrics.text_len
                else:
                    # A preserved subtree: measured as text, never scored.
                    preserved_text = _subtree_text_len(child)
                    text_len += preserved_text
                    inner_len += preserved_text
                    word_count += len(" ".join(child.itertext()).split())

            tail = child.tail
            if tail:
                stripped_tail = tail.strip()
                text_len += len(stripped_tail)
                inner_len += len(tail)
                word_count += len(stripped_tail.split())

        metrics[el] = _Metrics(text_len, inner_len, link_text_len, word_count)

    return metrics


def _subtree_text_len(el: HtmlElement) -> int:
    return sum(len(fragment.strip()) for fragment in el.itertext())


def _outer_len(el: HtmlElement, inner_len: int) -> int:
    """Serialized outer length of `el` given its cached inner length: the
    markup cost this element adds to its parent's density denominator."""

    tag = el.tag
    assert isinstance(tag, str)
    attrs_len = sum(4 + len(key) + len(value or "") for key, value in el.attrib.items())
    if tag in _VOID_TAGS:
        return 1 + len(tag) + attrs_len + 2  # `<tag ... />`
    # `<tag ...>` + inner + `</tag>`
    return (2 + len(tag) + attrs_len) + inner_len + (3 + len(tag))


def _prune_subtree(
    el: HtmlElement,
    metrics: dict[HtmlElement, _Metrics],
    *,
    threshold: float,
    threshold_type: ThresholdType,
    min_word_threshold: int | None,
) -> None:
    if not _is_element(el):
        return
    if el.tag in _PRESERVE_TAGS or _is_force_included(el):
        return

    node_metrics = metrics.get(el)
    if node_metrics is None:
        return

    if _should_remove(
        el,
        node_metrics,
        threshold=threshold,
        threshold_type=threshold_type,
        min_word_threshold=min_word_threshold,
    ):
        if el.getparent() is not None:
            # `drop_tree`, not `remove`: it merges the element's tail text into
            # its surroundings instead of deleting prose that merely happened
            # to sit after a dropped tag.
            el.drop_tree()
        return

    for child in list(el):
        _prune_subtree(
            child,
            metrics,
            threshold=threshold,
            threshold_type=threshold_type,
            min_word_threshold=min_word_threshold,
        )


def _is_force_included(el: HtmlElement) -> bool:
    """Honours the same force-include list `sanitizer._remove_boilerplate`
    does, so the two passes cannot disagree about what is untouchable."""

    for selector in selectors.FORCE_INCLUDE_SELECTORS:
        try:
            if el.cssselect(selector):
                return True
        except Exception:
            continue
    return False


def _should_remove(
    el: HtmlElement,
    m: _Metrics,
    *,
    threshold: float,
    threshold_type: ThresholdType,
    min_word_threshold: int | None,
) -> bool:
    score = _composite_score(el, m, min_word_threshold=min_word_threshold)

    if threshold_type == "fixed":
        return score < threshold

    tag_importance = _TAG_IMPORTANCE.get(str(el.tag), _DEFAULT_TAG_IMPORTANCE)
    text_ratio = m.text_len / m.inner_len if m.inner_len > 0 else 0.0
    link_ratio = m.link_text_len / m.text_len if m.text_len > 0 else 1.0

    adjusted = threshold
    if tag_importance > 1:
        adjusted *= 0.8
    if text_ratio > 0.4:
        adjusted *= 0.9
    if link_ratio > 0.6:
        adjusted *= 1.2

    return score < adjusted


def _composite_score(
    el: HtmlElement, m: _Metrics, *, min_word_threshold: int | None
) -> float:
    if min_word_threshold is not None and m.word_count < min_word_threshold:
        return -1.0

    text_density = m.text_len / m.inner_len if m.inner_len > 0 else 0.0
    link_density = 1 - (m.link_text_len / m.text_len if m.text_len > 0 else 0.0)
    tag_weight = _TAG_WEIGHTS.get(str(el.tag), _DEFAULT_TAG_WEIGHT)

    score = (
        _METRIC_WEIGHTS["text_density"] * text_density
        + _METRIC_WEIGHTS["link_density"] * link_density
        + _METRIC_WEIGHTS["tag_weight"] * tag_weight
        # Deviation 1: subtracted, not `max(0, ...)`-ed into nothing.
        + _METRIC_WEIGHTS["class_id_weight"] * _class_id_score(el)
        + _METRIC_WEIGHTS["text_length"] * math.log(m.text_len + 1)
    )
    return score / sum(_METRIC_WEIGHTS.values())


def _class_id_score(el: HtmlElement) -> float:
    """`0.0` for a neutral element, down to `-1.0` when both its class and its
    id name it as chrome. Never positive: a hopeful class name is not evidence,
    while `class="social-share"` is."""

    score = 0.0
    class_attr = el.get("class")
    if class_attr and _NEGATIVE_CLASS_RE.search(class_attr):
        score -= 0.5
    id_attr = el.get("id")
    if id_attr and _NEGATIVE_CLASS_RE.search(id_attr):
        score -= 0.5
    return score
