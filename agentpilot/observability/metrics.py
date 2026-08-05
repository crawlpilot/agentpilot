"""Every Prometheus metric in the fleet, in one place, per `plan.md`'s
Observability section:

- Pool: `contexts_active`/`contexts_idle` (gauges, set from a `Registry`
  snapshot -- see `gateway/routes/health.py`).
- Latency: `session_open_duration_seconds`, `execute_duration_seconds`
  histograms (per-tier `execute` histograms and placement/vault-restore
  latency are P2 concepts -- no tiers or vault exist yet to measure).
- Health: `error_responses_total` (labeled by `ErrorCode`, so 503/409 rates
  and `NODE_LOST` count are all just label filters on one counter, not
  separate metrics), `reaper_destroyed_total` (labeled by reason),
  `reaper_lease_reclaimed_total`.
- Per-tenant: `requests_total` labeled by tenant + route.

Challenge-detection/identity-burn rate are still skipped here -- P3's
challenge detector doesn't exist yet to feed them; adding an empty/always-0
counter now would just be metrics theatre. Placement metrics (below) are
real as of this pass: multi-worker placement/routing/admission.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

contexts_active = Gauge("agentpilot_contexts_active", "Number of ACTIVE browser contexts")
contexts_idle = Gauge("agentpilot_contexts_idle", "Number of IDLE browser contexts")

session_open_duration_seconds = Histogram(
    "agentpilot_session_open_duration_seconds", "POST /v1/sessions latency"
)
execute_duration_seconds = Histogram(
    "agentpilot_execute_duration_seconds", "POST /v1/sessions/{id}/execute latency"
)
scrape_duration_seconds = Histogram(
    "agentpilot_scrape_duration_seconds", "POST /v1/scrape latency (ephemeral open+execute+evict)"
)

error_responses_total = Counter(
    "agentpilot_error_responses_total", "Gateway error responses by typed code", ["code"]
)
requests_total = Counter(
    "agentpilot_requests_total", "Gateway requests by tenant and route", ["tenant", "route"]
)

reaper_destroyed_total = Counter(
    "agentpilot_reaper_destroyed_total", "Contexts destroyed by the reaper, by reason", ["reason"]
)
reaper_lease_reclaimed_total = Counter(
    "agentpilot_reaper_lease_reclaimed_total", "ACTIVE leases force-released for expiring unrenewed"
)

placement_decisions_total = Counter(
    "agentpilot_placement_decisions_total",
    "Gateway session-placement decisions by outcome",
    ["outcome"],  # affinity_hit | relocated | least_loaded | no_capacity
)
node_reaper_nodes_reaped_total = Counter(
    "agentpilot_node_reaper_nodes_reaped_total", "Dead worker nodes cleaned up by the node-reaper"
)
node_reaper_sessions_reclaimed_total = Counter(
    "agentpilot_node_reaper_sessions_reclaimed_total",
    "Sessions force-evicted because their worker node died",
)

# --- Agent loop (Wave 0 instrumentation) -------------------------------------
# Emitted directly from `agentpilot.agent.loop.run_agent_loop`. `observability`
# is not a layer in any import-linter contract and only depends on
# `prometheus_client`, so importing it from the agent loop adds no cycle.
agent_steps_total = Counter(
    "agentpilot_agent_steps_total",
    "Agent-loop steps executed, by outcome",
    ["outcome"],  # ok | action_failed | sequence_aborted | observe_error | llm_error
)
agent_step_llm_latency_seconds = Histogram(
    "agentpilot_agent_step_llm_latency_seconds", "Per-step agent LLM call latency"
)
agent_runs_total = Counter(
    "agentpilot_agent_runs_total",
    "Agent runs finished, by outcome",
    ["outcome"],  # success | failed | exhausted
)
agent_loop_nudges_total = Counter(
    "agentpilot_agent_loop_nudges_total", "Loop-detector nudges surfaced to the agent"
)
agent_judge_verdicts_total = Counter(
    "agentpilot_agent_judge_verdicts_total",
    "Independent completion-judge verdicts on self-reported successes",
    ["verdict"],  # passed | rejected | error
)

# --- Per-context health (Wave 0 instrumentation; feeds later leak-driven
# rotation) -------------------------------------------------------------------
# In-memory counts live on the driver's `_Context.health`; these process-global
# counters give fleet-wide visibility. `small_pages`/`leak_warnings` are tracked
# on `_ContextHealth` but deliberately have no metric yet -- no detector feeds
# them, and an always-zero counter would be metrics theatre (see module note).
context_tasks_total = Counter(
    "agentpilot_context_tasks_total", "Driver execute() batches dispatched (one per context task)"
)
context_task_outcomes_total = Counter(
    "agentpilot_context_task_outcomes_total",
    "Driver execute() batch outcomes",
    ["outcome"],  # success | failure
)
context_leak_warnings_total = Counter(
    "agentpilot_context_leak_warnings_total",
    "Bot-detection / block signals observed per context (feeds leak-driven rotation)",
    ["reason"],  # http_403 | http_429
)
context_rotations_total = Counter(
    "agentpilot_context_rotations_total",
    "Contexts retired + rotated due to degraded health, by policy",
    ["policy"],  # restart | fresh
)
