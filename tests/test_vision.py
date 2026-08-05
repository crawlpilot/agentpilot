"""Unit tests for the vision-grounding message assembly (`_user_content`).
Pure -- no browser, no LLM."""

from __future__ import annotations

import base64

from agentpilot.agent.loop import _user_content


def test_no_screenshot_returns_plain_text() -> None:
    assert _user_content("hello", None) == "hello"


def test_screenshot_produces_multimodal_parts_with_data_url() -> None:
    png = b"\x89PNG\r\n\x1a\nfake-bytes"
    content = _user_content("state here", png)
    assert isinstance(content, list)
    text_part, image_part = content
    assert text_part["type"] == "text"
    assert "state here" in text_part["text"]
    assert "screenshot" in text_part["text"].lower()
    assert image_part["type"] == "image_url"
    expected = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    assert image_part["image_url"]["url"] == expected
