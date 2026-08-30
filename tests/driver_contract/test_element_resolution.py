"""The failure classes the selector cascade could not handle.

Each test here fails against the previous `driver/ref_cache.py` -- which tried to
re-derive a CSS/XPath selector for an already-captured element and required it to
match exactly one live node -- and passes now that a ref resolves to its captured
node and is acted on over CDP by `(session_id, backend_node_id)`.

Real Patchright contexts against `pytest-httpserver` inline HTML; never a mocked
browser, never an external site (browser-use discipline).
"""

from __future__ import annotations

import pytest
from pytest_httpserver import HTTPServer

from crawlpilot.driver.patchright_driver import PatchrightDriver
from crawlpilot.spi.actions import (
    ClickAction,
    ExecuteJsAction,
    FillAction,
    NavigateAction,
    SnapshotAction,
)
from crawlpilot.spi.dom_tree import iter_elements
from crawlpilot.spi.lease import ContextRef


def _ref(node) -> str:
    return f"e{node.selector_index}"


def _find(tree, predicate):
    return [node for node in iter_elements(tree) if predicate(node)]


async def _snapshot(driver: PatchrightDriver, ctx: ContextRef, url: str):
    # `settle=True` is what the agent loop uses, and it is load-bearing for the
    # frame tests: `NavigateAction` waits only for `domcontentloaded`, which does
    # not wait for subframes, so without it the iframe has not fetched its
    # document yet and there is nothing in it to capture.
    result = await driver.execute(ctx, [NavigateAction(url=url), SnapshotAction(settle=True)])
    return result.fused_trees[0]


async def _read(driver: PatchrightDriver, ctx: ContextRef, script: str):
    result = await driver.execute(ctx, [ExecuteJsAction(script=script)])
    return result.js_returns[0]


# --------------------------------------------------------------- duplicate ids

DUPLICATE_ID_HTML = """<html><body>
<div id="log"></div>
<button id="dup" onclick="document.getElementById('log').textContent='first'">First</button>
<button id="dup" onclick="document.getElementById('log').textContent='second'">Second</button>
</body></html>"""


async def test_click_targets_the_right_one_of_two_elements_sharing_an_id(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """Duplicate ids are invalid HTML and completely ordinary in the wild.

    The cascade asked `page.locator('[id="dup"]')`, saw `count() == 2`, discarded
    the candidate as ambiguous, exhausted every remaining tier the same way and
    raised `StaleRefError` on an element it had captured perfectly well. Both
    buttons are now individually clickable.
    """

    httpserver.expect_request("/").respond_with_data(
        DUPLICATE_ID_HTML, content_type="text/html"
    )
    tree = await _snapshot(driver, open_ctx, httpserver.url_for("/"))

    buttons = _find(tree, lambda n: n.tag_name == "button")
    assert len(buttons) == 2

    await driver.execute(open_ctx, [ClickAction(ref=_ref(buttons[1]))])
    assert await _read(driver, open_ctx, "document.getElementById('log').textContent") == "second"

    await driver.execute(open_ctx, [ClickAction(ref=_ref(buttons[0]))])
    assert await _read(driver, open_ctx, "document.getElementById('log').textContent") == "first"


# ------------------------------------------------------------------- iframes

IFRAME_INNER_HTML = """<html><body>
<button id="inner" onclick="document.title='inner-clicked'">Inner button</button>
<input type="text" id="inner-field" />
</body></html>"""


def _iframe_host_html(src: str) -> str:
    return f"""<html><body>
<h1>Host document</h1>
<iframe src="{src}" width="400" height="200"></iframe>
</body></html>"""


async def test_click_inside_a_same_origin_iframe(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """A same-origin frame's document is inlined by `pierce:true`, so the cascade
    happily *indexed* the button and then sent every candidate to
    `page.locator()`, which only ever searches the main frame. The ref could not
    resolve -- or, worse, matched an unrelated main-frame lookalike."""

    httpserver.expect_request("/inner").respond_with_data(
        IFRAME_INNER_HTML, content_type="text/html"
    )
    httpserver.expect_request("/").respond_with_data(
        _iframe_host_html(httpserver.url_for("/inner")), content_type="text/html"
    )
    tree = await _snapshot(driver, open_ctx, httpserver.url_for("/"))

    inner = _find(tree, lambda n: n.attributes.get("id") == "inner")
    assert inner, "button inside the same-origin iframe was not captured"

    await driver.execute(open_ctx, [ClickAction(ref=_ref(inner[0]))])

    title = await _read(
        driver, open_ctx, "document.querySelector('iframe').contentDocument.title"
    )
    assert title == "inner-clicked"


async def test_fill_inside_a_cross_origin_iframe(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer, make_httpserver
) -> None:
    """A cross-origin frame is a separate renderer: its document never appears in
    the host's `DOM.getDocument`, so before this change everything inside one --
    payment fields, consent banners, embedded widgets -- was simply absent from
    what the agent could see, with no ref to fail on in the first place.

    `127.0.0.1` and `localhost` are different origins to the browser while both
    resolving to the same loopback host, which is what makes this a genuine OOPIF
    without needing a second machine.
    """

    inner_server = make_httpserver
    inner_server.expect_request("/inner").respond_with_data(
        IFRAME_INNER_HTML, content_type="text/html"
    )
    cross_origin_src = f"http://127.0.0.1:{inner_server.port}/inner"

    httpserver.expect_request("/").respond_with_data(
        _iframe_host_html(cross_origin_src), content_type="text/html"
    )
    tree = await _snapshot(driver, open_ctx, f"http://localhost:{httpserver.port}/")

    field = _find(tree, lambda n: n.attributes.get("id") == "inner-field")
    assert field, "input inside the cross-origin iframe was not captured"

    await driver.execute(open_ctx, [FillAction(ref=_ref(field[0]), text="across origins")])

    # Read back from inside the frame -- the host document cannot reach into a
    # cross-origin `contentDocument`, which is the whole point of the test.
    value = await _read(
        driver,
        open_ctx,
        "document.querySelector('iframe').getAttribute('src')",
    )
    assert value == cross_origin_src

    verified = await driver.execute(open_ctx, [SnapshotAction()])
    refreshed = _find(
        verified.fused_trees[0], lambda n: n.attributes.get("id") == "inner-field"
    )
    assert refreshed, "the cross-origin field vanished from the second capture"


async def test_cross_origin_frame_nodes_carry_their_own_session(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer, make_httpserver
) -> None:
    """The identity that makes the whole thing work.

    A `backendNodeId` is only meaningful in the session that issued it, so a node
    captured from a frame's own target has to carry that session for the driver to
    route its `DOM.getBoxModel`/`Input.dispatchMouseEvent` back to the right
    renderer. Without this the ids collide silently across frames.
    """

    inner_server = make_httpserver
    inner_server.expect_request("/inner").respond_with_data(
        IFRAME_INNER_HTML, content_type="text/html"
    )
    httpserver.expect_request("/").respond_with_data(
        _iframe_host_html(f"http://127.0.0.1:{inner_server.port}/inner"),
        content_type="text/html",
    )
    tree = await _snapshot(driver, open_ctx, f"http://localhost:{httpserver.port}/")

    inner = _find(tree, lambda n: n.attributes.get("id") == "inner")
    assert inner, "cross-origin frame content was not captured"
    host = _find(tree, lambda n: n.tag_name == "iframe")[0]

    assert inner[0].session_id is not None
    assert inner[0].session_id != host.session_id, (
        "frame content must carry its own session, not the host page's"
    )
    # Refs are unique across renderers even where backend ids are not.
    indices = [n.selector_index for n in iter_elements(tree)]
    assert len(indices) == len(set(indices))


# ---------------------------------------------------------------- shadow DOM

SHADOW_HTML = """<html><body>
<div id="log"></div>
<div id="host"></div>
<script>
  const root = document.getElementById('host').attachShadow({mode: 'open'});
  root.innerHTML =
    '<button id="shadow-btn">Shadow button</button><input type="text" id="shadow-field">';
  root.getElementById('shadow-btn').addEventListener('click', () => {
    document.getElementById('log').textContent = 'shadow-clicked';
  });
</script>
</body></html>"""


async def test_click_inside_an_open_shadow_root(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """`node.xpath()` passes *through* shadow roots, producing a path that
    document XPath can never match -- so the cascade's structural tier was dead
    on arrival for shadow content, leaving only whatever attributes happened to
    be unique."""

    httpserver.expect_request("/").respond_with_data(SHADOW_HTML, content_type="text/html")
    tree = await _snapshot(driver, open_ctx, httpserver.url_for("/"))

    button = _find(tree, lambda n: n.attributes.get("id") == "shadow-btn")
    assert button, "button inside the open shadow root was not captured"

    await driver.execute(open_ctx, [ClickAction(ref=_ref(button[0]))])
    assert await _read(
        driver, open_ctx, "document.getElementById('log').textContent"
    ) == "shadow-clicked"


async def test_fill_inside_an_open_shadow_root(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(SHADOW_HTML, content_type="text/html")
    tree = await _snapshot(driver, open_ctx, httpserver.url_for("/"))

    field = _find(tree, lambda n: n.attributes.get("id") == "shadow-field")
    assert field, "input inside the open shadow root was not captured"

    await driver.execute(open_ctx, [FillAction(ref=_ref(field[0]), text="shadow text")])

    value = await _read(
        driver,
        open_ctx,
        "document.getElementById('host').shadowRoot.getElementById('shadow-field').value",
    )
    assert value == "shadow text"


# ------------------------------------------------------- zero-box but usable

HIDDEN_INPUT_HTML = """<html><body>
<label for="file-input">Upload</label>
<input type="file" id="file-input" style="width:0;height:0;opacity:0" />
<input type="checkbox" id="styled-box" style="position:absolute;opacity:0" />
</body></html>"""


@pytest.mark.parametrize("element_id", ["file-input", "styled-box"])
async def test_zero_size_elements_still_resolve(
    driver: PatchrightDriver,
    open_ctx: ContextRef,
    httpserver: HTTPServer,
    element_id: str,
) -> None:
    """A visually hidden control is not a stale ref.

    `_resolve_ref` used to gate every ref on a non-zero `bounding_box()` and
    raise `StaleRefError` otherwise, which rejected exactly the elements real
    pages hide on purpose -- the file input behind a styled label, the checkbox
    under a custom sprite. Resolution no longer inspects geometry at all; only
    the verbs that need a point to aim at do.
    """

    httpserver.expect_request("/").respond_with_data(
        HIDDEN_INPUT_HTML, content_type="text/html"
    )
    tree = await _snapshot(driver, open_ctx, httpserver.url_for("/"))

    element = _find(tree, lambda n: n.attributes.get("id") == element_id)
    assert element, f"#{element_id} was not captured"

    node, _ = await driver._resolve_ref(  # noqa: SLF001 -- resolution is the subject
        driver._contexts[open_ctx.context_id].pages[  # noqa: SLF001
            driver._contexts[open_ctx.context_id].active_page_id  # noqa: SLF001
        ],
        _ref(element[0]),
    )
    assert node.attributes.get("id") == element_id
