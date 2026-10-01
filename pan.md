Ready for review
Select text to add comments on the plan
AgentPilot Browser Foundation Layer — Build Plan
Context
AgentPilot (baas-crawlpilot) must become a Kubernetes-native browser session platform: any caller claims a warm, isolated, stealth-ready browser in under 1 s, drives it over CDP or MCP through one gateway, and every session is metered, observable and recycled. The reference plan supplies the target. This plan maps it onto the code that exists, so we extend what works rather than rebuilding it.

Decisions taken (from the user):

Density is set per session class. Crawl keeps multi-context pods; API and agentic get one browser per pod.
Build on kind/k3d locally first, then move to EKS.
MCP is served by the gateway.
v1 serves internal and external tenants, so quotas, metering and CDP policy are in v1 scope.
What already exists (verified in code): extend it, do not rebuild
Capability	Today	Where
Browser library, separable	crawlpilot 0.2 (facade, tools registry, extensions, tiers, policy seams), CI proves it standalone	packages/crawlpilot/src/crawlpilot/
Gateway/worker role split	AGENTPILOT_ROLE=gateway|worker; stateless gateway; worker /internal/*	gateway/role.py, gateway/app.py:86-127
Dynamic placement + admission	Affinity → least-loaded, 409/503, one-retry on half-dead node	placement/placer.py, placement/lua/place_session.lua
Node registry + failure reaping	2 s heartbeat, 10 s TTL capacity:{node}, leader-elected reaper	placement/node_registry.py, placement/node_reaper.py
Leases, ≤1 ACTIVE per identity	Two-phase Redis Lua (acquire/bind/renew/release/evict)	control/redis_registry.py, control/lua/*
Context warm pool	Per-process, per-proxy-tier, off by default, scrape-only	crawlpilot/session/warm_pool.py, wiring.py:528
Raw CDP to clients	WS /v1/sessions/{id}/cdp + json/version rewrite (opt-in enable_cdp)	routes/cdp_proxy.py, routes/cdp.py
Live view	CDP screencast JPEG + interact input over WS	crawlpilot/driver/live_view.py, routes/live_view*.py
Profiles	Local profile dir + Fernet vault (cookies/localStorage), local disk only	crawlpilot/identity/{profile_store,vault}.py
Proxy pinning/health/burn	Sticky hash pin, retire caps, burn scoring (StateStore seam)	crawlpilot/identity/*, control/proxy_config.py
Metrics	Prometheus via injected Recorder	observability/metrics.py, crawlpilot/metrics.py
Job queues	Postgres FOR UPDATE SKIP LOCKED (crawl_tasks, agent_runs, recipe_runs)	jobs/*_store.py
SDK	agentpilot-client (RemoteSession ⊂ SessionVerbs)	packages/agentpilot-client/
Stealth regression	Detection page (webdriver, Runtime.enable timing, coords)	tests/fixtures/detection_page/
Resolved "to confirm" rows and real gaps
MCP: only a schema adapter exists (crawlpilot/tools/adapters.py::to_mcp). There is no server.
Playwright attach: works through connectOverCDP on the gateway CDP URL when enable_cdp=true.
Persistence: local disk only, and affinity pins identities to nodes. There is no object storage.
Missing pieces: claim API; session record (Postgres has no sessions/usage table); lifecycle states (RETIRING/RETIRED are unused; the route state is hard-coded "active"); detach/keepalive; close reasons (metric labels only); metering; tenant quotas (the planned rate_governor was never built); replay; k8s/Helm/KEDA; CA-bundle handling in images.
Latent bugs to fix first:
(a) A CDP-only client's session:{id} route can expire while the worker lease is still renewed (cdp_proxy.py never refreshes it).
(b) Worker session state is an in-process dict (routes/sessions.py:114), so a restart loses every session silently.
(c) WarmPool contexts bypass slot/admission accounting.
(d) Recipe and agent loops open browsers in-process, bypassing the placer.
Architecture principles (for modularity, scale and contributors)
Two-project boundary stays. crawlpilot is the browser library and has no tenancy, no k8s and no Redis. Everything in this plan lives in agentpilot or in new deploy artifacts. tests/test_project_boundary.py keeps guarding it.
Every infrastructure dependency sits behind a Protocol with an in-memory fake, following the pattern of policy.StateStore and RegistryProtocol. Kubernetes, agent-sandbox, S3 and KEDA are adapters. Compose keeps working through the non-k8s adapter, so contributors can run everything without a cluster.
One owner per state. The SessionController is the only writer of session state transitions. Gateway, workers and reapers request transitions. Kubernetes owns pod placement; Postgres owns session history and billing; Redis owns hot routing.
Contract suites per seam. Each Protocol gets a reusable pytest contract suite, following tests/driver_contract/, run against every implementation (fake, Redis/compose, k8s). New backends are accepted when they pass it.
Import-linter contracts land before the code (ground rule from the rearchitecture plan). New layer: gateway → sessions → {pool, metering, quota, profiles} → control.
ADRs in docs/adr/NNNN-*.md for each borrow-vs-build decision, so contributors see why.
Target module layout (new agentpilot packages)
packages/agentpilot/agentpilot/
├── sessions/            # the domain core — pure, no FastAPI, no k8s
│   ├── model.py         # SessionRecord, SessionState, CloseReason, SessionSpec
│   ├── machine.py       # allowed transitions table + guard (single source of truth)
│   ├── controller.py    # SessionController: claim/attach/detach/keepalive/release/drain
│   ├── store.py         # SessionStore Protocol + Postgres impl + InMemory fake
│   └── events.py        # SessionEvent (connect/detach/close/usage) + EventSink Protocol
├── classes/             # SessionClass registry: crawl | api | agentic (+ config-loaded)
├── pool/                # PoolClient Protocol — the ONLY way to get a browser endpoint
│   ├── protocol.py      # claim(spec) -> Endpoint, release(), ready_count(class, zone)
│   ├── node_pool.py     # adapter over existing SessionPlacer + worker HTTP (compose, crawl class)
│   └── k8s/             # agent-sandbox adapter (SandboxTemplate/WarmPool/Claim), pinned version
├── quota/               # per-tenant limits: concurrent sessions, CDP msg/s, session-hours/day (Redis Lua)
├── metering/            # usage aggregation from SessionEvents → usage_records
├── profiles/            # ProfileSnapshotStore Protocol: local-dir impl + S3 impl; /v1/profiles
├── mcp/                 # MCP server over crawlpilot.tools registry → execute() batches
├── replay/              # rrweb recorder injection + artifact storage
└── gateway/
    ├── cdp_policy.py    # per-class CDP method allow/deny filter used by cdp_proxy
    └── routes/{sessions_v1,profiles,mcp}.py
deploy/
├── helm/agentpilot/     # gateway, crawl-worker, browser-pod templates, KEDA ScaledObjects
├── kind/                # cluster config + agent-sandbox + KEDA install script
└── images/              # runtime image (kernel-images-derived) + CA bundle layer
SessionClass is the key abstraction. It is a frozen dataclass loaded from config and extensible without code changes: name, density (multi|single), runtime_class (runc|gvisor|kata), pool_target_per_zone, reuse_after_release (bool, max_pages), idle_timeout, detach_window_max, cdp_policy, live_view_allowed, replay_default, node_pool (spot|on-demand).

Crawl uses density=multi and runs on today's worker role through node_pool.py.
API and agentic use density=single and run through pool/k8s.
Callers never see which adapter served them.
Session lifecycle
Warming → Ready are pool states. They are owned by agent-sandbox for single-density classes and by the worker WarmPool for multi-density. Claimed → Active ⇄ Detached → Draining → Closed are session states, owned by SessionController and persisted in the Postgres browser_sessions table plus the Redis session:{id} hot route.

Claimed: the quota check passes, then PoolClient.claim(), profile snapshot restore (vault/S3) and proxy bind (existing ProxyPinner).
Active: the gateway sees ≥1 attached client (CDP WS, MCP, execute within the last N s). The metering clock runs.
Detached: the last client disconnected with keep_alive=true. The idle clock runs until detach_deadline, and reattach flips it back to Active (the Cloudflare pattern).
Draining: profile checkpoint, rrweb flush, usage close with a CloseReason (client_release, idle_timeout, detach_expired, max_duration, quota, node_lost, oom, crash, policy_violation), then release to the pool. The pool recycles or deletes, and multi-density reuse only happens if the class allows it.
Mapping: the existing lease becomes an implementation detail under Claimed/Active (the worker lease stays as the node-local guard). ContextState.RETIRING is used for Draining.
Public API v1 (additive; existing routes stay)
Method	Path	Notes
POST	/v1/sessions	Adds class, region, profile_id, proxy_policy, stealth_profile, timeout, keep_alive. Returns connect URLs (cdp, mcp, live_view)
GET	/v1/sessions/{id}	new: state, URLs, usage so far, close_reason
GET	/v1/sessions	Reads from the browser_sessions table, not a fan-out to nodes
POST	/v1/sessions/{id}/keepalive	new
DELETE	/v1/sessions/{id}	Triggers Draining
WS	/v1/sessions/{id}/cdp	Existing; adds cdp_policy + attach/detach events + route refresh (fixes bug a)
POST	/v1/sessions/{id}/mcp	new: MCP streamable HTTP; tools = crawlpilot.tools.registry.subset(agent_exposed)
GET	/v1/sessions/{id}/replay	new: rrweb events + close reason
CRUD	/v1/profiles	new: cookies, storage, fingerprint seed, pinned proxy; S3-backed snapshot
The OpenAPI golden test (existing tests/golden/ pattern) freezes v1 at the end of Phase 3. agentpilot-client gains get/keepalive/mcp_url/replay/profiles, with the Steel-shaped SDK surface.

Phases
Each phase is a sequence of small PRs. Each PR keeps pytest, lint-imports, mypy and ruff no worse than baseline. Exit criteria gate the next phase.

Phase 0 — Session core on Compose (no k8s yet)
ADRs 0001–0006: session model; SessionClass; PoolClient seam; borrow list with licences (agent-sandbox, KEDA, Karpenter, kernel-images, rrweb, gVisor/Kata = Apache/MIT; browserless reference-only, not forked); density per class; MCP in the gateway.
sessions/ model + transition table + SessionController, InMemorySessionStore, and the contract suite tests/session_store_contract/.
Alembic 0020_browser_sessions (id, tenant, class, state, node/pod, profile_id, timestamps per state, close_reason) and 0021_usage_records (session_id, tenant, active_s, idle_s, cdp_msgs, proxy_bytes, mem_peak_mb). Postgres store impl.
classes/ registry with the three defaults and config loading (AGENTPILOT_SESSION_CLASSES file).
pool/protocol.py, plus node_pool.py wrapping the existing SessionPlacer + worker /internal/sessions, plus a PoolClient contract suite.
Rewire gateway_proxy.open_session → SessionController.claim(). Recipe and agent loops claim via the controller too (fixes bug d), with in-process fast path kept behind the same interface for crawl class.
Fix bugs a–c:
CDP proxy refreshes the route and emits attach/detach.
Worker persists its session index in Redis (node_sessions already exists) and a restart reconciles it.
WarmPool claims slots.
Add GET /v1/sessions/{id}, keepalive, and the Detached state with a detach reaper (it extends the existing NodeReaper loop rather than adding a new daemon).
metering/: an EventSink writes usage on Draining. Prometheus per-session histograms: claim_wait, time_to_first_cdp, active/idle seconds, close_reason counter.
quota/: a Redis Lua token bucket + concurrency counter per tenant (the planned rate_governor shape). Returns 429 with Retry-After.
Exit: docker compose up, then claim, attach via Playwright connectOverCDP, detach, reattach, release. browser_sessions shows the full state history, usage_records has active and idle seconds, a quota breach gives 429, and all contract suites are green.

Phase 1 — Kubernetes on kind: pools, isolation, autoscaling
deploy/images/runtime: Patchright Chrome on a kernel-images-derived base. Bake the corporate CA bundle and set UV_NATIVE_TLS=1/SSL_CERT_FILE. Keep the amd64 Chrome / arm64 Chromium split. Xvfb per pod, so single-density pods get their own display, which removes the shared-Xvfb cdp_patches hazard in capacity-planning.md.
Helm chart:
gateway Deployment (stateless, HPA on RPS).
crawl-worker Deployment (multi-density, today's worker role).
browser-pod SandboxTemplate per single-density class, with a sidecar that is the existing worker in max_contexts=1 mode, so the internal API is unchanged.
Redis and Postgres via charts or external services.
NetworkPolicy replacing the iptables egress baseline, which keeps NET_ADMIN off pods.
deploy/kind/: cluster config plus install scripts for agent-sandbox (pinned), KEDA, and the gVisor runtime.
pool/k8s/: an agent-sandbox adapter (kubernetes-asyncio client) that creates SandboxClaims labelled tenant/profile/class/session, waits for Ready, and resolves the pod endpoint. It sits behind PoolClient and passes the same contract suite (run in a kind-gated CI job).
Leak reconciler: a gateway-side leader-elected loop, reusing the node_reaper lock pattern, that deletes claims without a live browser_sessions row after 5 min.
KEDA ScaledObjects:
crawl-worker scales on the Postgres queue depth (crawl_tasks/recipe_runs/agent_runs WHERE status='queued', postgresql scaler).
Pool targets scale on a Prometheus pool_ready_gap{class,zone} gauge exported by the gateway.
CPU is never a signal.
Spikes, run as scripts and recorded in ADRs:
Claim latency warm/cold.
Detection page under runc, gVisor and Kata (the gate for the agentic runtime class).
Memory growth per page to set max_pages recycle.
Exit:

Warm claim p95 under 1 s and cold p95 under 30 s on kind.
Detection page clean for every runtime class that gets enabled.
Leaked-claim audit = 0.
A load test (k6 or a Python script in scripts/load/) produces the SLO table from real numbers.
Phase 2 — Gateway as the one door: MCP, CDP policy, profiles
mcp/: MCP streamable-HTTP server at /v1/sessions/{id}/mcp. Tool list comes from crawlpilot.tools.registry, and calls become execute() batches on the owning pod. Snapshot budget flags are exposed.
gateway/cdp_policy.py: a per-class allow/deny list on the CDP frame pump in cdp_proxy.py. The agentic class denies Browser.close, cross-session Target.* and Browser.setDownloadBehavior/file methods unless enabled. Violations close with CloseReason policy_violation. It is enforced for external tenants.
profiles/: ProfileSnapshotStore Protocol, with the local-dir impl (today's behaviour) and an S3 impl (tar of the profile dir + vault blob, encrypted per tenant).
Restore happens on Claimed and save on Draining.
Affinity becomes an optimisation, not a correctness requirement.
One owner per profile, enforced by the existing active:{slug} Lua (the Durable-Object lesson).
/v1/profiles CRUD.
Proxy Manager v1: move pool config from env into a proxy_pools table plus admin routes. The existing ProxyPinner/ProxyHealth stay the engine. Add per-profile proxy binding and a geo/timezone consistency check at claim.
Exit: an MCP client (e.g. the Claude MCP inspector) drives a session end to end; a CDP policy test suite passes; a profile survives node loss via S3; the detection page is still clean.

Phase 3 — Observability, replay, API freeze, EKS
replay/: rrweb recorder injected via the extension BrowseMount hook (not a core driver change). Batches are shipped over the existing CDP binding to the pod, and the artifact is stored in S3. Behind the class flag replay_default.
Live view: keep the CDP screencast. Evaluate neko (WebRTC) for the agentic class only if screencast FPS fails a human-takeover test.
Grafana dashboards + alerts for every SLO. Close-reason ratio alert (infra closes > 1 %).
Freeze the OpenAPI v1 golden. The SDK release adds the new methods.
EKS: Karpenter NodePools per class (spot for crawl, on-demand floor for api/agentic), same Helm chart, per-zone warm pools, and gateway → pod same-zone routing.
Exit: SLO dashboard green for 7 days on EKS staging, v1 frozen, SDK published.

Phase 4 — Co-located code and advanced isolation
Co-located runtime (Browserbase Functions lesson): recipe and agent worker loops run as a sidecar or peer pod in the same zone as the claimed browser. This is a placement hint on SessionSpec and does not need a new service.
Snapshot/restore spike for long idle agentic sessions (Kernel lesson), behind PoolClient.suspend/resume, an optional capability Protocol.
microVM tier (cocoonstack sandbox-operator) as a new pool/k8s backend option, gated by the same contract suite.
Risks and mitigations
agent-sandbox API churn: pin the version; the PoolClient seam plus the contract suite make an upgrade a one-adapter change.
gVisor/Kata break stealth: the Phase 1 detection spike gates enabling each runtime class.
Idle warm-pool cost: per-class, per-time-of-day targets; refill-rate limit; spot share for crawl.
Chrome memory growth: max_pages recycle plus memory limits set from measured peaks.
Gateway single point of failure: stateless replicas; routing lives in Redis and Postgres.
Redis as a single point of failure (already the case): Sentinel/ElastiCache; fail closed for opens.
Scope creep: phase exit criteria; v1 frozen from Phase 3.
Critical files
Modify:
gateway/routes/{gateway_proxy,cdp_proxy,cdp,sessions}.py
gateway/wiring.py (composition root: inject controller, pool, quota, metering)
placement/placer.py (wrapped, not rewritten)
jobs/{agent_worker_loop,recipe_worker_loop}.py (claim via controller)
crawlpilot/session/warm_pool.py (slot accounting)
packages/agentpilot/pyproject.toml (new layer contracts)
docker-compose.yml
packages/agentpilot-client/
Reuse, do not rewrite: control/lua/*, placement/lua/*, node_reaper.py (leader-lock pattern), identity/vault.py, ProxyPinner/ProxyHealth/BurnTracker, crawlpilot/tools/registry.py + adapters.to_mcp, extensions hooks, driver/live_view.py, tests/driver_contract/ pattern.
Verification
Per PR: uv run pytest --ignore=tests/driver_contract, uv run lint-imports, uv run mypy, uv run ruff check, plus the new contract suites (session store, pool client, profile store) against the fake and real backends.
Phase 0 e2e (compose): a script in scripts/e2e_sessions.py that goes claim → Playwright connect_over_cdp → navigate the detection page → disconnect (Detached) → reattach → MCP-less execute → DELETE. It then asserts the browser_sessions state history, the usage_records row, and a 429 on quota breach.
Phase 1 (kind): make kind-up && helm install, run scripts/load/claim_latency.py for p95 warm/cold, run the detection page per runtime class, and kill a browser pod mid-session to assert close_reason=node_lost and 0 leaked claims.
Phase 2: MCP inspector drives navigate/snapshot/click; a CDP policy test sends a denied method and gets policy_violation; delete the node holding a profile, reclaim it, and confirm the cookies were restored from S3.
Phase 3: 7-day SLO dashboard on EKS staging; OpenAPI golden diff is empty.
Open items (not blocking Phase 0)
Peak concurrent sessions per class for sizing Phase 1 pools (derive from crawl_tasks volume).
S3 bucket, retention and per-tenant key management for profile snapshots.
Whether agentic v1 needs human takeover (live view interact already exists; decide whether it is exposed to external tenants).