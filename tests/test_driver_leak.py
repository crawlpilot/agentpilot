"""Unit tests for the driver's navigation leak classifier and the Wave 2
result/metric surfaces. Pure -- no browser (imports the module only)."""

from __future__ import annotations

from agentpilot.driver.patchright_driver import _ContextHealth, _navigation_leak_reason
from agentpilot.observability import metrics
from agentpilot.spi.actions import ActionResult


def test_navigation_leak_reason_flags_block_statuses() -> None:
    assert _navigation_leak_reason(403) == "http_403"
    assert _navigation_leak_reason(429) == "http_429"
    assert _navigation_leak_reason(200) is None
    assert _navigation_leak_reason(None) is None
    assert _navigation_leak_reason(404) is None  # not-found isn't a bot signal


def test_action_result_has_verifications_list() -> None:
    assert ActionResult().verifications == []


def test_context_health_tracks_leak_warnings_field() -> None:
    h = _ContextHealth()
    assert h.leak_warnings == 0
    h.leak_warnings += 1
    assert h.leak_warnings == 1


def test_leak_metric_exists_and_increments() -> None:
    before = metrics.context_leak_warnings_total.labels(reason="http_429")._value.get()
    metrics.context_leak_warnings_total.labels(reason="http_429").inc()
    assert metrics.context_leak_warnings_total.labels(reason="http_429")._value.get() == before + 1
