"""`crawlpilot.extraction.virtual_scroll` -- the snapshot merge, and the scripts
the driver evaluates.

The merge is the part worth testing hard. Driving a recycling container is a loop;
splicing its overlapping snapshots back into one document without losing or
duplicating rows is where the bugs are, and it is testable with a list of strings.
"""

from __future__ import annotations

from crawlpilot.extraction.virtual_scroll import (
    VirtualScrollConfig,
    build_container_html_script,
    build_scroll_script,
    merge_snapshots,
    scroll_delta_expression,
)

CONFIG = VirtualScrollConfig(container_selector="#feed")


# ---------------------------------------------------------------- the merge


def test_snapshots_overlapping_partially_are_spliced_at_the_seam() -> None:
    """The ordinary case. A scroll of 60% of the container leaves 40% of the rows
    on screen, so consecutive snapshots share a tail/head region."""

    merged = merge_snapshots(
        [
            "row 1\nrow 2\nrow 3\nrow 4",
            "row 3\nrow 4\nrow 5\nrow 6",
            "row 5\nrow 6\nrow 7\nrow 8",
        ]
    )
    assert merged.splitlines() == [f"row {n}" for n in range(1, 9)]


def test_a_repeated_row_survives_the_merge() -> None:
    """The property a set-union approach loses. A feed legitimately contains two
    posts with identical text, and a product grid repeats the same price many
    times; only the *overlap* between adjacent snapshots may be dropped."""

    merged = merge_snapshots(["a\nLoad more\nb", "b\nc\nLoad more"])
    assert merged.splitlines() == ["a", "Load more", "b", "c", "Load more"]


def test_snapshots_with_no_overlap_are_concatenated() -> None:
    """A scroll far enough to recycle the whole container. Nothing is shared, so
    nothing is dropped."""

    merged = merge_snapshots(["row 1\nrow 2", "row 9\nrow 10"])
    assert merged.splitlines() == ["row 1", "row 2", "row 9", "row 10"]


def test_an_unchanged_snapshot_adds_nothing() -> None:
    """What a container at its bottom produces: the same rows again."""

    merged = merge_snapshots(["row 1\nrow 2\nrow 3"] * 4)
    assert merged.splitlines() == ["row 1", "row 2", "row 3"]


def test_a_snapshot_wholly_contained_in_what_is_kept_adds_nothing() -> None:
    merged = merge_snapshots(["a\nb\nc\nd", "c\nd"])
    assert merged.splitlines() == ["a", "b", "c", "d"]


def test_the_longest_overlap_wins_over_a_coincidental_short_one() -> None:
    """Splicing at the first one-line match would duplicate rows. `x` appears both
    at the true seam and earlier, and the seam is the longer match."""

    merged = merge_snapshots(["x\np\nq\nx\ny", "x\ny\nz"])
    assert merged.splitlines() == ["x", "p", "q", "x", "y", "z"]


def test_blank_lines_and_whitespace_are_normalized() -> None:
    merged = merge_snapshots(["  row 1  \n\n\n  row 2 ", "row 2\nrow 3"])
    assert merged.splitlines() == ["row 1", "row 2", "row 3"]


def test_empty_snapshots_are_skipped() -> None:
    """A snapshot caught mid-recycle -- old rows gone, new ones unpainted -- is
    empty. Treating it as the end of the feed would truncate the capture."""

    merged = merge_snapshots(["row 1", "", "   \n  ", "row 1\nrow 2"])
    assert merged.splitlines() == ["row 1", "row 2"]


def test_no_snapshots_at_all() -> None:
    assert merge_snapshots([]) == ""


def test_a_single_snapshot_passes_through() -> None:
    assert merge_snapshots(["row 1\nrow 2"]).splitlines() == ["row 1", "row 2"]


def test_a_long_run_of_overlapping_snapshots_reconstructs_the_whole_feed() -> None:
    """The end-to-end shape of a real capture: 40 rows seen through a 5-row window
    advancing 3 rows at a time."""

    rows = [f"row {n}" for n in range(40)]
    snapshots = ["\n".join(rows[start : start + 5]) for start in range(0, 38, 3)]
    assert merge_snapshots(snapshots).splitlines() == rows


# --------------------------------------------------------------- the scripts


def test_the_delta_expression_reads_the_container_each_step() -> None:
    """Re-read in the page rather than computed once in Python, which is what makes
    this work on a container that grows as content loads."""

    assert scroll_delta_expression(CONFIG) == "el.clientHeight"


def test_the_delta_expression_honours_page_height_and_pixels() -> None:
    page = VirtualScrollConfig(container_selector="#f", scroll_by="page_height")
    assert scroll_delta_expression(page) == "window.innerHeight"
    pixels = VirtualScrollConfig(container_selector="#f", scroll_by=750)
    assert scroll_delta_expression(pixels) == "750"


def test_the_scroll_script_reports_whether_the_container_moved() -> None:
    """The termination signal: a container already at its bottom reports no
    movement, so a capture stops instead of spending its remaining budget."""

    script = build_scroll_script(CONFIG)
    assert "moved" in script
    assert "el.scrollTop > before" in script


def test_the_scroll_script_distinguishes_a_missing_container() -> None:
    """Scrolling a selector that matches nothing silently does nothing, so `found`
    is what turns a mistyped selector into a diagnosable result."""

    assert "found: false" in build_scroll_script(CONFIG)


def test_the_selector_is_embedded_as_a_valid_js_string_literal() -> None:
    """The selector is caller-supplied, so it must not be able to terminate the
    string it sits in. Asserted by parsing the literal back out rather than by
    matching escape sequences, which only tests the test author's escaping."""

    import json
    import re

    for selector in (
        "#feed",
        "div[data-x='quoted']",
        'div[data-x="double"]',
        "a\\b",
        "');alert(1);//",
    ):
        script = build_scroll_script(VirtualScrollConfig(container_selector=selector))
        # Matches the JSON string literal specifically (quote to quote), not
        # "anything up to the first `);`" -- one of the selectors below *contains*
        # `);`, which is the whole point of testing it.
        literal = re.search(r'querySelector\((".*?")\);', script, re.DOTALL)
        assert literal is not None
        # Round-trips through JSON, which is a subset of JS string syntax -- so if
        # this parses back to the original, the page will read it the same way.
        assert json.loads(literal.group(1)) == selector
        # And the injection attempt did not open a second call.
        assert script.count("querySelector") == 1
