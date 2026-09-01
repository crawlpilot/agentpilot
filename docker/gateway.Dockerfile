# syntax=docker/dockerfile:1
# Lean image for AGENTPILOT_ROLE=gateway: gateway is a stateless proxy
# (agentpilot/gateway/wiring.py's _init_gateway) that never imports agentpilot.driver at
# runtime, so it needs none of worker.Dockerfile's Chrome/Xvfb/X11/iptables
# packages, no `patchright install`, and no Xvfb-wait entrypoint script --
# there's nothing to wait on, so CMD goes straight into uvicorn.
#
# Same layer discipline as worker.Dockerfile: deps install before `COPY . .`,
# so a code change rebuilds only the final project-install layer.
FROM python:3.12-slim-bookworm

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates

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
# `--no-dev`: `uv sync` installs the workspace root's `dev` dependency-group by
# default, so this image was pulling ruff/mypy/pytest/import-linter -- tooling
# the gateway never runs. Must be identical in both syncs below, or the second
# re-resolves the environment the first one cached.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project --extra postgres --package agentpilot

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --extra postgres --package agentpilot

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "agentpilot.gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
