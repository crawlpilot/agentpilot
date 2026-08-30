"""Golden table pinning the *complete* tier surface, as one artifact.

Every other tier test covers one function well (`test_stealth_tier.py`,
`test_stealth_profile.py`, `test_humanize.py`, `test_scrape_tier_guard.py`).
None of them pins the surface as a whole, and that is what this file is for.

It was written as the instrument for the `TierPolicy` consolidation, back when
the tier vocabulary and its consequences were spread across six modules
(`spi/actions.py`, `session/stealth_profile.py`, `session/ephemeral.py`,
`driver/humanize.py`, `identity/proxy_config.py`, `gateway/schemas.py`) and
adding a tier meant editing all of them. That consolidation has landed:
`crawlpilot.tiers.TierPolicy.for_tier()` is now the single owner, and two of
those six modules no longer exist.

The table outlived its occasion, which is why it is still here. What it pins is
no longer "the refactor did not change behaviour" but "this is what each tier
*means*" -- six derived values per tier, in one place, so a change to any of
them is a visible edit to this table rather than a quiet behavioural drift
across the driver, the escalation ladder and the delay profile at once.

Deliberately asserts literal expected values rather than re-deriving them from
the modules under test -- a test that computes its own expectation cannot
detect a change in the computation.
"""

from __future__ import annotations

import pytest

from crawlpilot.driver import humanize
from crawlpilot.tiers import ESCALATION, PROTECTED, TierPolicy

TIERS = ("basic", "stealth", "enhanced", "auto")
"""The complete vocabulary, as declared by `gateway.schemas`. A new tier must
be added here and to every table below, which is the point."""


# --------------------------------------------------------------- the table

EXPECTED: dict[str, dict[str, object]] = {
    "basic": {
        "stealth_from_tier": True,
        "interact_profile": "default",
        "effective_tier": "basic",
        "is_protected": False,
        "delay_policy": "default",
        "escalation": ("basic", "stealth"),
    },
    "stealth": {
        "stealth_from_tier": True,
        "interact_profile": "stealth",
        "effective_tier": "stealth",
        "is_protected": True,
        "delay_policy": "stealth",
        "escalation": ("stealth",),
    },
    "enhanced": {
        "stealth_from_tier": False,
        "interact_profile": "stealth",
        "effective_tier": "enhanced",
        "is_protected": True,
        "delay_policy": "stealth",
        "escalation": ("enhanced",),
    },
    "auto": {
        "stealth_from_tier": False,
        "interact_profile": "default",
        "effective_tier": "stealth",
        "is_protected": True,
        "delay_policy": "default",
        "escalation": ("stealth", "enhanced"),
    },
}


@pytest.mark.parametrize("tier", TIERS)
def test_tier_surface_is_unchanged(tier: str) -> None:
    """One assertion per tier over every tier-derived value in the codebase."""

    p = TierPolicy.for_tier(tier)
    got = {
        "stealth_from_tier": p.no_runtime,
        "interact_profile": p.interact_profile,
        "effective_tier": str(p.effective),
        "is_protected": p.protected,
        # The driver resolves the profile *name* to a delay table itself
        # (`patchright_driver` calls `humanize.by_name(interact_profile)`),
        # so this is the value the driver would actually use.
        "delay_policy": humanize.by_name(p.interact_profile).name,
        "escalation": tuple(str(t) for t in p.escalation),
    }
    assert got == EXPECTED[tier], f"tier {tier!r} surface changed"


def test_the_table_covers_every_tier_the_api_accepts() -> None:
    """A tier added to the wire schema but not here would silently go
    uncharacterized, which would defeat the purpose of this file."""

    from agentpilot.gateway.schemas import ScrapeRequest

    declared = ScrapeRequest.model_fields["tier"].annotation
    assert set(getattr(declared, "__args__", ())) == set(TIERS)


def test_protected_tiers_membership_is_pinned() -> None:
    assert {str(t) for t in PROTECTED} == {"stealth", "enhanced"}


def test_only_auto_is_a_ladder_alias() -> None:
    """`effective_tier` is identity for every real rung; `auto` alone maps."""

    for tier in TIERS:
        effective = str(TierPolicy.for_tier(tier).effective)
        if tier == "auto":
            assert effective != tier
        else:
            assert effective == tier


def test_escalation_rungs_are_themselves_real_tiers() -> None:
    """The ladder may never escalate to a tier that does not exist -- a typo
    here would silently fall through to `_ESCALATION.get(tier, ("stealth",))`
    at the ephemeral call site."""

    for tier, ladder in ESCALATION.items():
        assert str(tier) in TIERS
        for rung in ladder:
            assert str(rung) in TIERS, f"{tier} escalates to unknown tier {rung!r}"


def test_humanize_resolves_the_profile_name_the_policy_hands_it() -> None:
    """`humanize` must stay a *name -> table* lookup with no tier knowledge of
    its own; `TierPolicy` owns tier -> name. Phase 1 removed `humanize.for_tier`
    precisely because it had no production caller -- the driver already receives
    a profile name through `open(interact_profile=...)`."""

    for tier in TIERS:
        policy = TierPolicy.for_tier(tier)
        assert humanize.by_name(policy.interact_profile).name == policy.interact_profile
