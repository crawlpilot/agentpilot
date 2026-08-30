"""The verbs ported from browser-use, against a real browser.

The catalog offered 11 agent verbs where browser-use offers 24, and the gap was
exactly the things an agent reaches for when a click will not do: a keyboard
shortcut, content below the observation budget, the real options of a dropdown.
Each test here drives the verb end to end and checks the page actually changed --
a tool that dispatches without effect is worse than a missing one, because the
model believes it worked.
"""

from __future__ import annotations

import pytest
from pytest_httpserver import HTTPServer

from crawlpilot.driver.patchright_driver import PatchrightDriver
from crawlpilot.spi.actions import (
    DropdownOptionsAction,
    ExecuteJsAction,
    FillAction,
    FindElementsAction,
    FindTextAction,
    NavigateAction,
    ScrollAction,
    SearchPageAction,
    SelectOptionAction,
    SendKeysAction,
    SnapshotAction,
    UploadFileAction,
)
from crawlpilot.spi.dom_tree import iter_elements
from crawlpilot.spi.lease import ContextRef

TOOLS_HTML = """<html><body>
<div id="log"></div>
<input type="text" id="field" value="seed" />
<select id="picker">
  <option value="a">Alpha</option>
  <option value="b">Beta</option>
  <option value="c">Gamma</option>
</select>
<ul id="items">
  <li><a href="/one" data-sku="S1">First item</a></li>
  <li><a href="/two" data-sku="S2">Second item</a></li>
  <li><a href="/three" data-sku="S3">Third item</a></li>
</ul>
<input type="file" id="uploader" />
<div style="height: 3000px"></div>
<p id="deep">a needle buried far below the fold</p>
<script>
  document.addEventListener('keydown', (e) => {
    if (e.key === 'k' && (e.ctrlKey || e.metaKey)) {
      document.getElementById('log').textContent = 'shortcut';
    }
  });
</script>
</body></html>"""


async def _open(driver: PatchrightDriver, ctx: ContextRef, httpserver: HTTPServer):
    httpserver.expect_request("/").respond_with_data(TOOLS_HTML, content_type="text/html")
    result = await driver.execute(
        ctx, [NavigateAction(url=httpserver.url_for("/")), SnapshotAction(settle=True)]
    )
    return result.fused_trees[0]


def _ref_by_id(tree, element_id: str) -> str:
    for node in iter_elements(tree):
        if node.attributes.get("id") == element_id:
            return f"e{node.selector_index}"
    raise AssertionError(f"#{element_id} was not captured")


async def _read(driver: PatchrightDriver, ctx: ContextRef, script: str):
    return (await driver.execute(ctx, [ExecuteJsAction(script=script)])).js_returns[0]


async def test_send_keys_delivers_a_modifier_shortcut(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """A shortcut is not reachable by clicking anything, which is why the verb
    exists. `mod` resolves to Cmd or Ctrl for the host the browser runs on."""

    await _open(driver, open_ctx, httpserver)
    await driver.execute(open_ctx, [SendKeysAction(keys="mod+k")])
    assert await _read(driver, open_ctx, "document.getElementById('log').textContent") == "shortcut"


async def test_find_text_scrolls_to_content_below_the_fold(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """Text the serializer truncated out of the observation is still reachable."""

    await _open(driver, open_ctx, httpserver)
    assert await _read(driver, open_ctx, "window.scrollY") == 0

    result = await driver.execute(open_ctx, [FindTextAction(text="a needle buried")])

    assert any("scrolled to" in v for v in result.verifications)
    assert await _read(driver, open_ctx, "window.scrollY") > 0


async def test_find_text_reports_absence_rather_than_failing(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """"Not found" is an answer the model can act on. Raising would burn the
    step and tell it nothing about the page."""

    await _open(driver, open_ctx, httpserver)
    result = await driver.execute(open_ctx, [FindTextAction(text="no such string anywhere")])
    assert any("was not found" in v for v in result.verifications)


async def test_dropdown_options_lists_the_real_choices(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """Answered from the captured tree with no CDP round trip -- the fusion
    capture already walked into the `<select>`."""

    tree = await _open(driver, open_ctx, httpserver)
    result = await driver.execute(
        open_ctx, [DropdownOptionsAction(ref=_ref_by_id(tree, "picker"))]
    )

    readout = result.readouts[0]
    assert "'Alpha'" in readout and "value='a'" in readout
    assert "'Gamma'" in readout


async def test_select_option_accepts_visible_text_not_just_value(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """A model reads the label, not the markup."""

    tree = await _open(driver, open_ctx, httpserver)
    await driver.execute(
        open_ctx, [SelectOptionAction(ref=_ref_by_id(tree, "picker"), values=["Beta"])]
    )
    assert await _read(driver, open_ctx, "document.getElementById('picker').value") == "b"


async def test_select_option_reports_the_available_options_on_a_bad_value(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """The failure has to be correctable: telling the model *what it could have
    picked* turns a wasted step into a right answer next step."""

    tree = await _open(driver, open_ctx, httpserver)
    result = await driver.execute(
        open_ctx, [SelectOptionAction(ref=_ref_by_id(tree, "picker"), values=["Delta"])]
    )

    message = " ".join(result.verifications)
    assert "no option matching" in message
    assert "Alpha" in message and "Gamma" in message
    # And nothing was selected.
    assert await _read(driver, open_ctx, "document.getElementById('picker').value") == "a"


async def test_search_page_finds_text_with_context(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    await _open(driver, open_ctx, httpserver)
    result = await driver.execute(open_ctx, [SearchPageAction(pattern="needle buried")])

    readout = result.readouts[0]
    assert "1 match(es)" in readout
    assert "needle buried" in readout


async def test_search_page_treats_a_literal_pattern_literally(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """Regex metacharacters in a plain search must not be interpreted, or a
    model searching for "price (USD)" gets a syntax error instead of a result."""

    await _open(driver, open_ctx, httpserver)
    result = await driver.execute(open_ctx, [SearchPageAction(pattern="needle (buried)")])
    assert "no match" in result.readouts[0]


async def test_find_elements_reads_repeated_structure(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """Bulk extraction of a list without spending a whole observation on it."""

    await _open(driver, open_ctx, httpserver)
    result = await driver.execute(
        open_ctx,
        [FindElementsAction(selector="#items a", attributes=["href", "data-sku"])],
    )

    readout = result.readouts[0]
    assert "3 match(es)" in readout
    assert "First item" in readout and "Third item" in readout
    assert "S2" in readout
    # `href` is read off the property, so it is the absolute URL a caller can use.
    assert "http://" in readout


async def test_find_elements_reports_a_selector_that_matches_nothing(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    await _open(driver, open_ctx, httpserver)
    result = await driver.execute(open_ctx, [FindElementsAction(selector=".absent")])
    assert "nothing matched" in result.readouts[0]


async def test_scroll_pages_scales_with_the_viewport(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """`pages` is what lets one action cover a long list. A half page must move
    less far than two pages, and both in the same direction."""

    await _open(driver, open_ctx, httpserver)
    await driver.execute(open_ctx, [ScrollAction(direction="down", pages=0.5)])
    half = await _read(driver, open_ctx, "window.scrollY")

    await driver.execute(open_ctx, [ExecuteJsAction(script="window.scrollTo(0, 0)")])
    await driver.execute(open_ctx, [ScrollAction(direction="down", pages=2.0)])
    double = await _read(driver, open_ctx, "window.scrollY")

    assert 0 < half < double


async def test_fill_clear_false_appends_instead_of_replacing(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    tree = await _open(driver, open_ctx, httpserver)
    ref = _ref_by_id(tree, "field")

    await driver.execute(open_ctx, [FillAction(ref=ref, text="-added", clear=False)])
    assert await _read(driver, open_ctx, "document.getElementById('field').value") == "seed-added"


async def test_fill_clears_by_default(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    tree = await _open(driver, open_ctx, httpserver)
    ref = _ref_by_id(tree, "field")

    await driver.execute(open_ctx, [FillAction(ref=ref, text="replaced")])
    assert await _read(driver, open_ctx, "document.getElementById('field').value") == "replaced"


async def test_upload_file_attaches_without_a_file_chooser(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer, tmp_path
) -> None:
    """One `DOM.setFileInputFiles` call and no click: the native chooser is an OS
    dialog nothing in the page can drive."""

    payload = tmp_path / "evidence.txt"
    payload.write_text("hello")

    tree = await _open(driver, open_ctx, httpserver)
    await driver.execute(
        open_ctx,
        [UploadFileAction(ref=_ref_by_id(tree, "uploader"), path=str(payload))],
    )

    assert await _read(
        driver, open_ctx, "document.getElementById('uploader').files[0].name"
    ) == "evidence.txt"


@pytest.mark.parametrize("keys", ["Escape", "Shift+Tab", "ctrl+a", "PageDown"])
async def test_send_keys_accepts_the_spellings_models_emit(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer, keys: str
) -> None:
    """A shortcut rejected on spelling wastes a step for something whose intent
    was never ambiguous, so the aliases browser-use accepts are accepted here."""

    await _open(driver, open_ctx, httpserver)
    await driver.execute(open_ctx, [SendKeysAction(keys=keys)])  # must not raise
