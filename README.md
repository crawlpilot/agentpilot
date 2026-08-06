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

### Local test credentials

For quick manual testing, set `AGENTPILOT_ADMIN_TOKEN=dev-admin-token` in `.env` (any fixed string
works locally — it isn't a real secret, just what gates `/v1/api-keys`) and mint a tenant key
against it with the exact command from step 5 above. The minted `bk_live_...` key isn't stable
across environments: it's a row in *this* Postgres volume, so it stops working the moment the
volume is wiped (`docker compose down -v`, a fresh `postgres-data` volume, etc.) — re-run the mint
command to get a new one rather than hardcoding an old key anywhere.

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
directly in one process — the path this repo's own tests exercise. For end-to-end UI work the
monolith is the simplest backend to point the frontend at (one origin, no worker/gateway hop):

```bash
AGENTPILOT_ADMIN_TOKEN=dev-admin-token uv run uvicorn agentpilot.gateway.app:app --reload --port 8000
```

## Frontend (dashboard & Playground)

The web UI lives in [`frontend/`](frontend/) — a React 19 + Vite + TanStack Query SPA. It's a thin
client over the same `/v1/...` HTTP/WebSocket API documented above (sign in with a tenant API key to
reach the Playground: scrape, crawl, map, agent runs, interactive sessions, live view, recipes; or
an admin token for the Nodes/fleet view). It is deployed **completely separately** from the
backend — there is no `/ui` mount and no Node/npm in any backend image (see
`agentpilot/gateway/app.py`), mirroring how Firecrawl keeps its dashboard out of its backend images.

### Prerequisites

- Node.js 20+ and npm
- A running backend (Docker Compose stack, or a `monolith` on `:8000` — see above)

### Local dev

```bash
cd frontend
npm install
npm run dev          # Vite dev server on http://localhost:5173
```

The dev server proxies `/v1`, `/healthz`, and `/readyz` (including the live-view WebSocket upgrade)
to `VITE_API_BASE_URL`, which defaults to `http://localhost:8000` and is set in
[`frontend/.env.development`](frontend/.env.development). Point it elsewhere if your gateway/monolith
isn't on `:8000`. Because the browser talks to the API through this same-origin proxy, there's no
CORS to configure in dev.

Open http://localhost:5173 and **Sign in**:

- **Tenant API key + tenant** (mint one via [step 5](#5-open-a-session) above) → lands on the
  Playground.
- **Admin token** alone → lands on the Nodes/fleet view.

### Production build & deploy

```bash
cd frontend
npm run build        # tsc -b && vite build -> frontend/dist/
npm run preview      # optional: locally preview the built bundle
```

The gateway has **no CORS middleware** and does **not** serve the SPA, so the built `dist/` bundle
must be served **same-origin** with the gateway: put a reverse proxy in front that serves the static
files and forwards `/v1`, `/healthz`, `/readyz` (with WebSocket upgrade) to the gateway. Leave
`VITE_API_BASE_URL` unset for that build so the app uses `window.location.origin`. Minimal example
with [Caddy](https://caddyserver.com/):

```caddyfile
# Caddyfile — serve the SPA and the API under one origin (http://localhost:8080)
:8080 {
    handle /v1/*     { reverse_proxy localhost:8000 }   # reverse_proxy upgrades WS automatically
    handle /healthz  { reverse_proxy localhost:8000 }
    handle /readyz   { reverse_proxy localhost:8000 }
    handle {
        root * ./frontend/dist
        try_files {path} /index.html                    # SPA fallback for client-side routes
        file_server
    }
}
```

```bash
caddy run           # then open http://localhost:8080
```

Any equivalent reverse proxy (nginx, Traefik, a CDN + API gateway) works the same way — the only
requirements are one shared origin and WebSocket pass-through for `/v1/.../live-view`.

## End-to-end: full stack locally

A single runbook that stands up backend + frontend and exercises the whole product through the UI.

1. **Backend** — start the two-tier stack and run migrations
   ([Quickstart](#quickstart-docker-compose) steps 1–4), *or* run a `monolith` on `:8000` (above).
2. **Mint a tenant API key** ([step 5](#5-open-a-session)) — you'll paste it into the UI.
3. **Frontend** — `cd frontend && npm install && npm run dev` (dev), or the Caddy deploy above for a
   production-style run.
4. **Sign in** with the tenant key + tenant, then walk the Playground tabs:
   - **Scrape** — enter a URL, pick formats; under *Advanced options* set `include_tags`/`exclude_tags`
     and (scrape-only) a `locale`/`timezone`; run and inspect the returned document.
   - **Crawl** — set `include_paths`/`exclude_paths`, add a **webhook** (URL + events + headers) and
     confirm the one-time **signing secret** is shown; watch progress and "Load more".
   - **Interact** — open a session, add a `snapshot` action (toggle *bounding boxes*, set *roles*) and
     an `extract` action (`structured_data`, tags), then *Run sequence*; use the crosshair to pick
     elements from a live snapshot.
   - **Agent** — give a task, set max steps / output schema, watch steps stream, and *Cancel* mid-run.
   - **Recipes** — create a recipe, then *Run*, *Heal*, and *Codegen* (choose a language); browse
     versions.
   - **Nodes** — sign in with the admin token to see live per-node memory/CPU across `worker`/`worker-2`.
5. **Automated backend e2e** (no browser mocking) is the `driver_contract` suite — see
   [Running the checks](#running-the-checks); it launches real Chrome against local fixtures and is
   the contract any driver must pass.

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
