"""Unit tests for `agentpilot.extraction.site_checkers` -- per-site
block/integrity checkers. Pure, no browser."""

from __future__ import annotations

from agentpilot.extraction.block_detect import Verdict
from agentpilot.extraction.site_checkers import AmazonChecker, JdChecker, WalmartChecker


def _big(n: int) -> str:
    return "<a href='#'>x</a>" + ("y" * n)


def test_relevance_is_host_scoped() -> None:
    assert WalmartChecker().is_relevant("https://www.walmart.com/ip/x/1")
    assert not WalmartChecker().is_relevant("https://example.com/ip/x/1")
    assert AmazonChecker().is_relevant("https://www.amazon.co.uk/dp/X")
    assert JdChecker().is_relevant("https://item.jd.com/1.html")


def test_walmart_defers_on_a_full_page() -> None:
    # A large product page: the checker has no opinion (None), so the chain
    # falls through to the generic classifier / OK.
    assert WalmartChecker().check(
        html=_big(350_000), url="https://www.walmart.com/ip/Thing/1", status=200
    ) is None


def test_jd_login_redirect_is_robot_check() -> None:
    v = JdChecker().check(html=_big(10), url="https://passport.jd.com/login.aspx", status=200)
    assert v is Verdict.ROBOT_CHECK_3


def test_jd_defers_when_no_tell() -> None:
    assert JdChecker().check(
        html=_big(300_000), url="https://item.jd.com/1.html", status=200
    ) is None


def test_amazon_captcha_short_page_is_robot_check() -> None:
    body = "<html>Type the characters you see in this image</html>"
    assert AmazonChecker().check(
        html=body, url="https://www.amazon.com/errors/validateCaptcha", status=200
    ) is Verdict.ROBOT_CHECK


def test_amazon_district_check_is_opt_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    body = "<div id='glow-ingress-block'>Deliver to Chicago 60601</div>" + _big(300_000)
    url = "https://www.amazon.com/dp/B00TEST"
    # Off by default: no WRONG_GEO even though the district doesn't match.
    assert AmazonChecker().check(html=body, url=url, status=200) is None
    # Opted in with a district the page doesn't mention -> WRONG_GEO.
    monkeypatch.setenv("AGENTPILOT_AMAZON_EXPECT_DISTRICT", "New York")
    assert AmazonChecker().check(html=body, url=url, status=200) is Verdict.WRONG_GEO
    # ...and satisfied when the page does mention it.
    body_ny = "<div id='glow-ingress-block'>Deliver to New York 10001</div>" + _big(300_000)
    assert AmazonChecker().check(html=body_ny, url=url, status=200) is None
