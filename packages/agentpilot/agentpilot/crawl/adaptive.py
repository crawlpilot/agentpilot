"""Adaptive crawling: stop when the crawl knows enough, not when it runs out of
budget.

Every crawl today ends one of two ways -- it exhausts `limit`, or it runs out of
in-scope links. Neither has anything to do with whether the caller got their
answer. Someone crawling a support site for return-policy information sets
`limit: 500` because 500 sounds safe, and either pays for 500 pages when the
answer was on page 4, or sets `limit: 50` and stops 12 pages short of it.

This tracks three things about what has been read so far and stops when their
combination crosses a threshold:

* **Coverage** -- do the query's terms actually appear across the pages read, and
  how densely? A crawl that has not yet seen the word "refund" has not covered a
  question about refunds.
* **Consistency** -- do the pages overlap in vocabulary? High overlap means the
  crawl is circling one coherent topic. Low overlap means it is wandering, and
  more pages of wandering will not help.
* **Saturation** -- is each new page still introducing vocabulary the crawl has
  not seen? When that rate collapses, further pages are repeating what is already
  known. This is the signal that actually says "enough".

Ported from crawl4ai's `StatisticalStrategy` (Apache-2.0; crawl4ai 0.9.4,
`crawl4ai/adaptive_crawler.py`). The metric definitions, their 0.4/0.3/0.3
weighting and the saturation-rate idea are theirs.

**Why the state shape is different.** Upstream's `AdaptiveCrawler.digest()` is a
single-process loop: it holds every crawled page in memory, recomputes metrics
over the whole knowledge base each batch, and persists by pickling to disk. None
of that survives here. This crawler is N worker processes claiming rows from one
Postgres queue with `FOR UPDATE SKIP LOCKED`, so adaptive state has to be
durable, shared, and updated **incrementally** -- one page at a time, by whichever
worker happened to get it. Which forces three adaptations, each marked below:

1. Only query-term statistics are kept in full. Upstream keeps frequencies for
   every term on every page; coverage only ever reads the query's terms, and a
   crawl of 500 pages has tens of thousands of terms that would be read and
   rewritten from the database on every single page.
2. Consistency is measured against a bounded sample, not every pair. Upstream is
   O(n²) in pages and recomputes from scratch; at 500 pages that is 125,000 set
   intersections per update, per worker.
3. The vocabulary is capped. Saturation needs to know whether a term is new,
   which needs the vocabulary; an uncapped one is unbounded state in a JSONB
   column. See `MAX_VOCABULARY` for what the cap does to the signal and why the
   direction it errs in is the safe one.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

MAX_VOCABULARY = 20_000
"""Cap on tracked distinct terms.

Past it, terms that are genuinely new stop being counted as new, so the measured
new-term rate falls and saturation rises. That biases toward stopping earlier,
which is the safe direction: the failure is "stopped a little sooner than ideal",
not "crawled forever". A 20,000-term vocabulary is roughly a 300-page corpus, and
a crawl that has read 300 pages without reaching its threshold is one whose
threshold was wrong.
"""

CONSISTENCY_SAMPLE = 16
"""How many previous documents a new one is compared against. Fixed-size, so an
update costs the same on page 500 as on page 5."""

SIGNATURE_TERMS = 256
"""Terms kept per sampled document. The most frequent ones, which is what makes a
Jaccard overlap between two signatures mean "these pages are about the same
thing" rather than "these pages are both in English"."""

HISTORY_LENGTH = 64
"""New-term counts retained. Saturation compares the recent rate against the
early rate, so only the ends matter -- but keeping a window rather than two
numbers means a single anomalous page cannot define either end."""

_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
"""Three characters minimum, matching upstream's `len(t) > 2`. Shorter tokens are
overwhelmingly stopwords and markup residue, and they dominate a frequency count
without carrying topic."""

# Weights from crawl4ai's `calculate_confidence`. Kept as-is: they are the one
# part of this that was tuned against real crawls rather than reasoned about.
_COVERAGE_WEIGHT = 0.4
_CONSISTENCY_WEIGHT = 0.3
_SATURATION_WEIGHT = 0.3


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class AdaptiveState:
    """What one crawl job knows so far. Serializable to a single JSONB column.

    Bounded by construction: every collection here has a cap, so the row does not
    grow with the crawl. That is the property that makes this safe to read and
    rewrite once per completed page across an arbitrary number of workers.
    """

    query: str = ""
    query_terms: list[str] = field(default_factory=list)

    total_documents: int = 0
    query_term_documents: dict[str, int] = field(default_factory=dict)
    """Per query term: how many documents contained it. Adaptation 1 -- only the
    query's terms, since coverage never asks about any other."""
    query_term_occurrences: dict[str, int] = field(default_factory=dict)
    max_term_occurrences: int = 0
    """Running maximum occurrence count over *all* terms, not just the query's.
    Coverage normalizes its frequency signal against the corpus's busiest term, so
    dropping this would make a query term's raw count meaningless."""

    vocabulary: list[str] = field(default_factory=list)
    """Capped at `MAX_VOCABULARY`. A list rather than a set purely so it round-trips
    through JSON; membership tests go through `_vocabulary_index`."""
    new_terms_history: list[int] = field(default_factory=list)

    signatures: list[list[str]] = field(default_factory=list)
    """Adaptation 2: the bounded document sample consistency is measured against."""
    consistency_sum: float = 0.0
    consistency_count: int = 0
    """A running mean, so consistency costs `CONSISTENCY_SAMPLE` comparisons per
    page instead of every pair in the corpus."""

    stopped_reason: str | None = None

    def __post_init__(self) -> None:
        self._vocabulary_index: set[str] | None = None

    # --- vocabulary membership ------------------------------------------------

    @property
    def _vocab(self) -> set[str]:
        index = getattr(self, "_vocabulary_index", None)
        if index is None:
            index = set(self.vocabulary)
            self._vocabulary_index = index
        return index

    def _add_terms(self, terms: set[str]) -> int:
        """Add `terms` to the vocabulary; return how many were new.

        Returns the count of genuinely-new terms even when the cap prevents
        storing them, so the *first* page past the cap reports honestly. Only
        subsequent pages re-seeing those terms are miscounted -- see
        `MAX_VOCABULARY`.
        """

        vocab = self._vocab
        new = terms - vocab
        if len(vocab) < MAX_VOCABULARY:
            room = MAX_VOCABULARY - len(vocab)
            admitted = list(new)[:room]
            vocab.update(admitted)
            self.vocabulary.extend(admitted)
        return len(new)

    # --- ingestion -----------------------------------------------------------

    def observe(self, content: str) -> None:
        """Fold one page's content into the state.

        Called once per successfully-scraped page, by whichever worker scraped it.
        """

        tokens = tokenize(content)
        if not tokens:
            # A page that yielded no text still counts as read -- otherwise a site
            # serving empty shells looks like a crawl making no progress, and the
            # saturation signal never moves.
            self.total_documents += 1
            self.new_terms_history.append(0)
            self._trim_history()
            return

        occurrences: dict[str, int] = {}
        for token in tokens:
            occurrences[token] = occurrences.get(token, 0) + 1

        self.max_term_occurrences = max(
            self.max_term_occurrences, max(occurrences.values())
        )

        for term in self.query_terms:
            count = occurrences.get(term, 0)
            if count:
                self.query_term_documents[term] = self.query_term_documents.get(term, 0) + 1
                self.query_term_occurrences[term] = (
                    self.query_term_occurrences.get(term, 0) + count
                )

        term_set = set(occurrences)
        self.new_terms_history.append(self._add_terms(term_set))
        self._trim_history()

        self._update_consistency(occurrences)
        self.total_documents += 1

    def _trim_history(self) -> None:
        if len(self.new_terms_history) > HISTORY_LENGTH:
            # Keep the head as well as the tail: saturation is a comparison
            # between the crawl's early rate and its recent one, so discarding the
            # beginning would destroy the baseline.
            head = self.new_terms_history[: HISTORY_LENGTH // 2]
            tail = self.new_terms_history[-(HISTORY_LENGTH // 2) :]
            self.new_terms_history = head + tail

    def _update_consistency(self, occurrences: dict[str, int]) -> None:
        signature = [
            term
            for term, _ in sorted(
                occurrences.items(), key=lambda item: (-item[1], item[0])
            )[:SIGNATURE_TERMS]
        ]
        incoming = set(signature)

        for existing in self.signatures:
            other = set(existing)
            union = incoming | other
            if union:
                self.consistency_sum += len(incoming & other) / len(union)
                self.consistency_count += 1

        self.signatures.append(signature)
        if len(self.signatures) > CONSISTENCY_SAMPLE:
            # Drop the oldest. A crawl that has moved on to a different section of
            # a site should be measured against where it is now, not against its
            # first page.
            self.signatures = self.signatures[-CONSISTENCY_SAMPLE:]

    # --- metrics -------------------------------------------------------------

    def coverage(self) -> float:
        """Whether the query's terms appear across the corpus, and how densely.

        Document coverage (what fraction of pages contain the term) with a
        logarithmic frequency boost, averaged over query terms, then square-rooted.
        The square root is upstream's and worth keeping: without it a term present
        in 25% of pages scores 0.25, which reads as failure when it is actually
        reasonable coverage of a specific question.
        """

        if not self.query_terms or self.total_documents == 0:
            return 0.0

        max_occurrences = max(self.max_term_occurrences, 1)
        scores: list[float] = []
        for term in self.query_terms:
            documents = self.query_term_documents.get(term, 0)
            if not documents:
                scores.append(0.0)
                continue
            document_coverage = documents / self.total_documents
            occurrences = self.query_term_occurrences.get(term, 0)
            frequency_signal = math.log(1 + occurrences) / math.log(1 + max_occurrences)
            scores.append(document_coverage * (1 + 0.5 * frequency_signal))

        return min(1.0, math.sqrt(sum(scores) / len(scores)))

    def consistency(self) -> float:
        """Mean pairwise vocabulary overlap across the sampled documents.

        `1.0` for fewer than two documents, matching upstream: one page cannot
        disagree with itself, and treating that as zero consistency would hold
        every crawl below threshold for its first page regardless of content.
        """

        if self.total_documents < 2 or self.consistency_count == 0:
            return 1.0
        return self.consistency_sum / self.consistency_count

    def saturation(self) -> float:
        """How far the new-term discovery rate has fallen from its early level.

        Two deviations from upstream, both found by watching the metric misbehave:

        1. **The first document is excluded from the baseline.** Its new-term count
           is not a discovery *rate* -- it is the whole starting vocabulary, since
           every term on page one is new by definition. Including it makes
           `early_rate` enormous and saturation reads ~0.7 by page four of a crawl
           that is still discovering plenty. Upstream compares the latest count
           against exactly that first one, so it has this effect at full strength.
        2. **Both ends are averaged, and more history is required.** Upstream's
           single-latest-against-single-first makes the whole signal hostage to two
           pages; one unusually long page anywhere near either end distorts it.
        """

        history = self.new_terms_history
        if len(history) < 6:
            # Too early to say anything. Zero rather than a guess: this is a
            # *stopping* signal, and inventing one from a handful of points would
            # end crawls on their second page.
            return 0.0

        baseline = history[1:]
        window = max(2, len(baseline) // 4)
        early_rate = sum(baseline[:window]) / window
        recent_rate = sum(baseline[-window:]) / window
        if early_rate <= 0:
            return 0.0
        return max(0.0, min(1.0, 1 - (recent_rate / early_rate)))

    def confidence(self) -> float:
        return (
            _COVERAGE_WEIGHT * self.coverage()
            + _CONSISTENCY_WEIGHT * self.consistency()
            + _SATURATION_WEIGHT * self.saturation()
        )

    def metrics(self) -> dict[str, float | int]:
        """Every number behind the stop decision, for the crawl's status response.

        Returned in full rather than just the confidence: a caller whose crawl
        stopped at 40 pages needs to see *which* signal crossed, or the behaviour
        is indistinguishable from a bug.
        """

        return {
            "confidence": round(self.confidence(), 4),
            "coverage": round(self.coverage(), 4),
            "consistency": round(self.consistency(), 4),
            "saturation": round(self.saturation(), 4),
            "documents": self.total_documents,
            "vocabulary": len(self.vocabulary),
        }

    # --- serialization -------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "query_terms": self.query_terms,
            "total_documents": self.total_documents,
            "query_term_documents": self.query_term_documents,
            "query_term_occurrences": self.query_term_occurrences,
            "max_term_occurrences": self.max_term_occurrences,
            "vocabulary": self.vocabulary,
            "new_terms_history": self.new_terms_history,
            "signatures": self.signatures,
            "consistency_sum": self.consistency_sum,
            "consistency_count": self.consistency_count,
            "stopped_reason": self.stopped_reason,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AdaptiveState:
        return cls(
            query=data.get("query", ""),
            query_terms=list(data.get("query_terms") or []),
            total_documents=data.get("total_documents", 0),
            query_term_documents=dict(data.get("query_term_documents") or {}),
            query_term_occurrences=dict(data.get("query_term_occurrences") or {}),
            max_term_occurrences=data.get("max_term_occurrences", 0),
            vocabulary=list(data.get("vocabulary") or []),
            new_terms_history=list(data.get("new_terms_history") or []),
            signatures=[list(sig) for sig in data.get("signatures") or []],
            consistency_sum=data.get("consistency_sum", 0.0),
            consistency_count=data.get("consistency_count", 0),
            stopped_reason=data.get("stopped_reason"),
        )

    @classmethod
    def for_query(cls, query: str) -> AdaptiveState:
        return cls(query=query, query_terms=sorted(set(tokenize(query))))


@dataclass(frozen=True)
class StopDecision:
    stop: bool
    reason: str | None = None
    """Which signal crossed, in words, for `jobs.stop_reason`. A crawl that
    stopped early and cannot say why is a crawl nobody will trust the next time."""


def should_stop(
    state: AdaptiveState,
    *,
    confidence_threshold: float,
    saturation_threshold: float = 0.85,
    min_documents: int = 3,
) -> StopDecision:
    """Whether the crawl has learned enough to stop.

    `min_documents` is a floor no threshold can undercut. Every metric is
    degenerate on tiny corpora -- `consistency()` returns 1.0 for a single
    document, which alone is 0.3 of the confidence -- so without a floor a crawl
    with a low threshold would stop on its first page having learned nothing. The
    floor is the difference between an adaptive crawl and a broken one.
    """

    if state.total_documents < min_documents:
        return StopDecision(stop=False)

    confidence = state.confidence()
    if confidence >= confidence_threshold:
        return StopDecision(
            stop=True,
            reason=(
                f"confidence {confidence:.2f} reached threshold "
                f"{confidence_threshold:.2f} after {state.total_documents} pages"
            ),
        )

    saturation = state.saturation()
    if saturation >= saturation_threshold:
        return StopDecision(
            stop=True,
            reason=(
                f"saturated at {saturation:.2f} after {state.total_documents} pages: "
                "new pages are no longer introducing new information"
            ),
        )

    return StopDecision(stop=False)
