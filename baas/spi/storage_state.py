"""Mirrors Playwright's native `context.storage_state()` JSON shape field-for-field.

Pure data only -- no capture logic lives here. Playwright/Patchright's native
`storage_state()` already captures cookies *and* every visited origin's
localStorage in one call, which is the fix over Browser4's `saveStorageState()`
(it only ever captured the single origin loaded at call time). The driver
calls the native method directly and wraps the result in these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LocalStorageEntry:
    name: str
    value: str


@dataclass
class OriginState:
    origin: str
    local_storage: list[LocalStorageEntry] = field(default_factory=list)


@dataclass
class StorageState:
    cookies: list[dict[str, Any]] = field(default_factory=list)
    origins: list[OriginState] = field(default_factory=list)
