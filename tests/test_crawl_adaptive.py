"""`agentpilot.crawl.adaptive` -- the three signals and the stop decision.

Driven by feeding synthetic page content in sequences that stand for the crawl
shapes that matter: a crawl still finding new things, one that has started
repeating itself, and one that has wandered off the caller's question. Those three
are the whole contract, and the tests are written as those scenarios rather than
as unit tests of each metric in isolation, because the metrics are only meaningful
in combination.
"""

from __future__ import annotations

from agentpilot.crawl.adaptive import (
    HISTORY_LENGTH,
    MAX_VOCABULARY,
    AdaptiveState,
    should_stop,
    tokenize,
)

ON_TOPIC = (
    "Refunds are issued within fourteen days of purchase. A refund requires the "
    "original receipt. Refund processing takes three business days."
)


def _novel(page: int, count: int = 60) -> str:
    """Content that introduces `count` terms never seen before."""

    return " ".join(f"novel{page}x{index}" for index in range(count))


def _run(
    page_content: list[str], *, query: str = "refund receipt", threshold: float = 0.7
) -> tuple[AdaptiveState, int | None]:
    """Feed pages until the state says stop. Returns the page number it stopped
    on, or `None` if it never did."""

    state = AdaptiveState.for_query(query)
    for number, content in enumerate(page_content, start=1):
        state.observe(content)
        if should_stop(state, confidence_threshold=threshold).stop:
            return state, number
    return state, None


# ------------------------------------------------------------- crawl shapes


def test_a_crawl_still_discovering_does_not_stop() -> None:
    """Every page brings sixty terms the crawl has never seen. Stopping here would
    be stopping mid-answer."""

    _, stopped = _run([ON_TOPIC + " " + _novel(page) for page in range(1, 25)])
    assert stopped is None


def test_a_crawl_that_starts_repeating_itself_stops() -> None:
    """The case the feature exists for: the site had five pages of new material and
    is now restating them. Further pages cost money and add nothing."""

    pages = [ON_TOPIC + " " + _novel(page) for page in range(1, 6)]
    pages += [ON_TOPIC + " the same words once again"] * 20
    state, stopped = _run(pages)
    assert stopped is not None
    assert 5 < stopped <= 12, f"stopped at {stopped}, expected shortly after page 5"
    assert state.saturation() > 0.5


def test_a_crawl_that_never_sees_the_query_does_not_stop_on_confidence() -> None:
    """Coverage is zero, so confidence cannot reach a 0.7 threshold however many
    pages are read. The crawl ends at `limit` instead, which is correct: it never
    found what was asked for and should not claim otherwise."""

    pages = [f"Careers at our company. Open roles page {page}. " + _novel(page) for page in range(1, 25)]
    state, stopped = _run(pages)
    assert stopped is None
    assert state.coverage() == 0.0


def test_a_saturated_off_topic_crawl_still_stops_on_saturation() -> None:
    """The other exit. A crawl that is learning nothing new should end even when it
    never matched the query -- otherwise an off-topic crawl of a small site burns
    its whole budget re-reading the same pages."""

    pages = ["Careers at our company. Open roles. " + _novel(page) for page in range(1, 4)]
    pages += ["Careers at our company. Open roles."] * 20
    state, stopped = _run(pages, threshold=0.99)
    assert stopped is not None
    assert state.coverage() == 0.0


# ------------------------------------------------------------------ floors


def test_nothing_stops_before_the_minimum_document_count() -> None:
    """Every metric is degenerate on a tiny corpus -- `consistency()` returns 1.0
    for a single document, which is 0.3 of the confidence on its own. Without this
    floor a low threshold would stop on page one having learned nothing."""

    state = AdaptiveState.for_query("refund")
    state.observe(ON_TOPIC)
    state.observe(ON_TOPIC)
    assert not should_stop(state, confidence_threshold=0.1).stop
    assert should_stop(state, confidence_threshold=0.1, min_documents=2).stop


def test_a_stop_decision_always_explains_itself() -> None:
    """A crawl that ends at 40 of a permitted 500 pages and cannot say why is one
    nobody will trust next time."""

    pages = [ON_TOPIC + " " + _novel(page) for page in range(1, 4)]
    pages += [ON_TOPIC] * 20
    state = AdaptiveState.for_query("refund receipt")
    for content in pages:
        state.observe(content)
        decision = should_stop(state, confidence_threshold=0.7)
        if decision.stop:
            assert decision.reason
            assert "pages" in decision.reason
            return
    raise AssertionError("expected this crawl to stop")


# ----------------------------------------------------------------- metrics


def test_coverage_rises_as_query_terms_appear() -> None:
    absent = AdaptiveState.for_query("refund receipt")
    present = AdaptiveState.for_query("refund receipt")
    for _ in range(5):
        absent.observe("Careers and open roles at our company today")
        present.observe(ON_TOPIC)
    assert present.coverage() > absent.coverage()
    assert absent.coverage() == 0.0


def test_consistency_is_higher_for_a_coherent_corpus() -> None:
    coherent = AdaptiveState.for_query("refund")
    wandering = AdaptiveState.for_query("refund")
    for page in range(1, 8):
        coherent.observe(ON_TOPIC + f" page {page}")
        wandering.observe(_novel(page, count=80))
    assert coherent.consistency() > wandering.consistency()


def test_saturation_excludes_the_first_page_from_its_baseline() -> None:
    """The first page's new-term count is not a discovery *rate* -- it is the whole
    starting vocabulary, since every term on it is new by definition. Counting it
    makes the baseline enormous and reports a crawl as saturated while it is still
    finding plenty. crawl4ai compares the latest count against exactly that first
    one, so it has this at full strength.

    Here every page after the first introduces the same number of new terms, so the
    true saturation is zero.
    """

    state = AdaptiveState.for_query("refund")
    for page in range(1, 12):
        state.observe(_novel(page, count=40))
    assert state.saturation() < 0.35


def test_saturation_stays_zero_until_there_is_enough_history() -> None:
    state = AdaptiveState.for_query("refund")
    for page in range(1, 5):
        state.observe(_novel(page))
        assert state.saturation() == 0.0


def test_consistency_of_a_single_document_is_one_not_zero() -> None:
    """One page cannot disagree with itself. Treating that as zero consistency
    would hold every crawl below threshold for its first page regardless of what
    the page said."""

    state = AdaptiveState.for_query("refund")
    state.observe(ON_TOPIC)
    assert state.consistency() == 1.0


def test_an_empty_page_still_counts_as_read() -> None:
    """Otherwise a site serving empty shells looks like a crawl making no progress
    and the saturation signal never moves -- so it would run to `limit` on a site
    that gave it nothing."""

    state = AdaptiveState.for_query("refund")
    state.observe("")
    assert state.total_documents == 1
    assert state.new_terms_history == [0]


def test_metrics_report_every_signal_not_just_the_confidence() -> None:
    state = AdaptiveState.for_query("refund receipt")
    for _ in range(4):
        state.observe(ON_TOPIC)
    metrics = state.metrics()
    assert set(metrics) == {
        "confidence",
        "coverage",
        "consistency",
        "saturation",
        "documents",
        "vocabulary",
    }


# ------------------------------------------------------------ bounded state


def test_the_state_stays_bounded_across_a_long_crawl() -> None:
    """The property that makes this safe to read and rewrite once per page from N
    workers. An unbounded blob would turn a 500-page crawl into hundreds of
    megabytes of transaction I/O."""

    state = AdaptiveState.for_query("refund receipt")
    for page in range(1, 400):
        state.observe(ON_TOPIC + " " + _novel(page, count=50))

    assert len(state.vocabulary) <= MAX_VOCABULARY
    assert len(state.new_terms_history) <= HISTORY_LENGTH
    assert len(state.signatures) <= 16
    assert all(len(signature) <= 256 for signature in state.signatures)


def test_history_trimming_keeps_both_ends() -> None:
    """Saturation is a comparison between the crawl's early rate and its recent
    one, so discarding the beginning of the history would destroy the baseline."""

    state = AdaptiveState.for_query("refund")
    for page in range(1, HISTORY_LENGTH * 3):
        state.observe(_novel(page, count=10))
    assert len(state.new_terms_history) <= HISTORY_LENGTH
    # The early entries are large (new vocabulary); had the head been dropped the
    # baseline would equal the recent rate and saturation would read zero forever.
    assert state.new_terms_history[0] > 0


# ---------------------------------------------------------- serialization


def test_the_state_round_trips_through_json() -> None:
    """It lives in a JSONB column and is reloaded by whichever worker gets the next
    page, so a field that does not survive this is a signal that silently resets."""

    original = AdaptiveState.for_query("refund receipt")
    for page in range(1, 10):
        original.observe(ON_TOPIC + " " + _novel(page))

    restored = AdaptiveState.from_json(original.to_json())

    assert restored.total_documents == original.total_documents
    assert restored.query_terms == original.query_terms
    assert restored.coverage() == original.coverage()
    assert restored.consistency() == original.consistency()
    assert restored.saturation() == original.saturation()
    assert restored.confidence() == original.confidence()


def test_a_restored_state_keeps_accumulating_correctly() -> None:
    """The real usage: worker A observes a page, the state is stored, worker B loads
    it and observes the next one."""

    first = AdaptiveState.for_query("refund receipt")
    first.observe(ON_TOPIC)
    second = AdaptiveState.from_json(first.to_json())
    second.observe(ON_TOPIC + " extra words here")

    assert second.total_documents == 2
    # The restored vocabulary was not lost, so the second page's shared terms are
    # not counted as new.
    assert second.new_terms_history[-1] < len(tokenize(ON_TOPIC))


def test_from_json_tolerates_a_state_written_before_a_field_existed() -> None:
    state = AdaptiveState.from_json({"query": "refund", "query_terms": ["refund"]})
    assert state.total_documents == 0
    assert state.consistency() == 1.0
    assert not should_stop(state, confidence_threshold=0.5).stop


def test_for_query_tokenizes_and_deduplicates() -> None:
    state = AdaptiveState.for_query("Refund REFUND receipt a")
    # Case-folded, deduplicated, and the two-letter token dropped.
    assert state.query_terms == ["receipt", "refund"]
