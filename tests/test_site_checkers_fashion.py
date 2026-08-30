"""The Zara / COS / H&M checker -- the three storefronts agentpilot is actually
blocked on. Pure; no browser.

The generic `_TOO_SMALL_LEN = 500` floor in `block_detect` cannot tell a 2 KB
DataDome interstitial from a thin real page, so before this checker existed
those walls were returned to callers as content.
"""

from __future__ import annotations

import pytest

from agentpilot.control.retail_extension import RetailExtension
from crawlpilot.extensions import ExtensionRegistry
from crawlpilot.extraction import block_detect
from crawlpilot.extraction.block_detect import Verdict

# The retail checkers are no longer auto-installed at import (plan D12): they are
# an ordinary extension the platform wires in. These tests exercise the same
# guarantees through that seam, which is also what proves the wiring works.
_RETAIL_HOOKS = ExtensionRegistry([RetailExtension()]).blocks


def _classify(**kwargs: object) -> Verdict:
    return block_detect.classify_page(hooks=_RETAIL_HOOKS, **kwargs)  # type: ignore[arg-type]

ZARA_PDP = "https://www.zara.com/uk/en/ribbed-dress-p03641044.html"
HM_PDP = "https://www2.hm.com/en_gb/productpage.1227667001.html"
COS_LISTING = "https://www.cosstores.com/en_gbp/women/dresses.html"


def _page(size: int, body: str = "<h1>Product</h1><a href='/x'>x</a>") -> str:
    return body + "y" * max(0, size - len(body))


def test_a_full_size_pdp_passes() -> None:
    assert _classify(html=_page(200_000), url=ZARA_PDP, status=200) is Verdict.OK


def test_a_stub_pdp_is_too_small() -> None:
    """A wall or geo/consent stub served with a clean 200 is a few KB; a real
    Inditex PDP is hundreds."""

    verdict = _classify(html=_page(3_000), url=ZARA_PDP, status=200)

    assert verdict is Verdict.TOO_SMALL
    assert block_detect.retry_scope(verdict) is block_detect.Scope.CRAWL


def test_hm_productpage_shape_is_recognised_as_a_pdp() -> None:
    assert _classify(html=_page(50_000), url=HM_PDP, status=200) is (
        Verdict.TOO_SMALL
    )


def test_a_listing_uses_the_lighter_floor() -> None:
    """Category pages are lighter than PDPs; using the PDP floor for them would
    fail every legitimate listing."""

    assert _classify(html=_page(50_000), url=COS_LISTING, status=200) is (
        Verdict.OK
    )


@pytest.mark.parametrize("path", ["/blocked", "/verify", "/errors/403", "/challenge"])
def test_a_landed_block_url_is_the_heaviest_verdict(path: str) -> None:
    """These storefronts bounce a suspected bot to a consent/error route with a
    clean status rather than serving a 403, so only the landed URL betrays it."""

    verdict = _classify(
        html=_page(200_000), url=f"https://www.zara.com{path}", status=200
    )

    assert verdict is Verdict.ROBOT_CHECK_3
    assert block_detect.retry_scope(verdict) is block_detect.Scope.PRIVACY


def test_datadome_interstitial_on_a_fashion_host_is_a_hard_block() -> None:
    body = _page(2_000, "<script src='https://geo.captcha-delivery.com/captcha/'></script>")

    verdict = _classify(html=body, url=HM_PDP, status=200)

    assert verdict is Verdict.ROBOT_CHECK_3
    assert block_detect.retry_scope(verdict) is block_detect.Scope.PRIVACY


def test_a_definite_wall_marker_beats_the_size_heuristic() -> None:
    """Site checkers run before the generic markers, so a size floor would
    otherwise downgrade a hard FORBIDDEN (instant burn) to a soft TOO_SMALL
    (same-identity retry) and the identity would keep hammering the wall."""

    body = _page(3_000, "<h1>Access Denied</h1> Reference #18.6d882c31 errors.edgesuite.net")

    verdict = _classify(html=body, url=ZARA_PDP, status=200)

    assert verdict is Verdict.FORBIDDEN
    assert block_detect.retry_scope(verdict) is block_detect.Scope.PRIVACY


def test_unrelated_hosts_are_untouched() -> None:
    """The floors are storefront-specific; a small page elsewhere must still
    take the generic path."""

    assert _classify(
        html=_page(3_000), url="https://example.test/blog/post", status=200
    ) is Verdict.OK
