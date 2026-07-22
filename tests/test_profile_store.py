"""`test_profile_store_rejects_path_traversal` per `plan.md`'s P2 test list.

`IdentityKey.slug()` already rejects literal `..`/`/`/`\\` in any segment at
*construction* time, so the only way to actually exercise
`profile_store.py`'s own defense-in-depth check is a symlink planted inside
`profiles_root` that points outside the tenant root -- exactly the class of
bug ("validate at the boundary, verify at the point of use") this second
gate exists for.
"""

from __future__ import annotations

import pytest

from agentpilot.identity.profile_store import PathTraversalError, resolve_profile_dir
from agentpilot.spi.identity import IdentityKey


def test_resolve_profile_dir_stays_within_tenant_root(tmp_path) -> None:
    identity = IdentityKey(tenant="acme", domain="example.com", name="alice")
    resolved = resolve_profile_dir(tmp_path, identity)
    assert resolved == (tmp_path / "acme" / "example.com" / "alice").resolve()


def test_symlinked_domain_dir_escaping_tenant_root_is_rejected(tmp_path) -> None:
    # The symlink must sit *below* the tenant segment (e.g. at the domain
    # level) -- planting it directly at the tenant segment would make
    # `tenant_root` itself resolve through the same symlink, so both sides
    # of the containment check would agree and nothing would be caught.
    tenant_dir = tmp_path / "acme"
    tenant_dir.mkdir(parents=True)
    outside = tmp_path.parent / "outside-secret"
    outside.mkdir(exist_ok=True)
    (tenant_dir / "example.com").symlink_to(outside)

    identity = IdentityKey(tenant="acme", domain="example.com", name="alice")
    with pytest.raises(PathTraversalError):
        resolve_profile_dir(tmp_path, identity)


def test_identity_key_itself_rejects_dotdot_segments() -> None:
    with pytest.raises(ValueError):
        IdentityKey(tenant="..", domain="example.com", name="alice").slug()
