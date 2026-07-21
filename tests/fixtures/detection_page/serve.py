"""Tiny static file server for the detection-page fixture.

Used both by `docker-compose.yml` (as the `detection-page` sidecar service)
and directly by tests that want a real HTTP origin rather than a `data:` URL
(the coordinate/localStorage checks need an actual origin).
"""

from __future__ import annotations

import http.server
import os
import sys


def main() -> None:
    port = int(os.environ.get("PORT", "8090"))
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = http.server.ThreadingHTTPServer(
        ("0.0.0.0", port), http.server.SimpleHTTPRequestHandler
    )
    print(f"serving detection_page fixture on :{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
