"""The tool chain against a real browser, through the public facade only.

Two things are under test at once, deliberately. The obvious one is that each
verb does what it says on a page built to be awkward -- duplicate ids, shadow
DOM, an occluding overlay, a same-origin and a cross-origin iframe. The other is
that `crawlpilot.api` is sufficient: nothing here imports `crawlpilot.driver` or
`crawlpilot.session`, so if a test needs one of those to do something ordinary,
the client-library surface has a hole.

Every assertion checks what the *page* observed (`#log`, a field's value, the
URL) rather than that a call returned without raising. An action that dispatches
and has no effect is the failure mode that matters, and it looks exactly like
success from the caller's side.

    uv run pytest packages/crawlpilot/tests/browser -m browser -v
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


# --------------------------------------------------------------------- helpers


async def _open(page, toolbench, name: str = "index.html") -> None:
    """Navigate to a toolbench page and settle it before anything is measured.

    The scroll position is normalised because the page does not reliably load at
    the top: its late-arriving frames shift layout, and the position observed
    right after `navigate` has been anywhere from 0 to ~1000px across runs. That
    matters because coordinates are measured from one layout and the input event
    lands on another, so a click or a wheel that is correct in principle hits
    whatever slid into its place. An agent re-observes every step and so never
    sees this; a test that navigates once and then acts does.
    """

    await page.navigate(toolbench.url(name))
    # Settle first, *then* normalise: scrolling to the top before the frames
    # have loaded just gets undone by the layout shift they cause.
    await page.snapshot()
    await page.execute_js("window.scrollTo(0, 0)")


async def _log(page) -> str:
    """What the page last recorded happening to it."""

    return await page.execute_js("document.getElementById('log').textContent")


# Two different questions, kept apart on purpose.
#
# *Addressable* is what the driver can act on: every element the capture walked
# over, which is what a ref resolves against. *Offered* is the narrower set the
# serializer decides to show a model -- occluded elements dropped, nested
# duplicates deduped. Conflating them makes a test either miss a real
# regression (asserting a hidden-but-usable element is "offered") or assert a
# guarantee that was never made.


async def _refs(page) -> dict[str, str]:
    """`{element id: ref}` for everything the driver can address."""

    from crawlpilot.spi.dom_tree import iter_elements

    tree = await page.snapshot()
    assert tree is not None, "snapshot returned no tree"
    found: dict[str, str] = {}
    for node in iter_elements(tree):
        element_id = node.attributes.get("id")
        # First wins, in document order: `dup` appears twice and the pair is
        # tested separately through `_all_refs`.
        if element_id and element_id not in found:
            found[element_id] = f"e{node.selector_index}"
    return found


async def _all_refs(page, element_id: str) -> list[str]:
    """Every addressable ref carrying `element_id`, in document order."""

    from crawlpilot.spi.dom_tree import iter_elements

    tree = await page.snapshot()
    assert tree is not None
    return [
        f"e{n.selector_index}"
        for n in iter_elements(tree)
        if n.attributes.get("id") == element_id
    ]


async def _tabs_settle(page, expected: int, *, timeout: float = 5.0):
    """Wait for the tab list to reach `expected`.

    A tab opened by the page appears asynchronously: the click returns as soon
    as it is dispatched, and Chrome registers the new target a moment later.
    Reading the list immediately is a race the test would lose most of the time
    and win occasionally, which is worse than failing outright.
    """

    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    tabs = await page.list_tabs()
    while len(tabs) != expected and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
        tabs = await page.list_tabs()
    return tabs


async def _offered(page) -> set[str]:
    """The element ids a model would actually be shown.

    The serializer's `selector_map`, which is the same pipeline the agent's
    observation is built from -- so it reflects the editorial stages (paint-order
    occlusion, containment dedup) that the raw tree does not.
    """

    from crawlpilot.dom.serializer import serialize

    tree = await page.snapshot()
    assert tree is not None
    return {
        node.attributes["id"]
        for node in serialize(tree).selector_map.values()
        if node.attributes.get("id")
    }


# ------------------------------------------------------------------ navigation


async def test_navigate_and_read_the_page(page, toolbench) -> None:
    await _open(page, toolbench)
    assert "Toolbench" in await page.text()


async def test_click_a_link_then_go_back(page, toolbench) -> None:
    await _open(page, toolbench)
    refs = await _refs(page)

    await page.click(refs["same-tab"])
    assert "via=link" in await page.execute_js("location.href")

    await page.go_back()
    assert "index.html" in await page.execute_js("location.href")


async def test_submitting_a_form_navigates_and_carries_the_field(page, toolbench) -> None:
    await _open(page, toolbench)
    refs = await _refs(page)

    await page.fill(refs["query"], "hello")
    await page.click(refs["submit"])

    assert "q=hello" in await page.execute_js("location.href")
    assert "Target page" in await page.execute_js("document.title")


async def test_a_target_blank_link_opens_a_tab_the_agent_can_reach(page, toolbench) -> None:
    """A link that opens a new tab is ordinary, and an agent that cannot follow
    it is stuck -- which is why tab management is agent-exposed."""

    await _open(page, toolbench)
    refs = await _refs(page)
    before = len(await page.list_tabs())

    await page.click(refs["new-tab"])

    tabs = await _tabs_settle(page, before + 1)
    assert len(tabs) == before + 1, [t.url for t in tabs]
    opened = next(t for t in tabs if "via=blank" in t.url)

    await page.switch_tab(opened.page_id)
    assert "via=blank" in await page.execute_js("location.href")

    await page.close_tab(opened.page_id)
    assert len(await _tabs_settle(page, before)) == before


# ---------------------------------------------------------------------- refs


async def test_every_interactive_element_is_indexed(page, toolbench) -> None:
    await _open(page, toolbench)
    refs = await _refs(page)

    for element_id in ("dup", "picker", "seeded", "submit", "uploader", "same-tab"):
        assert element_id in refs, f"#{element_id} was not indexed"

    # ...and the ones a model needs are also the ones it is shown.
    offered = await _offered(page)
    for element_id in ("dup", "picker", "seeded", "submit", "same-tab"):
        assert element_id in offered, f"#{element_id} was not offered"


async def test_duplicate_ids_are_individually_addressable(page, toolbench) -> None:
    """The case that broke the old selector cascade: `[id="dup"]` matches two
    elements, so a resolver requiring a unique match could address neither."""

    await _open(page, toolbench)
    first, second = await _all_refs(page, "dup")

    await page.click(second)
    assert await _log(page) == "dup-second"

    await page.click(first)
    assert await _log(page) == "dup-first"


async def test_an_occluded_button_is_not_offered(page, toolbench) -> None:
    """A button underneath an opaque overlay cannot be clicked by a person, so
    offering its ref to a model only invites a step that appears to work and
    changes nothing."""

    await _open(page, toolbench)

    assert "covered-button" not in await _offered(page), (
        "a button under an opaque overlay was offered to the model"
    )
    assert await _log(page) == "idle"


async def test_a_zero_size_element_still_resolves(page, toolbench) -> None:
    """A visually hidden checkbox is not a stale ref. The old visibility gate
    rejected exactly the elements real pages hide on purpose."""

    await _open(page, toolbench)
    refs = await _refs(page)
    assert "hidden-box" in refs

    await page.click(refs["hidden-box"])
    assert await page.execute_js("document.getElementById('hidden-box').checked") is True


async def test_a_ref_from_a_superseded_snapshot_is_rejected(page, toolbench) -> None:
    """Refs are scoped to the capture that minted them. Reusing one after a
    navigation must fail loudly rather than resolve against whatever now happens
    to carry that id."""

    from crawlpilot.spi.errors import StaleRefError

    await _open(page, toolbench)
    stale = (await _refs(page))["picker"]

    await page.navigate(toolbench.url("article.html"))
    with pytest.raises(StaleRefError):
        await page.click(stale)


# ----------------------------------------------------------------- shadow DOM


async def test_click_and_fill_inside_an_open_shadow_root(page, toolbench) -> None:
    await _open(page, toolbench)
    refs = await _refs(page)
    assert "shadow-button" in refs, "shadow content was not captured"

    await page.click(refs["shadow-button"])
    assert await _log(page) == "shadow-clicked"

    await page.fill(refs["shadow-field"], "shadow text")
    assert (
        await page.execute_js(
            "document.getElementById('shadow-host').shadowRoot"
            ".getElementById('shadow-field').value"
        )
        == "shadow text"
    )


# --------------------------------------------------------------------- frames


async def test_interact_inside_a_same_origin_iframe(page, toolbench) -> None:
    await _open(page, toolbench)
    refs = await _refs(page)
    assert "frame-button" in refs, "same-origin frame content was not captured"

    await page.click(refs["frame-button"])
    assert (
        await page.execute_js(
            "document.getElementById('same-origin').contentDocument.title"
        )
        == "frame-clicked"
    )


@pytest.mark.xfail(
    reason=(
        "The capability is covered and passing in "
        "tests/driver_contract/test_element_resolution.py "
        "(test_fill_inside_a_cross_origin_iframe, "
        "test_cross_origin_frame_nodes_carry_their_own_session). What fails here "
        "is the fixture: this page's sibling-origin iframe does not load at all "
        "-- the frame ends up on chrome-error://chromewebdata/, so the per-target "
        "capture faithfully captures an error page. Reproduced against both a "
        "stdlib http.server and pytest-httpserver, and with the src as a static "
        "attribute rather than JS-assigned, so it is a property of this page "
        "rather than of the capture. Left in place, and failing, rather than "
        "deleted: the toolbench should exercise this."
    ),
    strict=False,
)
async def test_interact_inside_a_cross_origin_iframe(page, toolbench) -> None:
    """The class of element that was previously invisible entirely: a separate
    renderer, whose document never appears in the host's DOM."""

    await _open(page, toolbench)

    # Both frames expose `frame-field`; the second belongs to the cross-origin
    # one, whose document never appears in the host's DOM at all.
    fields = await _all_refs(page, "frame-field")
    assert len(fields) == 2, f"expected both frames' fields, got {len(fields)}"

    await page.fill(fields[1], "across origins")

    # Read back through the capture, not JS: the host document cannot reach into
    # a cross-origin contentDocument, which is exactly why this needed the
    # per-target capture in the first place.
    from crawlpilot.spi.dom_tree import iter_elements

    tree = await page.snapshot()
    assert tree is not None
    typed = [
        n
        for n in iter_elements(tree)
        if n.attributes.get("id") == "frame-field" and n.session_id is not None
    ]
    assert typed, "no node carried a cross-origin frame session"


# --------------------------------------------------------------------- select


async def test_dropdown_options_lists_the_real_choices(page, toolbench) -> None:
    await _open(page, toolbench)
    refs = await _refs(page)

    options = await page.dropdown_options(refs["picker"])
    assert "Alpha" in options and "Gamma" in options
    assert "value='a'" in options


@pytest.mark.parametrize("chosen", ["b", "Beta"])
async def test_select_option_accepts_value_or_visible_text(page, toolbench, chosen) -> None:
    """A model reads the label, not the markup."""

    await _open(page, toolbench)
    refs = await _refs(page)

    await page.select_option(refs["picker"], chosen)
    assert await page.execute_js("document.getElementById('picker').value") == "b"
    assert await _log(page) == "picked:b"


async def test_select_option_reports_the_choices_on_a_bad_value(page, toolbench) -> None:
    await _open(page, toolbench)
    refs = await _refs(page)

    result = await page.select_option(refs["picker"], "Delta")

    message = " ".join(result.verifications)
    assert "no option matching" in message
    assert "Alpha" in message
    assert await page.execute_js("document.getElementById('picker').value") == "a"


async def test_an_aria_listbox_is_driven_by_clicking(page, toolbench) -> None:
    """Not every dropdown is a `<select>`. A custom listbox has no options to
    select, only divs to click -- so it must be indexed as clickable."""

    await _open(page, toolbench)
    tree = await page.snapshot()
    assert tree is not None

    from crawlpilot.spi.dom_tree import iter_elements

    options = [n for n in iter_elements(tree) if n.attributes.get("role") == "option"]
    assert len(options) == 2

    await page.click(f"e{options[1].selector_index}")
    assert await _log(page) == "aria:pear"


# -------------------------------------------------------------------- content


async def test_markdown_keeps_the_article_and_drops_the_chrome(page, toolbench) -> None:
    await _open(page, toolbench, "article.html")
    markdown = await page.markdown()

    assert "Measuring a browser automation stack" in markdown
    assert "first paragraph of genuine body content" in markdown
    assert "second paragraph continues" in markdown
    # Structure survives the conversion, not just the prose.
    assert "unordered list item" in markdown

    # ...and the boilerplate around it does not.
    assert "Subscribe to our newsletter" not in markdown
    assert "All rights reserved" not in markdown


async def test_text_and_html_agree_with_markdown(page, toolbench) -> None:
    await _open(page, toolbench, "article.html")

    assert "first paragraph of genuine body content" in await page.text()
    # `html()` is the raw document, so it keeps what main-content extraction drops.
    raw = await page.html()
    assert "Subscribe to our newsletter" in raw


async def test_screenshot_returns_a_png(page, toolbench) -> None:
    await _open(page, toolbench)
    assert (await page.screenshot()).startswith(b"\x89PNG")


# ---------------------------------------------------------------------- tools


async def test_send_keys_delivers_a_shortcut(page, toolbench) -> None:
    await _open(page, toolbench)
    await page.send_keys("mod+k")
    assert await _log(page) == "shortcut"


@pytest.mark.parametrize("keys", ["Escape", "Shift+Tab", "ctrl+a", "PageDown"])
async def test_send_keys_accepts_the_spellings_models_emit(page, toolbench, keys: str) -> None:
    """A shortcut rejected on spelling wastes a step for something whose intent
    was never ambiguous, so the aliases browser-use accepts are accepted here."""

    await _open(page, toolbench)
    await page.send_keys(keys)  # must not raise


async def test_find_text_reaches_content_below_the_fold(page, toolbench) -> None:
    await _open(page, toolbench)
    assert await page.execute_js("window.scrollY") == 0

    await page.find_text("a needle buried")
    assert await page.execute_js("window.scrollY") > 0


async def test_find_text_reports_absence_rather_than_failing(page, toolbench) -> None:
    """"Not found" is an answer the model can act on. Raising would burn the
    step and tell it nothing about the page."""

    await _open(page, toolbench)
    result = await page.find_text("no such string anywhere")
    assert any("was not found" in v for v in result.verifications)


async def test_search_page_finds_text_without_an_observation(page, toolbench) -> None:
    await _open(page, toolbench)
    readout = await page.search_page("needle buried")

    assert "1 match(es)" in readout
    assert "needle buried" in readout


async def test_search_page_treats_a_literal_pattern_literally(page, toolbench) -> None:
    """Regex metacharacters in a plain search must not be interpreted, or a
    search for "price (USD)" returns a syntax error instead of a result."""

    await _open(page, toolbench)
    assert "no match" in await page.search_page("needle (buried)")


async def test_find_elements_reads_repeated_structure(page, toolbench) -> None:
    await _open(page, toolbench)
    readout = await page.find_elements("#scrollbox li", attributes=["id"])

    assert "8 match(es)" in readout
    assert "Row 1" in readout and "Row 8" in readout


async def test_find_elements_reports_a_selector_that_matches_nothing(page, toolbench) -> None:
    await _open(page, toolbench)
    assert "nothing matched" in await page.find_elements(".absent")


async def test_scroll_pages_scales_with_the_viewport(page, toolbench) -> None:
    """`pages` is what lets one action cover a long list instead of one action
    per screenful."""

    await _open(page, toolbench)

    await page.scroll("down", pages=0.5)
    half = await page.execute_js("window.scrollY")

    await page.execute_js("window.scrollTo(0, 0)")
    await page.scroll("down", pages=2.0)

    assert 0 < half < await page.execute_js("window.scrollY")


async def test_scroll_moves_an_elements_own_overflow(page, toolbench) -> None:
    """A dropdown list, a virtualised table: scrolling the page does not move
    them, which is why `scroll` takes a `ref` at all."""

    await _open(page, toolbench)
    refs = await _refs(page)

    before = await page.execute_js("window.scrollY")
    await page.scroll("down", ref=refs["scrollbox"])

    assert await page.execute_js("document.getElementById('scrollbox').scrollTop") > 0
    # ...and the page itself did not move instead of it.
    assert await page.execute_js("window.scrollY") == before


async def test_fill_replaces_by_default_and_appends_on_request(page, toolbench) -> None:
    await _open(page, toolbench)
    refs = await _refs(page)

    await page.fill(refs["seeded"], "-added", clear=False)
    assert await page.execute_js("document.getElementById('seeded').value") == "seed-added"

    await page.fill(refs["seeded"], "replaced")
    assert await page.execute_js("document.getElementById('seeded').value") == "replaced"


async def test_upload_file_attaches_without_a_chooser(page, toolbench, tmp_path) -> None:
    payload = tmp_path / "evidence.txt"
    payload.write_text("hello")

    await _open(page, toolbench)
    refs = await _refs(page)
    await page.upload_file(refs["uploader"], payload)

    assert (
        await page.execute_js("document.getElementById('uploader').files[0].name")
        == "evidence.txt"
    )


# ------------------------------------------------------------------ call_tool


async def test_call_tool_drives_the_same_actions_by_name(page, toolbench) -> None:
    """The other half of the tool registry.

    `browser_tools()` and the provider adapters hand a caller tool *definitions*;
    this proves a call built from one of those schemas round-trips to a real
    effect, which is what makes the adapters usable by anyone who is not
    writing the dispatch loop themselves.
    """

    await page.call_tool("navigate", {"url": toolbench.index})
    assert "Toolbench" in await page.text()

    refs = await _refs(page)
    await page.call_tool("click", {"ref": refs["dup"]})
    assert await _log(page) == "dup-first"

    await page.call_tool("fill", {"ref": refs["empty"], "text": "by name"})
    assert await page.execute_js("document.getElementById('empty').value") == "by name"


async def test_call_tool_rejects_an_unknown_tool_and_a_wire_only_one(page) -> None:
    with pytest.raises(ValueError, match="no such tool"):
        await page.call_tool("teleport", {})

    # `upload_file` is deliberately not agent-callable: `path` names a file on
    # the machine the driver runs on.
    with pytest.raises(ValueError, match="wire-only"):
        await page.call_tool("upload_file", {"ref": "e1", "path": "/etc/passwd"})


async def test_call_tool_validates_its_arguments(page) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await page.call_tool("navigate", {"url": "file:///etc/passwd"})


# ----------------------------------------------------------- snapshot filters
#
# `viewport_only`, `max_nodes` and `roles` have been on the HTTP boundary since
# P1 and were read by nobody: the driver returns a tree and the caller
# serializes it, so the options had no route from the one to the other. These
# assert the route exists, using the driver's own `snapshot_views` exactly as
# the gateway does.


async def _offered(page, **options) -> set[int]:
    """The refs a snapshot with these options offers a model."""

    from crawlpilot.dom.serializer import serialize
    from crawlpilot.spi import actions as sa

    result = await page.execute([sa.SnapshotAction(**options)])
    assert result.fused_trees, "snapshot returned no tree"
    view = result.snapshot_views[0] if result.snapshot_views else None
    return set(serialize(result.fused_trees[0], view=view).selector_map)


async def test_an_unfiltered_snapshot_offers_everything(page, toolbench) -> None:
    await _open(page, toolbench)
    assert len(await _offered(page)) > 10


async def test_max_nodes_caps_what_the_snapshot_offers(page, toolbench) -> None:
    await _open(page, toolbench)
    assert len(await _offered(page, max_nodes=3)) == 3


async def test_roles_filters_the_snapshot_to_one_role(page, toolbench) -> None:
    await _open(page, toolbench)
    offered = await _offered(page, roles=("textbox",))

    assert offered, "the toolbench has text inputs"
    assert offered < await _offered(page), "a role filter must narrow the set"


async def test_viewport_only_drops_what_is_below_the_fold(page, toolbench) -> None:
    """The toolbench has a tall spacer and `#deep` far beneath it, which is the
    whole reason that spacer exists."""

    await _open(page, toolbench)
    assert await _offered(page, viewport_only=True) < await _offered(page)


async def test_selector_scopes_the_snapshot_to_one_subtree(page, toolbench) -> None:
    await _open(page, toolbench)
    scoped = await _offered(page, selector="#dialogs")

    assert scoped, "#dialogs contains four buttons"
    assert scoped < await _offered(page)


async def test_a_selector_that_matches_nothing_says_so(page, toolbench) -> None:
    """Rather than scoping to the empty set, which would look like an empty
    page and send the caller hunting for the wrong bug."""

    from crawlpilot.spi.errors import SelectorNotFound

    await _open(page, toolbench)
    with pytest.raises(SelectorNotFound):
        await _offered(page, selector="#no-such-element")
