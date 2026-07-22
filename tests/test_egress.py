"""`baas.egress.policy` unit tests. Safe to run anywhere: without `iptables`
on PATH (macOS dev, most CI runners) it must no-op with a warning rather than
raise or touch the host network stack."""

from __future__ import annotations

import shutil

import pytest

from baas.egress.policy import _deny_ranges, apply_baseline
from baas.spi.egress import EgressPolicy


def test_deny_ranges_includes_metadata_and_private_by_default() -> None:
    ranges = _deny_ranges(EgressPolicy())
    assert "169.254.0.0/16" in ranges
    assert "10.0.0.0/8" in ranges
    assert "172.16.0.0/12" in ranges
    assert "192.168.0.0/16" in ranges


def test_deny_ranges_respects_disabled_flags() -> None:
    ranges = _deny_ranges(EgressPolicy(block_metadata=False, block_private=False))
    assert ranges == []


def test_deny_ranges_includes_custom_deny_hosts() -> None:
    ranges = _deny_ranges(EgressPolicy(deny_hosts=("203.0.113.0/24",)))
    assert "203.0.113.0/24" in ranges


@pytest.mark.skipif(shutil.which("iptables") is not None, reason="iptables present")
def test_apply_baseline_noops_without_iptables() -> None:
    apply_baseline(EgressPolicy())  # must not raise
