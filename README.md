# baas-crawlpilot

Multi-tenant **Browser-as-a-Service** platform: a foundation for running agents, crawlers, and
scrapers against warm, reusable browser sessions exposed over a stable HTTP/WebSocket API, rather
than each caller managing its own Chrome process.

A session is opened once (`POST /v1/sessions`), driven with batched actions
(`POST /v1/sessions/{id}/execute` — navigate, snapshot, click, fill, extract, screenshot, ...),
and released back to a warm pool (`DELETE /v1/sessions/{id}`) instead of being torn down and
relaunched per request. Anti-detection is handled by [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)
(a patched Playwright build that avoids common CDP-based bot-detection signals).

## Architecture

The codebase is a layered `baas.gateway -> baas.session -> baas.identity -> baas.auth -> baas.spi`
stack, with a separate `baas.driver -> {baas.extraction, baas.egress} -> baas.spi` branch for the
concrete Playwright/Patchright implementation. The layering is enforced mechanically, not just by
convention: `pyproject.toml`'s `[tool.importlinter]` contracts (run via `lint-imports`) fail the
build if a layer reaches past its declared dependencies, and only the composition root
(`baas/gateway/wiring.py`) is allowed to import the concrete driver at all.

`baas.spi` is the seam: a `Protocol`-based `BrowserDriver` interface (batched `execute()`, not one
HTTP call per verb) that the rest of the system programs against, so a different driver
implementation could be swapped in without touching `baas.gateway`. `baas/driver/patchright_driver.py`
is the one concrete implementation today.

The process itself runs in one of three roles (`BAAS_ROLE`), same codebase:

- **`monolith`** — everything in one process (owns the driver, session registry, reaper); the
  default, and what this repo's own test suite runs against. Not a Docker Compose target.
- **`worker`** — owns the driver/registry/reaper, serves only `/internal/sessions/...`; internal-only,
  never tenant-facing. Built from `docker/worker.Dockerfile`.
- **`gateway`** — stateless HTTP/WebSocket proxy in front of a worker, serves the real tenant-facing
  `/v1/sessions/...` surface. Never imports/constructs a driver — built from `docker/gateway.Dockerfile`,
  a genuinely Chrome-free image.

See `plan.md` for the full design rationale and phased build history.

## Quickstart (Docker Compose)

```bash
cp .env.example .env   # fill in BAAS_ADMIN_TOKEN at minimum
docker compose up
```

This starts Redis, Postgres, a `worker` (Chrome/Patchright, internal-only), and a `gateway`
(publishes `8000:8000`) — the real two-tier topology. Open a session:

```bash
curl -X POST http://localhost:8000/v1/api-keys \
  -H "Authorization: Bearer $BAAS_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenant": "dev", "name": "local-dev"}'
# -> {"api_key": "bk_live_...", ...}

curl -X POST http://localhost:8000/v1/sessions \
  -H "Authorization: Bearer bk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"tenant":"dev","domain":"example.com","name":"my-session","tier":"auto"}'
```

## Local development (no Docker)

```bash
uv sync --group dev --extra driver          # add --extra postgres too if you need BAAS_DATABASE_URL
uv run patchright install chrome            # one-time, only needed for tests/local runs that launch a browser
uv run uvicorn baas.gateway.app:app --reload
```

`BAAS_ROLE` defaults to `monolith` when unset, which serves the full `/v1/sessions/...` API
directly in one process — the path this repo's own tests exercise.

## Running the checks

```bash
uv run ruff check baas tests       # lint
uv run mypy baas                   # type check (strict on baas.spi/driver/identity/session/auth)
uv run lint-imports                # layering contracts
uv run pytest                      # tests (see below)
```

`tests/` is mostly fast unit/integration tests with no real browser. `tests/driver_contract/`
is different by design: it launches a real Chrome via Patchright against local `pytest-httpserver`
fixtures — no mocked browser, no external sites — so it's slower and needs Chrome installed
(`uv run patchright install chrome`). Any new driver implementation is expected to pass this suite
unmodified, since it asserts on `baas.spi` behavior, not implementation details.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
