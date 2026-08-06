"""Wave 0 instrumentation unit tests: per-context health arithmetic and the
agent-loop / context metric objects. Pure -- no browser, no network."""

from __future__ import annotations

from agentpilot.driver.patchright_driver import _ContextHealth
from agentpilot.observability import metrics


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


def test_wave0_metrics_exist_and_increment() -> None:
    # Labeled counters: incrementing must not raise and must advance the sample.
    before = metrics.agent_steps_total.labels(outcome="ok")._value.get()
    metrics.agent_steps_total.labels(outcome="ok").inc()
    assert metrics.agent_steps_total.labels(outcome="ok")._value.get() == before + 1

    before_ctx = metrics.context_task_outcomes_total.labels(outcome="success")._value.get()
    metrics.context_task_outcomes_total.labels(outcome="success").inc()
    assert (
        metrics.context_task_outcomes_total.labels(outcome="success")._value.get()
        == before_ctx + 1
    )

    # Unlabeled counter + histogram observe path.
    metrics.agent_loop_nudges_total.inc()
    metrics.context_tasks_total.inc()
    metrics.agent_step_llm_latency_seconds.observe(0.01)
