"""The platform's Prometheus metric objects: labelled counters and the histogram
observe path. Pure -- no browser, no network.

The `_ContextHealth` arithmetic this file also used to cover now lives in
`packages/crawlpilot/tests/test_context_health.py`, beside the type itself:
reaching across the package boundary for another project's private symbol left
the standalone wheel shipping that arithmetic untested.
"""

from __future__ import annotations

from agentpilot.observability import metrics


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
