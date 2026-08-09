"""Unit tests for `agentpilot.extraction.block_detect` -- the ported
HtmlIntegrity block classifier and burn weights. Pure, no browser."""

from __future__ import annotations

from agentpilot.extraction import block_detect
from agentpilot.extraction.block_detect import Scope, Verdict


def _big(body: str) -> str:
    # Pad past the too-small floor so length heuristics don't misfire.
    return body + "<a href='#'>x</a>" + ("y" * 1_000)


def test_akamai_access_denied_is_forbidden_even_on_200() -> None:
    body = _big(
        "<h1>Access Denied</h1> You don't have permission to access "
        "on this server. Reference #18.6d882c31 errors.edgesuite.net"
    )
    assert block_detect.classify_page(html=body, url="https://zara.com/x", status=200) is (
        Verdict.FORBIDDEN
    )


def test_cloudflare_interstitial_is_robot_check() -> None:
    body = _big("<title>Just a moment...</title> /cdn-cgi/challenge-platform")
    assert block_detect.classify_page(html=body, url="https://x.com", status=200) is (
        Verdict.ROBOT_CHECK
    )


def test_amazon_short_captcha_is_robot_check() -> None:
    body = "<html>Type the characters you see in this image</html>"
    assert block_detect.classify_page(html=body, url="https://amazon.com", status=200) is (
        Verdict.ROBOT_CHECK
    )


def test_blocked_url_and_403_body_markers() -> None:
    assert block_detect.classify_page(
        html=_big("ok"), url="https://x.com/blocked", status=200
    ) is Verdict.ROBOT_CHECK
    assert block_detect.classify_page(
        html=_big("403 Forbidden"), url="https://x.com", status=200
    ) is Verdict.FORBIDDEN


def test_status_signals() -> None:
    assert block_detect.classify_page(html=_big("ok"), url="u", status=403) is Verdict.FORBIDDEN
    assert block_detect.classify_page(html=_big("ok"), url="u", status=429) is Verdict.RATE_LIMITED


def test_walmart_blocked_url_is_heaviest_robot_check() -> None:
    # The Walmart site checker upgrades a landed /blocked wall to ROBOT_CHECK_3
    # (weight 3) vs the generic ROBOT_CHECK (weight 1) a non-Walmart host gets.
    v = block_detect.classify_page(
        html=_big("ok"),
        url="https://www.walmart.com/blocked?url=abc",
        status=200,
    )
    assert v is Verdict.ROBOT_CHECK_3
    assert block_detect.warning_weight(v) == 3


def test_walmart_undersized_item_is_too_small() -> None:
    # A short /ip/ product page (well under the 300 KB floor) is a stub/wall.
    v = block_detect.classify_page(
        html=_big("some thin product markup"),
        url="https://www.walmart.com/ip/Thing/12345",
        status=200,
    )
    assert v is Verdict.TOO_SMALL


def test_walmart_full_item_is_ok() -> None:
    big_page = "<html><a href='#'>x</a>" + ("y" * 350_000) + "</html>"
    v = block_detect.classify_page(
        html=big_page, url="https://www.walmart.com/ip/Thing/12345", status=200
    )
    assert v is Verdict.OK


def test_amazon_undersized_dp_is_too_small() -> None:
    v = block_detect.classify_page(
        html=_big("thin"), url="https://www.amazon.com/dp/B00TEST", status=200
    )
    assert v is Verdict.TOO_SMALL
    assert block_detect.classify_page(html=_big("ok"), url="u", status=404) is Verdict.NOT_FOUND


def test_empty_and_too_small() -> None:
    assert block_detect.classify_page(html="", url="u", status=200) is Verdict.EMPTY
    assert block_detect.classify_page(html="   ", url="u", status=200) is Verdict.EMPTY
    # no anchor + tiny -> EMPTY; has anchor + tiny -> TOO_SMALL.
    assert block_detect.classify_page(html="<div>hi</div>", url="u", status=200) is Verdict.EMPTY
    assert block_detect.classify_page(html="<a>hi</a>", url="u", status=200) is Verdict.TOO_SMALL


def test_ok_page() -> None:
    assert block_detect.classify_page(
        html=_big("<html><body>real content</body></html>"), url="u", status=200
    ) is Verdict.OK


def test_is_abck_valid() -> None:
    assert block_detect.is_abck_valid(None) is False
    assert block_detect.is_abck_valid("") is False
    assert block_detect.is_abck_valid("abc~-1~def~0~1") is False  # not-yet-solved sensor field
    assert block_detect.is_abck_valid("abc~0~def~1") is True
    assert block_detect.is_abck_valid("longopaquevalue") is True


def test_warning_weights_and_scope() -> None:
    assert block_detect.warning_weight(Verdict.FORBIDDEN) == block_detect.MAX_WARNINGS
    # Graduated robot-check severities (Pulsar afterRun): 1/2/3.
    assert block_detect.warning_weight(Verdict.ROBOT_CHECK) == 1
    assert block_detect.warning_weight(Verdict.ROBOT_CHECK_2) == 2
    assert block_detect.warning_weight(Verdict.ROBOT_CHECK_3) == 3
    assert block_detect.warning_weight(Verdict.OK) == 0
    assert block_detect.retry_scope(Verdict.FORBIDDEN) is Scope.PRIVACY
    assert block_detect.retry_scope(Verdict.TOO_SMALL) is Scope.CRAWL
    assert block_detect.retry_scope(Verdict.OK) is Scope.NONE


def test_is_blocked() -> None:
    # PRIVACY-scope verdicts are hard walls (they raise + rotate the identity);
    # CRAWL-scope soft verdicts do not.
    assert block_detect.is_blocked(Verdict.ROBOT_CHECK) is True
    assert block_detect.is_blocked(Verdict.ROBOT_CHECK_3) is True
    assert block_detect.is_blocked(Verdict.FORBIDDEN) is True
    assert block_detect.is_blocked(Verdict.EMPTY) is True  # PRIVACY per Pulsar
    assert block_detect.is_blocked(Verdict.TOO_SMALL) is False  # CRAWL (soft)
    assert block_detect.is_blocked(Verdict.RATE_LIMITED) is False
    assert block_detect.is_blocked(Verdict.OK) is False
