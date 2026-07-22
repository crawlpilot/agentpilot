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

exec uv run uvicorn baas.gateway.app:app --host 0.0.0.0 --port 8000
