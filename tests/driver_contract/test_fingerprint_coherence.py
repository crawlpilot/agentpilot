"""Real-browser assertions about the injected fingerprint script.

Two things make this a `driver_contract` test rather than a unit test, and both
were learned the hard way while fixing the WebGL toString leak:

1. **Patchright evaluates in an isolated world.** `page.evaluate` runs in a
   different realm from the one `add_init_script` patches, so it cannot see the
   patches at all. Every assertion here therefore runs inside the *page's own*
   main world (an inline `<script>`) and reports back through `document.title`.
2. **`set_content` does not re-run init scripts.** It writes into the already-
   loaded `about:blank` document. The probe must be reached by a real
   navigation, hence the `data:` URL.

A probe that gets either of these wrong reports a clean fingerprint no matter
how broken the script is.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest_asyncio

from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.identity.fingerprint import generate
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef

IDENTITY_SLUG = "t/example.com/fingerprint-test"

_PROBE = """
<script>
  const nav = navigator;
  const proto = Object.getPrototypeOf(nav);
  const gl = () => { const c = document.createElement('canvas').getContext('webgl');
                     return c ? c.getParameter(37445) : null; };
  document.title = JSON.stringify({
    webdriver: nav.webdriver === undefined ? 'undefined' : String(nav.webdriver),
    getParameterToString: WebGLRenderingContext.prototype.getParameter.toString(),
    getParameter2ToString: (typeof WebGL2RenderingContext !== 'undefined')
      ? WebGL2RenderingContext.prototype.getParameter.toString() : '',
    hardwareConcurrency: nav.hardwareConcurrency,
    deviceMemory: nav.deviceMemory,
    platform: nav.platform,
    languages: nav.languages.join(','),
    vendorUnmasked: String(gl()),
    screenWidth: screen.width,
    screenHeight: screen.height,
  });
</script>
"""


@pytest_asyncio.fixture
async def fingerprinted_ctx(driver: PatchrightDriver, tmp_path) -> ContextRef:
    """A context opened the way a protected tier opens one."""

    fp = generate(IDENTITY_SLUG)
    identity = IdentityKey(tenant="t", domain="example.com", name="fingerprint-test")
    ctx = await driver.open(
        identity,
        tmp_path / "profile",
        None,
        headful=False,
        egress=EgressPolicy(),
        user_agent=fp.user_agent,
        init_script=fp.init_script(),
        extra_http_headers=fp.client_hint_headers(),
        locale=fp.geo.locale,
        timezone_id=fp.geo.timezone_id,
    )
    yield ctx
    await driver.close(ctx)


async def _probe(driver: PatchrightDriver, ctx: ContextRef) -> dict:
    # Reaches for the live page directly rather than going through
    # `execute()`: the probe has to report from the page's own main world, and
    # every extraction action reads it from the isolated one.
    cctx = driver._require_context(ctx)  # noqa: SLF001
    live = driver._require_page(cctx, None)  # noqa: SLF001
    await live.page.goto("data:text/html," + urllib.parse.quote(_PROBE))
    return json.loads(await live.page.title())


async def test_webgl_spoof_does_not_announce_itself(
    driver: PatchrightDriver, fingerprinted_ctx: ContextRef
) -> None:
    """The regression guard for the `patched.add(wrapped)` fix.

    Without it, `getParameter.toString()` returns the literal source of the
    spoof -- verified against a real Chrome -- which is a trivially scriptable
    tell that defeats the spoofing it is meant to provide, and among the first
    things a commercial sensor checks.
    """

    r = await _probe(driver, fingerprinted_ctx)

    assert "[native code]" in r["getParameterToString"]
    assert "37445" not in r["getParameterToString"]
    if r["getParameter2ToString"]:
        assert "[native code]" in r["getParameter2ToString"]
        assert "37445" not in r["getParameter2ToString"]


async def test_pinned_values_actually_reach_the_page(
    driver: PatchrightDriver, fingerprinted_ctx: ContextRef
) -> None:
    """The init script is only useful if it runs. It silently does not on
    `set_content`, and is invisible to `page.evaluate` -- see the module
    docstring."""

    fp = generate(IDENTITY_SLUG)
    r = await _probe(driver, fingerprinted_ctx)

    assert r["hardwareConcurrency"] == fp.hardware.hardware_concurrency
    assert r["deviceMemory"] == fp.hardware.device_memory
    assert r["platform"] == fp.hardware.platform
    assert r["languages"] == ",".join(fp.geo.languages)
    assert r["screenWidth"] == fp.screen.width
    assert r["screenHeight"] == fp.screen.height


async def test_no_navigator_webdriver(
    driver: PatchrightDriver, fingerprinted_ctx: ContextRef
) -> None:
    r = await _probe(driver, fingerprinted_ctx)

    assert r["webdriver"] in ("false", "undefined")


_PHASE2_PROBE = """
<script>
(async () => {
  const out = {};
  out.plugins = navigator.plugins.length;
  out.pluginName = navigator.plugins.length ? navigator.plugins[0].name : null;
  out.pluginIsPlugin = navigator.plugins.length
    ? (navigator.plugins[0] instanceof Plugin) : false;
  out.mimeTypes = navigator.mimeTypes.length;
  out.maxTouchPoints = navigator.maxTouchPoints;
  out.vendor = navigator.vendor;
  out.productSub = navigator.productSub;
  try { const p = await navigator.permissions.query({name: 'notifications'});
        out.perm = p.state; out.notif = Notification.permission;
  } catch (e) { out.perm = 'ERR'; }
  out.prepareStackTraceWritable =
    Object.getOwnPropertyDescriptor(Error, 'prepareStackTrace').writable;
  const draw = () => { const c = document.createElement('canvas');
    c.width = 60; c.height = 20; const x = c.getContext('2d');
    x.fillStyle = '#f60'; x.fillRect(0, 0, 60, 20);
    x.fillStyle = '#069'; x.font = '14px Arial'; x.fillText('agentpilot', 2, 2);
    return c.toDataURL(); };
  out.canvas = draw();
  out.canvasStable = (draw() === out.canvas);
  try { const ac = new OfflineAudioContext(1, 4410, 44100);
        out.audio = ac.createBuffer(1, 4410, 44100).getChannelData(0)[0];
  } catch (e) { out.audio = null; }
  out.toStringLeaks = [
    HTMLCanvasElement.prototype.toDataURL.toString(),
    CanvasRenderingContext2D.prototype.getImageData.toString(),
    AudioBuffer.prototype.getChannelData.toString(),
    Permissions.prototype.query.toString(),
    WebGLRenderingContext.prototype.getParameter.toString(),
  ].filter((s) => !s.includes('[native code]')).length;
  document.title = JSON.stringify(out);
})();
</script>
"""


async def _probe2(driver: PatchrightDriver, ctx: ContextRef) -> dict:
    cctx = driver._require_context(ctx)  # noqa: SLF001
    live = driver._require_page(cctx, None)  # noqa: SLF001
    await live.page.goto("data:text/html," + urllib.parse.quote(_PHASE2_PROBE))
    await live.page.wait_for_function("document.title.startsWith('{')", timeout=10_000)
    return json.loads(await live.page.title())


async def test_headless_tells_are_covered(
    driver: PatchrightDriver, fingerprinted_ctx: ContextRef
) -> None:
    """Headless Chrome reports an empty PluginArray and answers
    `permissions.query({notifications})` inconsistently with
    `Notification.permission`. Both are cheap, well-known checks."""

    fp = generate(IDENTITY_SLUG)
    r = await _probe2(driver, fingerprinted_ctx)

    assert r["plugins"] > 0
    assert r["pluginName"] == "PDF Viewer"
    assert r["pluginIsPlugin"] is True, "plugins must be real Plugin instances"
    assert r["mimeTypes"] > 0
    assert r["perm"] == r["notif"], "permissions.query must agree with Notification.permission"
    assert r["maxTouchPoints"] == fp.hardware.max_touch_points
    assert r["vendor"] == fp.hardware.vendor
    assert r["productSub"] == fp.hardware.product_sub


async def test_prepare_stack_trace_is_locked_down(
    driver: PatchrightDriver, fingerprinted_ctx: ContextRef
) -> None:
    """A page must not be able to install an `Error.prepareStackTrace` getter
    and observe how CDP serializes an Error -- the classic `Runtime.enable`
    side channel (Browser4 `stealth.js:121-130`)."""

    r = await _probe2(driver, fingerprinted_ctx)

    assert r["prepareStackTraceWritable"] is False


async def test_canvas_noise_is_stable_within_an_identity(
    driver: PatchrightDriver, fingerprinted_ctx: ContextRef
) -> None:
    """Deterministic on purpose: a canvas fingerprint that changes between
    reads is a *louder* signal than one that never changes."""

    r = await _probe2(driver, fingerprinted_ctx)

    assert r["canvasStable"] is True
    assert r["audio"] is not None


async def test_no_patch_announces_itself(
    driver: PatchrightDriver, fingerprinted_ctx: ContextRef
) -> None:
    """Every function the init script replaces must stringify as native. This
    is the generalisation of the WebGL leak above -- each new patch is one more
    chance to reintroduce it."""

    r = await _probe2(driver, fingerprinted_ctx)

    assert r["toStringLeaks"] == 0


_CLICK_PROBE = """
<html><body style="margin:0">
<button id="b" style="position:absolute;left:200px;top:150px;width:120px;height:60px">go</button>
<script>
  window.__ev = {moves: 0, over: 0, click: null, path: []};
  const b = document.getElementById('b');
  document.addEventListener('mousemove', (e) => {
    window.__ev.moves++;
    if (window.__ev.path.length < 400) window.__ev.path.push([e.clientX, e.clientY]);
  });
  b.addEventListener('mouseover', () => { window.__ev.over++; });
  b.addEventListener('click', (e) => {
    const r = b.getBoundingClientRect();
    window.__ev.click = {
      offX: e.clientX - r.left, offY: e.clientY - r.top,
      cx: r.width / 2, cy: r.height / 2,
    };
    document.title = JSON.stringify(window.__ev);
  });
</script>
</body></html>
"""


def _find_role(node, role: str):
    """Walk a fused `EnhancedDOMTreeNode` tree for the first node with `role`.
    Same helper shape as `test_driver_contract.py`."""

    if node.ax_role == role:
        return node
    for child in node.children_and_shadow_roots:
        found = _find_role(child, role)
        if found is not None:
            return found
    return None


async def test_click_is_a_curved_approach_to_an_off_centre_point(
    driver: PatchrightDriver, fingerprinted_ctx: ContextRef
) -> None:
    """`_human_click` used to build a jittered approach and then call
    `locator.click()`, which does its own `mouse.move` to the element's exact
    geometric centre -- discarding the approach and leaving a dead-centre
    teleport as the last thing the page saw. It now passes `position=`.
    """

    from agentpilot.spi.actions import ClickAction, SnapshotAction

    cctx = driver._require_context(fingerprinted_ctx)  # noqa: SLF001
    live = driver._require_page(cctx, None)  # noqa: SLF001
    await live.page.goto("data:text/html," + urllib.parse.quote(_CLICK_PROBE))

    # Refs are minted by a snapshot, not raw selectors (`driver/ref_cache.py`).
    snap = await driver.execute(fingerprinted_ctx, [SnapshotAction()])
    button = _find_role(snap.fused_trees[0], "button")
    assert button is not None

    await driver.execute(fingerprinted_ctx, [ClickAction(ref=f"e{button.backend_node_id}")])
    await live.page.wait_for_function("document.title.startsWith('{')", timeout=10_000)
    ev = json.loads(await live.page.title())

    assert ev["over"] >= 1, "no mouseover -- the pointer materialised inside the element"
    assert ev["moves"] > 5, "no movement stream, just a teleport"
    off_centre = abs(ev["click"]["offX"] - ev["click"]["cx"]) > 1 or (
        abs(ev["click"]["offY"] - ev["click"]["cy"]) > 1
    )
    assert off_centre, "click landed on the exact geometric centre"

    # The movement path must not be a straight line.
    pts = ev["path"]
    if len(pts) >= 3:
        (x0, y0), (x1, y1) = pts[0], pts[-1]
        dx, dy = x1 - x0, y1 - y0
        length = (dx * dx + dy * dy) ** 0.5
        if length > 20:
            worst = max(abs((px - x0) * dy - (py - y0) * dx) / length for px, py in pts)
            assert worst > 1.0, "pointer travelled in a straight line"
