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
import io
import os
import pathlib
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class _Handler(http.server.SimpleHTTPRequestHandler):
    # HTTP/1.1, not the stdlib default of 1.0. A browser opens several
    # connections per origin and keeps them alive; against an HTTP/1.0 server
    # that closes after every response, some of those requests are simply lost
    # -- which shows up as an iframe stuck on `chrome-error://chromewebdata/`
    # while `curl` on the same URL is perfectly happy.
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: object, cross_origin: str | None = None, **kwargs: object) -> None:
        self.cross_origin = cross_origin
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def log_message(self, *args: object) -> None:  # noqa: D102 - quiet by default
        pass

    def send_head(self):  # noqa: ANN201 - matches the stdlib signature
        """Substitute the cross-origin placeholder on the way out.

        The sibling origin's port is only known once the server is bound, and
        the iframe's `src` has to be a real attribute at parse time -- assigning
        it from JS afterwards produces a frame Chrome treats differently, and
        this fixture exists precisely to exercise the out-of-process path.
        """

        path = self.translate_path(self.path)
        if not path.endswith("index.html") or self.cross_origin is None:
            return super().send_head()

        body = (
            pathlib.Path(path)
            .read_text()
            .replace("__CROSS_ORIGIN__", self.cross_origin)
            .encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        return io.BytesIO(body)


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


def serve(port: int, cross_origin: str | None = None) -> _Server:
    handler = functools.partial(_Handler, directory=str(ROOT), cross_origin=cross_origin)
    server = _Server(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    base = int(os.environ.get("PORT", "8091"))
    secondary = serve(base + 1)
    primary = serve(base, cross_origin=f"http://127.0.0.1:{base + 1}")
    print(f"toolbench on :{base} (primary) and :{base + 1} (cross-origin)", file=sys.stderr)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        primary.shutdown()
        secondary.shutdown()


if __name__ == "__main__":
    main()
