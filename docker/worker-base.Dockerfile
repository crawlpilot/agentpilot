# syntax=docker/dockerfile:1
# Prebuilt BASE for the worker image -- the heavy, slow-changing half:
# Debian + Xvfb/X11/iptables + uv + Python deps + Chrome. Everything here
# depends only on system packages and the dependency manifest
# (pyproject.toml / uv.lock), NOT on app code, so it is built once and reused
# across every code change. `docker/worker.Dockerfile` adds a thin code layer
# on top with `FROM base`.
#
# Rebuilt only when deps change. docker-compose builds it automatically as the
# `base` context for the `worker` service (see `build.additional_contexts`);
# to build it by hand: `docker build -f docker/worker-base.Dockerfile -t
# crawlpilot/worker-base:latest .`.
FROM python:3.12-slim-bookworm

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11-utils \
    curl \
    ca-certificates \
    iptables

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
# postgres extra too: docker-compose gives the worker an AGENTPILOT_DATABASE_URL
# so it runs the crawl/agent/recipe worker loops, whose PostgresJobStore needs
# psycopg[pool] -- without it the worker crashes at boot importing psycopg_pool.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --extra driver --extra postgres

# Chrome + its apt deps depend only on the (already-installed) patchright
# version. `--no-sync` uses the venv from the step above without trying to
# install the not-yet-existent project.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    uv run --no-sync patchright install --with-deps chrome

ENV DISPLAY=:99
ENV AGENTPILOT_PROFILES_DIR=/var/lib/agentpilot/profiles
RUN mkdir -p /var/lib/agentpilot/profiles
