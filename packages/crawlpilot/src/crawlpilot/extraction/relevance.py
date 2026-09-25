"""Query-relevant content filtering: keep the blocks of a page that answer a
caller's question, drop the rest.

`prune.py` answers "is this block content or chrome?" without knowing why
anyone wanted the page. This module answers the narrower and more valuable
question -- "is this block *about* what the caller asked for?" -- by scoring
every block against a query with Okapi BM25 and keeping the ones that rank.

The point is token cost at the far end. A caller extracting return policies
from a 12,000-word support page currently pays a model to read all 12,000 words
and ignore 11,700 of them. `ScrapeOptions.relevance_query` moves that decision
to a ranking function that costs nothing per call.

Adapted from crawl4ai's `BM25ContentFilter` (Apache-2.0; crawl4ai 0.9.4,
`crawl4ai/content_filter_strategy.py`). Two deliberate deviations:

1. **BM25 is implemented here, not imported.** Upstream depends on
   `rank_bm25`. crawlpilot's base install is deliberately small -- httpx,
   lxml, pydantic, structlog -- and BM25 is forty lines, so it stays a base
   install rather than becoming an extra.
2. **The threshold is relative, not absolute.** Upstream compares raw BM25
   scores to a fixed `1.0`. Raw BM25 has no natural scale: it moves with
   corpus size, document length and query length, so one constant cannot mean
   the same thing on a 6-block page and a 600-block one, and the failure mode
   is silently returning nothing. Here `threshold` is a fraction of the
   best-scoring block on the page, which means the same thing everywhere.
"""

from __future__ import annotations

import math
import re
from typing import cast

from lxml.html import HtmlElement

_K1 = 1.2
"""Term-frequency saturation. 1.2 is the standard Okapi value: a term's fifth
occurrence in a block adds far less than its second."""

_B = 0.75
"""Length normalization, standard Okapi. At 0.75 a long block is discounted for
its length but not erased by it."""

DEFAULT_THRESHOLD = 0.3
"""Keep blocks scoring at least this fraction of the page's best block. Chosen
to be forgiving: the cost of keeping a marginal block is a few tokens, the cost
of dropping the one block that held the answer is a wrong result."""

_BLOCK_TAGS = frozenset(
    {
        "p", "li", "blockquote", "pre", "td", "th", "dd", "dt", "figcaption",
        "h1", "h2", "h3", "h4", "h5", "h6", "div", "section", "article", "main",
        "aside", "details", "summary", "caption",
    }
)  # fmt: skip

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Deliberately short. A long stopword list starts removing terms that carry the
# query -- "how", "when" and "not" are the whole question in "when does support
# not apply" -- and BM25's IDF already discounts anything that appears in most
# blocks.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
        "is", "it", "of", "on", "or", "that", "the", "to", "was", "were",
        "will", "with",
    }
)  # fmt: skip


def filter_by_query(
    root: HtmlElement,
    query: str,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_words: int = 2,
) -> HtmlElement:
    """Drop blocks of `root` that do not rank against `query`, in place.

    Fails open, and does so in three distinct cases, because a filter that can
    empty a page is worse than no filter: an unusable query (blank, or nothing
    but stopwords), a page with no scoreable blocks, and a page where no block
    scores above zero all return the tree untouched. A caller who gets markdown
    back that ignored their query has a tuning problem; one who gets an empty
    string has no way to tell that from a page that failed to load.

    `min_words` excludes very short blocks from being *scored* -- a two-word
    `<li>` cannot earn a meaningful BM25 score -- but they are never dropped on
    that basis alone, since a short block inside a relevant section is usually
    part of it.
    """

    query_terms = _tokenize(query)
    if not query_terms:
        return root

    blocks = _leaf_blocks(root)
    scoreable = [block for block in blocks if len(_tokenize(_block_text(block))) >= min_words]
    if not scoreable:
        return root

    scores = _bm25(scoreable, query_terms)
    best = max(scores.values(), default=0.0)
    if best <= 0.0:
        return root

    cutoff = best * threshold
    for block in scoreable:
        if scores[block] >= cutoff:
            continue
        if block.tag in _HEADING_TAGS:
            # Headings survive regardless: they cost a handful of tokens and
            # they are what tells the far end which section an answer came
            # from. A heading dropped out from over its own kept paragraph is
            # a worse result than a heading kept over a dropped one.
            continue
        if block.getparent() is not None:
            block.drop_tree()

    return root


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) > 1 and token not in _STOPWORDS
    ]


def _block_text(el: HtmlElement) -> str:
    return " ".join(fragment.strip() for fragment in el.itertext() if fragment.strip())


def _leaf_blocks(root: HtmlElement) -> list[HtmlElement]:
    """Block elements with no block descendant of their own.

    Taking only the innermost blocks is what keeps a block's text from being
    scored once per ancestor -- and it is also what makes this safe on
    div-soup markup, where the innermost text-bearing `<div>` is the paragraph
    everywhere except in name. Text sitting directly inside a *container*
    (`<div>intro<p>body</p></div>`) is never a candidate here, so this pass can
    only ever drop leaves; it cannot take a section's preamble with it.
    """

    blocks: list[HtmlElement] = []
    for el in root.iter():
        if not isinstance(el.tag, str) or el.tag not in _BLOCK_TAGS:
            continue
        if any(
            isinstance(descendant.tag, str) and descendant.tag in _BLOCK_TAGS
            for descendant in el.iterdescendants()
        ):
            continue
        if _block_text(el):
            blocks.append(cast("HtmlElement", el))
    return blocks


def _bm25(blocks: list[HtmlElement], query_terms: list[str]) -> dict[HtmlElement, float]:
    """Okapi BM25 over `blocks` as the corpus, one score per block.

    The corpus is the page itself, which is the whole trick: IDF is computed
    against the other blocks on this page, so a term that appears in every
    block of this page (the product name, the site's own vocabulary) carries no
    weight here even though it would be rare across the web.
    """

    tokenized = [_tokenize(_block_text(block)) for block in blocks]
    lengths = [len(tokens) for tokens in tokenized]
    total = len(blocks)
    avg_len = sum(lengths) / total if total else 0.0

    document_frequency: dict[str, int] = {}
    for tokens in tokenized:
        for term in set(tokens):
            document_frequency[term] = document_frequency.get(term, 0) + 1

    idf = {
        term: math.log(
            1 + (total - document_frequency.get(term, 0) + 0.5) / (document_frequency.get(term, 0) + 0.5)
        )
        for term in set(query_terms)
    }

    scores: dict[HtmlElement, float] = {}
    for block, tokens, length in zip(blocks, tokenized, lengths, strict=True):
        frequencies: dict[str, int] = {}
        for token in tokens:
            frequencies[token] = frequencies.get(token, 0) + 1

        score = 0.0
        for term in query_terms:
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            denominator = frequency + _K1 * (1 - _B + _B * (length / avg_len if avg_len else 1.0))
            score += idf[term] * (frequency * (_K1 + 1)) / denominator
        scores[block] = score

    return scores
