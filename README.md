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

The process itself runs in one of two roles (`AGENTPILOT_ROLE`), same codebase:

- **`worker`** — owns the driver/registry/reaper, serves only `/internal/sessions/...`; internal-only,
  never tenant-facing. Runs the crawl/agent/recipe queue loops. Built from `docker/worker.Dockerfile`.
- **`gateway`** (the default) — stateless HTTP/WebSocket proxy in front of a worker, serves the real
  tenant-facing `/v1/...` surface. Never imports/constructs a driver — built from
  `docker/gateway.Dockerfile`, a genuinely Chrome-free image.

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
make base          # first time (and after a dependency change): builds the worker BASE
                   #   image — Debian + Chrome + Python deps. Slow (pulls Chrome).
make up-build      # build the thin code layers on top and start everything. Fast.
# equivalently, first time: `make rebuild`
```

This starts Redis, Postgres, a one-shot `migrate` job (`alembic upgrade head`), two `worker`
instances (Chrome/Patchright, internal-only, each self-registering into the fleet), and a
`gateway` (publishes `8000:8000`) — the two-tier topology.

#### Docker build performance

The worker image is split in two so a **code change never re-downloads Chrome**:

- **`docker/worker-base.Dockerfile`** → `crawlpilot/worker-base:latest` — the heavy, slow-changing
  half (Debian + Xvfb/X11/iptables + Python deps + Chrome). Built by `make base` (or
  `docker compose build worker-base`); it's gated behind the `base` compose profile, so it never runs
  as a container. Rebuild it **only** when `pyproject.toml`/`uv.lock` change.
- **`docker/worker.Dockerfile`** → a thin `FROM base` + `COPY . .` + project install. A code change
  rebuilds only this (seconds).

So the everyday loop is just `make up-build` (or `docker compose up -d --build`); reach for
`make base` again only after changing dependencies. Both Dockerfiles also use BuildKit cache mounts
for `uv`/`apt`, and `.dockerignore` excludes `frontend/`, `.env`, and tooling caches from the build
context. (If a `worker` build errors with a missing `crawlpilot/worker-base:latest`, you haven't run
`make base` yet.)

Redis (`6379`) and Postgres (`5432`) are also published, but loopback-only
(`127.0.0.1:...`), so you can `redis-cli`/`psql` into them from the host for debugging or to run
migrations, without exposing them to the network.

### Rebuilding after code or Dockerfile changes

`docker compose up` (no flags) reuses whatever images are already built, so **edits to application
code, dependencies, or a Dockerfile do not take effect until you rebuild.** Pick the cheapest option
that covers what changed:

```bash
# Usual case after editing code: rebuild the thin code layers + recreate. Fast --
# Chrome/deps live in the prebuilt worker-base image and are NOT rebuilt here.
docker compose up --build -d          # or: make up-build

# Changed dependencies (pyproject.toml / uv.lock): rebuild the base first, then the rest.
docker compose build worker-base      # or: make base   (slow -- Chrome + deps)
docker compose up --build -d

# Picked up new .env / environment values but the image itself didn't change.
# (.env is read at container-creation time, not live — a plain restart won't see it.)
docker compose up -d --force-recreate

# Force a clean rebuild ignoring the layer cache — e.g. an apt/dep change Docker
# didn't detect, or to force a full Chrome reinstall. Slow. Base first if deps moved.
docker compose build --no-cache worker-base
docker compose up -d --build --force-recreate

# Nuclear: drop containers AND volumes (empty DB/Redis/profiles), then rebuild from scratch.
docker compose down -v
make rebuild                          # base + up-build
```

> Rule of thumb: **code change → `--build` (fast); dependency change → `make base` first; `.env`
> change → `--force-recreate`.** The base image is rebuilt only on dependency changes, so day-to-day
> code edits never pay the Chrome-download cost.

### 3. Database migrations (automatic)

Postgres persists tenant API keys (`agentpilot.auth.store`), crawl/agent/recipe jobs, and run
history. **Migrations run automatically on `docker compose up`**: a one-shot `migrate` service runs
`alembic upgrade head` against a healthy Postgres and exits, and `worker`/`worker-2`/`gateway` all
`depends_on: migrate` with `condition: service_completed_successfully` — so no app process serves a
request against a stale schema. Re-running when already at head is a fast no-op, so every deploy
brings the DB to head before the app starts. Watch it with:

```bash
docker compose logs migrate              # shows "Running upgrade 00NN -> 00NN+1 …" then exits 0
```

You only need Alembic on the host for **manual** inspection or authoring new revisions (below); the
normal `up` flow needs none of it:

```bash
uv sync --extra postgres                 # puts `alembic` + psycopg on the path (host-side only)
export AGENTPILOT_DATABASE_URL=postgresql://agentpilot:agentpilot@localhost:5432/agentpilot
uv run alembic current                   # show the revision the DB is on
uv run alembic upgrade head              # apply manually (idempotent — safe to re-run)
uv run alembic history --verbose         # list every revision (0001_create_api_keys … 0009_…)
```

**Authoring a new migration** (schema changes are **hand-written SQL** — this repo has no ORM
models, so `alembic revision --autogenerate` produces nothing useful):

```bash
uv run alembic revision -m "add my_column"   # creates alembic/versions/00NN_add_my_column.py
# then edit that file's upgrade()/downgrade() with op.execute("ALTER TABLE …") / etc.
uv run alembic upgrade head                  # apply it
uv run alembic downgrade -1                   # roll the last one back if needed
```

(None of this runs inside a container — Alembic connects to Postgres over `localhost:5432`.)

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

## Anti-detection, tiers & proxies

Scraping bot-protected sites (Akamai / PerimeterX / DataDome — Zara, COS, most major retail) is
driven by a per-request **tier** on `/v1/scrape` and `/v1/sessions`, backed by a proxy pool. The
tiers use Firecrawl's vocabulary:

| `tier` | what runs |
|---|---|
| `basic` | plain browser, no stealth extras — cheapest/fastest |
| `stealth` | + human warm-up (mouse/scroll/dwell + `_abck` wait), coherent per-identity fingerprint, body-level block detection |
| `enhanced` | stealth **+ headful** (under Xvfb on the worker) + residential proxy |
| `auto` *(default)* | starts on `stealth` and auto-escalates to `enhanced` on a detected block, each retry using a fresh identity / proxy / fingerprint |

For hardened targets the decisive factor is the **exit IP**: datacenter/cloud IPs are
reputation-blocked at the edge *regardless of fingerprint quality*, so `stealth`/`enhanced` require a
**residential or mobile** proxy pool and **fail closed with HTTP 503** when none is configured
(`basic`/`auto` proceed without one).

### Configuring proxies

Proxy pinning, per-proxy health, and burn accounting are Redis-backed, so this needs
`AGENTPILOT_REDIS_URL`. Set the proxy vars on the **worker** process — that's where the driver
runs; the `gateway` never scrapes.

- **`AGENTPILOT_PROXY_POOL`** — a flat, comma-separated list; the default pool for every tenant and
  tier:

  ```bash
  AGENTPILOT_PROXY_POOL=http://user:pass@res-gw:8000,http://user:pass@res-gw:8001
  ```

- **`AGENTPILOT_PROXY_POOLS`** — JSON for **per-client (tenant) + per-tier** pools, each endpoint
  optionally carrying `?country=XX` to declare its exit geo (used to align the browser's
  timezone/locale with the egress):

  ```bash
  AGENTPILOT_PROXY_POOLS='{
    "*":    { "residential": ["http://u:p@res-gw:8000?country=us"] },
    "acme": { "residential": ["http://u:p@acme-res:8000?country=in"],
              "datacenter":  ["http://acme-dc:8080"] }
  }'
  ```

  Resolution walks `tenant+tier → tenant+* → *+tier → *+*`, so a sparse config still resolves;
  `stealth`/`enhanced` request the `residential` tier. Either var works alone or together (the flat
  pool becomes the `*/*` default).

- **`AGENTPILOT_PROXY_MAX_SUCCESS`** *(default 100)* — retire a proxy after it has served roughly this
  many pages (±25% jitter), so one exit IP isn't reused long enough to be fingerprint-tracked.
  Proxies are also retired after 3 connection losses; retired proxies are skipped when picking, and a
  warm identity pinned to a now-retired proxy is re-pinned to a healthy one.

Beyond the proxy, the `stealth`/`enhanced` path also: pins a coherent per-identity browser
fingerprint (UA / WebGL / hardware / timezone, geo-matched to the proxy's country); runs a human
warm-up so Akamai's `_abck` sensor cookie validates before the page is read; detects bot walls in the
**response body** — including the HTTP-200 "Access Denied" pages Akamai serves — and raises a
challenge that drives auto-escalation; and retires a warm identity (`session_name`) that keeps
getting walled so its next visit is a clean, cookie-less browser.

> **Example — scrape a Zara product through a residential IN exit:**
> ```bash
> # on the worker:
> export AGENTPILOT_REDIS_URL=redis://localhost:6379/0
> export AGENTPILOT_PROXY_POOLS='{"*":{"residential":["http://user:pass@res-gw:8000?country=in"]}}'
> # then scrape with tier=enhanced (or auto, which escalates into it)
> curl -X POST http://localhost:8000/v1/scrape -H "Authorization: Bearer bk_live_…" \
>   -H "Content-Type: application/json" \
>   -d '{"tenant":"dev","url":"https://www.zara.com/in/en/…","tier":"enhanced"}'
> ```

### In Docker Compose

The compose `worker`/`worker-2` services pass the proxy vars through from your shell / `.env`
(`AGENTPILOT_PROXY_POOL`, `AGENTPILOT_PROXY_POOLS`, `AGENTPILOT_PROXY_MAX_SUCCESS`), but they're empty
by default. Set them in `.env`, then recreate the workers so they pick the new values up:

```bash
docker compose up -d --force-recreate worker worker-2
```

## Local development (no Docker)

Install the toolchain and run the unit tests (most never stand up the app, so they need neither a
role nor a browser):

```bash
uv sync --group dev --extra driver          # add --extra postgres too if you need AGENTPILOT_DATABASE_URL
uv run patchright install chrome            # one-time, only needed for tests/local runs that launch a browser
uv run pytest -q
```

There is no single-process backend: the app runs as a `gateway` (tenant-facing `/v1/...`) in front of
one or more `worker`s (driver + queue loops). `AGENTPILOT_ROLE` defaults to `gateway`, so a bare
`uvicorn agentpilot.gateway.app:app` is a gateway and needs a worker to serve driver-backed routes.

For a live backend, the Docker Compose stack is the intended path (it wires Redis, Postgres, the
`migrate` job, workers, and the gateway together) — run only the frontend on the host against it. To
run the two roles as host processes instead, you need a shared Redis and Postgres, then, in separate
terminals:

```bash
# worker (owns Chrome; self-registers into the fleet)
AGENTPILOT_ROLE=worker AGENTPILOT_REDIS_URL=redis://localhost:6379/0 \
  AGENTPILOT_NODE_ADDR=http://localhost:8001 \
  AGENTPILOT_DATABASE_URL=postgresql://agentpilot:agentpilot@localhost:5432/agentpilot \
  uv run uvicorn agentpilot.gateway.app:app --port 8001

# gateway (tenant-facing; discovers the worker via Redis)
AGENTPILOT_ROLE=gateway AGENTPILOT_REDIS_URL=redis://localhost:6379/0 \
  AGENTPILOT_ADMIN_TOKEN=dev-admin-token \
  AGENTPILOT_DATABASE_URL=postgresql://agentpilot:agentpilot@localhost:5432/agentpilot \
  uv run uvicorn agentpilot.gateway.app:app --port 8000
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
- A running backend (the Docker Compose stack, or a host `gateway` on `:8000` with a worker — see above)

### Local dev

```bash
cd frontend
npm install
npm run dev          # Vite dev server on http://localhost:5173
```

The dev server proxies `/v1`, `/healthz`, and `/readyz` (including the live-view WebSocket upgrade)
to `VITE_API_BASE_URL`, which defaults to `http://localhost:8000` and is set in
[`frontend/.env.development`](frontend/.env.development). Point it elsewhere if your gateway
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
```

The built SPA must be served **same-origin** with the API (the gateway has no CORS middleware).
There are two ways to do that:

**Option A — let the backend serve it (no proxy).** The `gateway` process serves the
built bundle same-origin when a build is present, so the whole product runs from one port. Leave
`VITE_API_BASE_URL` unset for the build (the app uses `window.location.origin`), then point
`AGENTPILOT_UI_DIR` at the output — or just have the default `frontend/dist` exist:

```bash
AGENTPILOT_UI_DIR=$PWD/frontend/dist \
  uv run uvicorn agentpilot.gateway.app:app --port 8000
# open http://localhost:8000 — dashboard + API on one origin
```

It's opt-in and inert when no build is present (the Chrome-free `gateway` Docker image ships without
Node/npm, so nothing serves there unless you mount a `dist/` in and set `AGENTPILOT_UI_DIR`); the
`worker` role never serves it. Client-side deep links (`/nodes`, `/recipes/:id`) fall back to
`index.html`, and real API routes always win over the SPA catch-all. See `agentpilot/gateway/spa.py`.

**Option B — reverse proxy.** If you'd rather host the static bundle separately (CDN, nginx), put a
proxy in front that serves `dist/` and forwards `/v1`, `/healthz`, `/readyz` (with WebSocket upgrade)
to the gateway, so the browser still sees one origin. Minimal [Caddy](https://caddyserver.com/)
example:

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

## Run both locally, end to end

Two complete paths. **Path A** runs the two roles as native host processes (no Docker Chrome
emulation, instant reloads) against compose datastores; **Path B** is the production-like two-tier
fleet fully in Docker. Both end with the frontend talking to the backend on one machine.

### Path A — host gateway + host worker + compose Postgres/Redis + frontend

Both roles run natively (the worker uses your host's native Chrome — no amd64 emulation), sharing the
compose datastores. Four terminals, but the fastest inner loop.

```bash
# 0. one-time: Python + browser + Node deps
uv sync --extra driver --extra postgres --group dev   # app + psycopg/alembic + test tooling
uv run patchright install chrome                       # native Chrome for the worker's driver
cp .env.example .env                                   # set AGENTPILOT_ADMIN_TOKEN=dev-admin-token

# 1. datastores only (loopback-published; no worker/gateway containers)
docker compose up -d postgres redis
export AGENTPILOT_DATABASE_URL=postgresql://agentpilot:agentpilot@localhost:5432/agentpilot
export AGENTPILOT_REDIS_URL=redis://localhost:6379/0

# 2. migrate the DB (see "Database migrations" for details)
uv run alembic upgrade head

# 3. Terminal 1 — worker (native Chrome; self-registers into the fleet)
AGENTPILOT_ROLE=worker AGENTPILOT_REDIS_URL=$AGENTPILOT_REDIS_URL \
AGENTPILOT_DATABASE_URL=$AGENTPILOT_DATABASE_URL AGENTPILOT_NODE_ADDR=http://localhost:8001 \
  uv run uvicorn agentpilot.gateway.app:app --reload --port 8001

# 4. Terminal 2 — gateway (tenant-facing on :8000; discovers the worker via Redis)
AGENTPILOT_ROLE=gateway AGENTPILOT_ADMIN_TOKEN=dev-admin-token \
AGENTPILOT_REDIS_URL=$AGENTPILOT_REDIS_URL AGENTPILOT_DATABASE_URL=$AGENTPILOT_DATABASE_URL \
  uv run uvicorn agentpilot.gateway.app:app --reload --port 8000

# 5. Terminal 3 — frontend (Vite dev server on :5173, proxies to :8000)
cd frontend && npm install && npm run dev
```

Open http://localhost:5173, mint a key (below), sign in, and you're running the full stack.

### Path B — full two-tier fleet (Docker Compose + frontend)

The real gateway + 2 workers topology. Slower on arm64 (emulated Chrome), but exercises placement,
the worker job loops, and live-view relay. The compose `migrate` job applies migrations automatically
before the app starts.

```bash
cp .env.example .env                       # AGENTPILOT_ADMIN_TOKEN at minimum
make base                                  # first time / after a deps change: worker base + Chrome
docker compose up --build -d               # redis, postgres, migrate, worker, worker-2, gateway
cd frontend && npm install && npm run dev  # or the Caddy same-origin deploy above
```

> **Note (worker DB dependency):** the compose workers are given `AGENTPILOT_DATABASE_URL` so they
> run the crawl/agent/recipe job loops, which means the worker image must include the `postgres`
> extra (`psycopg[pool]`). `docker/worker-base.Dockerfile` installs it; if you see a worker crash-loop
> with `ModuleNotFoundError: No module named 'psycopg_pool'`, rebuild the base image
> (`make base`) then `docker compose up --build -d worker worker-2`.

### Mint a tenant API key (both paths)

```bash
curl -s -X POST http://localhost:8000/v1/api-keys \
  -H "Authorization: Bearer dev-admin-token" -H "Content-Type: application/json" \
  -d '{"tenant":"dev","name":"local"}'      # -> {"api_key":"bk_live_…"}
```

Sign in at http://localhost:5173 with `tenant=dev` + that `bk_live_…` key (or the admin token alone
for the Nodes view).

### Walk the Playground

With the UI open and signed in, exercise each tab:
- **Scrape** — enter a URL, pick formats; under *Advanced options* set `include_tags`/`exclude_tags`
  and (scrape-only) a `locale`/`timezone`; run and inspect the returned document.
- **Crawl** — set `include_paths`/`exclude_paths`, add a **webhook** (URL + events + headers) and
  confirm the one-time **signing secret** is shown; watch progress and "Load more".
- **Interact** — open a session, add a `snapshot` action (toggle *bounding boxes*, set *roles*) and
  an `extract` action (`structured_data`, tags), then *Run sequence*; use the crosshair to pick
  elements from a live snapshot.
- **Agent** — give a task, set max steps / output schema, watch steps stream, and *Cancel* mid-run.
- **Recipes** — create a recipe, then *Run*, *Heal*, and *Codegen* (choose a language); browse versions.
- **Nodes** — sign in with the admin token to see live per-node memory/CPU across `worker`/`worker-2`.

For an **automated** backend end-to-end check (no browser mocking), the `driver_contract` suite
launches real Chrome against local fixtures — see [Running the checks](#running-the-checks).

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
