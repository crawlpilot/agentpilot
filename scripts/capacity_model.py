"""Pure capacity-planning model. See docs/capacity-planning.md for the
narrative and the first-pass numbers this reproduces.

`active_render_fraction` is the key unmeasured assumption: not every
concurrently-*held* context is simultaneously mid-render (most of a node's
warm contexts are idle-but-resident at any instant). It's a placeholder until
P1 replaces it with a measured value from real `docker stats` + CDP timing
data (see docs/capacity-planning.md's "Spike findings").
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CapacityInputs:
    throughput_per_hour: float
    hold_time_s: float
    mb_per_context: float
    cpu_per_context: float
    """vCPUs consumed by a context while actively rendering (mid page-load)."""
    mb_per_page: float
    browser_pct: float
    """Fraction of traffic that goes through a full browser context (vs. the
    cheap httpx tier)."""
    proxy_pct: float
    """Fraction of *browser* traffic that uses paid (e.g. residential) proxy
    bandwidth."""
    price_per_gb: float
    node_ram_gb: float = 64.0
    node_vcpu: float = 24.0
    active_render_fraction: float = 0.25
    ha_slack: float = 1.3
    """Headroom multiplier for reaper churn, node loss, and HA capacity."""


@dataclass(frozen=True)
class CapacityEstimate:
    concurrent_contexts: float
    ram_gb_total: float
    nodes_by_ram: float
    nodes_by_cpu: float
    nodes_recommended: int
    bandwidth_gb_per_day: float
    cost_per_day_usd: float
    cost_per_month_usd: float


def estimate(inputs: CapacityInputs) -> CapacityEstimate:
    requests_per_second = inputs.throughput_per_hour / 3600.0
    concurrent_contexts = requests_per_second * inputs.hold_time_s

    ram_gb_total = concurrent_contexts * inputs.mb_per_context / 1024.0
    nodes_by_ram = ram_gb_total / inputs.node_ram_gb

    rendering_vcpu_needed = (
        concurrent_contexts * inputs.active_render_fraction * inputs.cpu_per_context
    )
    nodes_by_cpu = rendering_vcpu_needed / inputs.node_vcpu

    nodes_recommended = math.ceil(max(nodes_by_ram, nodes_by_cpu) * inputs.ha_slack)

    browser_requests_per_day = inputs.throughput_per_hour * 24 * inputs.browser_pct
    bandwidth_gb_per_day = (
        browser_requests_per_day * inputs.mb_per_page / 1024.0 * inputs.proxy_pct
    )
    cost_per_day_usd = bandwidth_gb_per_day * inputs.price_per_gb

    return CapacityEstimate(
        concurrent_contexts=concurrent_contexts,
        ram_gb_total=ram_gb_total,
        nodes_by_ram=nodes_by_ram,
        nodes_by_cpu=nodes_by_cpu,
        nodes_recommended=nodes_recommended,
        bandwidth_gb_per_day=bandwidth_gb_per_day,
        cost_per_day_usd=cost_per_day_usd,
        cost_per_month_usd=cost_per_day_usd * 30,
    )


def _print_estimate(label: str, inputs: CapacityInputs) -> None:
    e = estimate(inputs)
    print(f"--- {label} ---")
    print(f"concurrent contexts:  {e.concurrent_contexts:,.0f}")
    print(f"RAM total:            {e.ram_gb_total:,.0f} GB")
    print(f"nodes (RAM-bound):    {e.nodes_by_ram:,.1f}")
    print(f"nodes (CPU-bound):    {e.nodes_by_cpu:,.1f}")
    print(f"nodes recommended:    {e.nodes_recommended}")
    print(f"bandwidth/day:        {e.bandwidth_gb_per_day:,.0f} GB")
    print(f"cost/day:             ${e.cost_per_day_usd:,.0f}")
    print(f"cost/month:           ${e.cost_per_month_usd:,.0f}")
    print()


if __name__ == "__main__":
    # P0 first-pass estimate: reproduces docs/capacity-planning.md's headline
    # numbers (~5,600 concurrent contexts, ~1.9TB RAM, 40-60 nodes).
    _print_estimate(
        "P0 first-pass (all traffic full-browser+residential, worst case)",
        CapacityInputs(
            throughput_per_hour=2_000_000,
            hold_time_s=10,
            mb_per_context=350,
            cpu_per_context=0.75,
            mb_per_page=2,
            browser_pct=1.0,
            proxy_pct=1.0,
            price_per_gb=0.5,
        ),
    )
    # Illustrative P4 tier-router scenario: only a minority of traffic needs
    # the full browser+residential path once the tier router escalates
    # selectively instead of defaulting every request to it.
    _print_estimate(
        "Illustrative P4 tier-router split (20% full-browser, half of that residential)",
        CapacityInputs(
            throughput_per_hour=2_000_000,
            hold_time_s=10,
            mb_per_context=350,
            cpu_per_context=0.75,
            mb_per_page=2,
            browser_pct=0.2,
            proxy_pct=0.5,
            price_per_gb=0.5,
        ),
    )
