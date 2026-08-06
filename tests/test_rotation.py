"""Unit tests for `agentpilot.session.rotation` -- the retire policy and its
thresholds. Pure -- no browser, no registry."""

from __future__ import annotations

from agentpilot.session.rotation import (
    RotationConfig,
    RotationPolicy,
    RotationThresholds,
    should_retire,
)
from agentpilot.spi.health import ContextHealth


def _health(*, tasks=0, successes=0, failures=0, leak_warnings=0) -> ContextHealth:
    return ContextHealth(
        tasks=tasks,
        successes=successes,
        failures=failures,
        small_pages=0,
        leak_warnings=leak_warnings,
    )


def test_leak_warnings_trip_immediately_at_cap() -> None:
    th = RotationThresholds(max_leak_warnings=8)
    assert not should_retire(_health(leak_warnings=7), th)
    assert should_retire(_health(leak_warnings=8), th)


def test_failure_rate_needs_enough_tasks() -> None:
    th = RotationThresholds(failure_rate_threshold=0.6, min_tasks_for_rate=5)
    # 3/4 failures is a high rate but too few tasks to trust.
    assert not should_retire(_health(tasks=4, failures=3), th)
    # 3/5 = 0.6 over enough tasks -> retire.
    assert should_retire(_health(tasks=5, failures=3), th)


def test_healthy_context_is_not_retired() -> None:
    th = RotationThresholds()
    assert not should_retire(_health(tasks=20, successes=19, failures=1, leak_warnings=1), th)


def test_rotation_policy_parse_is_lenient_and_defaults_restart() -> None:
    assert RotationPolicy.parse("fresh") is RotationPolicy.FRESH
    assert RotationPolicy.parse("RESTART") is RotationPolicy.RESTART
    assert RotationPolicy.parse(None) is RotationPolicy.RESTART
    assert RotationPolicy.parse("garbage") is RotationPolicy.RESTART


def test_rotation_config_defaults_disabled_restart() -> None:
    cfg = RotationConfig()
    assert cfg.enabled is False
    assert cfg.policy is RotationPolicy.RESTART
