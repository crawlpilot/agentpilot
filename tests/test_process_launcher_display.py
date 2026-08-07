"""Unit tests for `ProcessLauncher.ensure_display` -- the headful/Xvfb display
resolver. No browser, no real Xvfb (the Linux-start path is monkeypatched)."""

from __future__ import annotations

import pytest

from agentpilot.driver.process_launcher import ProcessLauncher


def test_existing_display_env_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    assert ProcessLauncher().ensure_display() is True


def test_no_display_and_not_linux_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("agentpilot.driver.process_launcher.sys.platform", "darwin")
    # macOS/dev: headful not available -> caller degrades to headless, no window.
    assert ProcessLauncher().ensure_display() is False


def test_linux_starts_xvfb_and_exports_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("agentpilot.driver.process_launcher.sys.platform", "linux")

    launcher = ProcessLauncher()

    def _fake_ensure_xvfb(display: str = ":99") -> None:
        launcher._xvfb_proc = object()  # type: ignore[assignment]

    monkeypatch.setattr(launcher, "ensure_xvfb", _fake_ensure_xvfb)
    assert launcher.ensure_display() is True
    import os

    assert os.environ["DISPLAY"] == ":99"


def test_linux_without_xvfb_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("agentpilot.driver.process_launcher.sys.platform", "linux")

    launcher = ProcessLauncher()
    monkeypatch.setattr(launcher, "ensure_xvfb", lambda display=":99": None)  # binary missing
    assert launcher.ensure_display() is False
