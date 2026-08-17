"""One module, parametrized over driver fixtures (just `patchright_driver` in
P0; `nodriver`/`agent_browser` stubs land with later drivers), asserting
*behavior* rather than implementation: any new driver should pass this suite
unmodified -- that's the entire point of `agentpilot.spi`.

Real Patchright contexts against `pytest-httpserver` inline HTML -- never a
mocked browser, never an external site (browser-use discipline).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets
from pytest_httpserver import HTTPServer

from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.spi.actions import (
    ClickAction,
    ExecuteJsAction,
    ExtractAction,
    FillAction,
    NavigateAction,
    SnapshotAction,
)
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import StaleRefError
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef

ARTICLE_HTML = """<html><body>
<nav><a href="#">Home</a><a href="#">About</a></nav>
<article>
<h1>Driver Contract Article</h1>
<p>This is the first paragraph of real body content, long enough and
distinct enough that the extraction pipeline should treat it as the main
article rather than boilerplate noise.</p>
<p>A second paragraph continues with different wording so extraction has
real multi-paragraph content to check against.</p>
</article>
<button id="probe" onclick="document.getElementById('result').textContent='clicked'">
Click me</button>
<input type="text" id="search-box" placeholder="Search" />
<div id="result"></div>
<footer>Copyright 2026</footer>
</body></html>"""


def _ref(node) -> str:
    return f"e{node.backend_node_id}"


def _find_role(node, role: str):
    """Walk a fused `EnhancedDOMTreeNode` tree for the first node with the
    given accessibility role."""

    if node.ax_role == role:
        return node
    for child in node.children_and_shadow_roots:
        found = _find_role(child, role)
        if found is not None:
            return found
    return None


async def test_navigate_snapshot_yields_refs_with_epoch(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")

    result = await driver.execute(
        open_ctx, [NavigateAction(url=httpserver.url_for("/")), SnapshotAction()]
    )

    assert len(result.fused_trees) == 1
    tree = result.fused_trees[0]

    button = _find_role(tree, "button")
    assert button is not None
    # Refs are `e<backendNodeId>` tokens, resolvable by the ref cache.
    assert _ref(button).startswith("e")
    assert button.backend_node_id > 0


async def test_navigate_then_snapshot_does_not_abort_batch(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """Regression test: `NavigateAction`'s own (expected) URL change must not
    trip the batch-abort guard and block the read actions that follow it in
    the same batch -- `execute([Navigate, Snapshot])` is the P0 exit
    criterion, and an earlier version of this guard broke exactly this."""

    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")

    result = await driver.execute(
        open_ctx,
        [
            NavigateAction(url=httpserver.url_for("/")),
            SnapshotAction(),
            ExtractAction(format="markdown"),
        ],
    )

    assert result.sequence_aborted is False
    assert len(result.fused_trees) == 1
    assert len(result.extracts) == 1


async def test_extract_markdown_returns_clean_main_content(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")

    result = await driver.execute(
        open_ctx, [NavigateAction(url=httpserver.url_for("/")), ExtractAction(format="markdown")]
    )

    text = result.extracts[0]
    assert "Driver Contract Article" in text
    assert "first paragraph" in text
    assert "Home" not in text
    assert "Copyright" not in text


STRUCTURED_DATA_HTML = """<html><head>
<title>Widget Product Page</title>
<meta property="og:title" content="Widget">
<meta property="og:description" content="A fine widget.">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product", "name": "Widget"}
</script>
<script id="__NEXT_DATA__" type="application/json">
{"props": {"pageProps": {"sku": "WID-1"}}}
</script>
</head><body><article>Widget details here.</article></body></html>"""


async def test_extract_structured_data_returns_json_ld_meta_and_hydration(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(
        STRUCTURED_DATA_HTML, content_type="text/html"
    )

    result = await driver.execute(
        open_ctx,
        [NavigateAction(url=httpserver.url_for("/")), ExtractAction(format="structured_data")],
    )

    data = json.loads(result.extracts[0])
    assert data["json_ld"] == [
        {"@context": "https://schema.org", "@type": "Product", "name": "Widget"}
    ]
    assert data["metadata"]["og:title"] == "Widget"
    assert data["hydration"]["__NEXT_DATA__"]["props"]["pageProps"]["sku"] == "WID-1"
    assert result.page_title == "Widget Product Page"


async def test_click_dispatches_via_ref_cache(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """P1's real interaction dispatch: snapshot a ref, then click it, and
    confirm the click actually landed on the live page (not just that no
    exception was raised)."""

    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    snap = await driver.execute(
        open_ctx, [NavigateAction(url=httpserver.url_for("/")), SnapshotAction()]
    )
    button = _find_role(snap.fused_trees[0], "button")
    assert button is not None

    await driver.execute(open_ctx, [ClickAction(ref=_ref(button))])

    result = await driver.execute(open_ctx, [ExtractAction(format="html")])
    assert "clicked" in result.extracts[0]


async def test_fill_dispatches_via_ref_cache(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    snap = await driver.execute(
        open_ctx, [NavigateAction(url=httpserver.url_for("/")), SnapshotAction()]
    )
    textbox = _find_role(snap.fused_trees[0], "textbox")
    assert textbox is not None

    await driver.execute(open_ctx, [FillAction(ref=_ref(textbox), text="hello")])

    # `.fill()` sets the live DOM `.value` *property*, not the `value`
    # attribute -- `page.content()`'s outerHTML serialization wouldn't show
    # it even on success, so read the property directly instead.
    result = await driver.execute(
        open_ctx,
        [ExecuteJsAction(script="document.getElementById('search-box').value")],
    )
    assert result.js_returns[0] == "hello"


async def test_click_with_unknown_ref_raises_stale_ref_error(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """A ref that was never recorded by a snapshot (typo, or a snapshot from
    a different context) must fail loudly and typed, never silently no-op or
    raise a raw Playwright error."""

    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    await driver.execute(open_ctx, [NavigateAction(url=httpserver.url_for("/"))])

    with pytest.raises(StaleRefError) as exc_info:
        await driver.execute(open_ctx, [ClickAction(ref="e999")])
    assert exc_info.value.epoch_superseded is False


async def test_ref_from_superseded_snapshot_raises_stale_ref_error(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """A ref minted by an earlier snapshot must be rejected once a newer
    snapshot has superseded it -- no cascade, no lookalike click on stale DOM.

    Replacing the element between snapshots (rather than just re-snapshotting
    an unchanged page) frees up its `backendNodeId`, so the old `e<id>` ref is
    no longer in the current fused index -- the fusion path raises
    `StaleRefError` (it does not distinguish "superseded" from "never existed";
    both are simply absent from the fresh capture)."""

    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    snap1 = await driver.execute(
        open_ctx, [NavigateAction(url=httpserver.url_for("/")), SnapshotAction()]
    )
    old_button = _find_role(snap1.fused_trees[0], "button")
    assert old_button is not None

    await driver.execute(
        open_ctx,
        [
            ExecuteJsAction(
                script="document.getElementById('probe').outerHTML = "
                "'<span>replaced</span>'"
            ),
            SnapshotAction(),  # fresh capture; the old backendNodeId is gone
        ],
    )

    with pytest.raises(StaleRefError):
        await driver.execute(open_ctx, [ClickAction(ref=_ref(old_button))])


async def test_snapshot_populates_leaf_bounding_boxes(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """The fusion capture carries per-node layout bounds (`absolute_position`)
    from the CDP DOMSnapshot merge -- no opt-in flag, always available for
    coordinate grounding."""

    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")

    result = await driver.execute(
        open_ctx,
        [NavigateAction(url=httpserver.url_for("/")), SnapshotAction()],
    )

    button = _find_role(result.fused_trees[0], "button")
    assert button is not None
    assert button.absolute_position is not None
    assert button.absolute_position.width > 0
    assert button.absolute_position.height > 0


async def test_export_restore_round_trips_cookies_and_multi_origin_localstorage(
    driver: PatchrightDriver, open_ctx: ContextRef
) -> None:
    """The exact bug fixed vs. a prior internal system: its storage-state
    capture there only ever captured the single origin loaded at call time.
    Assert on 2+ origins to prove the native `context.storage_state()`
    mirror actually captures what that system couldn't."""

    server_a = HTTPServer(host="127.0.0.1", port=0)
    server_b = HTTPServer(host="127.0.0.1", port=0)
    server_a.start()
    server_b.start()
    try:
        server_a.expect_request("/").respond_with_data(
            "<html><body><script>"
            "localStorage.setItem('k', 'from-a'); document.cookie = 'cookie_a=va';"
            "</script></body></html>",
            content_type="text/html",
        )
        server_b.expect_request("/").respond_with_data(
            "<html><body><script>"
            "localStorage.setItem('k', 'from-b'); document.cookie = 'cookie_b=vb';"
            "</script></body></html>",
            content_type="text/html",
        )

        await driver.execute(open_ctx, [NavigateAction(url=server_a.url_for("/"))])
        await driver.execute(open_ctx, [NavigateAction(url=server_b.url_for("/"))])

        state = await driver.export_state(open_ctx)
        origins = {o.origin: o for o in state.origins}

        origin_a = server_a.url_for("/").rstrip("/")
        origin_b = server_b.url_for("/").rstrip("/")
        assert origin_a in origins
        assert origin_b in origins
        assert any(e.name == "k" and e.value == "from-a" for e in origins[origin_a].local_storage)
        assert any(e.name == "k" and e.value == "from-b" for e in origins[origin_b].local_storage)

        cookie_names = {c["name"] for c in state.cookies}
        assert {"cookie_a", "cookie_b"} <= cookie_names

        await driver.restore_state(open_ctx, state)
    finally:
        server_a.stop()
        server_b.stop()


async def test_close_is_idempotent(driver: PatchrightDriver, tmp_path) -> None:
    identity = IdentityKey(tenant="t", domain="example.com", name="idempotent-close")
    ctx = await driver.open(
        identity, tmp_path / "profile", None, headful=False, egress=EgressPolicy()
    )

    await driver.close(ctx)
    await driver.close(ctx)


async def test_health_reflects_a_crashed_context(driver: PatchrightDriver, tmp_path) -> None:
    identity = IdentityKey(tenant="t", domain="example.com", name="crash-test")
    ctx = await driver.open(
        identity, tmp_path / "profile", None, headful=False, egress=EgressPolicy()
    )

    cctx = driver._contexts[ctx.context_id]
    live = cctx.pages[cctx.active_page_id]
    try:
        await live.page.goto("chrome://crash", timeout=3_000)
    except Exception:
        pass
    await asyncio.sleep(0.5)

    health = await driver.health(ctx)
    assert health.alive is False
    assert health.reason == "page_crash"


async def test_cdp_http_base_is_none_when_not_enabled(
    driver: PatchrightDriver, open_ctx: ContextRef
) -> None:
    assert await driver.cdp_http_base(open_ctx) is None


async def test_cdp_endpoint_accepts_real_devtools_connection(
    driver: PatchrightDriver, cdp_ctx: ContextRef
) -> None:
    base = await driver.cdp_http_base(cdp_ctx)
    assert base is not None

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base}/json/version")
    resp.raise_for_status()
    info = resp.json()
    assert info["webSocketDebuggerUrl"].startswith("ws://127.0.0.1")

    async with websockets.connect(info["webSocketDebuggerUrl"], max_size=None) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        reply = json.loads(raw)
        assert reply["id"] == 1
        assert "product" in reply["result"]
