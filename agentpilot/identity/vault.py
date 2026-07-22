"""Encrypts/decrypts `StorageState` at rest with Fernet (symmetric,
authenticated encryption) -- the vault is the durability layer per
`plan.md`: a profile dir is a node-local *cache*, the vault file is
*source of truth*. A thin wrapper only, by design: it does not decide
*when* to save/restore (see the module docstring on the two triggers
below) -- callers (the worker's session-open/release flow) own that.

**Restore trigger**: `load()` must only ever be fed into
`driver.restore_state()` when `open()` targets a *fresh* profile dir (new
node, or a reaper-recreated one) -- never onto an already-warm dir, which
would fight Playwright's own persistent-context state (browser-use's
documented persistent-dir-vs-storage_state conflict). Enforced by the
caller checking "did this profile dir exist before I created it", not by
this module.

**Save trigger**: a checkpoint on release-to-IDLE (not only at destroy)
bounds node-loss staleness to one session's delta -- also the caller's
job, at the same release call site.

**Key management**: one `BAAS_VAULT_KEY` (a `Fernet.generate_key()`
value) shared fleet-wide. Per-tenant *key derivation* (not just per-tenant
*file paths*, which this module already has via `IdentityKey.slug()`) is
real further hardening, deliberately left for later -- this pass gets
encryption-at-rest and the two trigger semantics right first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from baas.spi.identity import IdentityKey
from baas.spi.storage_state import LocalStorageEntry, OriginState, StorageState


def _to_json(state: StorageState) -> dict[str, Any]:
    return {
        "cookies": state.cookies,
        "origins": [
            {
                "origin": origin.origin,
                "localStorage": [
                    {"name": e.name, "value": e.value} for e in origin.local_storage
                ],
            }
            for origin in state.origins
        ],
    }


def _from_json(raw: dict[str, Any]) -> StorageState:
    return StorageState(
        cookies=list(raw.get("cookies", [])),
        origins=[
            OriginState(
                origin=o["origin"],
                local_storage=[
                    LocalStorageEntry(name=e["name"], value=e["value"])
                    for e in o.get("localStorage", [])
                ],
            )
            for o in raw.get("origins", [])
        ],
    )


class Vault:
    def __init__(self, root: Path, key: bytes) -> None:
        self._root = root
        self._fernet = Fernet(key)

    def _path(self, identity: IdentityKey) -> Path:
        # `identity.slug()` (not raw tenant/domain/name fields) is what
        # keeps this path-traversal-safe -- same sanitizer `profile_store.py`
        # relies on for the actual browser profile dir.
        return self._root / f"{identity.slug()}.json.enc"

    def save(self, identity: IdentityKey, state: StorageState) -> None:
        path = self._path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(_to_json(state)).encode("utf-8")
        path.write_bytes(self._fernet.encrypt(payload))

    def load(self, identity: IdentityKey) -> StorageState | None:
        path = self._path(identity)
        if not path.exists():
            return None
        try:
            payload = self._fernet.decrypt(path.read_bytes())
        except InvalidToken:
            # Corrupted file or ciphertext from a rotated-out key -- treated
            # as "nothing durable to restore" rather than a hard failure;
            # the caller falls back to a cold profile, never a crash.
            return None
        return _from_json(json.loads(payload))
