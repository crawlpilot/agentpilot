# syntax=docker/dockerfile:1
# Thin CODE layer on top of docker/worker-base.Dockerfile (Debian + Chrome +
# Python deps). A code change rebuilds ONLY this file's `COPY . .` + project
# install (seconds) -- Chrome and the Python deps live in the base and never
# rebuild for a code change; the base is invalidated only when
# pyproject.toml / uv.lock change.
#
# `base` is a named build context, wired by docker-compose via
# `build.additional_contexts: base=service:worker-base` (so `docker compose up
# --build` builds the base first, then this). To build by hand outside compose:
#   docker build -f docker/worker-base.Dockerfile -t crawlpilot/worker-base .
#   docker build -f docker/worker.Dockerfile \
#     --build-context base=docker-image://crawlpilot/worker-base:latest -t crawlpilot/worker .
FROM base

WORKDIR /app
COPY . .
# `--no-dev` must match worker-base.Dockerfile's sync exactly: a mismatch makes
# this layer re-resolve the environment the base already built (and re-download
# the dev group) instead of just installing the project on top of it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --extra driver --extra postgres --extra bedrock --package agentpilot

EXPOSE 8000
ENTRYPOINT ["/app/docker/worker-entrypoint.sh"]
