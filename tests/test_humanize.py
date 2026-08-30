"""Unit tests for `crawlpilot.driver.humanize` -- the ported InteractSettings
delay policy. Pure, no browser."""

from __future__ import annotations

import crawlpilot.driver.humanize as humanize
from crawlpilot.driver.humanize import DelayPolicy
from crawlpilot.tiers import TierPolicy


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


def test_tier_to_delay_table_mapping() -> None:
    """`humanize.for_tier` was removed in the Phase 1 tier consolidation -- it
    had no production caller (the driver receives a profile *name* via
    `open(interact_profile=...)` and calls `by_name`). The mapping it expressed
    is preserved here, now routed through its single owner."""

    def table(tier: str) -> humanize.DelayPolicy:
        return humanize.by_name(TierPolicy.for_tier(tier).interact_profile)

    assert table("stealth") is humanize.STEALTH
    assert table("enhanced") is humanize.STEALTH
    assert table("auto") is humanize.DEFAULT
    assert table("nonsense") is humanize.DEFAULT


def test_by_name_mapping() -> None:
    assert humanize.by_name("stealth") is humanize.STEALTH
    assert humanize.by_name("fast") is humanize.FAST
    assert humanize.by_name("nope") is humanize.DEFAULT


def test_sample_is_right_skewed_not_uniform() -> None:
    """Human latencies cluster low with an occasional long pause. A uniform
    draw has a flat histogram, which is itself learnable by keystroke-timing
    telemetry -- the delay is randomised but its *distribution* is not human.
    Both source projects sample uniformly; this asserts we do not.
    """

    import statistics

    lo, hi = humanize.STEALTH.ranges["type"]
    samples = [humanize.STEALTH.sample("type") for _ in range(20_000)]

    assert lo <= min(samples) and max(samples) <= hi, "escaped the declared range"
    # A uniform draw would put the median at the midpoint; a right-skewed one
    # sits clearly below it.
    assert statistics.median(samples) < (lo + hi) / 2
    assert statistics.mean(samples) > statistics.median(samples), "not right-skewed"


def test_sample_never_leaves_the_declared_range_for_any_action() -> None:
    for policy in (humanize.DEFAULT, humanize.FAST, humanize.STEALTH):
        for action, (lo, hi) in policy.ranges.items():
            for _ in range(200):
                v = policy.sample(action)
                assert lo <= v <= hi, f"{policy.name}.{action} produced {v}"


def test_humanize_holds_no_tier_knowledge_of_its_own() -> None:
    """After the Phase 1 consolidation `humanize` is a *name -> table* lookup
    and nothing more: `TierPolicy` owns tier -> name. Asserting the absence is
    the point -- a reintroduced `for_tier` here would re-split the mapping
    across two owners, which is the defect the consolidation removed."""

    assert not hasattr(humanize, "for_tier")
    for tier in ("basic", "stealth", "enhanced", "auto"):
        name = TierPolicy.for_tier(tier).interact_profile
        assert humanize.by_name(name).name == name
