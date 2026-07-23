# agentpilot

Multi-tenant **Browser-as-a-Service** platform: a foundation for running agents, crawlers, and
scrapers against warm, reusable browser sessions exposed over a stable HTTP/WebSocket API, rather
than each caller managing its own Chrome process.

A session is opened once (`POST /v1/sessions`), driven with batched actions
(`POST /v1/sessions/{id}/execute` — navigate, snapshot, click, fill, extract, screenshot, ...),
and released back to a warm pool (`DELETE /v1/sessions/{id}`) instead of being torn down and
relaunched per request. Anti-detection is handled by [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)
(a patched Playwright build that avoids common CDP-based bot-detection signals).

## Architecture

The codebase is a layered `agentpilot.gateway -> agentpilot.session -> agentpilot.identity -> agentpilot.auth -> agentpilot.spi`
stack, with a separate `agentpilot.driver -> {agentpilot.extraction, agentpilot.egress} -> agentpilot.spi` branch for the
concrete Playwright/Patchright implementation. The layering is enforced mechanically, not just by
convention: `pyproject.toml`'s `[tool.importlinter]` contracts (run via `lint-imports`) fail the
build if a layer reaches past its declared dependencies, and only the composition root
(`agentpilot/gateway/wiring.py`) is allowed to import the concrete driver at all.

`agentpilot.spi` is the seam: a `Protocol`-based `BrowserDriver` interface (batched `execute()`, not one
HTTP call per verb) that the rest of the system programs against, so a different driver
implementation could be swapped in without touching `agentpilot.gateway`. `agentpilot/driver/patchright_driver.py`
is the one concrete implementation today.

The process itself runs in one of three roles (`AGENTPILOT_ROLE`), same codebase:

- **`monolith`** — everything in one process (owns the driver, session registry, reaper); the
  default, and what this repo's own test suite runs against. Not a Docker Compose target.
- **`worker`** — owns the driver/registry/reaper, serves only `/internal/sessions/...`; internal-only,
  never tenant-facing. Built from `docker/worker.Dockerfile`.
- **`gateway`** — stateless HTTP/WebSocket proxy in front of a worker, serves the real tenant-facing
  `/v1/sessions/...` surface. Never imports/constructs a driver — built from `docker/gateway.Dockerfile`,
  a genuinely Chrome-free image.

See `plan.md` for the full design rationale and phased build history.

## Quickstart (Docker Compose)

### Prerequisites

- Docker + Docker Compose v2 (`docker compose version`)
- On Apple Silicon / other arm64 hosts: the `worker` image is pinned to `linux/amd64` (real Chrome
  is amd64-only), so Docker Desktop will emulate it via Rosetta/QEMU — expect `worker`/`worker-2` to
  build and boot noticeably slower than `gateway`. This is intentional, not a misconfiguration.

### 1. Configure environment

```bash
cp .env.example .env   # fill in AGENTPILOT_ADMIN_TOKEN at minimum
```

`AGENTPILOT_ADMIN_TOKEN` gates `/v1/api-keys` (issuing/revoking tenant API keys) — leave it unset
and that surface 401s unconditionally. `AGENTPILOT_VAULT_KEY` is optional (unset disables
encryption-at-rest for saved browser profiles). See `.env.example` for the rest; a handful of
tuning knobs (lease/idle/reaper TTLs, per-node context/tab ceilings, memory watermark) have
in-code defaults in `agentpilot/gateway/wiring.py` and don't need to be set for local use.

### 2. Build and start the stack

```bash
docker compose up --build
```

This starts Redis, Postgres, two `worker` instances (Chrome/Patchright, internal-only, each
self-registering into the fleet), and a `gateway` (publishes `8000:8000`) — the real two-tier
topology, not the single-process `monolith` role. `--build` forces a rebuild after pulling or
editing a Dockerfile; plain `docker compose up` reuses images already built. First build pulls
and installs Chrome inside the `worker` images, so expect it to take a few minutes.

Redis (`6379`) and Postgres (`5432`) are also published, but loopback-only
(`127.0.0.1:...`), so you can `redis-cli`/`psql` into them from the host for debugging or to run
migrations, without exposing them to the network.

### 3. Run database migrations

`AGENTPILOT_DATABASE_URL` persists tenant API keys in Postgres and has no automatic migration step
— run Alembic yourself, from the host, against the compose Postgres before issuing any API keys:

```bash
AGENTPILOT_DATABASE_URL=postgresql://agentpilot:agentpilot@localhost:5432/agentpilot uv run alembic upgrade head
```

(Requires `uv sync --group dev` locally so `alembic` is on the path — this doesn't need to run
inside a container.)

### 4. Verify it's up

```bash
curl http://localhost:8000/healthz   # liveness -> {"status": "ok"}
curl http://localhost:8000/readyz    # readiness
docker compose ps                    # all services should be "running"
docker compose logs -f gateway       # tail a specific service; swap in worker/worker-2/postgres/redis
```

### 5. Open a session

```bash
curl -X POST http://localhost:8000/v1/api-keys \
  -H "Authorization: Bearer $AGENTPILOT_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tenant": "dev", "name": "local-dev"}'
# -> {"api_key": "bk_live_...", ...}

curl -X POST http://localhost:8000/v1/sessions \
  -H "Authorization: Bearer bk_live_..." \
  -H "Content-Type: application/json" \
  -d '{"tenant":"dev","domain":"example.com","name":"my-session","tier":"auto"}'
```

### Tearing down

```bash
docker compose down          # stop containers, keep volumes (Postgres/Redis data, browser profiles, vault)
docker compose down -v       # also wipe volumes -- next `up` starts from an empty DB/Redis/profiles
```

### Troubleshooting

- **`worker`/`worker-2` fail to become healthy, or Chrome crashes on launch**: check
  `docker compose logs worker` for Xvfb/Chrome sandbox errors. The container needs
  `cap_add: [SYS_ADMIN, NET_ADMIN]` (already set in `docker-compose.yml`) for a real Chrome sandbox
  and `agentpilot.egress` iptables rules — don't strip these caps.
- **`/v1/api-keys` returns 401 even with a token**: confirm `AGENTPILOT_ADMIN_TOKEN` is set in `.env`
  *before* `docker compose up` (compose reads `.env` at container-creation time, not live) and that
  you're passing the same value in `Authorization: Bearer ...`.
- **Session creation fails / no worker available**: the `gateway` discovers workers via Redis
  self-registration, not compose service names — give `worker`/`worker-2` a few seconds after
  startup to register before creating sessions; check `docker compose logs worker` for registration
  errors.
- **Migrations fail to connect**: `alembic/env.py` requires `AGENTPILOT_DATABASE_URL` to be set
  explicitly (no fallback) and expects it reachable from the host, i.e. `localhost:5432`, not
  `postgres:5432` (that hostname only resolves inside the compose network).

## Local development (no Docker)

```bash
uv sync --group dev --extra driver          # add --extra postgres too if you need AGENTPILOT_DATABASE_URL
uv run patchright install chrome            # one-time, only needed for tests/local runs that launch a browser
uv run uvicorn agentpilot.gateway.app:app --reload
```

`AGENTPILOT_ROLE` defaults to `monolith` when unset, which serves the full `/v1/sessions/...` API
directly in one process — the path this repo's own tests exercise.

## Running the checks

```bash
uv run ruff check agentpilot tests       # lint
uv run mypy agentpilot                   # type check (strict on agentpilot.spi/driver/identity/session/auth)
uv run lint-imports                # layering contracts
uv run pytest                      # tests (see below)
```

`tests/` is mostly fast unit/integration tests with no real browser. `tests/driver_contract/`
is different by design: it launches a real Chrome via Patchright against local `pytest-httpserver`
fixtures — no mocked browser, no external sites — so it's slower and needs Chrome installed
(`uv run patchright install chrome`). Any new driver implementation is expected to pass this suite
unmodified, since it asserts on `agentpilot.spi` behavior, not implementation details.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
