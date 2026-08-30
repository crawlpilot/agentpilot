"""`_ContextHealth` arithmetic -- the per-context success/failure accounting the
rotation policy reads. Pure: no browser, no network, no Chrome binary.

Lives in crawlpilot's own suite because the type does. It was previously tested
from agentpilot's `test_wave0_instrumentation.py`, which reached across the
package boundary for a private symbol -- so the standalone wheel shipped this
arithmetic with no test of its own, and the rates that gate context rotation
were covered only when the platform's suite ran.
"""

from __future__ import annotations

from crawlpilot.driver.patchright_driver import _ContextHealth


def test_context_health_defaults_are_zero() -> None:
    h = _ContextHealth()
    assert (h.tasks, h.successes, h.failures, h.small_pages, h.leak_warnings) == (0, 0, 0, 0, 0)
    # No tasks yet -> rates are a safe 0.0, never a ZeroDivisionError.
    assert h.failure_rate == 0.0
    assert h.success_rate == 0.0


def test_context_health_rates() -> None:
    h = _ContextHealth(tasks=10, successes=7, failures=3)
    assert h.success_rate == 0.7
    assert h.failure_rate == 0.3
