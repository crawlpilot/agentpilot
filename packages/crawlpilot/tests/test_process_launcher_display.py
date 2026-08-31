"""Unit tests for `ProcessLauncher.ensure_display` -- the headful/Xvfb display
resolver. No browser, no real Xvfb (the Linux-start path is monkeypatched)."""

from __future__ import annotations

import pytest

from crawlpilot.driver.process_launcher import ProcessLauncher


def test_existing_display_env_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    assert ProcessLauncher().ensure_display() is True


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_native_window_server_is_a_display(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """macOS and Windows have a display even with no `DISPLAY` set.

    This asserted `False` until 0.2, on the reasoning that we should not "pop a
    real window on a dev machine". The effect was that `DISPLAY` -- an X11
    variable -- was treated as the definition of "has a display" on the two
    platforms that never set it, so `headful=True` silently ran headless on
    every Mac. Headless is a strong bot signal, which is how a stealth scrape
    ended up served a CAPTCHA wall on every attempt while asking for a window.

    Opting out is the caller's job (`headful=False`), not this method's.
    """

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("crawlpilot.driver.process_launcher.sys.platform", platform)
    assert ProcessLauncher().ensure_display() is True


def test_linux_starts_xvfb_and_exports_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("crawlpilot.driver.process_launcher.sys.platform", "linux")

    launcher = ProcessLauncher()

    def _fake_ensure_xvfb(display: str = ":99") -> None:
        launcher._xvfb_proc = object()  # type: ignore[assignment]

    monkeypatch.setattr(launcher, "ensure_xvfb", _fake_ensure_xvfb)
    assert launcher.ensure_display() is True
    import os

    assert os.environ["DISPLAY"] == ":99"


def test_linux_without_xvfb_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("crawlpilot.driver.process_launcher.sys.platform", "linux")

    launcher = ProcessLauncher()
    monkeypatch.setattr(launcher, "ensure_xvfb", lambda display=":99": None)  # binary missing
    assert launcher.ensure_display() is False
