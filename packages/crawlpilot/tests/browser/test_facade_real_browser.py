"""The facade against real Chrome, using **only** the public API.

Proof that a caller who imports nothing but `crawlpilot.api` (plus the action
dataclasses for real batching) can drive a real browser -- which is what the
standalone `crawlpilot` wheel promises an external user.

If this file ever needs `crawlpilot.driver` or `crawlpilot.session` to do
something ordinary, the facade has a hole.

Where `test_toolchain.py` covers the breadth of the verbs, this covers the
lifecycle around them: that the context opens, batches in one round trip, and
scrapes one-shot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from crawlpilot.api import Browser
from crawlpilot.spi import actions as spi_actions

pytestmark = [pytest.mark.asyncio, pytest.mark.browser]

PAGE_HTML = """
<html><head><title>Facade</title></head>
<body><main>
  <h1>Hello</h1>
  <p>Facade works. This is the first paragraph of real body content, long
  enough and distinct enough that the extraction pipeline treats it as the
  main article rather than boilerplate noise.</p>
  <p>A second paragraph continues with different wording so that extraction
  has real multi-paragraph content to work with, and so the page clears the
  block classifier's EMPTY / too-small floors -- a 250-byte fixture reads as
  a wall, not a page.</p>
  <input id="q" aria-label="query">
  <button id="go">Go</button>
</main></body></html>
"""


def _serve(httpserver: HTTPServer) -> str:
    httpserver.expect_request("/").respond_with_data(PAGE_HTML, content_type="text/html")
    return httpserver.url_for("/")


def _ref(node) -> str:  # type: ignore[no-untyped-def]
    # `selector_index`, not `backend_node_id`: the capture mints the ref, and the
    # two diverge once a cross-origin frame's renderer reuses an id.
    return f"e{node.selector_index}"


def _find_role(node, role: str):  # type: ignore[no-untyped-def]
    if node.ax_role == role:
        return node
    children = list(node.children_and_shadow_roots)
    if node.content_document is not None:
        children.append(node.content_document)
    for child in children:
        found = _find_role(child, role)
        if found is not None:
            return found
    return None


async def test_a_caller_drives_a_real_browser_through_the_facade(
    tmp_path: Path, httpserver: HTTPServer
) -> None:
    url = _serve(httpserver)
    async with Browser(profiles_root=tmp_path) as browser:
        async with browser.session() as page:
            await page.navigate(url)
            markdown = await page.markdown()

    assert "Hello" in markdown
    assert "Facade works." in markdown


async def test_one_batch_reaches_chrome_in_one_round_trip(
    tmp_path: Path, httpserver: HTTPServer
) -> None:
    """Batching is the transport: `execute()` dispatches the whole sequence in a
    single call rather than decomposing into one call per action."""

    url = _serve(httpserver)
    async with Browser(profiles_root=tmp_path) as browser:
        async with browser.session() as page:
            result = await page.execute(
                [
                    spi_actions.NavigateAction(url=url),
                    spi_actions.ExtractAction(format="markdown"),
                    spi_actions.ScreenshotAction(),
                ]
            )

    assert result.page_title == "Facade"
    assert len(result.extracts) == 1
    assert len(result.screenshots) == 1
    assert result.screenshots[0].startswith(b"\x89PNG")


async def test_interaction_through_the_facade(tmp_path: Path, httpserver: HTTPServer) -> None:
    """Refs come from a snapshot (`e<backendNodeId>`), not CSS selectors -- the
    same contract the agent loop uses."""

    url = _serve(httpserver)
    async with Browser(profiles_root=tmp_path) as browser:
        async with browser.session() as page:
            await page.navigate(url)
            tree = await page.snapshot()
            assert tree is not None

            textbox = _find_role(tree, "textbox")
            button = _find_role(tree, "button")
            assert textbox is not None
            assert button is not None

            await page.fill(_ref(textbox), "typed")
            await page.click(_ref(button))


async def test_scrape_is_one_shot_and_returns_a_document(
    tmp_path: Path, httpserver: HTTPServer
) -> None:
    """`scrape()` mints an identity, runs one batch and tears the context down --
    the shape for a list of independent URLs."""

    url = _serve(httpserver)
    async with Browser(profiles_root=tmp_path) as browser:
        document = await browser.scrape(url, tier="basic")

    assert document.markdown is not None
    assert "Facade works." in document.markdown
