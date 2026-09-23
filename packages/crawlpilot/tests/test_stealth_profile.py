"""Unit tests for `crawlpilot.session.stealth_profile` -- the per-tier stealth
kwargs shared by the scrape and interactive paths. Pure, no browser.

The parity assertions here are the regression guard for the hole this module
closed: `/v1/sessions` and every agent run used to open with none of these
kwargs no matter which tier was requested.
"""

from __future__ import annotations

import pytest

from crawlpilot.session import stealth_profile
from crawlpilot.spi.identity import IdentityRef, ProfileKind
from crawlpilot.spi.proxy import ProxyEndpoint
from crawlpilot.tiers import TierPolicy

IDENTITY = IdentityRef(key="acme/zara.com/s1", kind=ProfileKind.DEFAULT)


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

    assert TierPolicy.for_tier("auto").effective == "stealth"
    assert TierPolicy.for_tier("auto").protected is True

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
        IdentityRef(key="acme/zara.com/s2"), "stealth"
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

    from crawlpilot.spi.driver import BrowserDriver

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
    """`humanize` resolves the profile name `TierPolicy` hands it, so the two can never
    drift; this asserts the delegation is actually wired."""

    from crawlpilot.driver import humanize

    for tier in ("auto", "stealth", "enhanced", "basic", "nonsense"):
        assert humanize.by_name(TierPolicy.for_tier(tier).interact_profile).name == (
            TierPolicy.for_tier(tier).interact_profile
        )


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


def test_the_akamai_handshake_is_not_gated_on_block_detection() -> None:
    """Avoidance and reaction are different questions, and were one flag.

    `_abck` only becomes valid after 2-3 accepted sensor POSTs, which the
    warm-up's scrolls are what produce. Gating the *wait* on `detect_blocks`
    meant the interactive path -- every agent run, every recipe -- did the
    scrolls and then read anyway while the cookie was still unsolved, so
    Akamai answered Access Denied on hm.com and cos.com. The wait belongs to
    the tier that already paid for the warm-up.
    """

    off = stealth_profile.resolve(IDENTITY, "stealth", detect_blocks=False)
    assert off.warmup is True
    # The whole point: reaction off, avoidance still complete.
    assert off.detect_blocks is False
    assert off.wait_abck is True

    # An unprotected tier runs no warm-up, so there is nothing to wait for.
    basic = stealth_profile.resolve(IDENTITY, "basic", detect_blocks=False)
    assert basic.warmup is False
    assert basic.wait_abck is False


def test_the_wait_reaches_the_driver_as_its_own_kwarg() -> None:
    # `as_open_kwargs` is splatted straight into `driver.open`, so a field that
    # does not survive it is a field the driver never sees.
    kwargs = stealth_profile.resolve(IDENTITY, "stealth", detect_blocks=False).as_open_kwargs()
    assert kwargs["wait_abck"] is True
    assert kwargs["detect_blocks"] is False


# --- the claimed Chrome version tracks the real one --------------------------
#
# `DEFAULT_CHROME_VERSION`'s own docstring says it "must track the Chrome
# actually deployed ... a stale pin here while the browser reports a newer build
# is a hard, deterministic bot tell for WAFs that cross-check UA against Client
# Hints". Keeping that true was a human's job, and on the machine this was
# written on it had drifted from 131 to the browser's real 153 -- twenty-two
# majors, in the UA and Sec-CH-UA of every request from every identity.


def test_an_operator_pin_is_honoured(monkeypatch) -> None:
    """An explicitly configured version is a decision, not a default, and must
    not be second-guessed by probing the binary."""

    from crawlpilot.config import FingerprintConfig

    monkeypatch.setenv("CRAWLPILOT_CHROME_VERSION", "120.0.1.2")
    cfg = FingerprintConfig.from_env()

    assert cfg.pinned is True
    assert cfg.resolved_chrome_version("/nonexistent/browser") == "120.0.1.2"


def test_an_unpinned_version_follows_the_binary(monkeypatch) -> None:
    from crawlpilot.config import FingerprintConfig
    from crawlpilot.driver import browser_discovery

    monkeypatch.delenv("CRAWLPILOT_CHROME_VERSION", raising=False)
    monkeypatch.delenv("AGENTPILOT_CHROME_VERSION", raising=False)
    monkeypatch.setattr(browser_discovery, "browser_version", lambda p: "151.0.7922.137")

    cfg = FingerprintConfig.from_env()
    assert cfg.pinned is False
    assert cfg.resolved_chrome_version("/usr/bin/google-chrome") == "151.0.7922.137"


def test_an_unaskable_binary_falls_back_to_the_default(monkeypatch) -> None:
    """A channel launch names a registry entry rather than a path, and a locked
    down container may refuse to exec. Neither is a reason to fail."""

    from crawlpilot.config import DEFAULT_CHROME_VERSION, FingerprintConfig
    from crawlpilot.driver import browser_discovery

    monkeypatch.delenv("CRAWLPILOT_CHROME_VERSION", raising=False)
    monkeypatch.delenv("AGENTPILOT_CHROME_VERSION", raising=False)
    monkeypatch.setattr(browser_discovery, "browser_version", lambda p: None)

    cfg = FingerprintConfig.from_env()
    assert cfg.resolved_chrome_version(None) == DEFAULT_CHROME_VERSION


def test_browser_version_survives_a_binary_that_cannot_be_run() -> None:
    from crawlpilot.driver.browser_discovery import browser_version

    assert browser_version("/definitely/not/a/browser") is None
    assert browser_version(None) is None


# --- negative signals --------------------------------------------------------


def test_webrtc_is_neutered_without_being_removed() -> None:
    """The one patch here that closes a LEAK rather than smoothing a difference.

    WebRTC never goes through an HTTP proxy, so a page that opens a peer
    connection and reads the ICE candidates gets the host's real address --
    undoing the whole per-identity proxy pin. Deleting `RTCPeerConnection` is
    its own tell (every real desktop Chrome has it, and ordinary sites
    feature-detect it), so the constructor stays and the candidates go.
    """

    from crawlpilot.identity.fingerprint import generate

    script = generate("tenant/example.com/slot-0").init_script()
    assert "RTCPeerConnection" in script
    assert "icecandidate" in script
    assert "stopImmediatePropagation" in script
    # Not deleted -- feature detection must still find it.
    assert "delete window.RTCPeerConnection" not in script


def test_no_flag_that_marks_the_browser_as_driven() -> None:
    """Scrapling refuses five flags as automation markers
    (`engines/constants.py::HARMFUL_ARGS`). We were passing one of them for
    tidiness, which is not worth a signal."""

    from crawlpilot.identity.fingerprint import generate

    args = generate("tenant/example.com/slot-0").launch_args()
    harmful = {
        "--enable-automation",
        "--disable-popup-blocking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
    }
    assert not (set(args) & harmful)
    # And still never our own `--disable-blink-features`, which Patchright owns.
    assert not any(a.startswith("--disable-blink-features") for a in args)
