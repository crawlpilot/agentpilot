"""Unit tests for `agentpilot.driver.warmup` -- the ported CommonRPA.visit
human warm-up. Uses a fake Patchright page (no browser); `asyncio.sleep` is
patched to a no-op so the human delays don't slow the suite."""

from __future__ import annotations

import asyncio

import pytest

from agentpilot.driver import humanize, warmup


class _FakeMouse:
    def __init__(self) -> None:
        self.wheels: list[tuple[float, float]] = []

    async def wheel(self, dx: float, dy: float) -> None:
        self.wheels.append((dx, dy))


class _FakeContext:
    def __init__(self, cookie_states: list[list[dict]]) -> None:
        self._states = cookie_states
        self.calls = 0

    async def cookies(self) -> list[dict]:
        state = self._states[min(self.calls, len(self._states) - 1)]
        self.calls += 1
        return state


class _FakePage:
    def __init__(self, cookie_states: list[list[dict]] | None = None, url: str = "https://x") -> None:
        self.mouse = _FakeMouse()
        self.context = _FakeContext(cookie_states or [[]])
        self.url = url

    async def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


async def test_human_scroll_count_and_deltas() -> None:
    page = _FakePage()
    await warmup.human_scroll(page, humanize.STEALTH)
    assert 2 <= len(page.mouse.wheels) <= 6
    for dx, dy in page.mouse.wheels:
        assert dx == 0
        assert 100 <= dy <= 280


async def test_wait_for_abck_returns_true_once_validated() -> None:
    page = _FakePage(
        cookie_states=[
            [{"name": "_abck", "value": "a~-1~b"}],  # not yet solved
            [{"name": "_abck", "value": "a~0~b"}],  # validated
        ]
    )
    assert await warmup.wait_for_abck(page, humanize.STEALTH, timeout_s=2.0) is True


async def test_wait_for_abck_times_out_without_cookie() -> None:
    page = _FakePage(cookie_states=[[]])
    assert await warmup.wait_for_abck(page, humanize.STEALTH, timeout_s=0.2) is False


async def test_warm_up_without_abck_wait_returns_true() -> None:
    page = _FakePage()
    assert await warmup.warm_up(page, humanize.STEALTH, wait_abck=False) is True
    assert len(page.mouse.wheels) >= 2  # it scrolled


async def test_warm_up_never_raises_on_page_errors() -> None:
    class _BrokenPage(_FakePage):
        async def wait_for_selector(self, selector: str, timeout: int | None = None) -> None:
            raise RuntimeError("detached")

    page = _BrokenPage()
    # Must swallow and still return a bool, never propagate.
    assert await warmup.warm_up(page, humanize.STEALTH, wait_abck=False) is True
