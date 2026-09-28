"""The per-tier stealth configuration for a browser context, in one place.

This used to live inline in `ephemeral.py`'s `_opener`, which meant it applied
to `/v1/scrape` and to nothing else: `session/interactive.py` -- the path behind
`/v1/sessions` and every agent run -- opened its context with no fingerprint, no
init script, no locale/timezone pin, no warm-up and no block detection,
*regardless of the tier the caller asked for*. On those paths `tier` only ever
reached the tier's stealth flag, so an agent run against a hardened retail site was
a naked, cookieless, unfingerprinted Chrome and was blocked accordingly.

Both callers now resolve their `driver.open()` stealth kwargs here, so a tier
means the same thing everywhere.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from crawlpilot.config import DEFAULTS, BrowserConfig
from crawlpilot.egress.geo import EgressGeo
from crawlpilot.identity.fingerprint import generate as generate_fingerprint
from crawlpilot.identity.fingerprint import geo_for_region
from crawlpilot.spi.identity import IdentityRef
from crawlpilot.spi.proxy import ProxyEndpoint
from crawlpilot.tiers import TierPolicy


@dataclass
class StealthProfile:
    """The subset of `BrowserDriver.open()` kwargs that a tier decides."""

    locale: str | None = None
    timezone_id: str | None = None
    warmup: bool = False
    detect_blocks: bool = False
    wait_abck: bool = False
    user_agent: str | None = None
    init_script: str | None = None
    extra_http_headers: dict[str, str] | None = None
    extra_launch_args: list[str] | None = field(default=None)
    interact_profile: str | None = None
    wants_headful: bool = False
    """Whether this tier needs a real window. Not an `open()` kwarg -- `headful`
    is positional there and belongs to the caller -- so `as_open_kwargs` leaves
    it out and each caller ORs it into its own choice.

    Set for every protected tier in truthful mode. The spoofed profile hid
    `HeadlessChrome` behind its UA override; truthful has no override, and a
    headless Chrome announces itself in the UA, which Akamai's edge rejects before
    any JavaScript runs (measured: `raw_headless` -> 403, every headful run -> 200).
    """

    def as_open_kwargs(self) -> dict[str, Any]:
        kwargs = asdict(self)
        kwargs.pop("wants_headful")
        return kwargs


def browser_executable(config: BrowserConfig) -> str | None:
    """The binary that will launch, for asking its version.

    Best-effort and never fatal: this runs while assembling a stealth profile,
    and a browser that cannot be located is a problem for the launch path to
    report properly, not something to surface from here as a fingerprint error.
    A channel launch resolves to no path at all, which is a legitimate `None` --
    the caller then keeps its configured version.
    """

    from crawlpilot.driver import browser_discovery

    try:
        launch = browser_discovery.resolve_browser(
            executable_path=config.launch.executable_path,
            channel=config.launch.channel,
        )
    except Exception:  # noqa: BLE001 - see docstring
        return None
    return launch.executable_path


def resolve(
    identity: IdentityRef,
    tier: str,
    *,
    proxy: ProxyEndpoint | None = None,
    locale: str | None = None,
    timezone_id: str | None = None,
    detect_blocks: bool = True,
    config: BrowserConfig = DEFAULTS,
    egress_geo: EgressGeo | None = None,
) -> StealthProfile:
    """The stealth kwargs for `identity` on `tier`.

    An unprotected tier gets the caller's own locale/timezone and nothing else,
    which is exactly the behaviour every existing non-scrape caller had before
    this module existed -- so wiring it in cannot regress them.

    On a protected tier, `config.fingerprint.mode` decides how the browser
    presents itself:

    - `truthful` (default): the real browser reports itself -- no UA override,
      no client-hint headers, no init script, no launch flags -- and the tier
      asks for a real window (`wants_headful`). What remains is avoidance that
      cannot be cross-checked into a lie: the warm-up, the `_abck` wait, and a
      timezone aligned to the exit.
    - `spoofed`: one coherent device preset is pinned to the identity for life
      and applied on top.

    Either way the timezone follows the *exit* -- the proxy's declared country,
    else `egress_geo` (this worker's own egress, from `egress.geo.resolve`) --
    and never the identity's hash bucket, which put `Europe/London` on an
    Indian IP. An explicit request value still wins over all of it.

    `detect_blocks=False` keeps the block *avoidance* (fingerprint, warm-up,
    and the `_abck` wait that makes the warm-up count) while dropping the block
    *reaction*. A one-shot scrape has an escalation
    ladder to act on a `ChallengeDetected` -- retry on a higher tier with a
    fresh identity -- so raising is useful there. A long-lived session has no
    ladder: raising mid-run just converts the situation into a failed run. It
    is also far more false-positive-prone there, because an agent navigates
    through blank pages, SPA shells and post-click transitions where the
    `EMPTY`/`TOO_SMALL` verdicts are the *expected* state rather than a wall.
    An agent that lands on a real block page still sees it, and can say so.
    """

    policy = TierPolicy.for_tier(tier)
    if not policy.protected:
        return StealthProfile(locale=locale, timezone_id=timezone_id)

    # The exit the site actually sees: the proxy's declared country when there is
    # one, otherwise this worker's own egress. Never the identity's hash bucket.
    region = proxy.country if proxy and proxy.country else (
        egress_geo.country if egress_geo else None
    )

    if config.fingerprint.mode == "truthful":
        return StealthProfile(
            # Only what the caller asked for. Playwright's `locale=` sends a bare
            # `Accept-Language: en-IN` with no q-list, which no real Chrome does;
            # the browser's own language settings are more believable than that.
            locale=locale,
            timezone_id=timezone_id or _egress_timezone(proxy, egress_geo, region),
            warmup=policy.warmup,
            detect_blocks=detect_blocks,
            wait_abck=policy.warmup,
            interact_profile=policy.interact_profile,
            wants_headful=True,
        )

    fp = generate_fingerprint(
        identity.slug(),
        region=region,
        # The version the browser about to launch actually reports, not the
        # pinned constant -- unless an operator pinned one deliberately. The
        # constant had drifted twenty majors behind the deployed Chrome, which
        # put a contradiction between the UA and the real Client Hints into
        # every request. See `browser_discovery.browser_version`.
        chrome_version=config.fingerprint.resolved_chrome_version(
            browser_executable(config)
        ),
    )
    # A preset carries a geo of its own, which is only right when the preset was
    # chosen *by* region. With no region it was chosen by hash, so its timezone
    # is a coin-toss -- use the egress one instead when we know it.
    fp_timezone = fp.geo.timezone_id if region else None
    return StealthProfile(
        locale=locale or fp.geo.locale,
        timezone_id=timezone_id
        or fp_timezone
        or _egress_timezone(proxy, egress_geo, region)
        or fp.geo.timezone_id,
        warmup=policy.warmup,
        detect_blocks=detect_blocks,
        # Follows the tier, NOT `detect_blocks`. A protected tier already pays
        # for the warm-up scrolls; skipping the `_abck` wait afterwards throws
        # away what they bought, because Akamai only validates the cookie after
        # the sensor POSTs those scrolls trigger. That is why a stealth-tier
        # session could still land on Access Denied at hm.com and cos.com.
        wait_abck=policy.warmup,
        user_agent=fp.user_agent,
        init_script=fp.init_script(),
        # Pin the Client-Hint headers to the same Chrome build as the UA, so
        # Sec-CH-UA / navigator.userAgentData / UA all agree (the trio Akamai
        # cross-checks). Without this the header leaks the real, newer Chrome.
        extra_http_headers=fp.client_hint_headers(),
        extra_launch_args=fp.launch_args(),
        # Using the *requested* tier, not the effective rung, is deliberate.
        # For a one-shot scrape the cadence costs one navigation's worth of
        # pauses, but a long-lived session pays STEALTH's 900-1600 ms `gap`
        # before every click, fill and press for the whole run -- so an agent
        # run on the default tier would become several seconds per step slower
        # for a behavioural signal that matters most on the page *load*, which
        # the warm-up above already covers. An explicit `stealth`/`enhanced`
        # still opts into the slow table.
        interact_profile=policy.interact_profile,
    )


def _egress_timezone(
    proxy: ProxyEndpoint | None, egress_geo: EgressGeo | None, region: str | None
) -> str | None:
    """The timezone of the exit, or `None` to leave the browser's own clock alone.

    A direct egress lookup reports its timezone outright; a proxy only declares a
    country, which maps to a timezone through the region table.
    """

    if proxy is None and egress_geo is not None and egress_geo.timezone:
        return egress_geo.timezone
    geo = geo_for_region(region)
    return geo.timezone_id if geo else None
