"""`PatchrightDriver.open()`'s `locale`/`timezone_id` overrides (anti-detection
A3) actually reach the running browser -- a real Patchright context, asserting
`navigator.language` and the JS-resolved timezone match what was passed.
Distinct non-default values (`en-GB`/`Europe/London`) so a host that happens
to already be `en-US`/`America/*` can't make this pass by accident."""

from __future__ import annotations

from agentpilot.control.identity import identity_for
from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.spi.actions import ExecuteJsAction
from agentpilot.spi.egress import EgressPolicy

_PROBE = (
    "() => [navigator.language, "
    "Intl.DateTimeFormat().resolvedOptions().timeZone]"
)


async def test_open_applies_locale_and_timezone(driver: PatchrightDriver, tmp_path) -> None:
    identity = identity_for("t", "example.com", "loc-test")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    ctx = await driver.open(
        identity,
        profile_dir,
        None,
        headful=False,
        egress=EgressPolicy(),
        locale="en-GB",
        timezone_id="Europe/London",
    )
    try:
        result = await driver.execute(ctx, [ExecuteJsAction(script=_PROBE)])
        language, timezone = result.js_returns[0]
        assert language == "en-GB"
        assert timezone == "Europe/London"
    finally:
        await driver.close(ctx)


async def test_open_without_overrides_leaves_browser_defaults(
    driver: PatchrightDriver, tmp_path
) -> None:
    """The `None` default path must not force any locale/timezone -- it just
    reports whatever the browser/host naturally is, and (critically) doesn't
    error on the omitted-kwarg path."""

    identity = identity_for("t", "example.com", "default-loc")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    ctx = await driver.open(identity, profile_dir, None, headful=False, egress=EgressPolicy())
    try:
        result = await driver.execute(ctx, [ExecuteJsAction(script=_PROBE)])
        language, timezone = result.js_returns[0]
        assert isinstance(language, str) and language  # some value, not forced
        assert isinstance(timezone, str) and timezone
    finally:
        await driver.close(ctx)
