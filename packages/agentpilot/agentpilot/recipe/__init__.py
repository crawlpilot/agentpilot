"""Phase 2: selector-generation & self-healing data-collection recipes.

Given a URL and a caller-specified field schema, `agentpilot.recipe` explores
the page once (via `agentpilot.agent.loop.run_agent_loop`), captures stable,
re-resolvable locators per field, and persists a versioned `Recipe` that can
then be replayed deterministically -- no further LLM calls -- on demand or on
a schedule. See `agentpilot/recipe/models.py` for the artifact shape.

Layering: `gateway -> recipe -> agent -> session -> llm -> spi` (and
`jobs -> recipe -> agent -> session -> llm -> spi` for the worker/scheduler
loops that live in `agentpilot.jobs`, mirroring where `agent_worker_loop.py`
lives relative to `agentpilot.agent`). `agentpilot.recipe` never imports
`agentpilot.driver` directly -- only through `agentpilot.session.interactive`.
"""

from __future__ import annotations
