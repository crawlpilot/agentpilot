"""Unit tests for `agentpilot.identity.fingerprint` -- deterministic, coherent
per-identity fingerprints. Pure, no browser."""

from __future__ import annotations

from agentpilot.identity import fingerprint as fp


def test_generate_is_deterministic() -> None:
    a = fp.generate("tenant:zara.com:alice")
    b = fp.generate("tenant:zara.com:alice")
    assert a == b


def test_bundle_is_internally_consistent() -> None:
    # Every parameter block must come from the SAME device preset -- the whole
    # point (a mixed bundle is a stronger bot tell than no spoof).
    f = fp.generate("some:identity:slug")
    presets = {
        (p.screen, p.hardware, p.webgl, p.geo) for p in fp._PRESETS  # noqa: SLF001
    }
    assert (f.screen, f.hardware, f.webgl, f.geo) in presets


def test_region_selects_matching_geo() -> None:
    assert fp.generate("x", region="IN").geo.timezone_id == "Asia/Kolkata"
    assert fp.generate("x", region="US").geo.timezone_id == "America/New_York"
    assert fp.generate("x", region="GB").geo.timezone_id == "Europe/London"
    assert fp.generate("x", region="uk").geo.timezone_id == "Europe/London"


def test_context_kwargs_match_fingerprint() -> None:
    f = fp.generate("x", region="US")
    kw = f.context_kwargs()
    assert kw["user_agent"] == f.user_agent
    assert kw["locale"] == f.geo.locale == "en-US"
    assert kw["timezone_id"] == f.geo.timezone_id == "America/New_York"


def test_init_script_embeds_pinned_values() -> None:
    f = fp.generate("x", region="US")
    script = f.init_script()
    assert str(f.hardware.hardware_concurrency) in script
    assert f.webgl.unmasked_renderer in script
    assert "[native code]" in script  # the toString-native guard is present
    assert "37445" in script and "37446" in script  # UNMASKED_VENDOR/RENDERER_WEBGL


def test_canvas_seed_is_stable_and_sized() -> None:
    f = fp.generate("x")
    assert len(f.canvas_seed) == 16
    assert fp.generate("x").canvas_seed == f.canvas_seed


def test_ua_string_and_client_hints_share_one_chrome_major() -> None:
    # The regression under fix: the UA header, the Sec-CH-UA headers, and
    # navigator.userAgentData must all name the SAME Chrome build -- a mismatch
    # (stale UA vs the real, newer Chrome the header would carry) is a hard
    # Akamai bot tell.
    f = fp.generate("x", region="US")
    major = f.chrome_major

    assert f"Chrome/{major}.0.0.0" in f.user_agent  # UA freezes minor/build/patch
    headers = f.client_hint_headers()
    assert f'"Google Chrome";v="{major}"' in headers["sec-ch-ua"]
    assert f'"Chromium";v="{major}"' in headers["sec-ch-ua"]
    # init_script (navigator.userAgentData) carries the same major + full version.
    script = f.init_script()
    assert f'"version": "{major}"' in script
    assert f.chrome_full_version in script


def test_client_hint_platform_matches_device_family() -> None:
    # Sec-CH-UA-Platform is the CH token ("Windows"/"macOS"/"Linux"), distinct
    # from navigator.platform -- and it must agree with the pinned device.
    assert fp.generate("x", region="US").client_hint_headers()["sec-ch-ua-platform"] == '"Windows"'
    assert fp.generate("x", region="GB").client_hint_headers()["sec-ch-ua-platform"] == '"macOS"'
    assert fp.generate("x", region="IN").client_hint_headers()["sec-ch-ua-platform"] == '"Linux"'


def test_client_hint_headers_are_the_always_sent_low_entropy_trio() -> None:
    headers = fp.generate("x").client_hint_headers()
    assert set(headers) == {"sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}
    assert headers["sec-ch-ua-mobile"] == "?0"


def test_apple_silicon_reports_arm_architecture() -> None:
    # Consistency contract: the M1 (macOS) family must report arm, not x86.
    assert fp.generate("x", region="GB").ch_architecture == "arm"
    assert fp.generate("x", region="US").ch_architecture == "x86"
    assert '"architecture": "arm"' in fp.generate("x", region="GB").init_script()
