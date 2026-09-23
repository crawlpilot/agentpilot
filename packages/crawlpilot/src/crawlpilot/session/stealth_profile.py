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
from crawlpilot.identity.fingerprint import generate as generate_fingerprint
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

    def as_open_kwargs(self) -> dict[str, Any]:
        return asdict(self)


def _browser_executable(config: BrowserConfig) -> str | None:
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
) -> StealthProfile:
    """The stealth kwargs for `identity` on `tier`.

    An unprotected tier gets the caller's own locale/timezone and nothing else,
    which is exactly the behaviour every existing non-scrape caller had before
    this module existed -- so wiring it in cannot regress them.

    On a protected tier one coherent fingerprint is pinned to the identity for
    life. When the resolved proxy declares an exit-IP country the fingerprint is
    seeded from it, so the pinned timezone/locale match the egress geo;
    otherwise the family is a stable function of the identity slug. The
    fingerprint's own geo fills any locale/timezone the caller did not pin --
    an explicit request value still wins.

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


    fp = generate_fingerprint(
        identity.slug(),
        region=proxy.country if proxy else None,
        # The version the browser about to launch actually reports, not the
        # pinned constant -- unless an operator pinned one deliberately. The
        # constant had drifted twenty majors behind the deployed Chrome, which
        # put a contradiction between the UA and the real Client Hints into
        # every request. See `browser_discovery.browser_version`.
        chrome_version=config.fingerprint.resolved_chrome_version(
            _browser_executable(config)
        ),
    )
    return StealthProfile(
        locale=locale or fp.geo.locale,
        timezone_id=timezone_id or fp.geo.timezone_id,
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
