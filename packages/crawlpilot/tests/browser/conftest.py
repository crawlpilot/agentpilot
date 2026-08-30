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

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "toolbench"

PAGES = ("index.html", "article.html", "frame_inner.html", "form_target.html")

CROSS_ORIGIN_PLACEHOLDER = "__CROSS_ORIGIN__"
"""Stands in for the sibling origin's base URL inside `index.html`.

The port is only known once the server is bound, and the cross-origin iframe's
`src` has to be a real attribute at parse time -- a frame whose src JavaScript
assigns afterwards behaves differently, and exercising the out-of-process path is
the whole reason that iframe exists."""


@dataclass(frozen=True)
class Toolbench:
    """The fixture site, served on two origins."""

    primary: str
    secondary: str

    def url(self, page: str = "index.html") -> str:
        return f"{self.primary}/{page}"

    @property
    def index(self) -> str:
        """The main page, with its cross-origin iframe already pointing at the
        sibling origin."""

        return self.url("index.html")


def _mount(server: HTTPServer, *, cross_origin: str | None = None) -> str:
    """Serve the toolbench's pages from `server`, returning its base URL."""

    for name in PAGES:
        body = (FIXTURE_ROOT / name).read_text()
        if cross_origin is not None:
            body = body.replace(CROSS_ORIGIN_PLACEHOLDER, cross_origin)
        for path in (f"/{name}", f"/{name}" if name != "index.html" else "/"):
            server.expect_request(path).respond_with_data(body, content_type="text/html")
    return f"http://{server.host}:{server.port}"


@pytest.fixture(scope="session")
def toolbench(make_httpserver) -> Iterator[Toolbench]:
    """The fixture site on two origins.

    Served by `pytest-httpserver` rather than a hand-rolled `http.server`: a
    stdlib `SimpleHTTPRequestHandler` serves these files perfectly well to
    `curl`, but Chrome would not load one of them as a *sub-frame*, leaving the
    cross-origin iframe stuck on `chrome-error://chromewebdata/`. The same
    two-origin arrangement works against werkzeug, which is also what the
    repo's other browser suite already uses.

    `localhost` and `127.0.0.1` are different origins to the browser while both
    resolving to loopback, so the frame is genuinely out-of-process without a
    second machine or any DNS.
    """

    assert FIXTURE_ROOT.is_dir(), f"toolbench fixture missing at {FIXTURE_ROOT}"

    secondary = make_httpserver
    secondary_url = f"http://127.0.0.1:{secondary.port}"
    _mount(secondary)

    primary = make_httpserver
    _mount(primary, cross_origin=secondary_url)

    try:
        yield Toolbench(
            primary=f"http://localhost:{primary.port}", secondary=secondary_url
        )
    finally:
        primary.clear()
        secondary.clear()


@pytest.fixture
async def browser(tmp_path: Path):
    """A `Browser` with a profile root that dies with the test.

    Constructed with no launch arguments beyond `headless`, on purpose: the
    browser-discovery path is the one a new user hits first, so every test in
    the suite exercises it.
    """

    from crawlpilot.api import Browser  # noqa: PLC0415 -- keeps collection Chrome-free

    async with Browser(profiles_root=tmp_path / "profiles", headless=True) as instance:
        yield instance


@pytest.fixture
async def page(browser):
    """An open session on the toolbench, through the public facade."""

    async with browser.session() as session:
        yield session
