"""The one cross-origin frame guarantee that only the driver can express.

The rest of the element-resolution suite this was extracted from now lives in
`packages/crawlpilot/tests/browser/test_toolchain.py`, driven through the public
facade -- which is where it belongs, because it is the standalone library's own
contract and the facade is what an external caller has.

This test cannot move there. It asserts on `EnhancedDOMTreeNode.session_id`, a
driver-internal detail the facade deliberately does not surface: a
`backendNodeId` is only meaningful in the session that issued it, so a node
captured from a frame's own target has to carry that session for the driver to
route its `DOM.getBoxModel` / `Input.dispatchMouseEvent` back to the right
renderer. Without it the ids collide silently across frames, and every
symptom shows up somewhere else entirely.
"""

from __future__ import annotations

from pytest_httpserver import HTTPServer

from crawlpilot.driver.patchright_driver import PatchrightDriver
from crawlpilot.spi.actions import NavigateAction, SnapshotAction
from crawlpilot.spi.dom_tree import iter_elements
from crawlpilot.spi.lease import ContextRef

IFRAME_INNER_HTML = """<html><body>
<button id="inner" onclick="document.title='inner-clicked'">Inner button</button>
<input type="text" id="inner-field" />
</body></html>"""


def _iframe_host_html(src: str) -> str:
    return f"""<html><body>
<h1>Host document</h1>
<iframe src="{src}" width="400" height="200"></iframe>
</body></html>"""


def _find(tree, predicate):
    return [node for node in iter_elements(tree) if predicate(node)]


async def _snapshot(driver: PatchrightDriver, ctx: ContextRef, url: str):
    # `settle=True` is load-bearing here: `NavigateAction` waits only for
    # `domcontentloaded`, which does not wait for subframes, so without it the
    # iframe has not fetched its document yet and there is nothing to capture.
    result = await driver.execute(ctx, [NavigateAction(url=url), SnapshotAction(settle=True)])
    return result.fused_trees[0]


async def test_cross_origin_frame_nodes_carry_their_own_session(
    driver: PatchrightDriver,
    open_ctx: ContextRef,
    httpserver: HTTPServer,
    make_httpserver,
) -> None:
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
