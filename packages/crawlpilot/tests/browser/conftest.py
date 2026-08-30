"""Fixtures for the real-browser suite.

**Imports nothing but `crawlpilot`, deliberately.** The existing real-browser
suite (`tests/driver_contract/`) imports `agentpilot.control.identity` in its
conftest, which is the single line that makes none of it runnable in the
crawlpilot-only venv -- so the standalone install had no browser test of its own,
and the thing most likely to break for an external user was the thing least
covered. Everything here goes through the public facade (`crawlpilot.api`), which
means the suite doubles as the client-library test: if it needs a private module
to do something ordinary, the facade has a hole.

Marked `browser` and deselected by default, so CI job 1's clean-venv run stays
Chrome-free. Run it with `-m browser`.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "toolbench"


@dataclass(frozen=True)
class Toolbench:
    """The fixture site, served on two origins."""

    primary: str
    secondary: str

    def url(self, page: str = "index.html", **params: str) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.primary}/{page}" + (f"?{query}" if query else "")

    @property
    def index(self) -> str:
        """The main page, wired to load its cross-origin iframe from the sibling
        origin. Without the parameter the page drops that iframe entirely, so a
        test that forgets it fails on a missing element rather than silently
        passing against a same-origin stand-in."""

        return self.url("index.html", crossOrigin=self.secondary)


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Otherwise teardown waits out every keep-alive socket the browser is still
    # holding, and the suite looks like it hung.
    block_on_close = False


def _serve() -> tuple[_Server, str]:
    handler = functools.partial(_Handler, directory=str(FIXTURE_ROOT))
    server = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture(scope="session")
def toolbench() -> Iterator[Toolbench]:
    """The fixture site on two ports.

    `localhost` and `127.0.0.1` are different origins to the browser while both
    resolving to loopback, which is what makes the cross-origin iframe genuinely
    out-of-process without a second machine or any DNS.
    """

    assert FIXTURE_ROOT.is_dir(), f"toolbench fixture missing at {FIXTURE_ROOT}"
    primary, primary_port = _serve()
    secondary, secondary_port = _serve()
    try:
        yield Toolbench(
            primary=f"http://localhost:{primary_port}",
            secondary=f"http://127.0.0.1:{secondary_port}",
        )
    finally:
        primary.shutdown()
        secondary.shutdown()


@pytest.fixture
async def browser(tmp_path: Path):
    """A `Browser` with a profile root that dies with the test.

    Constructed with no launch arguments on purpose: this is the discovery path
    a new user hits, so every test in the suite exercises it.
    """

    from crawlpilot.api import Browser  # noqa: PLC0415 -- keeps collection Chrome-free

    async with Browser(profiles_root=tmp_path / "profiles", headless=True) as instance:
        yield instance


@pytest.fixture
async def page(browser):
    """An open session on the toolbench, through the public facade."""

    async with browser.session() as session:
        yield session
