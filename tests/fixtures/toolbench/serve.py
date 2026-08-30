"""Static server for the toolbench fixture, on two ports.

Two ports rather than one, because a single origin cannot produce a
cross-origin iframe. `localhost:A` serving a page whose iframe points at
`127.0.0.1:B` is a genuine out-of-process frame -- a different origin to the
browser, while both resolve to loopback and need no second machine, no DNS and
no network. That is the only way to exercise the OOPIF capture path locally.

Used the same two ways `tests/fixtures/detection_page/serve.py` is: as a
docker-compose sidecar, and directly from tests that want a real HTTP origin
rather than inline HTML (the toolbench pages are large, and are themselves the
subject under test).

    python serve.py            # ports 8091 and 8092
    PORT=9000 python serve.py  # ports 9000 and 9001
"""

from __future__ import annotations

import functools
import http.server
import os
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: D102 - quiet by default
        pass

    def end_headers(self) -> None:
        # The fixture is served fresh on every request: a cached index.html
        # would silently mask an edit to the page a test is asserting against.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Without this, teardown blocks until every keep-alive connection the
    # browser is holding open times out -- the suite appears to hang at the end.
    block_on_close = False


def serve(port: int) -> _Server:
    handler = functools.partial(_Handler, directory=str(ROOT))
    server = _Server(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    base = int(os.environ.get("PORT", "8091"))
    primary, secondary = serve(base), serve(base + 1)
    print(f"toolbench on :{base} (primary) and :{base + 1} (cross-origin)", file=sys.stderr)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        primary.shutdown()
        secondary.shutdown()


if __name__ == "__main__":
    main()
