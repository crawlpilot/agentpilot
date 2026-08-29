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

import shutil
from pathlib import Path

import structlog

from agentpilot import config
from agentpilot.spi.identity import IdentityKey

log = structlog.get_logger(__name__)


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


PROTOTYPE_ENV = config.PROTOTYPE_ENV
"""Re-exported for callers that still name it here. The variable is *read* by
`config.ProfileConfig.from_env()` at a composition root, never by this module.
The prototype *catalog* it points at is resolved by
`control.prototypes.DirectoryPrototypes`, behind `policy.PrototypeProvider` --
this module keeps only the mechanics (path-traversal guard, directory copy)."""
"""Root holding hand-warmed Chrome profiles to seed new identities from.

Layout, checked most-specific first:

    $AGENTPILOT_PROTOTYPE_PROFILE_DIR/
        www.zara.com/       <- used for identities whose domain matches
        default/            <- used for every other domain

The single highest-value technique in either source project
(`BrowserFileSystem.prepareUserDataDir`, `PrototypePrivacyAgentGenerator`).
You browse a target site normally in a real Chrome once -- accept the cookie
banner, click around, let Akamai's `_abck`/`bm_sz` and DataDome's `datadome`
cookies mature -- and every synthetic identity is then born a byte-level clone
of that profile, with history, localStorage and a device reputation, instead of
the cookieless first-visit browser that `ephemeral.py` otherwise creates for
every anonymous scrape and which its own docstring already flags as a bot
signal.

Unset -> no seeding, and every profile starts empty exactly as before.
"""

_SKIP_ENTRIES = frozenset(
    {
        # Chrome refuses to reuse a profile another process holds, and these
        # are per-run lock/socket state rather than the browsing history we
        # actually want to clone.
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "lockfile",
        # Crash/metrics state from the prototype run is noise at best, and
        # reporting another machine's crash on first launch at worst.
        "Crashpad",
        "CrashpadMetrics",
        "BrowserMetrics",
        "ShaderCache",
        "GrShaderCache",
        "GraphiteDawnCache",
    }
)


def seed_profile_dir(profile_dir: Path, prototype: Path) -> bool:
    """Clone `prototype` into `profile_dir`. Returns whether anything was copied.

    Only ever seeds a directory that does not already exist or is empty --
    re-seeding a live profile would throw away the cookies it has since earned,
    which is the opposite of the point.

    Best-effort by contract: a failed copy leaves whatever was written and
    returns `False`, because a cold profile is a worse scrape, not a failed
    one. The caller must not treat this as a gate.
    """

    try:
        if profile_dir.exists() and any(profile_dir.iterdir()):
            return False
        shutil.copytree(
            prototype,
            profile_dir,
            dirs_exist_ok=True,
            symlinks=False,
            ignore=shutil.ignore_patterns(*_SKIP_ENTRIES),
            # A prototype can contain dead symlinks and sockets that would
            # abort the whole copy; skipping them beats failing.
            ignore_dangling_symlinks=True,
        )
    except Exception:
        log.warning("profile_store.seed_failed", prototype=str(prototype), target=str(profile_dir))
        return False
    return True


def delete_profile_dir(profiles_root: Path, identity: IdentityKey) -> None:
    """For an ephemeral (`/v1/scrape`) identity only, right after
    `registry.evict()` + `driver.close()`: a warm/interactive identity's
    profile dir is meant to outlive its context (that's the entire point of
    `launch_persistent_context` -- see `driver.close()`'s callers in
    `routes/sessions.py`, which never delete it), so this must only ever be
    called for a one-shot identity that will never be reopened. Goes through
    the same `resolve_profile_dir()` path-traversal guard as every other
    caller that touches a profile dir on disk -- no separate validation here.
    """

    path = resolve_profile_dir(profiles_root, identity)
    shutil.rmtree(path, ignore_errors=True)
