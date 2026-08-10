# Heavy image for AGENTPILOT_ROLE=worker (and, run outside Docker, monolith): the
# only role that ever imports agentpilot.driver. Debian base because Chrome +
# Xvfb + cdp_patches' X11 input dispatch all need real system packages, not
# a distroless/alpine image. See docker/gateway.Dockerfile for the lean
# image used by the stateless gateway role, which needs none of this.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11-utils \
    curl \
    ca-certificates \
    iptables \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
# postgres extra too: docker-compose gives the worker an AGENTPILOT_DATABASE_URL
# so it runs the crawl/agent/recipe worker loops, whose PostgresJobStore needs
# psycopg[pool] -- without it the worker crashes at boot importing psycopg_pool.
RUN uv sync --no-install-project --extra driver --extra postgres

COPY . .
RUN uv sync --extra driver --extra postgres
RUN uv run patchright install --with-deps chrome

ENV DISPLAY=:99
ENV AGENTPILOT_PROFILES_DIR=/var/lib/agentpilot/profiles
RUN mkdir -p /var/lib/agentpilot/profiles

EXPOSE 8000
ENTRYPOINT ["/app/docker/worker-entrypoint.sh"]
