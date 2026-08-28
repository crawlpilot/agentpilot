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
