#!/bin/sh
set -e

Xvfb "$DISPLAY" -screen 0 1920x1080x24 &
XVFB_PID=$!
trap 'kill $XVFB_PID 2>/dev/null' EXIT

# Give Xvfb a moment to bind before Chrome tries to attach to $DISPLAY.
for _ in $(seq 1 20); do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
        break
    fi
    sleep 0.25
done

# `--no-sync`: run the environment worker.Dockerfile built, don't re-resolve it
# at boot. See the note in docker/gateway.Dockerfile's CMD -- a bare `uv run`
# rebuilds the workspace wheels and pulls the dev group on every start, and its
# implicit sync carries none of this image's `--extra driver/postgres/bedrock`.
exec uv run --no-sync uvicorn agentpilot.gateway.app:app --host 0.0.0.0 --port 8000
