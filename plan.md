# AgentPilot Platform (agentpilot) — Full Design & Build Plan

> Consolidated plan: the original Python-adaptation design, every review finding folded into its
> owning section, and the fleet placement/routing/admission architecture promoted to a first-class
> section. Supersedes the standalone plan and the placement addendum.

## Context

A multi-tenant, distributed **Browser-as-a-Service** platform — a foundation on which agents,
crawlers, and scrapers are built by consuming warm browser sessions as tools over a stable
protocol, targeting millions of crawls/hour. Informed by patterns from an earlier internal
JVM-based browser-automation system, but rebuilt single-stack in Python because the stealth layer
(Patchright) forces Python anyway; running two ecosystems for an I/O-bound coordinator was pure
overhead. Target repo `agentpilot` is empty — a from-scratch build informed by that system's
*patterns*, not a code port (its hand-rolled CDP client isn't reusable).

**The system is a fleet, not a node.** At the §Capacity numbers (~5,600 concurrent contexts,
~31–50 nodes) three facts are load-bearing and shape the design from P0: a session is physically
pinned to the node running its Chrome process; an identity's warm profile dir exists on exactly
one node's disk; and fleet capacity is finite, so "open a context" must sometimes be answered
"not now." Any design that only works on one container is wrong here.

Research grounding (verified against actual package internals):
- **Prior internal system** — strong prior art for identity/profile modeling, driver pooling with
  lease/release-to-IDLE, sticky proxy pinning keyed by identity, and a success/failure feedback
  loop retiring "burned" identities. Has **zero** multi-tenancy and **zero** challenge solving
  (only heuristic classification → retirement) — both net-new here.
- `patchright` (1.61.2) and `re-cdp-patches` (0.9.1, import `cdp_patches`) are real and
  installable. `cdp_patches` OS-level input dispatch **only works headful** (its own README) —
  hence the headful-policy split below.
- `page.aria_snapshot(mode="ai")` is a real ref-annotated snapshot API (same shape Playwright's
  MCP server uses). Whether an `aria-ref=` locator engine resolves those refs lives in the
  compiled driver binary — unverifiable by reading source, so a P1 spike with a fallback.
- **Firecrawl** (`apps/api`, Node/Express in front of a pluggable Playwright renderer) — mature
  production API design for exactly this shape: batched discriminated-union **actions**; a
  user-facing **`proxy: basic|stealth|enhanced|auto`** tier knob that auto-escalates and reports
  what actually ran; a stateful `POST /browser` → `execute` → `DELETE` session lifecycle with a
  single-writer profile lock returning **409**; strict (extra-forbidden) schemas; SDK ergonomics.
  The prior internal system independently converged on a press/fill/mouseWheel/batch primitive —
  convergent evidence that batched action execution is right, not a Firecrawl-only opinion.
- **browser-use** — migrated off Playwright onto raw CDP, but its **pre-migration** code solves
  our P1 ref→locator problem almost exactly (`git show <pre-migration-sha>^:<path>`). Concretely
  transferable: (a) a cascading match-level fallback (exact-hash → stable-hash → xpath →
  accessible-name → unique-attribute) for resolving a possibly-stale ref; (b) on-demand CSS
  re-derivation from xpath+attributes with iframe-chaining; (c) an occlusion check before
  clicking; (d) a two-layer batch guard (static "this action ends the sequence" flag + runtime
  URL/focus diff); (e) one shared driver-API singleton per process. **Not** copying its `bubus`
  event-bus/watchdog architecture — that solves multi-tab-within-one-session complexity our
  per-identity one-context model doesn't have; a plain dispatch table inside `execute()` suffices.

---

## Locked-in decisions

Original decisions, plus decisions forced by the review, all treated as settled unless explicitly
reopened:

1. **Headful is not universal.** Navigate/scrape-only sessions (no click/fill) may run headless
   with Playwright-level input, skipping Xvfb and `cdp_patches`. Interactive sessions (OS-level
   input) must run headful-in-Xvfb — a hard functional requirement, a policy flag on session
   creation, and a branch in driver + tier router.
2. **Real Chrome sandbox** (`cap_add: SYS_ADMIN`, not `--no-sandbox`) — untrusted third-party
   pages on shared multi-tenant nodes; inter-tenant Chrome-process isolation beats deploy
   simplicity.
3. **Content extraction is a first-class action, not an ExecuteJs hack** *(review)*. For a
   scraper/crawler platform, page content *is* the product; `ExtractAction` ships in P0.
4. **Two-tier gateway/worker topology** *(review)*, same codebase, `--role` flag. Not
   direct-to-node URLs. Rationale in §Placement.
5. **Vault is the source of truth for identity state; a profile dir is a node-local cache of it**
   *(review)*. No shared/network filesystem for profiles; profile dirs on local NVMe. This one
   invariant resolves node failure, relocation, and the no-NFS decision at once.
6. **Snapshot refs are epoch-scoped** *(review)*. The match cascade degrades gracefully *within*
   an epoch and hard-fails *across* epochs — never silently clicking a lookalike after a
   navigation.
7. **Multi-tenant network egress is fail-closed** *(review)*. Browser processes and the httpx
   tier cannot reach cloud metadata or RFC1918 space; post-DNS-resolution IP validation guards
   against rebinding. Table stakes for AgentPilot.
8. **Placement + admission is one atomic Lua script** *(review)*, sharing keys with the lease
   Lua — which is why it must be designed with P2's registry, not after.

---

## Recommended stack

| Concern | Choice | Why |
|---|---|---|
| Language/runtime | Python 3.12+, asyncio | Single stack; Patchright is async-native |
| Browser stealth | `patchright` (`from patchright.async_api import ...`) | CDP-layer `Runtime.enable` fix baked in; never `playwright` |
| OS-level input | `re-cdp-patches` (import `cdp_patches`) | Fixes page≠screen coordinate leak (crbug#1477537); headful-only |
| Content extraction | `trafilatura` (main-content → markdown/text) + raw-HTML passthrough | Readability-grade extraction is the scrape product; don't hand-roll |
| Gateway/worker HTTP | FastAPI + uvicorn | Async-native, free OpenAPI (feeds SDK + MCP gen), mature auth middleware |
| Validation | Pydantic v2, **scoped to `gateway/schemas.py` only** | Untrusted-input boundary only; `spi`/`session`/`identity` stay plain dataclasses (hot-path) |
| Registry / placement / rate limiting | `redis.asyncio` + Lua (`register_script`) | Atomic cross-node invariants; SPOF → Sentinel HA (P2) |
| Metrics | `prometheus-client` | Pool occupancy, per-tier latency, challenge/burn rate — fleet is unobservable without it |
| Packaging | `uv` | Fast resolver, workspace-friendly if it splits into sub-packages |
| Lint/format/type | `ruff` + `mypy --strict` on `agentpilot.spi`/`agentpilot.driver` | Protocol-heavy code benefits most from strict typing |
| Import boundaries | `import-linter` (`lint-imports` in CI) | Enforces the layered contract mechanically |
| Tests | `pytest` + `pytest-asyncio` + `pytest-httpserver`, testcontainers for Redis | Real browser vs. real local fixtures — no mocking, no external-site flakiness (browser-use pattern) |
| Logging | `structlog` w/ contextvars (`tenant`, `identity`, `lease_id`, `context_id`, `node_id`) | Every line attributable in a multi-tenant fleet |
| Cheap-tier fetch (P4) | `httpx` | No-browser fetch path for the tier router (egress-guarded) |
| MCP surface (P5) | Custom FastMCP-style server over the SDK | The "tools exposed to agents" deliverable; Playwright-MCP is the reference shape |

---

## Module layout (dependency direction strictly downward)

```
mcp ──► gateway
gateway ──► placement ──► spi
gateway ──► session   ──► identity ──► spi
gateway ──► control   ──► spi
driver  ──► extraction ──► spi        (extraction is a pure transform)
driver  ──► egress     ──► spi        (egress is applied at the browser/httpx boundary)
driver  ──► spi                       (concrete; imported ONLY by the composition root)
```

- `agentpilot.driver` is imported by exactly one file: the composition root (`agentpilot/wiring.py`). At P2
  the root splits by role, so gateway-role process graphs never include `agentpilot.driver` at all;
  worker-role graphs do.
- `agentpilot.extraction` and `agentpilot.egress` are leaves (spi-only) so they're trivially unit-testable
  and reusable across drivers.
- `agentpilot.placement` owns routing/affinity/admission (spi + Redis); imported only by gateway.
- `agentpilot.mcp` is the top layer — imports the gateway app/schemas, imported by nothing.

Enforced via `import-linter` contracts in `pyproject.toml` (layers + forbidden contracts as in the
original, extended for the new modules):

```toml
[tool.importlinter]
root_package = "agentpilot"

[[tool.importlinter.contracts]]
name = "gateway -> session -> identity -> spi"
type = "layers"
layers = ["agentpilot.gateway", "agentpilot.session", "agentpilot.identity", "agentpilot.spi"]

[[tool.importlinter.contracts]]
name = "gateway -> placement -> spi"
type = "layers"
layers = ["agentpilot.gateway", "agentpilot.placement", "agentpilot.spi"]

[[tool.importlinter.contracts]]
name = "gateway -> control -> spi"
type = "layers"
layers = ["agentpilot.gateway", "agentpilot.control", "agentpilot.spi"]

[[tool.importlinter.contracts]]
name = "driver -> {extraction, egress} -> spi"
type = "layers"
layers = ["agentpilot.driver", "agentpilot.extraction", "agentpilot.spi"]

[[tool.importlinter.contracts]]
name = "session/identity/control/placement never import driver"
type = "forbidden"
source_modules = ["agentpilot.session", "agentpilot.identity", "agentpilot.control", "agentpilot.placement"]
forbidden_modules = ["agentpilot.driver"]

[[tool.importlinter.contracts]]
name = "only the composition root imports the concrete driver"
type = "forbidden"
source_modules = ["agentpilot.gateway.routes", "agentpilot.gateway.schemas", "agentpilot.gateway.auth", "agentpilot.gateway.app", "agentpilot.mcp"]
forbidden_modules = ["agentpilot.driver"]
```

`lint-imports` runs in CI as a P0 exit criterion, not deferred.

---

## `agentpilot.spi` — foundation types (build first)

Plain `dataclasses`/`Enum`/`Protocol` — no Pydantic (hot path, trusted internal callers).

- **`identity.py`** — `IdentityKey(tenant, domain, name)` frozen/hashable + `.slug()` for
  filesystem paths; `ProfileKind` enum (`DEFAULT/PROTOTYPE/GROUP/TEMPORARY/PERMANENT`) — structural
  port of the prior system's profile-identity concept.
- **`proxy.py`** — `ProxyEndpoint(scheme, host, port, username, password, vendor, sticky_key)`;
  `sticky_key` defaults to owning `IdentityKey` — explicit form of the same sticky-proxy-pinning
  pattern.
- **`lease.py`** — `LeaseId`, `ContextState` enum (`IDLE/ACTIVE/RETIRED/RETIRING`),
  `Lease(lease_id, identity, owner, acquired_at, ttl_seconds, context_ref)`,
  `ContextRef(context_id, identity, state, pid, node_id)` — driver-agnostic, never holds a
  Playwright object. `node_id` added for fleet routing.
- **`snapshot.py`** — `SnapshotNode`/`AXSnapshot(epoch, ref, role, name, children)` — driver-agnostic
  ref-annotated tree. **`epoch`** *(review)*: a monotonic id minted per `SnapshotAction`; every ref
  carries the epoch it was born in. See `errors.StaleRefError` and the ref-cache cascade for how
  epoch mismatch hard-fails rather than degrading.
- **`storage_state.py`** — mirrors Playwright's native `storage_state()` JSON shape
  (`cookies`, `origins[].localStorage`) field-for-field. **Fix over the prior system**: its
  equivalent only captured the current origin; Patchright's native
  `context.storage_state()` captures all origins — the vault uses it directly.
- **`actions.py`** — the single interaction primitive (adapted from Firecrawl's `actionSchema`
  discriminated union, convergent with the prior internal system's own batch primitive). A closed
  set of tagged, driver-agnostic dataclasses:
  - Navigation/read: `NavigateAction(url, timeout_ms)`, `GoBackAction()`, `SnapshotAction(viewport_only=False, max_nodes=None, roles=None)`, `ScreenshotAction(full_page=False, ...)`, `WaitAction(ms=None, ref=None)`.
  - **`ExtractAction(format: Literal["markdown","text","html"], main_content=True)`** *(review)* — the scrape output. `markdown`/`text` route through `agentpilot.extraction` (trafilatura); `html` returns `page.content()` raw. This is what a crawler actually harvests.
  - Interaction (P1): `ClickAction(ref, all=False)`, `FillAction(ref, text)`, `SelectOptionAction(ref, values)` *(review)*, `HoverAction(ref)` *(review)*, `PressAction(key)`, `ScrollAction(direction, ref=None)`.
  - Escape hatch: `ExecuteJsAction(script)`.

  `ActionResult` carries **per-type correlated output lists** (`snapshots`, `screenshots`,
  `extracts`, `js_returns`, ...) mirroring Firecrawl's response shape. One batched round-trip
  instead of one HTTP call per verb — the thing that matters at millions/hour.

  **Batch-abort guard** (browser-use `Agent.multi_act()`): actions that can invalidate the rest
  (`NavigateAction`, `GoBackAction`, any click that redirects) carry `terminates_sequence: bool`;
  `execute()` also captures pre-batch URL and diffs after each step. On mismatch it stops and sets
  `ActionResult.sequence_aborted = True` so the caller re-snapshots instead of acting on a stale DOM.

  **Popup/download policy** *(review)* — `target="_blank"` and file downloads happen on real
  P0 sites and both break the one-context-one-page assumption:
  - Popups: default **auto-adopt newest page as "the page"**, set `ActionResult.page_changed=True`;
    a session-open flag `block_popups: bool = False` switches to hard-block at context level.
  - Downloads: `context.expect_download` captured, streamed to the tenant-scoped artifact store
    (P2), surfaced as `ActionResult.downloads: list[ArtifactRef]`. A crawler fetching PDFs/CSVs is
    a primary use case, not an edge case.
- **`driver.py`** — the seam, narrowed to batch execution (mirrors the prior internal system's own
  batched driver path rather than one method per verb):
  ```python
  @runtime_checkable
  class BrowserDriver(Protocol):
      async def open(self, identity: IdentityKey, profile_dir: Path,
                     proxy: ProxyEndpoint | None, headful: bool,
                     egress: EgressPolicy) -> ContextRef: ...
      async def close(self, ctx: ContextRef) -> None: ...
      async def execute(self, ctx: ContextRef, actions: list[Action]) -> ActionResult: ...
      async def export_state(self, ctx: ContextRef) -> StorageState: ...
      async def restore_state(self, ctx: ContextRef, state: StorageState) -> None: ...
      async def health(self, ctx: ContextRef) -> HealthStatus: ...
  ```
  Single-verb convenience calls are thin sugar over `execute(ctx, [SingleAction])` — never a
  second code path the driver implements separately. `runtime_checkable` lets the composition root
  `isinstance`-assert at startup.
- **`errors.py`** — `DriverError`, `NavigationTimeout`, `ContextCrashed`, `ChallengeDetected`,
  `StaleRefError(epoch_superseded: bool)` *(review — distinguishes "re-snapshot" from "element
  gone within current epoch")*, `LeaseConflict`, `NodeLost` *(review)*, `CapacityExhausted`
  *(review)*, `EgressBlocked` *(review)*.
- **`health.py`** — `HealthStatus(alive, reason)`.
- **`streaming.py`** — live-view types (see §Live View): `LiveViewFrame(data, format, width, height, ts)`,
  `InputEvent` union (`MouseMoveEvent`/`MouseButtonEvent`/`WheelEvent`/`KeyEvent`), and the
  `LiveViewCapable` Protocol. Deliberately **not** reusing `actions.Action` — agents target refs,
  a human targets pixels.
- **`egress.py`** *(review)* — `EgressPolicy(block_metadata=True, block_private=True, allow_hosts, deny_hosts)`
  passed into `driver.open()`; the driver and httpx tier both enforce it.
- **`artifact.py`** *(review)* — `ArtifactRef(artifact_id, tenant, kind, size, sha256)` for downloads.

---

## API design — adapted from Firecrawl (gateway layer, all phases)

Firecrawl's `apps/api` is a mature example of exactly our shape (one API in front of pluggable
engines). All adaptations are translated through `agentpilot.spi` — **`gateway/schemas.py` is a Pydantic
mirror of `spi` types for HTTP-boundary validation only; it never defines a shape `spi` doesn't
own.** Same one-way dependency discipline the prior internal system followed between its own API
and core layers.

- **Session lifecycle, not stateless actions-per-scrape.** `POST /v1/sessions` (open) →
  `POST /v1/sessions/{id}/execute` (batched `Action` list) → `DELETE /v1/sessions/{id}` (release to
  IDLE — reaper owns destruction). Our whole point is warm reusable sessions.
- **Page-tier as an explicit request field**: `tier: Literal["basic","stealth","enhanced","auto"]`
  on open, default `"auto"` (Firecrawl's exact vocabulary). `"basic"` → cheap `httpx` tier;
  `"stealth"`/`"enhanced"` → full Patchright+residential; `"auto"` starts cheap, escalates on
  failure signals (403/429, empty/too-short content, P3's challenge heuristic). Makes P4's tier
  router a first-class, tenant-visible cost/stealth dial.
- **Report what actually ran**: every response carries `metadata: {tier_used, cache_state,
  duration_ms, node_id, credits_used?}` — port of Firecrawl's `Document.metadata`.
- **Lease conflict → HTTP 409, not a silent queue.** Maps onto "at most one ACTIVE context per
  IdentityKey" (P1 registry, P2 `bind_active_context.lua`). 409 carries `Retry-After`, and open
  accepts an optional `wait_for_lease: <ms>` bounded wait so agent callers don't hot-poll *(review)*.
  Contract test `test_session_open_conflict_returns_409`.
- **Capacity exhaustion → HTTP 503 + `Retry-After`** *(review)* — distinct from 409. See §Placement
  admission control. Optional `wait_for_capacity: <ms>` bounded server-side wait (P4).
- **Strict, friendly schemas**: every model uses `model_config = ConfigDict(extra="forbid")`, a
  central exception handler translating validation errors into Firecrawl's friendly-400
  (`{"success": false, "code": "BAD_REQUEST", "error": "..."}`).
- **Typed error codes end-to-end**: `gateway/errors.py` defines a closed `ErrorCode` str-enum
  mirrored 1:1 from `spi.errors` (`SESSION_LEASE_CONFLICT`, `CAPACITY_EXHAUSTED`, `NODE_LOST`,
  `NAVIGATION_TIMEOUT`, `CHALLENGE_UNRESOLVED`, `CONTEXT_CRASHED`, `RATE_LIMITED`,
  `TENANT_QUOTA_EXCEEDED`, `EGRESS_BLOCKED`, ...), returned in `{success:false, code, error, details?}`.
- **Rate limiting with real headers** (improvement over Firecrawl, which only string-embeds it):
  per-`(tenant, domain)` token bucket on P2's Redis+Lua, emitting real
  `X-RateLimit-Limit/Remaining/Reset` + `Retry-After` headers alongside the JSON body.
- **Bulk/async job model** (P4/P5, when a "many URLs" endpoint is needed): reuse Firecrawl's
  proven shape — `POST` returns `{id}`; `GET /v1/jobs/{id}` polls `{status, completed, total, next?}`;
  optional `webhook: {url, headers, events}`.

---

## Placement, routing & admission (fleet architecture)

At the §Capacity scale, sessions pin to nodes, identities pin to node-local profile dirs, and
capacity is finite. This section decides how requests find nodes, how identities stick, and what
happens at the capacity boundary — designed **with** P2's registry because the Lua shares keys.

### Topology — two tiers, one codebase

```
                    ┌────────────► worker (node 1) ──► N × Chrome ctx
client ──► LB ──► gateway (×N, stateless)
                    └────────────► worker (node k) ──► N × Chrome ctx
                          │
                        Redis (routing + affinity + capacity + leases)
```

- **Gateway tier**: stateless FastAPI behind an LB. Owns auth, tenant resolution, rate governor,
  strict schemas, error mapping, egress-policy resolution. Zero session state; every routing
  decision is a Redis lookup. Scales horizontally.
- **Worker tier**: one process per node, same codebase started `--role worker` — serves an
  **internal-only** `/internal/sessions/...` surface bound to the VPC, never tenant-exposed. Owns
  the shared Patchright singleton, `PatchrightDriver`, this node's context-registry slice, the
  reaper, Xvfb. The "only the composition root imports driver" rule becomes role-conditional:
  the driver import lives behind the worker role.
- **Why not direct-to-node URLs** (Browserbase-style): leaks fleet topology into the tenant
  contract, forces auth/rate-limit/schema re-implementation at every node, complicates LB/TLS —
  for no win, since the intra-VPC proxy hop is sub-ms against multi-second page loads. The one
  place it'd matter (live-view frame bandwidth) is off the hot path anyway; the gateway proxies
  the WebSocket too, keeping one auth path.
- **Proxying long calls**: `execute` batches run 10–60s; the gateway streams the worker response
  through (httpx streaming pass-through) with deadline = batch timeout + slack. A mid-execute
  worker drop maps to typed `NODE_LOST`, never a bare 502.

### Session routing

```
session:{session_id} → {node_id, tenant, identity, tier, state, created_at}   TTL = lease TTL, renewed
node:{node_id}       → {addr, started_at}                                     written at worker boot
```

`.../execute` at the gateway: `HGET session:{id} node_id` → verify tenant matches (no route skips
this) → proxy to `node.addr`. Session TTL = lease TTL, so an expired lease and a dangling route
die together — no separate route-expiry to keep consistent.

### Identity → node affinity

```
affinity:{tenant}:{domain}:{name} → node_id     TTL = profile retention window, refreshed on open
```

Placement on `POST /v1/sessions`, in order:
1. **Affinity hit, node healthy + has capacity** → place there. Hot path: warm dir, no vault.
   Instrument `placement_affinity_hit_ratio`; a falling ratio is an incident.
2. **Affinity hit, node full/unhealthy** → relocate **only if** the identity holds no ACTIVE
   context (checked in the same atomic script). Relocation = pick a new node, write new affinity,
   open a fresh profile dir there → the base plan's "restore only into a fresh dir" vault trigger
   fires by construction, so no new carve-out. The orphaned warm dir is garbage; the old node's
   reaper collects it on noticing the affinity key no longer names it. If the identity **is**
   ACTIVE elsewhere, this is the plain 409 — relocation never races a live context.
3. **No affinity** → least-loaded, capacity-weighted placement; write affinity.

**Profile dirs on local NVMe; vault is the only durability layer.** No EFS/NFS/EBS-multi-attach —
Chrome's SQLite-heavy profile I/O over network FS is a corruption/latency tarpit, and shared FS
tempts the two-nodes-one-profile write conflict the single-ACTIVE invariant exists to prevent.
Invariant: **vault is source of truth for identity state; a profile dir is a node-local cache.**
Losing a node loses warmth, never state — provided the reaper checkpoints (vault save on
release-to-IDLE, not only at destroy), bounding node-loss staleness to one session's delta.

### Admission control

- **Node capacity advertisement**: each worker heartbeats
  `capacity:{node_id} → {max_contexts, active, idle, mem_used_pct, cpu_used_pct}` every 2s, 10s TTL.
  `max_contexts` from the **CPU-corrected** capacity model, not RAM-only.
- **Local gate**: the worker refuses `open` beyond `max_contexts` or above a memory watermark
  regardless of the gateway's (up-to-one-heartbeat-stale) view — the worker is the last line.
- **Global gate**: no healthy node clears → gateway returns **503 + `Retry-After`** (from mean
  hold time; even static `5` beats nothing). Optional `wait_for_capacity: <ms>` bounded wait (P4,
  on rate-governor machinery, per-tenant queue-depth caps so one tenant can't own the waiting room).
- **Placement + admission = one Lua script** `place_session.lua` (joins `bind_active_context.lua`
  in `session/lua/`): atomically checks the ACTIVE binding (409), reads affinity, validates target
  heartbeat liveness + capacity counter, increments the counter, writes the route, refreshes
  affinity — no check-then-act window. Shares keys with the registry Lua, which is exactly why it's
  designed in P2 not bolted on later.

### Node failure

- Heartbeat TTL expiry is the liveness signal — no separate health-checker.
- A **node-reaper** (background task on every gateway instance, Redis-lock-elected so one acts)
  scans `node:{id}` whose `capacity:{id}` heartbeat is gone: marks its sessions `CONTEXT_CRASHED`,
  releases leases + ACTIVE bindings via the release Lua, deletes affinities to the dead node. Next
  open for affected identities takes placement path 3 + vault restore — degraded to cold, never wrong.
- Half-dead nodes (heartbeating but failing opens) → local gate + `NODE_LOST` mapping + one
  placement retry against the next candidate (bounded to one so a sick node can't absorb fleet latency).

### Consolidated Redis schema

| Key | Value | Writer | TTL |
|---|---|---|---|
| `session:{id}` | node_id, tenant, identity, tier, state | `place_session.lua` / release | lease TTL, renewed |
| `affinity:{tenant}:{domain}:{name}` | node_id | `place_session.lua` | profile retention window |
| `capacity:{node_id}` | max/active/idle/mem/cpu | worker heartbeat | 10s |
| `node:{node_id}` | addr, started_at | worker boot | none (node-reaper deletes) |
| `active:{identity}` | context_ref, lease_id | `bind_active_context.lua` | lease TTL |
| `proxy:{identity}` | ProxyEndpoint | `HSETNX` (P2 pinning) | none (released on retire) |
| `success:{identity}` | tasks/successes/warnings | P4 success tracker | none |
| `ratelimit:{tenant}:{domain}` | token bucket | `rate_governor.lua` | window |

**HA posture**: Redis is a SPOF from P2 — run Redis Sentinel; on partition/outage the gateway
**fails closed** (503 for opens, in-flight `execute` on already-routed sessions continues since the
worker holds local state). Name this in `docs/` and test it.

---

## Content extraction (new — the scrape product)

`agentpilot.extraction` is a pure transform (spi-only), imported by the driver to fulfill `ExtractAction`.

- `markdown`/`text` → `trafilatura` main-content extraction (readability-grade boilerplate
  stripping), which is what a crawler wants 90% of the time. `main_content=False` widens to
  full-body.
- `html` → `page.content()` raw passthrough for callers doing their own parsing.
- Runs inside `execute()` on the worker (it has the page); the result rides `ActionResult.extracts`.
- Kept a leaf module so it's unit-testable against static HTML fixtures with zero browser.

Without this, an agent can click but can't harvest without `ExecuteJsAction("...outerHTML")` hacks
— unacceptable for a platform whose purpose is scraping.

---

## Network egress / SSRF policy (new — multi-tenant safety)

Tenants control navigation targets and `ExecuteJsAction`; the browser and httpx tier can otherwise
reach `169.254.169.254` and RFC1918 from inside the VPC. `agentpilot.egress` enforces `EgressPolicy`:

- **Browser processes**: network-namespace / iptables rules on the worker blocking cloud-metadata
  IPs and private ranges for Chrome's uid/netns. Applied at `open()` time from the policy.
- **httpx basic tier**: resolve DNS first, validate every resolved IP against the deny ranges
  **before connecting** (rebinding-safe), reject with `EgressBlocked` → `EGRESS_BLOCKED` 403.
- The Chrome sandbox protects the *node from the page*; egress protects *your network from the
  tenant* — orthogonal, both required. Baseline (metadata + RFC1918 block) lands P0; full
  post-DNS validation in the httpx tier lands with P2's tier work.

---

## Live View — human-in-the-loop browser streaming

A frontend needs to show the live tab and let an operator interact — demoing, manually clearing a
login/CAPTCHA the P3 solver can't, or watching a crawl. Nothing in the prior internal system or any
dependency covers this; it doesn't touch stealth, so it's legitimate new engineering. Standard
technique (Browserbase/steel-browser-style) is CDP screencast.

- **Mechanism**: CDP `Page.startScreencast`/`screencastFrame`(+ack) outbound;
  `Input.dispatchMouseEvent`/`dispatchKeyEvent` inbound — via `context.new_cdp_session(page)`.
  **Page and Input domains only — never Runtime**, to preserve Patchright's anti-leak guarantee;
  documented as a standing constraint in `driver/live_view.py`.
- **Optional capability, not a required Protocol method** — a second `runtime_checkable` Protocol
  `LiveViewCapable` in `spi.streaming`, checked at the composition root (drivers without it simply
  don't get a live-view route). Input types are separate from `actions.Action` (pixels vs refs).
- **Gateway surface**: `GET /v1/sessions/{id}/live-view` (WebSocket, gateway-proxied to the node);
  `mode=view` frames-only vs `mode=interact` bidirectional (mirrors Firecrawl's
  `liveViewUrl`/`interactiveLiveViewUrl`). Same tenant auth; handshake verifies tenant ==
  session's `IdentityKey.tenant`.
- **Cost-bounded, opt-in**: `live_view: bool` on open (default false); excluded from the §Capacity
  hot-path model by design (stated in `docs/capacity-planning.md`).
- **Works regardless of headful/headless**: screencast captures the rendered frame buffer either
  way; a human's clicks dispatch at CDP `Input` level (no physical device to relay from) — a
  different code path from P1's automated OS-level input, not a headful-policy violation.
- **Phase placement**: view-only screencast in **P1**; interactive input + human-takeover fallback
  wired into **P3** (detector flags unsolved challenge → "needs human" → operator clears via
  interactive live view → P4 `success_tracker` records the outcome like an automated solve).

---

## Observability (new — the fleet is unobservable without it)

`structlog` alone won't run 40 nodes. Prometheus metrics land **P1**, not P4:
- Pool: `contexts_active`, `contexts_idle`, `placement_affinity_hit_ratio`, per-node occupancy.
- Latency: per-tier `execute` histograms, placement latency, vault-restore latency.
- Health: challenge-detection rate, identity-burn rate, `NODE_LOST` count, 503/409 rates.
- Per-tenant: request rate, quota consumption, rate-limit rejections.
`/metrics` on both gateway and worker roles; contextvars already tag logs for correlation.

---

## Capacity-planning model (`docs/capacity-planning.md` + `scripts/capacity_model.py`)

First-pass at P0 end, refined with real P1 measurements. **CPU-corrected** *(review)* — the fleet
is CPU-bound before RAM-bound.

- Target 2,000,000 crawls/hour ≈ 556/s. At ~10s avg exclusive hold → **~5,600 concurrent contexts**.
- RAM: 200–500MB/context (~350MB avg) → **~1.9TB** for Chrome alone.
- **CPU** *(review)*: 180 concurrently *rendering* Chromes need ~0.5–1 vCPU each during page load;
  a 64GB node has ~16–32 vCPU, so concurrency is **CPU-limited well before RAM**. Node count rises
  vs. the RAM-only estimate — strengthening, not weakening, the tier-router argument.
- Nodes: RAM-only said ~31 (180 ctx/64GB); CPU-corrected is materially higher — model both and
  take the max, plus reaper/HA slack → realistically 40–60.
- Bandwidth: ~2MB/page × 2M/hr ≈ **~96TB/day** if every request went residential. At ~$0.5/GB →
  **~$48K/day (~$1.4M/month)** if full-browser+residential were the default for all traffic.
- **This is the concrete justification for P4's tier router** — the difference between a viable and
  non-viable cost structure, not an optimization. State explicitly.
- `scripts/capacity_model.py`: pure function of `(throughput_per_hour, hold_time_s,
  mb_per_context, cpu_per_context, mb_per_page, browser_pct, proxy_pct, price_per_gb)` — recomputed
  as measurements replace estimates. `browser_pct`/`proxy_pct` model the tier-weighted traffic split.

---

## Memory-pressure management (new — P1)

Idle-TTL-only reaping lets warm-IDLE contexts dominate node memory under high identity cardinality
long before TTL expiry. Add *(review)*:
- **Pressure-based LRU eviction**: below-TTL vault-save + destroy when node memory crosses a
  watermark, oldest-IDLE-first.
- **Per-process memory ceiling** (cgroup-scoped Chrome launches, or per-pid accounting feeding
  eviction) so one tenant's 4GB SPA tab can't OOM-kill 179 neighbors.
- Lands in P1 alongside the measured-memory capacity revision.

---

## Phase plan

### P0 — the seam (+ extraction, egress baseline, popup policy, epoch, concurrency spike)

**Files**: `agentpilot/spi/*` (minimal: `driver.py`, `actions.py`, `identity.py`, `snapshot.py`,
`storage_state.py`, `errors.py`, `egress.py`), `agentpilot/extraction/*`, `agentpilot/egress/*`,
`agentpilot/driver/{patchright_driver.py, process_launcher.py}`,
`agentpilot/gateway/{app.py, routes/sessions.py, routes/health.py, schemas.py, wiring.py, errors.py}`,
`agentpilot/wiring.py` (composition root), `docker-compose.yml`, `Dockerfile`,
`docs/capacity-planning.md`, `tests/{driver_contract/, fixtures/detection_page/, test_seam_e2e.py}`.

- `PatchrightDriver.open()` → `chromium.launch_persistent_context(user_data_dir=profile_dir,
  channel="chrome", headless=not headful, no_viewport=True, proxy=...)`; `headful` gates whether
  `cdp_patches.AsyncInput` is wired; `egress` applies the netns/iptables baseline. Playwright
  objects never leave `agentpilot.driver`.
- `execute()` dispatches each `Action`, accumulating one `ActionResult`. P0 lands
  `NavigateAction`, `GoBackAction`, `SnapshotAction`, **`ExtractAction`**, `ScreenshotAction`,
  `WaitAction`, `ExecuteJsAction`. Interaction verbs land P1 (need `ref_cache`).
- `routes/sessions.py`: `POST /v1/sessions` (open; body has `tier`, `headful`, `block_popups`,
  `live_view`), `POST /.../execute` (batched), `DELETE /.../{id}` (release to IDLE). 409 via
  `LeaseConflict`.
- **One shared Patchright API object per process** (semaphore-guarded singleton in `wiring.py`) —
  browser-use `GLOBAL_PATCHRIGHT_API_OBJECT` pattern; every `open()` launches its own context.
- **Proactive crash detection**: subscribe `page.on("crash")`/`context.on("close")` at open,
  flip `ContextRef` health on event (browser-use `CrashWatchdog`), not poll-only.
- **`cdp_patches` shared-Xvfb concurrency spike** *(review)* — **one-day timeboxed, P0**: does the
  Linux input path move a display-global pointer (XTEST) or window-targeted events? If global,
  concurrent OS-level input on one `:99` corrupts sessions → fallback to Xvfb-per-context or
  per-display serialized dispatch, which changes capacity math. Same rigor as the `aria-ref=` spike.
- **Snapshot epoch** wired from P0 in `SnapshotAction`/`AXSnapshot` even though refs are consumed
  in P1.
- **Docker Compose** (single `agentpilot` service, gateway+worker collapsed): Debian base +
  `patchright install chrome` + Xvfb + `python-xlib`; entrypoint
  `Xvfb :99 ... & export DISPLAY=:99; exec uvicorn agentpilot.gateway.app:app`; **`cap_add: [SYS_ADMIN]`**,
  no `--no-sandbox`; `shm_size: '2gb'`; volume for `/var/lib/agentpilot/profiles`; `detection-page`
  static server; no Redis yet.
- **Detection regression test**: own static fixture inlining `navigator.webdriver`, `window.chrome`,
  a `Runtime.enable` timing side-channel, and the page/screen-coordinate check `cdp_patches`
  targets. Asserted in CI every P0+ change.
- **Prometheus `/metrics` skeleton** stood up now so P1 metrics have a home.

**Exit**: `docker compose up` → open → `execute([Navigate, Snapshot])` → refs → `execute([Extract(markdown)])`
returns clean content → `execute([Click(ref)])` (stubbed until P1 or minimal) → detection page shows
no `Runtime.enable`/webdriver/coordinate leak → cdp_patches concurrency spike resolved → `lint-imports`
green → `docs/capacity-planning.md` exists.

### P1 — registry + lease + ref→locator + live-view + memory pressure + metrics

**Files**: `agentpilot/session/{registry.py, lease.py, reaper.py}`, `agentpilot/driver/ref_cache.py`,
`agentpilot/driver/live_view.py`, `agentpilot/gateway/routes/live_view.py`, `agentpilot/observability/*`.

- `registry.py`: in-memory `dict[IdentityKey, ContextRef]` + `dict[LeaseId, Lease]` guarded by a
  **per-`IdentityKey` `asyncio.Lock`** (port of a get-or-create-under-lock pattern from the prior
  internal system). Invariant enforced here first: **≤1 ACTIVE per IdentityKey** (P2 re-implements
  as Lua).
- `reaper.py`: idle-timeout scan; vault (stubbed until P2) before destroy; release → IDLE; only
  reaper destroys. **+ memory-pressure LRU eviction + per-process ceiling** (see §Memory-pressure).
- **Ref→locator hardening**:
  1. Spike: does Patchright's binary support `aria-ref=` resolving `aria_snapshot(mode="ai")` refs?
     One-day timeboxed.
  2. Fallback (build regardless — `ref_cache.py`, from browser-use `get_locate_element`/
     `_enhanced_css_selector_for_element` + `MatchLevel` cascade), a graceful-degradation cascade:
     **EXACT** (attr+position hash) → **STABLE** (hash minus dynamic classes) → **XPATH**
     (re-derive CSS from xpath + attr allowlist, iframe-chain via `frame_locator()`, fall back to
     `xpath=`) → **AX_NAME** → **ATTRIBUTE**. Cache `ref → Locator` per `ContextRef`, invalidated on
     next navigate/snapshot. **Epoch enforcement** *(review)*: the cascade runs only for refs from
     the current epoch; a ref from a superseded epoch raises `StaleRefError(epoch_superseded=True)`
     immediately — no cascade, no lookalike click. **Visibility**: require non-empty `bounding_box()`
     (w/h>0), not just `is_visible()`. **Occlusion**: confirm the element under the click coordinate
     matches before dispatching; JS-`.click()` fallback when CDP-geometry can't find a clean quad.
  3. Click/fill/select/hover dispatch inside `execute()`: resolve `bounding_box()` center → if
     headful, per-`ContextRef`-cached `AsyncInput(browser=context)` OS-level; else the Locator's own
     `.click()`/`.fill()`/`.select_option()`/`.hover()`.
- **Lease renewal** *(review)*: `execute()` renews the lease; expiry mid-execute completes the batch
  then releases (never mid-batch teardown).
- **Snapshot token budget** *(review)*: `SnapshotAction.viewport_only`/`max_nodes`/`roles` filtering
  implemented — `aria_snapshot` on a real commerce page is enormous; agents choke without it.
- **Live view (view-only)**: `driver/live_view.py`, `gateway/routes/live_view.py`.
- **Prometheus metrics** (see §Observability) wired across registry/reaper/driver.
- Revise `docs/capacity-planning.md` with **measured** per-context RAM **and CPU** from real P0
  containers (`docker stats`), replacing estimates.

### P2 — vault + sticky + Redis registry + placement/routing/admission + role split

**Files**: `agentpilot/identity/{profile_store.py, vault.py, proxy_pinning.py}`,
`agentpilot/session/registry.py` (→ Redis, same interface), `agentpilot/session/lua/*.lua`,
`agentpilot/placement/*`, `agentpilot/gateway/routes/internal.py` (worker surface), role split in `wiring.py`,
`agentpilot/egress/httpx_guard.py`.

- `vault.py`: wraps driver `export_state()`/`import_state()`; its own job is encryption-at-rest +
  tenant-scoped keys (`vault/{tenant}/{domain}/{name}.json.enc`). **Trigger** (avoids the
  persistent-dir vs storage_state conflict browser-use warns about): `restore_state()` runs only
  when `open()` targets a **fresh** profile dir (new node, or reaper-recreated), never onto an
  already-warm dir. **+ checkpoint on release-to-IDLE** *(review)* to bound node-loss staleness.
- `proxy_pinning.py`: `ProxyPinner.get_or_assign(identity)` backed by `HSETNX proxy:{identity}` —
  assign-once-keep-for-life across restarts, same pattern as the prior internal system's proxy pool.
- **Cross-tenant enforcement**: `profile_store.py` path-builder rejects any `IdentityKey` whose
  resolved path escapes the tenant root (`../` in domain/name) — `test_profile_store_rejects_path_traversal`.
- Redis Lua (`acquire_lease.lua`, `release_lease.lua`, `bind_active_context.lua`,
  **`place_session.lua`**, `rate_governor.lua`) — atomic cross-node invariants; loaded via
  `register_script()`, wrapped so callers never see raw Lua. `place_session.lua` shares keys with
  the lease Lua (the reason placement is designed here).
- **Role split** *(review)*: `--role gateway|worker`; gateway-role graph excludes `agentpilot.driver`;
  worker serves `/internal/...`. Compose gains a second gateway-role service.
- **Full egress** *(review)*: `httpx_guard.py` post-DNS IP validation for the basic tier.
- **Redis HA**: Sentinel; gateway fails closed on outage.
- Tests: `test_placement_affinity`, `test_open_no_capacity_returns_503`,
  `test_node_death_clears_affinity_and_routes`, `test_session_open_conflict_returns_409`,
  `test_redis_outage_fails_closed`.

### P3 — challenge handling

**Files**: `agentpilot/driver/challenge/{detector.py, turnstile.py, interstitial.py}`.

- `detector.py`: structural port of a challenge-classification enum (`ROBOT_CHECK`, `WRONG_COUNTRY`,
  `FORBIDDEN`, ...) from the prior internal system — cheap DOM/status heuristics.
- Turnstile/interstitial **solving** is new (the prior system only classifies+retires). Study
  Scrapling for technique; **verify license/importability first** — dedicated spike at P3 start.
- On `ChallengeDetected`, driver raises the typed error; P4's `success_tracker` decides burn/rotate.
  P3 stops at detect + one in-driver solve; on failure the session goes "needs human" and the
  interactive Live View becomes the operator fallback — not an immediate identity burn.

### P4 — control plane

**Files**: `agentpilot/control/{success_tracker.py, rate_governor.py, tier_router.py, proxy_vendor.py}`.

- `success_tracker.py` — the most portable pattern from the prior internal system's privacy-context
  model: per-identity `(tasks, successes, warnings)` in Redis hashes. Defaults:
  `is_high_failure_rate = tasks > 100 and (1 - successes/tasks) > 0.6`;
  `mark_warning/mark_leaked/mark_success`; retire at `warnings >= 8` (`PRIVACY_MAX_WARNINGS`) →
  release pinned proxy, clear for reassignment with a **new** pin next use — never a mid-life swap.
- `rate_governor.py` — token-bucket per `(tenant, domain)` backing the gateway's real
  `X-RateLimit-*`/`Retry-After` headers. New (the prior internal system has no multi-tenant
  governance). Also backs `wait_for_lease`/`wait_for_capacity` bounded queues (per-tenant caps).
- `tier_router.py` — new, API-shaped (Firecrawl `basic|stealth|enhanced|auto`): `"auto"` =
  retry-with-escalation — start cheap `httpx` (egress-guarded), on failure signal re-run on full
  Patchright+residential, record `tier_used`. Where §Capacity economics become operational.
- `proxy_vendor.py` — pluggable `ProxyVendorLoader` per vendor, generalizing the prior internal
  system's per-vendor proxy loader family.

### P5 — consumer SDKs + MCP server (the agent-tool surface)

- **Python SDK** from FastAPI OpenAPI (thin `httpx` wrapper). Adopt Firecrawl SDK ergonomics: flat
  kwargs mirroring the schema 1:1, sync/fire-and-forget method pairs
  (`client.execute(...)` polls; `client.start_execute(...)` returns a job id), a `Watcher` class
  wrapping WS/SSE status streaming with polling fallback. Keep backward-compat method aliases in
  mind — LLM-agent callers hallucinate prior method names.
- **MCP server** *(review — the literal "tools exposed to agents" ask)*: `agentpilot.mcp` wraps
  sessions/execute as MCP tools (Playwright-MCP is the reference shape), so any MCP-speaking agent
  gets warm-session browsing as a native tool. The snapshot token budget (P1) is what makes these
  tools usable — an unbounded `aria_snapshot` blows an agent's context.

---

## Testing strategy

- **Driver-contract suite** (`tests/driver_contract/`): one `pytest` module parametrized over
  driver fixtures (`patchright_driver` now, `nodriver`/`agent_browser` stubs later), asserting
  *behavior*: open→navigate→snapshot yields refs; click(ref) mutates state; **extract(markdown)
  returns clean main content**; export/restore round-trips cookies+localStorage across a fresh
  context; close() idempotent; health() reflects a killed process; **stale-epoch ref raises**. Any
  new driver passes this unmodified — the entire point of `spi`.
- **Never mock the browser** (browser-use discipline): component tests (`ref_cache` cascade,
  `execute()` batch-abort, challenge detector, extraction transform) run **real** Patchright
  contexts against **`pytest-httpserver`** inline HTML — fast, no compose, no external-site flake.
  Reserve compose-based `test_seam_e2e.py` for the one true end-to-end path.
- **Placement tests** (P2): affinity hit/relocation, 503 on exhaustion, node-death re-route,
  path-traversal rejection, Redis-outage fail-closed.
- **Egress tests**: metadata/RFC1918 blocked for browser and httpx tiers; DNS-rebinding rejected.
- **P0 detection regression**: own static fixture, not an external site.
- **import-linter** as its own CI job.

---

## Verification (end-to-end, once P0 lands)

1. `docker compose up` in `agentpilot`.
2. `POST /v1/sessions` with a test `IdentityKey` (+`tier`, `headful`) → session id; repeat with the
   same identity from a second "owner" → **409** (+ `Retry-After`).
3. `POST /.../execute` `[Navigate(detection_page), Snapshot()]` → `ActionResult` snapshot has refs
   carrying an epoch.
4. `POST /.../execute` `[Extract(format="markdown")]` → clean main-content markdown, no boilerplate.
5. `POST /.../execute` `[Click(ref)]` → page state changed; a stale-epoch ref → `StaleRefError`.
6. Detection page's own JS reports no `Runtime.enable` emission and no page/screen coordinate
   mismatch.
7. Egress: `Navigate("http://169.254.169.254/...")` and the httpx tier both rejected `EGRESS_BLOCKED`.
8. `DELETE /.../{id}` → context returns to IDLE (not destroyed); reaper test confirms destroy only
   after idle-TTL + vault save; a memory-watermark test confirms below-TTL LRU eviction.
9. (P2) Two-node compose: open pins to a node; kill it → next open for that identity re-routes +
   vault-restores; capacity exhaustion → **503 + Retry-After**.
10. `lint-imports`, `pytest tests/driver_contract/`, egress + placement suites all green.
11. `docs/capacity-planning.md` present, CPU-corrected, matches `scripts/capacity_model.py`.