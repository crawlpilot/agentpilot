"""The composition root -- the ONLY file in the repo that imports `baas.driver`.

P0 has no registry/placement layer yet, so this also holds the small
single-process precursor to P1's `registry.py`: an in-memory session dict and
an `active_identities` map enforcing "at most one ACTIVE context per
IdentityKey", guarded by one lock (P1 replaces this with per-identity locks
and P2 re-implements the invariant as Lua -- this isn't thrown away, just
extended with idle-reaping and finer-grained locking).

`warm_contexts` exists because P0 has no reaper: releasing a session marks
its `ContextRef` IDLE but never calls `driver.close()` on it, so the
underlying Chrome process (and its profile dir's `SingletonLock`) is still
there. Without this map, re-opening the same `IdentityKey` after a release
would try to launch a *second* Chrome onto the same profile dir and crash
with "ProcessSingleton ... already in use" -- discovered by actually using
the UI, not by inspection. Tracking the warm `ContextRef` per identity and
reusing it on the next open (new `session_id`, same underlying context) is
the minimal correct fix, and it's also just P1's registry pattern
(Browser4's `computeIfAbsent`) arriving a little early because real usage
needed it now rather than waiting for P1.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from baas.driver.patchright_driver import PatchrightDriver
from baas.driver.process_launcher import ProcessLauncher
from baas.spi.driver import BrowserDriver
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef


@dataclass
class Session:
    session_id: str
    identity: IdentityKey
    ctx: ContextRef
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
        self.active_identities: dict[IdentityKey, str | None] = {}
        self.warm_contexts: dict[IdentityKey, ContextRef] = {}
        self.lock = asyncio.Lock()

    async def close(self) -> None:
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
