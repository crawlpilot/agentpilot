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
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --extra postgres

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --extra postgres

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "agentpilot.gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
