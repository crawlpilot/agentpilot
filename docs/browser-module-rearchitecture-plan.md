# browserpilot — Implementation Plan

Extract the browser platform out of `agentpilot` into `browserpilot`: a separately
built, separately versioned, independently installable Python project that
`agentpilot` consumes as an ordinary third-party dependency.

**Repo:** `baas-crawlpilot` · Python 3.12 · uv 0.9.28 + hatchling · ~23k LOC
**Status:** ready to implement · **Date:** 2026-08-29

---

## 1. Goal and scope

**Done means:**

1. `browserpilot` builds and ships without `agentpilot` present — own
   `pyproject.toml`, version, tests, CI job, wheel.
2. `agentpilot` declares `browserpilot>=0.1,<0.2` in `dependencies` and writes
   `from browserpilot import BrowserSession` — the same line an unrelated crawler
   in `Browser4` or `crawlPilot` writes. No privileged access.
3. `pip install browserpilot[engine,markdown]` yields a working browser platform
   with **no FastAPI, uvicorn, Postgres, prometheus, or Redis** in its closure.
4. Every browser capability exists three ways from **one** definition: a plain
   awaitable function, a `BrowserSession` method, and an agent tool with a
   generated schema. Markdown extraction included.
5. `browserpilot` contains **no tenancy and no per-site knowledge**. Both are
   injected at boot by the platform.
6. Site-specific behaviour is added by **extension package**, not by editing the
   library.

**Explicitly out of scope:** behavioural change to `agentpilot.{agent,recipe,crawl,jobs}`
beyond updating moved imports; new browser features; performance work.

**A caller's whole program:**

```python
from browserpilot import Browser, BrowserConfig

async with Browser(BrowserConfig(tier="stealth")) as browser:
    async with browser.session() as s:
        await s.navigate("https://example.com/p/123")
        md = await s.markdown()
```

---

## 2. Current state

### 2.1 What is already right — preserve it

The codebase is **not** unstructured. It is layered, and the layering is enforced
mechanically. The work here is consolidation, facade and packaging — not a rewrite.

| Asset | Where |
|---|---|
| Protocol seam — `BrowserDriver`, batched `execute()` | `spi/driver.py` |
| 14 enforced layer contracts | `pyproject.toml` `[tool.importlinter]` |
| Composition-root discipline — only `wiring.py` imports the driver | `gateway/wiring.py` |
| Pure leaves depending on `spi` alone | `extraction/`, `egress/`, `dom/` |
| Browser-free extraction pipeline | `extractor.py` → `sanitizer` → `markdown_converter` → `postprocess` |
| Extras proving a Chrome-free image works | `[driver]`, `[postgres]`, `[bedrock]` |
| Driver contract suite — the acceptance harness | `tests/driver_contract/` (12 files) |

### 2.2 The twelve defects

| # | Defect | Evidence | Fixed in |
|---|---|---|---|
| D1 | No public API surface | Every `__init__.py` empty or a docstring; `agentpilot/__init__.py` is 0 bytes; no `py.typed` anywhere | §3.2 |
| D2 | No facade; entry points are 15–18-kwarg free functions requiring a 524-line composition root | `run_ephemeral_scrape()`, `open_interactive_session()`, `gateway/wiring.py` | §3.4 |
| D3 | Tier policy spread over 6 files, vocabulary declared 3× | `spi/actions.py:44,57,64`; `session/stealth_profile.py:38,62`; `session/ephemeral.py::_ESCALATION`; `driver/humanize.py:145`; `identity/proxy_config.py:72`; `gateway/schemas.py:23,213,422` | §3.6 |
| D4 | Identity/stealth straddles the `session`/`identity` boundary; `identity/` is two domains | `session/browser_headers.py`, `session/stealth_profile.py`; fingerprint+vault vs proxy×3 | §3.2 |
| D5 | The action vocabulary is mirrored three times, hand-synced | `spi/actions.py` (dataclasses), `agent/actions.py` (Pydantic — its docstring admits the duplication), `gateway/schemas.py::ActionIn` + `action_conversion.py` | §3.5 |
| D6 | Markdown reachable only through a driver batch | `extraction.extract()` is pure but unexported; results correlate positionally via `ActionResult.extracts: list[str]` | §3.5 |
| D7 | Two DOM implementations; `agentpilot.dom` is in **no** layer contract | `driver/dom_fusion*.py` (384 LOC) + `fused_locators.py` vs `dom/serializer.py` + `clickable_elements.py` + `paint_order.py` | §3.2 |
| D8 | Leaf modules read process env directly | `identity/{fingerprint,proxy_config,profile_store,proxy_health}.py`, `driver/process_launcher.py`, `egress/policy.py`, `extraction/site_checkers.py`, `recipe/config.py` | §3.3 |
| D9 | Pooling is mandatory even for one-shot use | Every entry point demands a `RegistryProtocol` + lease TTL | §3.4 |
| D10 | **Tenancy and per-site policy welded into the core** | `IdentityKey(tenant, domain, name)` — `.slug()` makes the SaaS tenancy model the on-disk layout; `ProxyConfig.resolve(tenant, tier)`; `prototype_dir_for(domain)`; `site_checkers.py` ships `_WALMART_ITEM_MIN`/`_AMAZON_CAPTCHA` and `install_default_site_checkers()` fires **at import** | §3.7 |
| D11 | **Redis is a hard dependency of the browser core** | `session/redis_registry.py` (+6 Lua scripts), `identity/proxy_pinning.py`, `identity/proxy_health.py`, `identity/burn_tracker.py` — all cross-process shared state; only one has a seam | §3.7 |
| D12 | **Site behaviour can only be added by editing the library** | `block_detect._SITE_CHECKERS` is a module-level global populated at import; no hook for URL rewriting, warm-up, block *resolution*, markup repair, or enrichment | §3.8 |

D10–D12 are the structural ones. D1–D9 are consequences of never having drawn a
product boundary.

---

## 3. Target architecture

### 3.1 Two projects

`browserpilot` takes a **distinct top-level package name**, not a subpackage of
`agentpilot`: the import must carry no implication that the caller is inside
agentpilot, and this sidesteps PEP 420 namespace-package tooling risk (mypy,
hatchling, import-linter root resolution) entirely.

```
baas-crawlpilot/                     # uv workspace root
├── pyproject.toml                   # [tool.uv.workspace] members = ["packages/*"]
├── uv.lock                          # ONE lock for the workspace
└── packages/
    ├── browserpilot/                # PROJECT 1 — builds standalone
    │   ├── pyproject.toml           # own version; deps: httpx[http2], brotli, zstandard
    │   ├── README.md                # a public artifact, documented as one
    │   ├── src/browserpilot/…       # src-layout
    │   └── tests/                   # no agentpilot import anywhere
    └── agentpilot/                  # PROJECT 2 — depends on project 1
        ├── pyproject.toml           # ["browserpilot>=0.1,<0.2", fastapi, redis, …]
        ├── agentpilot/{gateway,agent,recipe,crawl,jobs,auth,placement,llm,control}/
        └── tests/
```

`src/` layout is deliberate: it makes importing from the source tree instead of
the installed wheel impossible, which is the exact failure that would let an
unnoticed dependency creep back in.

**Same repo now, separate repo later.** Keep both in `baas-crawlpilot` through the
migration — atomic cross-boundary commits are worth a lot while the API moves.
This already satisfies "built separately". Promote to a standalone repo once the
API has been stable for a release or two; that is
`git filter-repo --subdirectory-filter packages/browserpilot` plus a remote, with
**no code change**, because the dependency was always a version range.

> **⚠ `browserpilot` is taken on PyPI** — version 0.2.53, *"Natural language
> browser automation"*, by a different author, depending on selenium/openai/
> llama-index. The Phase 0 spike hit this for real: an install without index
> pinning resolved `browserpilot>=0.1,<0.2` to that stranger's **0.1.4** from
> PyPI, installed it as a namespace package that shadows ours, and failed at
> import. `agentpilot` is *also* taken (0.2.0rc1, an unrelated desktop app).
> `crawlpilot` and `browserpilot-core` are free.
>
> Until the name is settled (§8.1), **every consumer install must pin the name to
> the internal index explicitly** — this is not optional hardening, it is the
> difference between installing our wheel and a stranger's:
>
> ```toml
> [[tool.uv.index]]
> name = "internal"
> url  = "file:///srv/wheels"     # a local dir now; a private index later
> explicit = true                 # only used by packages that name it
>
> [tool.uv.sources]
> browserpilot = { index = "internal" }
> ```
>
> Verified in the spike: this resolves our 0.1.0 (`Requires: httpx`) rather than
> PyPI's 0.1.4 (`Requires: selenium, openai, …`), while `fastapi` and every other
> dependency still resolve from PyPI normally. `--no-index` is **not** a
> workable alternative — it blocks PyPI entirely and cannot resolve `fastapi`.

**Local development — one declaration, two resolutions:**

```toml
# packages/agentpilot/pyproject.toml
dependencies = ["browserpilot>=0.1,<0.2", "fastapi>=0.115", ...]

[tool.uv.sources]
browserpilot = { workspace = true }   # editable path locally;
                                      # the published wheel in CI/prod
```

### 3.2 Module layout

```
packages/browserpilot/src/browserpilot/
├── __init__.py      # THE public API: __all__, re-exports, nothing else   (D1)
├── py.typed
├── api.py           # Browser, BrowserSession — the facade                (D2, D9)
├── config.py        # BrowserConfig/ProxyConfig/StealthConfig + from_env() (D8)
├── errors.py        # public exception hierarchy
│
├── contracts/       # ← spi/     Protocol seam, actions, results,
│                    #            IdentityRef (opaque — no tenancy)        (D10)
├── policy/          # Proxy/Prototype providers + ProxyPinStore /
│                    # ProxyHealthStore / BurnStore, all with in-process
│                    # defaults — the injection seam                 (D10, D11)
├── extensions/      # mounts, hook chains, manifest, dispatch, discovery  (D12)
├── engine/          # ← driver/  patchright_driver, process_launcher, mouse,
│                    #            humanize, warmup, ref_cache, live_view
├── dom/             # ← dom/ + driver/dom_fusion*, fused_locators (merged) (D7)
├── content/         # ← extraction/  extract(), to_markdown(), sanitizer,
│                    #   structured_data, block_detect (generic only — no
│                    #   site rules, no site-rule type)                    (D10)
├── identity/        # fingerprint, headers (← session/browser_headers.py),
│                    # profiles/{store,vault,seeding}, burn scoring         (D4)
├── proxy/           # config, pinning, health  (← identity/*)              (D4)
├── stealth/         # profile (← session/stealth_profile.py), warmup       (D4)
├── tiers/           # Tier, TierPolicy, ESCALATION — the only tier module  (D3)
├── net/             # ← egress/ + session/http_fetch.py
├── pool/            # ← session/  registry (IN-MEMORY ONLY), lease, warm_pool,
│                    #   reaper, acquire, rotation. redis_registry.py + lua/
│                    #   move to agentpilot                            (D9, D11)
└── tools/           # @tool registry — vendor-neutral ToolSpec + JSON Schema;
                     # optional adapters/ for provider shapes, no SDK      (D5)
```

**Internal layer contract** (`root_package = "browserpilot"`):

```
api → tools → extensions → {pool, stealth, tiers} → {engine, identity, proxy}
    → {dom, content, net} → policy → contracts
```

`policy` sits just above `contracts` so every layer may consume an injected
provider and none may reach around one to a hard-coded default. Plus a
`forbidden` contract: nothing in `browserpilot` may import `agentpilot`,
`fastapi`, `starlette`, `psycopg`, `prometheus_client`, or `redis`.

### 3.3 Dependencies, extras, configuration

| Extra | Pulls | For |
|---|---|---|
| *(base)* | `httpx[http2]`, `brotli`, `zstandard` | HTTP fast-path tier, content pipeline |
| `[engine]` | `patchright`, `re-cdp-patches` | real Chrome |
| `[markdown]` | `lxml`, `cssselect` | HTML → Markdown / structured data |
| `[vault]` | `cryptography` | encrypted profile storage |
| `[all]` | the above | |

**No `redis` extra, deliberately** (D11 — see §3.7). An extra would still put
Redis semantics in the library's API; a Protocol means a single-process crawler
never learns Redis exists.

**Config is arguments, not environment** (D8). `BrowserConfig`, `ProxyConfig`,
`StealthConfig`, `ProfileConfig` are frozen dataclasses with `.from_env()`
classmethods called *only* at a composition root. No leaf module reads
`os.environ`.

**The Chrome-free gateway image survives.** `agentpilot`'s gateway role needs
`browserpilot`'s types but never launches Chrome, so `gateway.Dockerfile` installs
`browserpilot` **without** `[engine]` while `worker.Dockerfile` installs
`browserpilot[engine,markdown,vault]`. Both Dockerfiles' dep-cache layer must
change from a single root `pyproject.toml` to `COPY packages/*/pyproject.toml uv.lock ./`.

### 3.4 The facade

```python
class Browser:
    def __init__(self, config: BrowserConfig | None = None, *,
                 driver: BrowserDriver | None = None,
                 registry: RegistryProtocol | None = None,
                 proxy_provider: ProxyProvider | None = None,
                 prototype_provider: PrototypeProvider | None = None,
                 extensions: Sequence[Extension] = ()): ...
    #  Every argument defaults to something inert-but-working: PatchrightDriver,
    #  in-memory Registry, NullPrototypes, StaticProxies([]), no extensions.
    #  A one-shot caller passes nothing (D9); the platform injects.

    async def __aenter__(self) -> Browser: ...
    def session(self, *, identity: IdentityRef | str | None = None,
                tier: Tier | str = "auto", **overrides) -> AsyncContextManager[BrowserSession]: ...
    async def scrape(self, url: str, *, formats=("markdown",), **opts) -> Document: ...

class BrowserSession:
    async def navigate(self, url, *, wait_until="load") -> NavigateResult: ...
    async def click(self, ref: str) -> ActionResult: ...
    async def fill(self, ref: str, text: str) -> ActionResult: ...
    async def markdown(self, *, main_content=True) -> str: ...
    async def snapshot(self) -> DomSnapshot: ...
    async def execute(self, actions: list[Action]) -> ActionResult: ...   # batch escape hatch
```

`identity` **defaults to `None`**, which mints a fresh throwaway scope whose
profile dir is deleted on teardown — a cookie-less first-visit browser, exactly
what `run_ephemeral_scrape` does today when `session_name is None`. You pass an
identity only when you want the *opposite*: stickiness across calls, so repeat
visits reuse one profile, one pinned proxy and one fingerprint and look like a
returning visitor. It is an opaque scope handle, never a domain (§3.7); the URL
goes to `navigate()`/`scrape()`, where it belongs. `interactive.py` and `ephemeral.py`
become the internals of `session()` and `scrape()`; their 15-arg signatures
collapse into `BrowserConfig` + per-session overrides. **Batching stays the
transport** — the facade composes batches and must never degrade into one round
trip per verb.

### 3.5 One definition, three surfaces (D5, D6)

One decorated async function per verb is the single source of truth:

```python
@tool(name="navigate", description="Navigate the current tab to an absolute http(s) URL.",
      safety="safe")                      # "sensitive" → excluded from agent defaults
async def navigate(
    session: BrowserSession,
    url: Annotated[str, Field(description="Absolute http:// or https:// URL")],
    wait_until: WaitUntil = "load",
) -> NavigateResult: ...
```

The decorator introspects the signature (`pydantic.create_model`, as
`browser_use/tools/registry/service.py` does) and derives: the **Pydantic param
model** (replacing `agent/actions.py`), a **plain JSON Schema**, the **`Action`
dataclass binding** for the batch path, and a registry entry. All three of D5's
mirrors collapse into this.

**The registry is vendor-neutral — it knows nothing about any LLM.** Its unit is
a `ToolSpec(name, description, params: type[BaseModel], json_schema: dict, fn)`.
That is the same discipline as browser-use, whose `Registry` yields a generic
`ActionModel` and leaves provider serialisation to its LLM layer. `browserpilot`
depends on no LLM SDK and must not grow a notion of "the Anthropic format" in
its core: an agent framework, an MCP server, a CLI, a test harness and a plain
crawler are all equally first-class consumers of a `ToolSpec`.

Provider shapes are then a **separate, optional adapter module**
(`browserpilot.tools.adapters`) — pure dict re-shaping over the JSON Schema the
registry already emits, importing no vendor SDK and adding no dependency, so it
stays useful out of the box without making the registry LLM-dependent. If an
adapter ever needs a vendor package, it does not belong in `browserpilot`.

```python
from browserpilot.tools import navigate, registry
await navigate(session, "https://example.com")            # plain function
await session.navigate("https://example.com")             # method
await registry.dispatch(session, "navigate", {...})       # generic dispatch

specs = registry.subset(exclude={"execute_js"}).specs()   # vendor-neutral
[s.json_schema for s in specs]                            # standard JSON Schema

from browserpilot.tools.adapters import to_anthropic      # optional, ~20 lines
to_anthropic(specs)
```

`subset(allowed=…, exclude=…, domain=…)` replaces
`agent/actions.py::DEFAULT_ALLOWED_ACTIONS`. Tool names are **namespaced**
(`browser.navigate`, `walmart.solve_wall`) and **registering a duplicate
namespace raises** rather than silently overriding.

Content follows the same rule (D6):

```python
from browserpilot.content import to_markdown, extract, structured_data
md = to_markdown(html)          # pure — no browser, no network
md = await session.markdown()   # method
registry["content.markdown"]    # agent tool
```

### 3.6 Tier policy (D3)

`browserpilot/tiers/` becomes the only place a tier means anything:

```python
class Tier(StrEnum):
    BASIC = "basic"; STEALTH = "stealth"; ENHANCED = "enhanced"; AUTO = "auto"

@dataclass(frozen=True)
class TierPolicy:
    stealth: bool                 # ← spi/actions.stealth_from_tier
    interact_profile: str         # ← spi/actions.interact_profile_for_tier
    delay_policy: DelayPolicy     # ← driver/humanize.for_tier
    proxy_tier: str | None        # ← identity/proxy_config.resolve
    warmup: bool
    detect_blocks: bool
    header_profile: HeaderProfile # ← session/browser_headers

    @classmethod
    def for_tier(cls, tier: Tier) -> TierPolicy: ...

ESCALATION: dict[Tier, tuple[Tier, ...]]   # lifted out of session/ephemeral.py
```

Consumers take a `TierPolicy`, never a `str`. The three `Literal` declarations in
`gateway/schemas.py` become one import of `Tier`. Lifting `ESCALATION` out of
`run_ephemeral_scrape` is what lets the agent and recipe paths reuse it.

**`EgressPolicy` is deliberately not here.** It is an SSRF guard —
`block_metadata` (the cloud metadata endpoint), `block_private` (RFC1918),
allow/deny host lists — and nothing in the codebase varies it by tier: all seven
construction sites pass a bare `EgressPolicy()`. It is a deployment safety
invariant, so it belongs in `BrowserConfig`, set once, defaulting to the safe
values. Folding it into `TierPolicy` would invent a coupling that does not
exist and would let a tier choice weaken an SSRF guard.

### 3.7 The policy-injection seam (D10, D11)

**The rule:** `browserpilot` knows about **URLs and origins**, because you cannot
drive a browser without them. It knows nothing about tenants, customers, named
retailers, or who owns a profile. Those are control-plane facts, resolved one
level up and injected at construction.

**(a) `IdentityKey` loses its tenancy.**

```python
@dataclass(frozen=True)
class IdentityRef:
    """Opaque handle for 'these requests share cookies, profile, and exit IP'.

    browserpilot never parses `key` — it validates filesystem-safety and uses it
    for scoping. Composition is the caller's business: the SaaS builds
    f"{tenant}/{domain}/{name}", a single-tenant crawler builds "example.com",
    and neither shape is privileged.
    """
    key: str
    kind: ProfileKind = ProfileKind.TEMPORARY
```

This keeps what the browser genuinely needs — stable scoping for cookie, proxy
and fingerprint stickiness — and drops the belief that identity is a 3-tuple with
a customer ID in it. **The rendered slug stays byte-identical**, so existing
profile dirs and vault entries remain valid; only the owner of the composition
changes.

**(b) Site knowledge leaves the library completely — it is consumer code.**
This is how general browser platforms work: Playwright, Puppeteer and Selenium
ship zero site knowledge, no site-rule type, and no rule store. They give you
primitives and hooks; whatever you know about a particular site lives in *your*
code. `browserpilot` does the same.

Concretely, that means the library ships **none** of the following: a `SiteRules`
data type, a rule-provider Protocol, a rule store or catalog, or any bundle of
retailer rules. Defining a schema for "what site knowledge looks like" is itself
site-rule management — it presumes every consumer models a site the way we do,
and it is the reason a `_WALMART_ITEM_MIN` constant found its way into a browser
library in the first place.

What stays is genuinely generic: `block_detect.classify_page`, which decides
"was I walled" from status codes and challenge-page markers that are not
site-specific. **Delete `install_default_site_checkers()` and its import-time
call** — global mutable state that makes behaviour depend on import order — and
delete the Walmart/Amazon/JD constants outright. That knowledge moves to
`agentpilot` as an extension it owns (§3.8), written against the same
`classify`/`resolve` hooks any other consumer would use. No privileged path.

**(c) Two provider Protocols**, each a constructor argument with an inert
default (`NullPrototypes`, `StaticProxies([])`):

```python
class ProxyProvider(Protocol):
    def endpoints_for(self, identity: IdentityRef, tier: Tier) -> Sequence[ProxyEndpoint]: ...
class PrototypeProvider(Protocol):
    def prototype_for(self, origin: str) -> Path | None: ...
```

Both are lookups the *consumer* implements against its own data model —
`browserpilot` defines no catalog and no schema, only the question it needs
answered. That is the line: a Protocol asking a question is a seam; a data type
plus a store plus shipped instances is management.

**(d) Shared state gets the same treatment — and that is how Redis leaves.**
All four Redis-backed modules answer cross-process questions; their own docstrings
say "shared across worker processes". That is a deployment property, not a browser
one.

| Today | Protocol in `browserpilot.policy` | Default | Redis impl |
|---|---|---|---|
| `RedisRegistry` | `RegistryProtocol` *(exists)* | `InMemoryRegistry` | `agentpilot/control/` |
| `ProxyPinner` | `ProxyPinStore` | `InMemoryPinStore` | `agentpilot/control/` |
| `ProxyHealth` | `ProxyHealthStore` | `InMemoryHealthStore` | `agentpilot/control/` |
| `BurnTracker` | `BurnStore` | `InMemoryBurnStore` | `agentpilot/control/` |

**The policy stays, only the storage moves.** `burn_tracker.py` keeps its
Pulsar-ported weighted scoring and takes a `BurnStore`; the `INCRBY`/`EXPIRE`
calls go. `redis_registry.py` and the six lease Lua scripts move wholesale.
(`place_session.lua` and `clear_stale_affinity.lua` belong to `placement/`, already
platform-side.)

Providers are resolved once at construction and cached. An optional
`async def refresh()` lets a cloud-config-backed implementation hot-reload;
`browserpilot` calls it only when asked and never schedules it — cadence is a
platform decision.

**Landing zone:** a new `agentpilot/control/` package (a name today's contracts
already reserve as an optional layer) holds the tenant→proxy mapping, the
domain→prototype catalog, and the four Redis stores. Site knowledge is not here
either — it is an extension (§3.8).
`gateway/wiring.py` constructs and injects them.

```python
browser = Browser(BrowserConfig(tier="stealth"))          # plain crawler: nothing to pass

browser = Browser(                                        # agentpilot, injected at boot
    BrowserConfig.from_env(),
    proxy_provider     = platform.proxy_pools,
    prototype_provider = platform.profile_catalog,
    registry           = platform.redis_registry,
)
```

### 3.8 The extension system (D12)

§3.7b sends site knowledge out of the library; this is the seam it leaves
through, and the **only** one. There is no second, data-shaped path — a consumer
that wants declarative rules writes a small extension that reads its own config,
which is its business, not the library's. The mechanism is not a new invention: `block_detect._SITE_CHECKERS` is already a
chain (`is_relevant(url)` → `check(...) -> Verdict | None`, first non-`None` wins,
`None` defers) — this promotes it from a module-level global to an injectable,
multi-hook, pip-installable registry, keeping the defer convention so porting the
existing checkers is mechanical.

**Mount points, not one fat Protocol.** An extension implements only the surfaces
it participates in, so the host knows what it touches without calling it and a
content-only extension never sees a driver.

```python
class BrowseMount(Protocol):     # launch → navigate → interact
    def configure_browse(self, h: BrowseHooks) -> None: ...
class ContentMount(Protocol):    # fetch → parse → extract
    def configure_content(self, h: ContentHooks) -> None: ...
class BlockMount(Protocol):      # detection AND resolution
    def configure_blocks(self, h: BlockHooks) -> None: ...
class ToolMount(Protocol):       # contribute agent tools (§3.5)
    def tools(self) -> Sequence[Tool]: ...
```

**Extensions configure named chains rather than being polled** — one extension can
register on many hooks, add several handlers to one hook, and control ordering:

```python
class WalmartExtension:
    manifest = ExtensionManifest(name="walmart", version="1.0",
                                 api_version="0.1", requires=("content",))

    def configure_blocks(self, h: BlockHooks) -> None:
        h.classify.add_last(self._detect_wall)      # → Verdict | None
        h.resolve.add_last(self._click_through)     # → Resolution | None

    def configure_browse(self, h: BrowseHooks) -> None:
        h.document_steady.add_last(self._dismiss_modal)
```

**Hooks**, with `will`/`did` pairing:

| Phase | Hooks, in execution order |
|---|---|
| **Browse** | `will_launch` → `launched` → `will_navigate` (may rewrite URL) → `navigated` → `will_interact` → `document_loaded` → **`document_steady`** → `did_interact` → `will_close` |
| **Content** | `will_fetch` → `fetched` → `will_parse` → `parsed` (repair HTML) → `will_extract` → `extracted` (enrich `Document`) |
| **Blocks** | `classify` → `resolve` |

`resolve` is the capability with no equivalent today: current site knowledge can
only *say* "this is a wall", never act on it. Its `Resolution` feeds §3.6's escalation ladder rather than bypassing
it. `document_steady` — the DOM has stopped mutating, distinct from "loaded" — is
the correct place for a site-specific warm-up or modal dismissal, and we have no
equivalent today.

**Manifest and compatibility gating:**

| Extension `api_version` vs host | Verdict |
|---|---|
| same major | load |
| older major | load, with a warning — old extensions keep working |
| newer major | refuse — it may need APIs this host lacks |
| missing / unparseable | load best-effort, with a warning |

Pair it with a load policy so an operator can disable an extension by config
without uninstalling it.

**Registration — explicit, scoped, never at import time:**

```python
browser = Browser(config, extensions=[WalmartExtension()])   # explicit
browser = Browser(config, extensions=discover_extensions())  # opt-in scan
```

```toml
[project.entry-points."browserpilot.extensions"]
walmart = "browserpilot_walmart:WalmartExtension"
```

**Dispatch rules — decide once, write down, test:**

| Concern | Rule |
|---|---|
| Ordering | `add_first`/`add_last` within a hook; explicit extensions before discovered ones |
| Combination | Filter-shaped (`classify`, `resolve`, `will_navigate`, `will_parse`): **first non-`None` wins**, `None` defers. Notification-shaped (`navigated`, `document_steady`, `extracted`): **all matching run in order**, `extracted` chaining its `Document` |
| Scope | Per-`Browser` instance. No global registry, no import-time mutation |
| Failure | Every hook call individually wrapped: log with the extension name, treat as `None`, continue. A broken extension degrades one site; it never kills the crawl |
| Timeout | Per-hook deadline from config; overrun is a failure per the row above |
| Capability | Hooks receive `BrowserSession` — never the raw driver or CDP client |
| Observability | Every firing is a structlog event plus a metric labelled by extension and hook; log each extension loaded with its source package |

**Extension package layout** — the shape that gives you function + tool + hook
from one place:

```
browserpilot_walmart/
├── config.py        # typed settings
├── service.py       # the plain callable logic
├── tools.py         # exposes it as an agent tool  (ToolMount)
└── integration.py   # lifecycle hooks             (BrowseMount/ContentMount/BlockMount)
```

### 3.9 Prior art: Browser4 (PulsarRPA)

`agentpilot` already ports Pulsar's *algorithms* — privacy-context burn
accounting, `ChainedHtmlIntegrityChecker`, the Walmart/Amazon checkers,
retry-delay policy. Browser4 also solved the **extensibility and packaging**
problem, and that half was never ported. §3.8 is drawn from it directly:
`skeleton/plugin/MountPoints.kt` (mount points, configure-a-chain),
`PluginManifest.kt` and `boot/plugin/PluginCompatibility.kt` (manifest, the
verdict table above), `boot/plugin/PluginManager.kt` (per-mount error isolation,
disable-by-config), `agentic/tools/CustomToolRegistry.kt` (namespacing, raise on
duplicate), and the 26-hook `will`/`did` inventory in its plugin archetype. The
`browser4-plugins/browser4-captcha` plugin is direct validation of `resolve`.

**Also copy the PDK.** Browser4 does not merely have an SPI — it ships the means
to build against it: a BOM pinning versions for extension authors, an archetype
that scaffolds a new plugin, and `browser4-pdk-test-plugin`, an in-tree reference
plugin exercising every mount. That last one is what keeps an SPI honest.

**What not to copy:**

- **Spring auto-configuration.** Browser4 discovers mounts as DI beans. Entry
  points plus explicit construction is Python's equivalent — do not build a DI
  container to imitate it.
- **Markdown as a plugin.** Browser4 ships `browser4-markdown` as one of eleven
  plugins. Here markdown stays **core** — it is the primary output contract and
  the Firecrawl-parity surface, and the converter is already pure. But the split
  is the right hint for its neighbours: SEO, headings, word-count and similar
  transforms are extensions, not core.
- **A crawl-phase mount, for now.** Browser4's `CrawlEventMount` (URL admission,
  result handling) maps to `agentpilot.crawl`, which is platform-side.

---

## 4. The two-project contract

This is what makes the separation real rather than cosmetic. It is the part most
likely to be skipped.

**Versioning.** `browserpilot` follows SemVer independently, starting at `0.1.0`;
while on `0.x` the minor is the breaking position. `agentpilot` pins a range,
never `*` and never a git SHA. A change to any symbol in `browserpilot.__all__`
drives the version bump; internal moves behind the facade do not. Removing a
public symbol requires one release of `DeprecationWarning` first.

**Allowed imports.** `agentpilot` may import only `browserpilot.__all__` and the
documented submodule APIs (`browserpilot.tools`, `.content`, `.contracts`).
Reaching into `browserpilot.engine.patchright_driver` or `.pool.lease` is a bug,
not a shortcut. import-linter cannot span two root packages, so a test in
`agentpilot`'s suite walks its own AST and asserts every `browserpilot.*` import
resolves to a published symbol. This test inherits the job that today's "only the
composition root imports the driver" contract does.

**CI — three jobs, and the third is the important one:**

| Job | Proves |
|---|---|
| `browserpilot` alone | Clean venv, `pip install ./packages/browserpilot[engine,markdown]`, run its own tests + `driver_contract/`. Nothing else installed. Assert `uv pip tree` contains no `fastapi`, `starlette`, `psycopg`, `prometheus-client`, or `redis` |
| `agentpilot` on workspace | Normal dev resolution via `[tool.uv.sources]`. The everyday signal |
| `agentpilot` on the **published** wheel | Build browserpilot, install from a local index with the source override disabled, run agentpilot's suite. **Catches every accidental reliance on source-tree layout or unpublished internals** — the failure that silently re-merges the two projects |

**Distribution.** A local directory index for now (`file://`), PEP 503 layout,
promoted to a real private index later without code change. Because the name
collides on PyPI, the explicit-index pinning in §3.1 is part of this contract,
not a deployment detail — a consumer that omits it silently installs someone
else's package.

**Release order.** `browserpilot` releases first, always; `agentpilot` bumps its
pin in a separate commit. Never one commit that changes a public symbol and its
call site and claims both are released.

---

## 5. Implementation phases

**Measured baseline (2026-08-29), before any phase.** "Green" means *no worse
than this*, because two gates were already red on arrival:

| Gate | At start | After Phase 0 |
|---|---|---|
| `pytest` (excl. `driver_contract`) | 675 passed, 65 skipped | **751 passed**, 65 skipped |
| `lint-imports` | **15 kept, 1 BROKEN** | **16 kept, 0 broken** ✅ fixed |
| `mypy agentpilot` | 11 errors / 7 files | 11 errors / 7 files *(untouched — out of scope)* |
| `ruff check` | 13 errors | 13 errors *(all pre-existing; none in touched files)* |

The broken contract was `agentpilot.spi.dom_tree -> agentpilot.driver.dom_fusion`
— the bottom layer importing the top one, which is D7 surfacing as an actual
failure and would have been a `contracts/ → engine/` violation after the split.

Each phase is one PR, independently shippable, leaving `lint-imports`, `mypy` and
`pytest` no worse than the row above. **Phases 1–6 change no package names and create no new project** —
all mechanical movement is deferred to Phase 7, by which point the contracts that
verify it already exist.

### Phase 0 — Guardrails

- Add `py.typed`; write a public-API snapshot test over the intended surface.
- Add characterization tests where coverage is thin — `ephemeral.py`'s escalation
  ladder, `stealth_profile.py`, `browser_headers.py`, `humanize.for_tier`. These
  are exactly what Phases 1, 3 and 7 will move.
- Add §3.2's target import-linter contracts in `(parenthesised)` optional-layer
  form, correct the moment the packages exist. The repo already uses this idiom.
- **Spike the workspace split on a throwaway branch**: two members, a trivial
  `browserpilot`, proving `[tool.uv.sources]`, mypy across the boundary, and both
  Dockerfiles' dep-cache layer — *before* committing to Phase 7's shape.

**Status: done.** What landed:

- **Fixed the pre-existing broken contract.** Moved `LayoutInfo` from
  `driver/dom_fusion.py` to `spi/dom_tree.py` — it is pure data over
  `spi.geometry.BoundingBox` with no engine behaviour, and `spi` typed a field
  with it. `dom_fusion` re-exports the name, so all existing imports still work.
  Fixed properly rather than suppressed: **16 kept, 0 broken.**
- **`tests/test_tier_surface_characterization.py`** (9 tests) — a golden table
  pinning all four tiers × six tier-derived values as literals, plus invariants
  (every ladder rung is a real tier; `humanize.for_tier` delegates rather than
  duplicating). The instrument Phase 1 is measured against. Existing coverage was
  better than assumed — `test_stealth_profile/tier/humanize/browser_headers` all
  exist — but none pinned the surface *as a whole*, which is what a
  pure-delegation refactor needs.
- **`tests/test_public_api_surface.py`** (67 tests) — importability tripwire over
  the 60 symbols that must survive the extraction, plus a `py.typed` check and an
  AST scan asserting the future-`browserpilot` packages import no web framework.
- **`agentpilot/py.typed`** added.
- **Workspace spike** (`/tmp/wsspike`, throwaway): two-member uv workspace
  resolves; cross-boundary import works; `uv build --all-packages` produces both
  wheels; `browserpilot` alone in a clean venv pulls **11 packages, all httpx
  transitives — no fastapi/starlette/psycopg/prometheus/redis**; `py.typed` ships
  in the wheel at the correct path.
- **Found the PyPI name collision** (§3.1, §8.1) and verified the explicit-index
  mitigation end to end.

**Not done, deliberately:** the 11 mypy and 13 ruff errors are pre-existing and
out of scope for a guardrails phase; they are recorded above so a regression is
detectable. The Dockerfile dep-cache change is deferred to Phase 7, where it
belongs — the spike proved the layout it must target.

### Phase 1 — Consolidate tier policy (D3)

Create `tiers/` as new code; move the six scattered pieces behind `TierPolicy`
and make old call sites delegate. Lift `ESCALATION` out of `ephemeral.py`.
Collapse the three `Literal` declarations in `gateway/schemas.py`.

**Status: done.** `agentpilot/tiers/` is now the single owner. What moved:

| From | To |
|---|---|
| `spi/actions.py` — `_STEALTH_TIERS`, `stealth_from_tier`, `_TIER_TO_INTERACT_PROFILE`, `interact_profile_for_tier` | `TierPolicy.no_runtime`, `.interact_profile` |
| `session/stealth_profile.py` — `PROTECTED_TIERS`, `_LADDER_ENTRY`, `effective_tier`, `is_protected` | `PROTECTED`, `TierPolicy.effective`, `.protected` |
| `session/ephemeral.py` — `_ESCALATION`, `_PROTECTED_TIERS` | `ESCALATION`, `TierPolicy.escalation` |
| `driver/humanize.py` — `for_tier` | **deleted** (see below) |
| `gateway/schemas.py` — the `Literal`, ×3 | one `TierName` import |
| `"residential" if protected else None`, open-coded 4× in 2 files | `TierPolicy.proxy_tier` |

Notes on judgement calls:

- **`humanize.for_tier` was deleted, not moved** — it had *no production
  caller*. The driver already receives a profile **name** through
  `open(interact_profile=…)` and resolves it with `humanize.by_name`
  (`patchright_driver.py:524`), which is the target shape. Its three tests were
  rewritten to assert the mapping through its new owner, plus one that asserts
  `humanize` has no `for_tier` — reintroducing it would re-split the mapping.
- **`tiers` is a pure leaf** with zero `agentpilot` imports, enforced by a new
  `forbidden` contract. It has to be: `driver` and `session` sit on separate
  arms above `spi` and may not import each other, so a shared module can only be
  shared if it depends on nothing.
- **The wire type did not change.** `TierName` is a `Literal` alias, not the
  `Tier` enum, so the generated OpenAPI for all three request models is
  byte-identical (`{'default': 'auto', 'enum': [...], 'title': 'Tier', 'type':
  'string'}`, verified before and after). Swapping the wire to the enum would
  change the runtime type of every parsed `request.tier`; that is a separate
  change with its own blast radius.
- **A regression was caught and fixed by the Phase 0 table.** The first
  implementation coerced an unrecognised tier to `auto`, which silently made it
  *protected* and gave it a residential proxy — previously an unknown tier was
  unprotected. `TierPolicy` now reproduces the old per-lookup fallbacks exactly
  (identity rung, `False` flags, `"default"` profile, `("stealth",)` ladder).
  This is the entire justification for Phase 0.

**Gates:** 752 passed / 65 skipped (was 751); **17 contracts kept, 0 broken**
(+1 new); mypy 11 errors and ruff 13 — both identical to baseline, none in any
file this phase touched.

### Phase 2 — Config objects, env at the root (D8)

Frozen config dataclasses with `.from_env()`. Remove every `os.environ` read from
the eight leaf modules; values arrive as arguments. `gateway/wiring.py` calls
`.from_env()`.

**Status: done.** `agentpilot/config.py` is now the only module in the browser
layer that reads the environment. Five leaves stopped:

| Leaf | Was | Now |
|---|---|---|
| `identity/fingerprint.py` | `AGENTPILOT_CHROME_VERSION` at **import** scope — the value froze on first import | `generate(..., chrome_version=…)`; presets carry a `{chrome_ver}` template |
| `identity/profile_store.py` | env fallback inside `prototype_dir_for` | `root=None` disables seeding; the root is supplied |
| `identity/proxy_health.py` | env fallback inside `__init__` | `max_success` is a required-with-default argument |
| `egress/policy.py` | sniffed `AGENTPILOT_LLM_BASE_URL` at apply time | the host arrives via `EgressPolicy.allow_hosts` — using a field that already existed |
| `extraction/site_checkers.py` | env read inside `check()` | `AmazonChecker(expect_district)` constructor state |

`gateway.wiring` calls `BrowserConfig.from_env()` once and threads it through
the two session entry points and the three job loops.

Judgement calls:

- **Two env reads deliberately stay, and are now documented in code plus
  allow-listed in a test.** `driver/process_launcher.py` touches `DISPLAY`,
  which is the X11 protocol's own channel, not configuration — Chrome is
  launched as a child and reads it from the inherited environment, so it must
  genuinely be there and a config field could not replace it.
  `identity/proxy_config.py` reads inside a `from_env()` constructor called once
  from the composition root, which is the sanctioned pattern rather than a read
  at the point of use. The blanket acceptance grep was too blunt to express
  this; `test_browser_layer_reads_no_ambient_environment` encodes the real rule
  and will catch any *new* read.
- **One `BrowserConfig` is threaded, not five loose values.** Phase 5's facade
  takes this object directly, so this is building that vehicle early rather than
  churn to be undone.
- **The parameter is named `browser_config`, not `config`** — `run_ephemeral_scrape`
  already binds a local `config` (an `LLMConfig`) partway through its body,
  which would have shadowed a parameter of that name.
- **Caught a silent config break in review:** the first draft invented
  `AGENTPILOT_PROFILE_PROTOTYPE_ROOT`; the real variable is
  `AGENTPILOT_PROTOTYPE_PROFILE_DIR`. Only `AGENTPILOT_PROXY_MAX_SUCCESS` is
  declared in `.env.example`/`docker-compose.yml`; the rest were undocumented
  knobs, all now wired through the root so none silently stopped working.

**Gates:** 762 passed / 65 skipped (was 754); 17 contracts kept, 0 broken
(`config` joined `tiers` in the pure-leaf contract); mypy 11 and ruff 13, both
at baseline.

### Phase 3 — De-tenant the core; policy seam; Redis out (D10, D11)

Do this **before** the facade and well before the split: it changes the most
widely-referenced type in the seam, and after Phase 7 that is a breaking release.

- Replace `IdentityKey(tenant, domain, name)` with opaque `IdentityRef`. Move slug
  composition up into `agentpilot`; `browserpilot` only validates path-safety.
- Define the provider Protocols and inert defaults in `policy/`.
- `prototype_dir_for(domain)` → `PrototypeProvider.prototype_for(origin)`;
  `ProxyConfig.resolve(tenant, tier)` → `ProxyProvider.endpoints_for(identity, tier)`.
- Strip the `Redis` constructor argument and every `redis.asyncio` import from
  `proxy_pinning.py`, `proxy_health.py`, `burn_tracker.py`, keeping their
  scoring/retirement logic; move `redis_registry.py` + the six lease Lua scripts
  to `agentpilot`.
- Create `agentpilot/control/`; `gateway/wiring.py` constructs and injects.

**Accept:** `grep -rniE "tenant|redis"` over the browser packages returns nothing; a test asserts `IdentityRef(key="t/d/n").slug()` equals the old `IdentityKey("t","d","n").slug()` byte-for-byte; `test_place_session_lua.py`, `test_burn_tracker.py` and `test_placement_redis_outage.py` pass unchanged against the injected implementations.

### Phase 4 — Extension system (D12)

- Build `extensions/`: mounts, typed hook chains, `ExtensionManifest`, the
  compatibility gate, the disable-by-config load policy, §3.8's dispatch rules
  (ordering, first-non-`None` vs run-all, per-hook error isolation and timeout,
  metrics), and opt-in entry-point discovery.
- Rewire `block_detect._site_verdict()` to the registry; **delete
  `install_default_site_checkers()` and its import-time call**.
- Port Walmart/Amazon/JD out of `browserpilot` entirely into an extension package
  `agentpilot` owns, and wire it into `agentpilot`'s default config **in the same
  PR**, so detection never regresses. It gets no privileged API — it is the
  reference example of what any consumer writes.
- Add `resolve` and connect its `Resolution` to §3.6's ladder.
- Ship the PDK minimum: an in-tree reference extension exercising every mount, run
  in CI, plus a documented template for the §3.8 package layout.

> `ToolMount` is the one mount depending on Phase 6's tool registry. Define the
> Protocol here, wire it in Phase 6, keep the reference extension's tool
> contribution behind that.

**Accept:** the reference extension exercises every mount and passes in CI; a newer-major `api_version` is refused and an older one loads with a warning; a deliberately-throwing extension does not fail a scrape; a fresh import of the content pipeline leaves the registry empty; `tests/test_block_detect.py`'s retailer fixtures still classify correctly through the extension path; `grep -rniE "walmart|amazon|_abck"` over the browser packages returns nothing.

### Phase 5 — The facade (D2, D9)

Implement `Browser`/`BrowserSession` over the existing `interactive.py` and
`ephemeral.py`, with in-memory registry, default driver, inert providers and an
empty extension registry — so a caller can pass nothing. Add an `examples/`
script that crawls three pages to markdown with no gateway involved.

**Accept:** the example runs; `driver_contract/` gains a facade-level test using only the public API.

### Phase 6 — Tool registry; delete the mirrors (D5, D6)

Build `tools/registry.py` (vendor-neutral `ToolSpec`) plus the optional
`tools/adapters.py`; port every verb in `spi/actions.py` to a `@tool` function;
wire `ToolMount`. Then **delete** `agent/actions.py`'s
hand-written union and generate `gateway/schemas.py::ActionIn` and
`action_conversion.py` from the registry.

**Accept:** adding a verb requires editing exactly one file; `browserpilot` imports no LLM SDK (assert over its dependency closure); the generated JSON Schema per tool matches a golden snapshot, and adapter output is tested as a pure transform of it; a duplicate tool namespace raises; the agent loop's existing tests pass unmodified against the generated models.

### Phase 7 — Split into two projects

Create the uv workspace; `git mv` per §3.2; write both `pyproject.toml`s with
§3.3's extras and §3.1's `[tool.uv.sources]`; codemod `agentpilot`'s
`agentpilot.{spi,driver,session,identity,egress,extraction,dom}` imports to
`browserpilot.*`; update both Dockerfiles' dep-cache layer; split the test suites
and the import-linter config. Keep a compatibility shim in `agentpilot` for one
release re-exporting old paths with a `DeprecationWarning`.

**Accept:** `uv build --all-packages` produces two wheels; §4's job 1 is green.

### Phase 8 — Prove the consumer story

Wire up §4's job 3. Port one real crawler pipeline in `Browser4`/`crawlPilot` to
`pip install browserpilot[engine,markdown]` and record what was awkward. Write
`packages/browserpilot/README.md` around the four canonical uses: direct scrape,
interactive session, agent tools, pure markdown.

**Accept:** all three CI jobs green; the ported pipeline runs against the wheel only.

---

## 6. Ground rules

1. **Do not rewrite working logic.** `patchright_driver.py` (1282 LOC),
   `fingerprint.py`, `markdown_converter.py` and the Lua scripts are ported,
   battle-tested code. Move, wrap, consolidate — do not reimplement.
2. **Preserve the dense docstrings.** They encode *why* — CDP quirks, WAF
   cross-checks, Firecrawl and Pulsar port rationale. They move with the code.
3. **Contracts before code.** Add the contract for a boundary before moving code
   across it, so the move is verified as it happens.
4. **One phase per PR**, green at every boundary. Never leave `main` half-moved.
5. **`git mv`, not copy-delete** — preserve blame across Phase 7.
6. **Batching is the transport**, not an implementation detail.
7. **No new dependency in `browserpilot`** without a PR note on size and licence.
   Its dependency list is part of its public API now.

---

## 7. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| The two projects drift back into one via internal-reach imports | **High** | §4's AST test plus job 3 building against the *published* wheel. The single most important control in this plan |
| `IdentityRef` migration orphans on-disk profiles and vault entries | **High** | Phase 3 keeps the rendered slug byte-identical; the equality test lands *before* any call site changes |
| Removing Redis silently degrades multi-worker behaviour — two workers each keeping private pin/burn state | **High** | In-process defaults are single-process **only**. `agentpilot` must inject the Redis implementations, and a startup assertion must fail loudly if a `worker` role runs with an in-memory store. `test_placement_redis_outage.py` stays the regression guard |
| Merging the two DOM implementations changes agent element refs | **High** | Do it **last**, behind `test_dom_fusion*.py` and `test_agent_loop_refs.py`. If they genuinely differ, keep both behind one interface and say so rather than forcing a merge |
| Docker dep-cache layer breaks — both Dockerfiles copy a single root `pyproject.toml` today | High | Covered in the Phase 0 spike; both change in Phase 7 |
| **Dependency confusion — a stranger's `browserpilot` on PyPI is installed instead of ours** | **High, and already reproduced** | Not theoretical: the Phase 0 spike did exactly this. Explicit-index pinning (§3.1) in every consumer manifest, plus a CI assertion that the installed `browserpilot` reports our expected `Requires:` set. The durable fix is a rename (§8.1) |
| Deleting import-time checker registration silently disables Walmart/Amazon detection | Medium | The replacement extension must be written and wired into `agentpilot`'s default config in the *same* PR that deletes the constants, with `test_block_detect.py`'s retailer fixtures as the proof |
| Generated `ActionIn` diverges from the hand-written wire schema, breaking API clients | Medium | Golden-schema snapshot in Phase 6; diff generated vs. current OpenAPI before merge |
| An extension throws, hangs, or misclassifies and takes a crawl down | Medium | §3.8's error isolation and per-hook timeout are requirements, not nice-to-haves |
| Phase 7's import rewrite is large and mechanical | Medium | Codemod, not hand-editing; the shim absorbs misses; mypy catches the rest |
| Scope creep into agent/recipe | High | Those layers change **only** where they import moved symbols |

---

## 8. Open decisions

Settled and recorded in the design: the distinct top-level package name (§3.1),
no Redis in `browserpilot` (§3.7d), markdown stays core (§3.9).

1. **The distribution name** — now urgent, and evidence-backed. `browserpilot` on
   PyPI is an unrelated browser-automation library, so the name is confusable in
   the worst possible way and the collision is live even on a private index (see
   §3.1 and the risk register). `agentpilot` is taken too. Three options:
   **(a)** rename to something free — `crawlpilot` is available and fits the org;
   **(b)** keep `browserpilot` and mandate explicit-index pinning forever, never
   publishing publicly; **(c)** publish under a namespaced name.
   *Recommendation:* decide before Phase 7 — it is one line in `pyproject.toml`
   and the codemod's target prefix until then, and expensive afterwards. Index
   pinning is required in the meantime regardless of the choice.
   *(Settled: distribution is a local directory index now, a private index later.)*
2. **How does `agentpilot` manage its own site knowledge?** Now purely an
   `agentpilot` question — `browserpilot` has no opinion (§3.7b). The choices are
   a hard-coded extension, an extension reading a versioned file, or one backed by
   a control-plane table. *Recommendation:* start with a plain extension; add
   config-driven loading when a customer needs a site handled without a deploy.
3. **Does `IdentityRef.key` need a structured escape hatch?** Fully opaque is
   cleaner, but origin-granular proxy stickiness may want the browser to know the
   origin. It already receives the URL on `navigate()`. *Recommendation:* derive
   the origin from that and keep `key` opaque. Confirm before Phase 3.
4. **Ship an MCP server** over `browserpilot.tools` in this cut, or defer? The
   tool registry makes it nearly free and it is a strong argument for the
   standalone project existing.
