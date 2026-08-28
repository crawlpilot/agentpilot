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

    auto = stealth_profile.resolve(IDENTITY, "auto")
    stealth = stealth_profile.resolve(IDENTITY, "stealth")

    # Identical everywhere the anti-block signal lives...
    assert (auto.user_agent, auto.init_script, auto.extra_http_headers) == (
        stealth.user_agent,
        stealth.init_script,
        stealth.extra_http_headers,
    )
    assert (auto.warmup, auto.detect_blocks) == (stealth.warmup, stealth.detect_blocks)
    # ...and only the per-action cadence differs, which is the one cost that
    # scales with session length rather than being paid once per page load.
    assert auto.interact_profile == "default"
    assert stealth.interact_profile == "stealth"


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


@pytest.mark.parametrize(
    ("tier", "expected"),
    [("auto", "default"), ("stealth", "stealth"), ("enhanced", "stealth")],
)
def test_interaction_cadence_follows_the_requested_tier(tier: str, expected: str) -> None:
    """A long-lived session pays the delay table's inter-action `gap` before
    every click, fill and press for the whole run. Putting the default tier on
    the slow table cost seconds per agent step for a signal that matters most
    during page load -- which the warm-up covers separately."""

    assert stealth_profile.resolve(IDENTITY, tier).interact_profile == expected


def test_humanize_and_spi_agree_on_the_cadence_mapping() -> None:
    """`humanize.for_tier` delegates to the spi mapping so the two can never
    drift; this asserts the delegation is actually wired."""

    from agentpilot.driver import humanize

    for tier in ("auto", "stealth", "enhanced", "basic", "nonsense"):
        assert humanize.for_tier(tier).name == stealth_profile.interact_profile_for_tier(tier)


def test_block_detection_is_opt_out_for_long_lived_sessions() -> None:
    """A scrape has an escalation ladder to answer `ChallengeDetected` with; a
    session does not, so raising mid-run only converts the situation into a
    failed run. Avoidance (fingerprint, warm-up) is kept either way.

    This is the regression guard for a real break: turning detection on for the
    interactive path made every agent-loop, recipe and session-lifecycle
    contract test fail with `ChallengeDetected: empty` on ordinary thin pages.
    """

    on = stealth_profile.resolve(IDENTITY, "stealth")
    off = stealth_profile.resolve(IDENTITY, "stealth", detect_blocks=False)

    assert on.detect_blocks is True
    assert off.detect_blocks is False
    # Everything that helps *avoid* a block is unchanged.
    assert off.warmup is True
    assert (off.user_agent, off.init_script) == (on.user_agent, on.init_script)
