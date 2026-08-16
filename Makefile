# Convenience targets for the docker-compose stack. The worker image is split
# into a heavy, slow-changing base (Debian + Chrome + Python deps) and a thin
# code layer, so a code change never re-downloads Chrome. See README "Docker
# build performance".
.PHONY: base up up-build down logs rebuild

# Build (or rebuild) the worker base image. Slow -- pulls Chrome + apt deps.
# Only needed the first time, and after a dependency change (pyproject.toml /
# uv.lock). The `base` profile keeps this out of `docker compose up`.
base:
	docker compose build worker-base

# Start the stack, building the thin worker/gateway code layers on top of the
# already-built base. Fast for code-only changes.
up-build:
	docker compose up -d --build

up:
	docker compose up -d

# First run (or after a deps bump): base then the rest.
rebuild: base up-build

down:
	docker compose down

logs:
	docker compose logs -f
