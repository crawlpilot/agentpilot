# Capacity planning (P0 first pass)

First-pass estimate at the target of **2,000,000 crawls/hour** (≈556 requests/s),
computed by `scripts/capacity_model.py`. This is CPU-corrected, not RAM-only —
the fleet is CPU-bound before it's RAM-bound, which is the concrete
justification for P4's tier router (see below), not an optimization.

Run it yourself: `uv run python scripts/capacity_model.py`.

## Headline numbers

| Metric | Value |
|---|---|
| Concurrent contexts (10s avg exclusive hold) | ~5,600 |
| RAM for Chrome alone (350MB avg/context) | ~1.9 TB |
| Nodes, RAM-bound (180 ctx/64GB node) | ~30 |
| Nodes, CPU-bound (see below) | ~43 |
| **Nodes recommended** (max of both + reaper/HA slack) | **~57** (realistically 40-60) |
| Bandwidth if *all* traffic were full-browser + residential | ~94 TB/day |
| Cost if *all* traffic were full-browser + residential | ~$47K/day (~$1.4M/month) |

The last two rows are the **worst case** — every request routed through a full
Chrome context over a paid residential proxy. That worst case is exactly the
scenario P4's `tier_router` exists to avoid: routing the majority of traffic
through the cheap `httpx` "basic" tier and reserving full-browser+residential
for requests that actually need it collapses the bandwidth/cost line by
5-10x (see the second scenario printed by `capacity_model.py`). This is the
concrete number behind "the tier router is a cost necessity, not an
optimization."

## Why CPU, not just RAM

A 64GB node fits ~180 contexts at 350MB each — the RAM-only estimate. But a
Chrome process actively rendering a page needs ~0.5-1 vCPU during that
render, and a 64GB node only has 16-32 vCPU. Not every concurrently-*held*
context is mid-render at any given instant (most warm contexts are
idle-but-resident), so the model uses `active_render_fraction` (default
`0.25`, i.e. a node's contexts are assumed 25% "hot" at any snapshot) as the
CPU-bound multiplier. **This fraction is an unmeasured placeholder** — P1
must replace it with a real number from `docker stats` + wall-clock CPU
sampling across contexts under realistic load. Whichever of the RAM-bound or
CPU-bound node count is larger wins; here CPU-bound (~43) already exceeds
RAM-bound (~30) even at a conservative 25% hot-fraction assumption, so the
real fleet is very likely CPU-limited well before RAM-limited, meaning the
true node count is probably higher than the RAM-only estimate would suggest,
not lower.

## Spike findings

Three P0-mandated one-day spikes, resolved by reading source / running real
Patchright+Chrome rather than by assumption:

### 1. `cdp_patches` shared-Xvfb concurrency (resolved: NOT safe to share)

Question: does `cdp_patches`' Linux OS-level input dispatch move a
display-global pointer, or is it window-targeted?

**Answer: display-global.** `cdp_patches/input/os_base/linux.py` dispatches
every mouse/keyboard action through `Xlib.ext.xtest.fake_input()` — the X
Test extension, which injects synthetic input at the X **server** level.
`move()` calls `fake_input(display, X.MotionNotify, x=x, y=y)` with absolute
display coordinates and no window handle; `down()`/`up()`/`send_keystrokes()`
likewise take no window parameter. XTEST warps the **one shared pointer** a
display has and delivers keyboard events to whatever window currently holds
input focus. There is exactly one pointer/focus per X display.

**Implication**: two concurrent headful+interactive sessions sharing one
Xvfb display (`:99`) *will* race for the same pointer and steal each other's
keyboard focus — this would corrupt sessions, not just look wrong. **P1 must
give each headful session its own Xvfb display** (`:100`, `:101`, ...) once
interaction verbs (Click/Fill/...) actually dispatch OS-level input, or
serialize dispatch per shared display (which would bound interactive-session
throughput per display and change the capacity model again). P0 itself is
unaffected — no P0 action dispatches OS-level input yet (`ProcessLauncher`
starts one shared `:99` purely so headful Chrome has somewhere to render;
`cdp_patches.AsyncInput` isn't even constructed until P1) — but this is a
must-fix-before-P1-ships-Click finding, not a someday concern.

### 2. `Runtime.enable` detection (resolved: timing side-channel works, getter trick doesn't)

Two candidate techniques were tested against a real Patchright/Chrome
context with a manually-attached CDP session:

- **Getter-invocation on console.log preview** (a technique cited in older
  anti-bot writeups: log an object with a getter property, expecting
  DevTools' object-preview machinery to eagerly invoke it): **does not
  fire** on the Chrome version in this image. CDP reports the property as
  `"type": "accessor"` without evaluating it. This leak appears to have been
  patched upstream; it is **not** included in `tests/fixtures/detection_page`
  since shipping it would always silently "pass" regardless of true leak
  state.
- **console.log timing overhead**: enabling the CDP Runtime domain forces
  V8 to build and send a `Runtime.consoleAPICalled` protocol event on every
  `console.*` call, measurably slower even if nothing ever reads the event.
  20,000 `console.log` calls: ~25-31ms with no CDP Runtime listener attached,
  ~100-145ms with one attached (measured standalone and on the actual
  fixture page) — reproducible across repeated runs, a >2x gap. This is what
  `tests/fixtures/detection_page/detect.js` checks, with a 70ms threshold
  (comfortably between the two clusters for this reference container).

**Known limitation**: the threshold is calibrated for *native* execution.
Running the same check under QEMU/Rosetta emulation (e.g. building this
image for `linux/amd64` on an Apple Silicon host via Docker Desktop) produces
inflated timings (~170ms observed) even with **no** CDP Runtime listener
attached — a false positive caused by emulation overhead, not a real leak.
CI and production must run on native hardware matching the image's target
architecture for this check to be meaningful; it is not reliable under
cross-architecture emulation.

### 3. Official Chrome is amd64-only on Linux

`patchright install --with-deps chrome` **hard-fails** on `linux/arm64`
("ERROR: not supported on Linux Arm64") — Google does not ship official
Chrome for Linux ARM64 at all, only Chromium. Since the plan locks in real
Chrome (not Chromium) for sandbox fidelity, `docker-compose.yml` pins
`platform: linux/amd64` for the `agentpilot` service rather than silently
substituting Chromium on ARM dev hosts. This matches the actual fleet target
(cloud nodes are overwhelmingly amd64 for this exact reason) but means local
builds on Apple Silicon run under Rosetta/QEMU emulation — slower, and per
finding #2, not valid for the timing-based detection check.

### 4. A blanket RFC1918 block also blocks the container's own network (resolved)

Discovered running the real compose stack, not by inspection: the
`detection-page` sidecar became unreachable from `agentpilot` as soon as a session
opened (which triggers `apply_baseline()`). Docker Compose's default bridge
network sits at `172.18.0.0/16` — inside the `172.16.0.0/12` RFC1918 range
the baseline blocks. A blanket per-container "deny all private ranges" rule
doesn't just block the tenant's browsed page from reaching internal
infrastructure; it blocks the *container's own* legitimate traffic on its
directly-connected network, which is exactly the network real workers use to
reach Redis/the registry in P2. Same shape of problem on real cloud infra:
a worker's own VPC subnet is RFC1918 too.

Fixed in `agentpilot.egress.policy` by reading the container's directly-connected
routes from `/proc/net/route` (Linux-only, no extra package needed) and
inserting `ACCEPT` rules for those specific subnets *ahead of* the broader
`REJECT` rules — narrower than exempting all of RFC1918, so `169.254.169.254`
and *other* private ranges stay blocked; only the container's own segment is
trusted. This is a genuine, if partial, stand-in for the plan's "Chrome
uid/netns"-scoped enforcement, which needs Chrome running as a distinct OS
user to implement properly — not yet true in P0's single collapsed
gateway+worker container (this baseline still blocks the gateway process's
own egress too, not only Chrome's).

### 5. IDLE sessions leave Chrome processes (and profile locks) running

Also surfaced by running the real stack across repeated test invocations: a
released (IDLE) session is exactly as documented -- P0 has no reaper, so nothing
ever calls `driver.close()` on it. Re-opening a session for the *same*
`IdentityKey` later in the same container's lifetime collides with the still-
running old Chrome process's `SingletonLock` on that profile dir
("`Failed to create a ProcessSingleton ... profile already in use`"). This
isn't a bug so much as the P0→P1 gap manifesting one layer lower than
expected -- not just "the gateway's in-memory active-identity map doesn't
enforce this anymore," but "the OS-level Chrome process is still there
holding the file lock too." `tests/test_seam_e2e.py` works around it with a
unique identity per test run; **P1's reaper is the real fix** and this is a
concrete argument for landing it promptly rather than letting the compose
demo silently mask how quickly unclaimed IDLE contexts pile up.

## P1 update: interaction verbs shipped without touching the Xvfb question

Finding #1 above flagged giving each headful session its own Xvfb display as
a **must-fix-before-P1-ships-Click** item, on the assumption that clicking
would dispatch through `cdp_patches.AsyncInput` (XTEST, display-global). P1's
actual `ref_cache.py` + `patchright_driver.py` dispatch Click/Fill/
SelectOption/Hover/Press/Scroll through Playwright/Patchright's own native
`Locator` methods instead -- CDP `Input.dispatchMouseEvent`/
`dispatchKeyEvent` scoped to that page's own session, not the X server's
single shared pointer. `cdp_patches.AsyncInput` is still never constructed
anywhere in this codebase. **Finding #1's shared-Xvfb hazard is therefore
still real but still not yet triggered** -- it only applies once/if a future
phase adds OS-level XTEST-based input for extra stealth on top of the
native dispatch that ships in P1. Tracked, not resolved, not currently a P1
blocker.

The `aria-ref=` spike (plan.md's "does Patchright's binary support resolving
`aria_snapshot(mode="ai")` refs" question) resolves **yes** --
`page.locator(f"aria-ref={ref}")` works against this image's Patchright/
Chrome build; see `driver/ref_cache.py`'s module docstring.

## P1 reaper

P0 finding #5 (IDLE Chrome processes/profile locks never reaped) is fixed:
`agentpilot.session.registry.Registry` reuses a released identity's still-warm
context on reopen (no second Chrome onto the same profile dir), and
`agentpilot.session.reaper.Reaper` destroys genuinely-idle contexts past
`AGENTPILOT_IDLE_TTL_SECONDS` (default 300s), evicts oldest-IDLE-first under node
memory pressure (`/proc/meminfo`, default 85% watermark), and kills any
single context whose Chrome process RSS exceeds `AGENTPILOT_PER_PROCESS_CEILING_MB`
(default 4096MB) regardless of TTL.

## Refinement plan

Every estimate above is still first-pass; replacing them with measured
numbers from `docker stats` (RAM/CPU per context) and wall-clock sampling
for `active_render_fraction` remains open, now trackable via the P1
`/metrics` endpoint (`agentpilot_contexts_active`/`agentpilot_contexts_idle`,
`agentpilot_execute_duration_seconds`, `agentpilot_session_open_duration_seconds`)
instead of only ad hoc `docker stats` snapshots.
