"""Tier vocabulary and the policy derived from it."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

TierName = Literal["basic", "stealth", "enhanced", "auto"]
"""The wire vocabulary, for Pydantic request models.

A `Literal` rather than the `Tier` enum below because this is the *only* thing
`gateway.schemas` needs and swapping the wire type would change the generated
OpenAPI schema and the runtime type of every parsed `request.tier`. Declared
here once instead of three times there; the enum swap is a separate, later
change with its own blast radius.
"""


class Tier(StrEnum):
    """Internal vocabulary. `StrEnum`, so `Tier.AUTO == "auto"` and every
    existing string comparison and dict lookup keeps working unchanged."""

    BASIC = "basic"
    STEALTH = "stealth"
    ENHANCED = "enhanced"
    AUTO = "auto"


PROTECTED: frozenset[str] = frozenset({Tier.STEALTH, Tier.ENHANCED})
"""Tiers that opt into the ported stealth path: pinned fingerprint, human
warm-up, body-level block detection, and the slow interaction cadence."""

_LADDER_ENTRY: dict[str, str] = {Tier.AUTO: Tier.STEALTH}
"""`auto` is not itself a rung. A one-shot scrape resolves it through
`ESCALATION`; a long-lived session has no ladder to climb, so without this
mapping `auto` -- the default tier for agent runs -- would resolve to no
stealth at all, which is precisely the hole this closes."""

_NO_RUNTIME: frozenset[str] = frozenset({Tier.BASIC, Tier.STEALTH})
"""Tiers whose DOM fusion must run Runtime-free (no `getEventListeners`).
`enhanced`/`auto` allow Runtime, for full browser-use parity."""

_INTERACT_PROFILE: dict[str, str] = {
    Tier.AUTO: "default",
    Tier.STEALTH: "stealth",
    Tier.ENHANCED: "stealth",
}
"""Which named delay table (`driver.humanize`) a tier interacts on.

`stealth`/`enhanced` take the slowest, most-human table. `auto` takes the
default: a long-lived session pays the slow table's inter-action gap before
every click, fill and press for the whole run, so `auto` on the slow table
would cost seconds per agent step -- while the behavioural signal it buys
matters most during page load, which the warm-up covers separately. `basic`
never reaches a browser.
"""

ESCALATION: dict[str, tuple[str, ...]] = {
    # `auto` climbs on `ChallengeDetected` (Firecrawl's start-cheap,
    # escalate-on-failure semantics). Each retry mints a fresh throwaway
    # identity, so it also gets a new proxy pick and a new pinned fingerprint --
    # the PRIVACY-scope rotation ported from `BrowserResponseHandlerImpl.kt`.
    # An explicitly requested tier does *not* auto-escalate: the caller chose it.
    # `basic` fetches over plain HTTP first (no browser) and only escalates to a
    # real browser on a hard wall -- the cheap-first path.
    Tier.AUTO: (Tier.STEALTH, Tier.ENHANCED),
    Tier.BASIC: (Tier.BASIC, Tier.STEALTH),
    Tier.STEALTH: (Tier.STEALTH,),
    Tier.ENHANCED: (Tier.ENHANCED,),
}

_RESIDENTIAL = "residential"
"""The proxy pool a protected rung wants: a datacenter IP is scored negatively
by Akamai-class WAFs before any JS runs."""


@dataclass(frozen=True)
class TierPolicy:
    """Everything that follows from a requested tier."""

    tier: str
    """The tier as requested -- not the effective rung. Kept because the
    interaction cadence is deliberately chosen from the *request* (see
    `interact_profile`).

    Typed `str`, not `Tier`: an unrecognised tier arrives from the wire and
    must pass through unchanged, exactly as the previous per-function
    `.get(..., default)` lookups let it. `Tier` is a `StrEnum`, so a known
    tier compares equal to its name either way."""

    effective: str
    """The concrete rung this behaves as. Identity for real rungs and for an
    unrecognised tier; `auto` resolves to the ladder's first rung."""

    protected: bool
    no_runtime: bool
    interact_profile: str
    proxy_tier: str | None
    escalation: tuple[str, ...]

    @property
    def warmup(self) -> bool:
        """Protected rungs run the human pre-read routine after navigation."""
        return self.protected

    @classmethod
    def for_tier(cls, tier: str | Tier) -> TierPolicy:
        """Resolve a requested tier.

        An unrecognised tier is **not** coerced to a known one. Each lookup
        below falls back exactly as its predecessor did -- identity for the
        effective rung, `False` for the flags, `"default"` for the profile,
        `("stealth",)` for the ladder -- which together mean an unknown tier is
        unprotected and gets no residential proxy. Coercing it to `auto`
        instead would silently make it *protected*, changing behaviour for any
        caller that passes a tier this module has not heard of.
        """

        t = str(tier)
        effective = _LADDER_ENTRY.get(t, t)
        protected = effective in PROTECTED
        return cls(
            tier=t,
            effective=effective,
            protected=protected,
            no_runtime=t in _NO_RUNTIME,
            # From the *requested* tier, not `effective` -- deliberate, see
            # `_INTERACT_PROFILE`. `auto` keeps the fast cadence even though it
            # resolves to the `stealth` rung for fingerprinting purposes.
            interact_profile=_INTERACT_PROFILE.get(t, "default"),
            proxy_tier=_RESIDENTIAL if protected else None,
            escalation=ESCALATION.get(t, (Tier.STEALTH,)),
        )

    def rung(self, tier: str | Tier) -> TierPolicy:
        """The policy for one rung of this tier's escalation ladder."""
        return TierPolicy.for_tier(tier)
