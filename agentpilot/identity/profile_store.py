"""Resolves an `IdentityKey` to its on-disk profile directory, with a
defense-in-depth path-traversal check independent of `IdentityKey.slug()`'s
own per-segment sanitization.

`slug()` already rejects `..`/`/`/`\\` in any single segment at
*construction* time, so a crafted `IdentityKey` can't produce a `../`
escape today. This module is the second gate, checked again at the point a
profile dir is actually resolved for disk I/O -- the standard "validate at
the boundary, verify at the point of use" pattern, so a future change that
loosens `slug()`'s sanitizer (or a symlink planted inside `profiles_root`
by whatever provisions it) still can't walk a tenant out of its own root.
"""

from __future__ import annotations

from pathlib import Path

from agentpilot.spi.identity import IdentityKey


class PathTraversalError(ValueError):
    pass


def resolve_profile_dir(profiles_root: Path, identity: IdentityKey) -> Path:
    tenant_root = (profiles_root / identity.tenant).resolve()
    resolved = (profiles_root / identity.slug()).resolve()
    try:
        resolved.relative_to(tenant_root)
    except ValueError as exc:
        raise PathTraversalError(
            f"identity {identity.slug()!r} resolves to {resolved}, "
            f"outside its tenant root {tenant_root}"
        ) from exc
    return resolved
