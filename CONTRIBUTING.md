# Contributing

## Setup

```bash
uv sync --group dev --extra driver          # --extra postgres too if you're touching agentpilot.auth.store.PostgresApiKeyStore
uv run patchright install chrome            # needed to run tests/driver_contract/ or a local worker
```

The `driver` extra (Patchright, `re-cdp-patches`, `lxml`, `cssselect`, `cryptography`) is only ever
imported by `agentpilot.driver`, `agentpilot.extraction`, and `agentpilot.identity.vault` — it's deliberately not a
hard dependency of the package as a whole, so a `gateway`-role deployment can skip it entirely
(see `docker/gateway.Dockerfile`).

## Before opening a PR

Run the same checks CI-equivalent tooling expects:

```bash
uv run ruff check agentpilot tests
uv run mypy agentpilot
uv run lint-imports
uv run pytest --ignore=tests/driver_contract   # fast path
uv run pytest tests/driver_contract            # slower, launches real Chrome
```

- **`ruff`**: `select = ["E", "F", "I", "UP", "B"]` (see `pyproject.toml`). Should be clean —
  there are no existing suppressions to work around.
- **`mypy`**: strict mode is enforced on the core layers (`agentpilot.spi`, `agentpilot.driver`, `agentpilot.identity`,
  `agentpilot.session`, `agentpilot.auth`) via `[[tool.mypy.overrides]]` in `pyproject.toml`; `agentpilot.gateway` and
  the rest are checked non-strict. New code in the strict-mode packages needs to type-check clean
  under `strict = true`.
- **`lint-imports`**: enforces the layering contracts described in the README's Architecture
  section. If you're adding a new cross-package import and this fails, that's usually a sign the
  code belongs in a different layer, not that the contract needs loosening — but if you do have a
  genuine reason to change a contract, that change belongs in `pyproject.toml`'s
  `[tool.importlinter]` section with its own explanation.

## Code style

- No comments explaining *what* code does — names should do that. A comment earns its place by
  explaining a non-obvious *why*: a hidden constraint, a workaround for a specific bug, a decision
  that would otherwise look arbitrary. Look at any existing module docstring in `agentpilot/` for the
  house style.
- `agentpilot.spi` types are plain `dataclasses`/`Enum`/`Protocol` — no Pydantic there; Pydantic is
  scoped to `agentpilot/gateway/schemas.py` as the HTTP-boundary validation layer only, mirroring `spi`
  shapes rather than inventing new ones.
- Driver-agnostic code (`agentpilot.spi`, `agentpilot.session`, `agentpilot.identity`, `agentpilot.gateway`) must never
  import Playwright/Patchright types directly — `agentpilot.driver.patchright_driver` is the one module
  where those objects are allowed to exist; everything it returns to callers is a `agentpilot.spi`
  dataclass.

## Tests

- Plain unit/integration tests live flat under `tests/*.py`.
- `tests/driver_contract/` is a *behavioral contract* suite, not implementation-specific: it
  launches a real browser against local `pytest-httpserver` fixtures (never a mocked browser, never
  an external site) and asserts on `agentpilot.spi` behavior. Any new `BrowserDriver` implementation is
  expected to pass it unmodified — that's the point of the `spi` boundary.
- Prefer a real fixture/local server over mocking wherever practical, following the pattern already
  used throughout `tests/driver_contract/`.

## Commit messages

Write a commit message that says *why*, not just *what changed* — "what" is usually visible from
the diff itself. Keep history useful for the next person doing `git blame`/`git log -S` archaeology.

## Questions

Open an issue if something in this doc or the architecture doesn't match what you're seeing in the
code — that's a bug in the docs, not just an FAQ.
