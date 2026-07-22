"""One module, parametrized over driver fixtures (just `patchright_driver` in
P0; `nodriver`/`agent_browser` stubs land with later drivers), asserting
*behavior* rather than implementation: any new driver should pass this suite
unmodified -- that's the entire point of `baas.spi`.

Real Patchright contexts against `pytest-httpserver` inline HTML -- never a
mocked browser, never an external site (browser-use discipline).
"""

from __future__ import annotations

import asyncio

import pytest
from pytest_httpserver import HTTPServer

from baas.driver.patchright_driver import PatchrightDriver
from baas.spi.actions import (
    ClickAction,
    ExecuteJsAction,
    ExtractAction,
    FillAction,
    NavigateAction,
    SnapshotAction,
)
from baas.spi.egress import EgressPolicy
from baas.spi.errors import StaleRefError
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef

ARTICLE_HTML = """<html><body>
<nav><a href="#">Home</a><a href="#">About</a></nav>
<article>
<h1>Driver Contract Article</h1>
<p>This is the first paragraph of real body content, long enough and
distinct enough that trafilatura should treat it as the main article rather
than boilerplate noise.</p>
<p>A second paragraph continues with different wording so extraction has
real multi-paragraph content to check against.</p>
</article>
<button id="probe" onclick="document.getElementById('result').textContent='clicked'">
Click me</button>
<input type="text" id="search-box" placeholder="Search" />
<div id="result"></div>
<footer>Copyright 2026</footer>
</body></html>"""


def _find_role(node, role: str):
    if node.role == role:
        return node
    for child in node.children:
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

    assert len(result.snapshots) == 1
    snapshot = result.snapshots[0]
    assert snapshot.epoch == 1

    button = _find_role(snapshot.root, "button")
    assert button is not None
    assert button.ref
    assert button.epoch == snapshot.epoch


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
    assert len(result.snapshots) == 1
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
    button = _find_role(snap.snapshots[0].root, "button")
    assert button is not None and button.ref

    await driver.execute(open_ctx, [ClickAction(ref=button.ref)])

    result = await driver.execute(open_ctx, [ExtractAction(format="html")])
    assert "clicked" in result.extracts[0]


async def test_fill_dispatches_via_ref_cache(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    snap = await driver.execute(
        open_ctx, [NavigateAction(url=httpserver.url_for("/")), SnapshotAction()]
    )
    textbox = _find_role(snap.snapshots[0].root, "textbox")
    assert textbox is not None and textbox.ref

    await driver.execute(open_ctx, [FillAction(ref=textbox.ref, text="hello")])

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


async def test_ref_from_superseded_epoch_raises_stale_ref_error(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    """A ref minted by an earlier snapshot must be rejected once a newer
    snapshot has superseded it -- no cascade, no lookalike click on stale DOM."""

    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    snap1 = await driver.execute(
        open_ctx, [NavigateAction(url=httpserver.url_for("/")), SnapshotAction()]
    )
    old_button = _find_role(snap1.snapshots[0].root, "button")
    assert old_button is not None

    await driver.execute(open_ctx, [SnapshotAction()])  # bumps the epoch

    with pytest.raises(StaleRefError) as exc_info:
        await driver.execute(open_ctx, [ClickAction(ref=old_button.ref)])
    assert exc_info.value.epoch_superseded is True


async def test_snapshot_viewport_only_drops_offscreen_elements(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    html = """<html><body>
    <button id="onscreen">Visible</button>
    <button id="offscreen" style="position:absolute; top:9000px;">Hidden</button>
    </body></html>"""
    httpserver.expect_request("/").respond_with_data(html, content_type="text/html")

    result = await driver.execute(
        open_ctx,
        [NavigateAction(url=httpserver.url_for("/")), SnapshotAction(viewport_only=True)],
    )

    names = _collect_names(result.snapshots[0].root)
    assert "Visible" in names
    assert "Hidden" not in names


async def test_snapshot_roles_filter_drops_non_matching_leaves(
    driver: PatchrightDriver, open_ctx: ContextRef, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")

    result = await driver.execute(
        open_ctx,
        [NavigateAction(url=httpserver.url_for("/")), SnapshotAction(roles=("button",))],
    )

    assert _find_role(result.snapshots[0].root, "button") is not None
    assert _find_role(result.snapshots[0].root, "link") is None


def _collect_names(node) -> set[str]:
    names = {node.name} if node.name else set()
    for child in node.children:
        names |= _collect_names(child)
    return names


async def test_export_restore_round_trips_cookies_and_multi_origin_localstorage(
    driver: PatchrightDriver, open_ctx: ContextRef
) -> None:
    """The exact bug fixed vs. Browser4: `saveStorageState()` there only ever
    captured the single origin loaded at call time. Assert on 2+ origins to
    prove the native `context.storage_state()` mirror actually captures what
    Browser4 couldn't."""

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

    live = driver._live[ctx.context_id]
    try:
        await live.page.goto("chrome://crash", timeout=3_000)
    except Exception:
        pass
    await asyncio.sleep(0.5)

    health = await driver.health(ctx)
    assert health.alive is False
    assert health.reason == "page_crash"
