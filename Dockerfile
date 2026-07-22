# Single `baas` service for P0 (gateway+worker collapsed -- the role split
# lands in P2). Debian base because Chrome + Xvfb + cdp_patches' X11 input
# dispatch all need real system packages, not a distroless/alpine image.
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
RUN uv sync --no-install-project

COPY . .
RUN uv sync
RUN uv run patchright install --with-deps chrome

ENV DISPLAY=:99
ENV BAAS_PROFILES_DIR=/var/lib/baas/profiles
RUN mkdir -p /var/lib/baas/profiles

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
