"""One shared Patchright API object per process (browser-use's
`GLOBAL_PATCHRIGHT_API_OBJECT` pattern) -- every `open()` launches its own
context off this one driver instance rather than spawning a Patchright
process per session. Also owns the Xvfb subprocess lifecycle for headful
sessions.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

import structlog
from patchright.async_api import Playwright, async_playwright

log = structlog.get_logger(__name__)


class ProcessLauncher:
    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._lock = asyncio.Lock()
        self._xvfb_proc: subprocess.Popen[bytes] | None = None

    async def get_playwright(self) -> Playwright:
        async with self._lock:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
        return self._playwright

    def ensure_xvfb(self, display: str = ":99") -> None:
        """Starts the shared Xvfb display once, lazily, on first headful open().

        P0's `docs/capacity-planning.md` spike note covers whether a single
        shared display is safe for *concurrent* OS-level input across
        sessions -- this method only covers process lifecycle, not that
        concurrency question.
        """

        if self._xvfb_proc is not None:
            return
        if shutil.which("Xvfb") is None:
            log.warning(
                "process_launcher.xvfb_unavailable",
                reason="Xvfb binary not found -- expected outside the Linux worker "
                "container; headful sessions will fail to launch here",
            )
            return
        self._xvfb_proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("process_launcher.xvfb_started", display=display, pid=self._xvfb_proc.pid)

    def ensure_display(self) -> bool:
        """Ensure a usable display exists for a *headful* launch, and return
        whether headful is actually possible on this host.

        - An already-exported `DISPLAY` (someone else's Xvfb, a real X server) is
          used as-is.
        - On Linux we lazily start our own Xvfb (`:99`) and export `DISPLAY` to
          it -- this is the production worker path (headful under Xvfb is what
          `cdp_patches` OS-level input, a P1 spike, will eventually need).
        - Otherwise (macOS/dev, or a Linux container without Xvfb) headful is
          not available, so this returns `False` and the caller degrades to
          headless rather than failing the launch. We deliberately do NOT pop a
          real window on a dev machine or in tests.
        """

        if os.environ.get("DISPLAY"):
            return True
        if sys.platform.startswith("linux"):
            self.ensure_xvfb()
            if self._xvfb_proc is not None:
                os.environ["DISPLAY"] = ":99"
                return True
        return False

    async def close(self) -> None:
        async with self._lock:
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
        if self._xvfb_proc is not None:
            self._xvfb_proc.terminate()
            self._xvfb_proc.wait(timeout=5)
            self._xvfb_proc = None
