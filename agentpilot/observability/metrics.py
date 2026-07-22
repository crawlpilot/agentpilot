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

`placement_affinity_hit_ratio` and challenge-detection/identity-burn rate
are skipped here -- they're meaningless before P2's affinity routing and
P3's challenge detector exist to feed them; adding empty/always-1.0 gauges
now would just be metrics theatre.
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
