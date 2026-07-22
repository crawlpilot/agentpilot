# Lean image for BAAS_ROLE=gateway: gateway is a stateless proxy
# (baas/gateway/wiring.py's _init_gateway) that never imports baas.driver at
# runtime, so it needs none of worker.Dockerfile's Chrome/Xvfb/X11/iptables
# packages, no `patchright install`, and no Xvfb-wait entrypoint script --
# there's nothing to wait on, so CMD goes straight into uvicorn.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project --extra postgres

COPY . .
RUN uv sync --extra postgres

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "baas.gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
