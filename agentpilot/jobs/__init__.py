"""The async job layer for `/v1/crawl` and `/v1/batch/scrape`: a Postgres-
backed queue/store (`store.py`), the crawl-worker processing loop
(`worker_loop.py`), and a thin HTTP client to a worker's `/internal/scrape`
(`scrape_client.py`).

A second top-level consumer of `agentpilot.placement`, alongside
`agentpilot.gateway` -- not a layering violation, the same shape
`agentpilot.placement` already supports for two independent callers.
Never imports `agentpilot.driver` directly (see `pyproject.toml`'s
import-linter "only the composition root imports the concrete driver"
contract, which this package's forbidden-modules list is added to).
"""

from __future__ import annotations
