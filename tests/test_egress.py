"""`agentpilot.egress.policy` unit tests. Safe to run anywhere: without `iptables`
on PATH (macOS dev, most CI runners) it must no-op with a warning rather than
raise or touch the host network stack."""

from __future__ import annotations

import shutil

import pytest

from agentpilot.egress.policy import (
    _allow_cidrs,
    _deny_ranges,
    _llm_endpoint_host,
    _resolve_host_to_cidrs,
    apply_baseline,
)
from agentpilot.spi.egress import EgressPolicy


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


def test_resolve_host_to_cidrs_ipv4_literal() -> None:
    assert _resolve_host_to_cidrs("192.168.65.254") == ["192.168.65.254/32"]


def test_resolve_host_to_cidrs_drops_ipv6_literal() -> None:
    # baseline is IPv4-iptables only -- IPv6 is neither blocked nor exempted
    assert _resolve_host_to_cidrs("fdc4:f303:9324::254") == []


def test_resolve_host_to_cidrs_unresolvable_is_empty() -> None:
    assert _resolve_host_to_cidrs("no-such-host.invalid") == []


def test_llm_endpoint_host_parsed_from_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPILOT_LLM_BASE_URL", "http://host.docker.internal:11434/v1")
    assert _llm_endpoint_host() == "host.docker.internal"


def test_llm_endpoint_host_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTPILOT_LLM_BASE_URL", raising=False)
    assert _llm_endpoint_host() is None


def test_allow_cidrs_exempts_llm_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # An RFC1918 LLM host (literal IP) must land in the ACCEPT list so the
    # container-wide private-range REJECT can't sever the worker's LLM call.
    monkeypatch.setenv("AGENTPILOT_LLM_BASE_URL", "http://192.168.65.254:11434/v1")
    assert "192.168.65.254/32" in _allow_cidrs(EgressPolicy())


def test_allow_cidrs_includes_operator_allow_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTPILOT_LLM_BASE_URL", raising=False)
    assert "203.0.113.9/32" in _allow_cidrs(EgressPolicy(allow_hosts=("203.0.113.9",)))


@pytest.mark.skipif(shutil.which("iptables") is not None, reason="iptables present")
def test_apply_baseline_noops_without_iptables() -> None:
    apply_baseline(EgressPolicy())  # must not raise
