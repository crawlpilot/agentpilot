"""The per-tier stealth configuration for a browser context, in one place.

This used to live inline in `ephemeral.py`'s `_opener`, which meant it applied
to `/v1/scrape` and to nothing else: `session/interactive.py` -- the path behind
`/v1/sessions` and every agent run -- opened its context with no fingerprint, no
init script, no locale/timezone pin, no warm-up and no block detection,
*regardless of the tier the caller asked for*. On those paths `tier` only ever
reached `stealth_from_tier()`, so an agent run against a hardened retail site was
a naked, cookieless, unfingerprinted Chrome and was blocked accordingly.

Both callers now resolve their `driver.open()` stealth kwargs here, so a tier
means the same thing everywhere.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from agentpilot.identity.fingerprint import generate as generate_fingerprint
from agentpilot.spi.actions import interact_profile_for_tier
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.proxy import ProxyEndpoint

PROTECTED_TIERS = frozenset({"stealth", "enhanced"})
"""Tiers that opt into the ported stealth path: pinned fingerprint, human
warm-up, body-level block detection, and the slow STEALTH interaction cadence."""

_LADDER_ENTRY = {"auto": "stealth"}
"""`auto` is not itself a rung. `ephemeral.py` resolves it through
`_ESCALATION` and only ever calls `resolve()` with a concrete rung, but a
long-lived session has no ladder to climb -- it opens one context and keeps it.
Without this mapping `auto`, which is the *default* tier for agent runs, would
resolve to no stealth at all, which is precisely the hole this module exists to
close. Mapping it to the ladder's first rung makes the two paths agree."""


def effective_tier(tier: str) -> str:
    """The concrete rung `tier` behaves as. Identity for real rungs."""

    return _LADDER_ENTRY.get(tier, tier)


@dataclass
class StealthProfile:
    """The subset of `BrowserDriver.open()` kwargs that a tier decides."""

    locale: str | None = None
    timezone_id: str | None = None
    warmup: bool = False
    detect_blocks: bool = False
    user_agent: str | None = None
    init_script: str | None = None
    extra_http_headers: dict[str, str] | None = None
    extra_launch_args: list[str] | None = field(default=None)
    interact_profile: str | None = None

    def as_open_kwargs(self) -> dict[str, Any]:
        return asdict(self)


def is_protected(tier: str) -> bool:
    return effective_tier(tier) in PROTECTED_TIERS


def resolve(
    identity: IdentityKey,
    tier: str,
    *,
    proxy: ProxyEndpoint | None = None,
    locale: str | None = None,
    timezone_id: str | None = None,
    detect_blocks: bool = True,
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

    `detect_blocks=False` keeps the block *avoidance* (fingerprint, warm-up)
    while dropping the block *reaction*. A one-shot scrape has an escalation
    ladder to act on a `ChallengeDetected` -- retry on a higher tier with a
    fresh identity -- so raising is useful there. A long-lived session has no
    ladder: raising mid-run just converts the situation into a failed run. It
    is also far more false-positive-prone there, because an agent navigates
    through blank pages, SPA shells and post-click transitions where the
    `EMPTY`/`TOO_SMALL` verdicts are the *expected* state rather than a wall.
    An agent that lands on a real block page still sees it, and can say so.
    """

    if not is_protected(tier):
        return StealthProfile(locale=locale, timezone_id=timezone_id)


    fp = generate_fingerprint(identity.slug(), region=proxy.country if proxy else None)
    return StealthProfile(
        locale=locale or fp.geo.locale,
        timezone_id=timezone_id or fp.geo.timezone_id,
        warmup=True,
        detect_blocks=detect_blocks,
        user_agent=fp.user_agent,
        init_script=fp.init_script(),
        # Pin the Client-Hint headers to the same Chrome build as the UA, so
        # Sec-CH-UA / navigator.userAgentData / UA all agree (the trio Akamai
        # cross-checks). Without this the header leaks the real, newer Chrome.
        extra_http_headers=fp.client_hint_headers(),
        extra_launch_args=fp.launch_args(),
        # The interaction cadence comes from `humanize.for_tier`, the mapping
        # the codebase already declares for exactly this (`auto` -> DEFAULT,
        # `stealth`/`enhanced` -> STEALTH) and which nothing had been calling.
        #
        # Using the *requested* tier, not the effective rung, is deliberate.
        # For a one-shot scrape the cadence costs one navigation's worth of
        # pauses, but a long-lived session pays STEALTH's 900-1600 ms `gap`
        # before every click, fill and press for the whole run -- so an agent
        # run on the default tier would become several seconds per step slower
        # for a behavioural signal that matters most on the page *load*, which
        # the warm-up above already covers. An explicit `stealth`/`enhanced`
        # still opts into the slow table.
        interact_profile=interact_profile_for_tier(tier),
    )
