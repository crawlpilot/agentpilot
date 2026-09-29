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
# Phase 7 split the repo into a uv workspace: `packages/crawlpilot` (the browser
# platform) and `packages/agentpilot` (this service). The dependency-cache layer
# must therefore copy *both* manifests plus the workspace root -- a single root
# `pyproject.toml` no longer describes the dependency graph, and copying only it
# silently resolved an empty project.
COPY pyproject.toml uv.lock* ./
COPY packages/crawlpilot/pyproject.toml packages/crawlpilot/README.md ./packages/crawlpilot/
COPY packages/agentpilot/pyproject.toml packages/agentpilot/README.md ./packages/agentpilot/
# postgres extra too: docker-compose gives the worker an AGENTPILOT_DATABASE_URL
# so it runs the crawl/agent/recipe worker loops, whose PostgresJobStore needs
# psycopg[pool] -- without it the worker crashes at boot importing psycopg_pool.
# bedrock extra: the worker is where LLMConfig.from_env() runs, so
# AGENTPILOT_LLM_PROVIDER=bedrock needs `anthropic` present in this image.
# `--no-dev`: `uv sync` installs the workspace root's `dev` dependency-group by
# default, so the runtime image was pulling ruff/mypy/pytest/import-linter/
# fakeredis/lupa -- build time and image size spent on tooling no container ever
# runs. CI installs them explicitly (`uv sync --all-extras`); images don't.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project --extra driver --extra postgres --extra bedrock --package agentpilot

# The browser + its apt deps depend only on the (already-installed) patchright
# version. `--no-sync` uses the venv from the step above without trying to
# install the not-yet-existent project.
#
# Native to the build architecture, never emulated. Google ships Chrome for
# linux/amd64 only, so an arm64 build installs Playwright's Chromium instead --
# `browser_discovery` finds it as the bundled browser when no system Chrome
# exists. This image used to be pinned to amd64 and emulated on arm64 hosts,
# which is what got it blocked. MEASURED on cos.com (Akamai), same Apple Silicon
# host, same residential IP, no proxy:
#
#     linux/amd64 Google Chrome under emulation -> Access Denied, every run
#     linux/arm64 Chromium, native             -> product page, 3/3
#
# Emulated Chrome ran the sensor's JavaScript 3-4x slower and crashed renderers
# on heavy pages; nothing a flag could fix.
#
# Fonts: a slim image has almost none, and the installed font set is itself
# fingerprinted -- these are the common desktop Linux defaults.
ARG TARGETARCH
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    if [ "$TARGETARCH" = "amd64" ]; then BROWSER=chrome; else BROWSER=chromium; fi \
    && uv run --no-sync patchright install --with-deps "$BROWSER" \
    && apt-get update && apt-get install -y --no-install-recommends \
       fonts-liberation fonts-dejavu-core fonts-noto-color-emoji

ENV DISPLAY=:99
ENV AGENTPILOT_PROFILES_DIR=/var/lib/agentpilot/profiles
RUN mkdir -p /var/lib/agentpilot/profiles
