"""The composition root -- the ONLY file in the repo that imports `baas.driver`.

Owns the shared Patchright singleton, the `PatchrightDriver`, the P1
`Registry` (identity -> warm context, <=1 ACTIVE invariant) and `Reaper`
(idle-TTL + memory-pressure + per-process-ceiling destruction), and the
`session_id -> Session` dict the HTTP layer actually looks sessions up by
(`Registry` is keyed by `IdentityKey`, not `session_id`, since one warm
context can be reused across many session_ids over its lifetime -- open,
release, reopen mints a new session_id for the same underlying context).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from baas.driver.patchright_driver import PatchrightDriver
from baas.driver.process_launcher import ProcessLauncher
from baas.session.reaper import Reaper
from baas.session.registry import Registry
from baas.spi.driver import BrowserDriver
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef, LeaseId


@dataclass
class Session:
    session_id: str
    identity: IdentityKey
    ctx: ContextRef
    lease_id: LeaseId
    tier: str
    headful: bool
    block_popups: bool


class Wiring:
    def __init__(self) -> None:
        self.launcher = ProcessLauncher()
        self.driver: BrowserDriver = PatchrightDriver(self.launcher)
        assert isinstance(self.driver, BrowserDriver)

        self.profiles_root = Path(os.environ.get("BAAS_PROFILES_DIR", "/var/lib/baas/profiles"))
        self.sessions: dict[str, Session] = {}
        self.registry = Registry()
        self.lease_ttl_seconds = float(os.environ.get("BAAS_LEASE_TTL_SECONDS", "300"))
        self.reaper = Reaper(
            self.registry,
            self.driver,
            idle_ttl_seconds=float(os.environ.get("BAAS_IDLE_TTL_SECONDS", "300")),
            scan_interval_seconds=float(os.environ.get("BAAS_REAPER_INTERVAL_SECONDS", "15")),
            mem_pressure_watermark_pct=float(os.environ.get("BAAS_MEM_WATERMARK_PCT", "85")),
            per_process_ceiling_mb=float(os.environ.get("BAAS_PER_PROCESS_CEILING_MB", "4096")),
        )
        self.reaper.start()

    async def close(self) -> None:
        await self.reaper.stop()
        for session in list(self.sessions.values()):
            await self.driver.close(session.ctx)
        await self.launcher.close()


_wiring: Wiring | None = None


def get_wiring() -> Wiring:
    global _wiring
    if _wiring is None:
        _wiring = Wiring()
    return _wiring


async def reset_wiring() -> None:
    """Test-only: tears down and clears the singleton so each test gets a
    fresh driver/session store."""

    global _wiring
    if _wiring is not None:
        await _wiring.close()
    _wiring = None
