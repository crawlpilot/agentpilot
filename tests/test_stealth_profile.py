"""Unit tests for `agentpilot.session.stealth_profile` -- the per-tier stealth
kwargs shared by the scrape and interactive paths. Pure, no browser.

The parity assertions here are the regression guard for the hole this module
closed: `/v1/sessions` and every agent run used to open with none of these
kwargs no matter which tier was requested.
"""

from __future__ import annotations

import pytest

from agentpilot.session import stealth_profile
from agentpilot.spi.identity import IdentityKey, ProfileKind
from agentpilot.spi.proxy import ProxyEndpoint

IDENTITY = IdentityKey(tenant="acme", domain="zara.com", name="s1", kind=ProfileKind.DEFAULT)


@pytest.mark.parametrize("tier", ["stealth", "enhanced", "auto"])
def test_protected_tiers_get_the_full_stealth_kwargs(tier: str) -> None:
    profile = stealth_profile.resolve(IDENTITY, tier)

    assert profile.warmup is True
    assert profile.detect_blocks is True
    assert profile.interact_profile == "stealth"
    assert profile.user_agent
    assert profile.init_script
    assert profile.extra_http_headers
    assert profile.extra_launch_args
    assert profile.locale and profile.timezone_id


@pytest.mark.parametrize("tier", ["basic", "unknown"])
def test_unprotected_tiers_get_nothing_but_the_callers_own_values(tier: str) -> None:
    """Wiring this helper into the interactive path must not change behaviour
    for the tiers that never had stealth."""

    profile = stealth_profile.resolve(IDENTITY, tier, locale="fr-FR", timezone_id="Europe/Paris")

    assert profile == stealth_profile.StealthProfile(
        locale="fr-FR", timezone_id="Europe/Paris"
    )


def test_auto_resolves_to_the_ladders_first_rung() -> None:
    """`auto` is the default tier for agent runs. It is not itself a rung, and
    a long-lived session has no escalation ladder to resolve it through -- so
    without this it would silently mean "no stealth"."""

    assert stealth_profile.effective_tier("auto") == "stealth"
    assert stealth_profile.is_protected("auto") is True
    assert stealth_profile.resolve(IDENTITY, "auto") == stealth_profile.resolve(
        IDENTITY, "stealth"
    )


def test_explicit_locale_and_timezone_win_over_the_fingerprints() -> None:
    profile = stealth_profile.resolve(
        IDENTITY, "stealth", locale="fr-FR", timezone_id="Europe/Paris"
    )

    assert profile.locale == "fr-FR"
    assert profile.timezone_id == "Europe/Paris"


def test_fingerprint_is_pinned_per_identity() -> None:
    a = stealth_profile.resolve(IDENTITY, "stealth")
    b = stealth_profile.resolve(IDENTITY, "stealth")
    other = stealth_profile.resolve(
        IdentityKey(tenant="acme", domain="zara.com", name="s2"), "stealth"
    )

    assert a == b
    assert a.user_agent is not None
    # Different identities need not differ (the preset pool is small), but the
    # same identity must never drift -- that is what "pinned for life" means.
    assert other.user_agent is not None


def test_proxy_country_seeds_the_geo() -> None:
    """A proxy that declares its exit country must align the pinned
    timezone/locale with the egress, or the two contradict each other."""

    proxy = ProxyEndpoint(scheme="http", host="gw", port=8000, country="IN")

    profile = stealth_profile.resolve(IDENTITY, "stealth", proxy=proxy)

    assert profile.timezone_id == "Asia/Kolkata"
    assert profile.locale == "en-IN"


def test_as_open_kwargs_matches_the_driver_open_signature() -> None:
    """The kwargs are splatted straight into `BrowserDriver.open()`; a rename
    on either side must fail here rather than at runtime."""

    import inspect

    from agentpilot.spi.driver import BrowserDriver

    accepted = set(inspect.signature(BrowserDriver.open).parameters)
    assert set(stealth_profile.resolve(IDENTITY, "stealth").as_open_kwargs()) <= accepted
