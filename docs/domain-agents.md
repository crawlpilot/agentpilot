# Domain agents

**Status:** specification. Depends on [`recipe-contract-v2.md`](recipe-contract-v2.md).

---

## The idea

A recipe is an internal artifact. A **domain agent** is the thing you can hand to someone: *"this
is the Zara retail collector — here is what it knows how to collect, call it with a URL."*

```
DomainAgent
  agent_id, tenant, name            # "zara-in-retail"
  domains:       list[str]
  description:   str                # what it collects, written for a model to read
  recipes:       list[recipe_id]    # ordered; a URL routes to the first matching TargetSpec
  output_schema: JSON Schema        # derived from the bound recipes' field specs
  defaults:      {tier, proxy_pool, profile, schedule}
  fallback:      "agent_loop" | "fail"
  status:        "active" | "paused"
```

The entity is thin on purpose. It is a *binding* — a name, an audience-facing description, and an
ordered set of recipes — not a new execution engine. Everything it does, the recipe layer already
does; the agent decides *which* recipe and *under what defaults*.

---

## Why this is worth building

Because the alternative is what happens today: an agent that must collect Zara product data
re-derives the selectors on every single run. That is the LLM-per-page economics the whole recipe
pipeline exists to escape, reintroduced at the top of the stack.

A recipe that has been built, verified on several sample URLs, reviewed by a human and promoted to
`published` is a *proven capability*. Nothing in the system currently lets a model use it as one.

---

## Two invocation modes

### 1. Deterministic collect — the default

```
POST /v1/agents/{id}/collect  {url, metadata}
```

Route the URL through each bound recipe's `TargetSpec.match` in order, take the first that
accepts, `replay_recipe`, return the typed result.

**No LLM. No model call anywhere in the path.** Fast, cheap, reproducible, schedulable, and it
needs no new machinery beyond routing — replay is already zero-LLM. This is the literal meaning of
"already knows how to collect the data", and it is the mode that should serve the overwhelming
majority of traffic.

`TargetSpec` was designed as a guard (contract §1); routing is the same predicate read from the
other end, which is why binding several page-type recipes to one agent works without any new
matching concept.

### 2. Assisted — the fallback

```
POST /v1/agents/{id}/tasks  {task}
```

Runs `run_agent_loop` with the bound recipes exposed to the model as **callable tools**. The model
navigates, searches and discovers — the things it is good at — and **delegates extraction** to a
proven recipe instead of re-deriving selectors it has no way to validate.

This is the right shape for "find me the three cheapest midi dresses and get their full specs":
navigation is open-ended, extraction is solved.

`fallback: "agent_loop"` makes mode 2 the automatic escape hatch when no bound recipe matches a
URL. `fallback: "fail"` is the correct setting for a scheduled pipeline that must never silently
become expensive.

---

## The one architectural change

The agent's action vocabulary is generated entirely from `crawlpilot.tools.catalog` — browser
verbs only. `agent/actions.py` builds `_ACTION_MODELS` from
`browser_tools().subset(agent_exposed=True)`, and everything downstream (JSON schema, parsing,
dispatch) follows from that one registry.

A `collect_with_recipe` tool is an **agentpilot** concept. It must not be pushed down into
`crawlpilot`, which is contractually forbidden from importing the platform — enforced by an
import-linter `forbidden` contract *and* by `tests/test_project_boundary.py`.

**So `run_agent_loop` gains a seam: it accepts extra tool specs alongside the browser catalog, and
the caller injects them.**

```python
async def run_agent_loop(
    *,
    task: str,
    ...,
    extra_tools: tuple[ToolSpec, ...] = (),   # new
) -> AgentRunResult:
```

The direction is exactly right for the existing layering. `agentpilot.recipe` sits **above**
`agentpilot.agent`, so the recipe layer may hand tools *down*, while the agent stays ignorant that
recipes exist. No contract is bent to make this work, and the `jobs → recipe → agent → llm` chain
is unchanged.

The injected tool is small:

```
collect_with_recipe(recipe_id: str, url: str, metadata: object = {}) -> RecipeRunResult
```

Its description — which is what the model actually reads — is generated from the recipe's `name`,
`description` and field list, so a well-written recipe advertises itself.

### What the model must not be given

The tool takes a `recipe_id` from the agent's **bound set**, never an arbitrary one, and never a
recipe body. A model that could synthesise a recipe inline would be a model that could run
arbitrary Lua and arbitrary selectors on demand, which is precisely the boundary
[`recipe-contract-v2.md`](recipe-contract-v2.md) §7.1 draws.

---

## Publication gating

**Only `published` recipes are bindable to an agent.**

That is the whole point of the `draft → approved → published` lifecycle. The chain is:

1. `draft` — build or heal wrote it. Verified on whatever `sample_urls` it had.
2. `approved` — a human reviewed it in the studio.
3. `published` — it may become a customer-facing capability.

A recipe carrying `has_script: true` (any Lua) should require explicit acknowledgement at the
`approved → published` step, because Lua was authored by a model reading an untrusted page.

An agent bound to a recipe that later goes `broken` reports the affected fields as unavailable
rather than silently returning partial data — and `blocked` outcomes are surfaced as
infrastructure problems, not as empty results. The distinction matters more here than anywhere
else in the system, because this is the surface someone else's code is calling.

---

## Storage

One Alembic revision, house style (raw SQL, `TEXT` PK, `TEXT` + `CHECK` enums, JSONB blobs,
partial index on any claim predicate):

```
domain_agents          agent_id, tenant, name, description, domains JSONB,
                       defaults JSONB, fallback, status, created_at, updated_at
domain_agent_recipes   agent_id, recipe_id, position        -- ordered binding
```

`output_schema` is derived on read from the bound recipes rather than stored, so it cannot drift
from the recipes it describes.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/agents` | create |
| `GET /v1/agents` | list |
| `GET /v1/agents/{id}` | detail, incl. derived `output_schema` and bound recipe health |
| `PUT /v1/agents/{id}` | update, incl. reordering the recipe binding |
| `POST /v1/agents/{id}/collect` | mode 1 — deterministic, no LLM |
| `POST /v1/agents/{id}/tasks` | mode 2 — assisted, returns an agent run id |
| `DELETE /v1/agents/{id}` | delete |

`collect` is browser-backed, so it follows the worker `/internal/…` + gateway `_proxy` pattern.
The rest is CRUD on Postgres and mounts directly on the gateway, like `recipes` and `agent_runs`.

---

## UI

`/agents` list and `/agents/:id` detail, plus a **Publish as agent** action in the studio.

The detail page carries the three things an operator actually needs: the ordered recipe binding
(with each recipe's `health_status` and trailing fill rate), per-field health across recent runs
from `recipe_field_metrics`, and a try-it-now `collect` box.

---

## Scheduling

A scheduled domain-agent collection is a recipe run with the agent's defaults applied. The
existing `RecipeSchedulerLoop` already walks `recipes.next_due_at`; binding it to an agent changes
which tier, proxy pool and profile the run uses, not how it is queued.

This is also where `built_under` earns its place (see
[`recipe-operations.md`](recipe-operations.md) §D6): an agent whose `defaults.proxy_pool` differs
from the region its recipes were built under should warn at bind time, not produce mysteriously
empty results at 3am.

---

## Not specified here

**MCP.** Domain agents are the natural unit to expose over MCP — each already carries a name, a
model-readable description, and an output schema, which is exactly an MCP tool definition. Worth
doing; out of scope for this document.

**Cross-domain composition** (an agent that collects the same schema from Zara, Walmart and
Amazon and normalises across them). The three worked examples show how differently those three
pages must be read; a unifying layer is a real design problem and deserves its own treatment
rather than an assumption that `output_schema` union is sufficient.
