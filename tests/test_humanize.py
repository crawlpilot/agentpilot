"""Unit tests for `agentpilot.driver.humanize` -- the ported InteractSettings
delay policy. Pure, no browser."""

from __future__ import annotations

import agentpilot.driver.humanize as humanize
from agentpilot.driver.humanize import DelayPolicy


def test_stealth_is_slower_than_fast_on_every_action() -> None:
    for action in humanize.STEALTH.ranges:
        s_lo, s_hi = humanize.STEALTH.ranges[action]
        f_lo, f_hi = humanize.FAST.ranges[action]
        assert s_lo >= f_lo and s_hi >= f_hi, action


def test_sample_respects_global_clamp() -> None:
    for _ in range(500):
        v = humanize.STEALTH.sample("dragAndDrop")
        assert humanize.MIN_DELAY_MS <= v <= humanize.MAX_DELAY_MS


def test_sample_within_preset_range() -> None:
    lo, hi = humanize.DEFAULT.ranges["type"]
    for _ in range(200):
        assert lo <= humanize.DEFAULT.sample("type") <= hi


def test_unknown_action_falls_back_to_default_range() -> None:
    lo, hi = humanize.DEFAULT.ranges["default"]
    for _ in range(100):
        assert lo <= humanize.DEFAULT.sample("no_such_action") <= hi


def test_malformed_range_uses_safe_fallback() -> None:
    bad = DelayPolicy("bad", {"default": (0, 5), "click": (10, 99_999)})
    for _ in range(100):
        # (0,5): lo<=0 -> fallback 500..1000; (10,99999): hi>10000 -> fallback.
        assert 500 <= bad.sample("default") <= 1_000
        assert 500 <= bad.sample("click") <= 1_000


def test_for_tier_mapping() -> None:
    assert humanize.for_tier("stealth") is humanize.STEALTH
    assert humanize.for_tier("enhanced") is humanize.STEALTH
    assert humanize.for_tier("auto") is humanize.DEFAULT
    assert humanize.for_tier("nonsense") is humanize.DEFAULT


def test_by_name_mapping() -> None:
    assert humanize.by_name("stealth") is humanize.STEALTH
    assert humanize.by_name("fast") is humanize.FAST
    assert humanize.by_name("nope") is humanize.DEFAULT
