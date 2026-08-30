"""Composing and decomposing a multi-tenant `IdentityRef`.

`agentpilot.spi.identity.IdentityRef` carries an opaque `key`; the browser layer
never parses it. This module is where the platform's meaning lives: an identity
is `(tenant, domain, name)`, rendered as `"tenant/domain/name"`.

Both directions belong here. Composition, because a tenant is a customer and the
browser has no business minting one. Decomposition, because the pieces that
legitimately need the tenant -- authorization in `gateway.routes`, per-tenant
proxy pools, the placement Lua payloads -- are all platform code, and they
should read the tenant from the platform's own parser rather than from a field
on a browser-layer type.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentpilot.spi.identity import IdentityRef, ProfileKind


@dataclass(frozen=True)
class IdentityParts:
    tenant: str
    domain: str
    name: str


def identity_for(
    tenant: str,
    domain: str,
    name: str,
    kind: ProfileKind = ProfileKind.TEMPORARY,
) -> IdentityRef:
    """The platform's identity shape. The rendered slug is byte-identical to
    the pre-Phase-3c `IdentityRef(tenant, domain, name).slug()`, which is what
    keeps existing profile dirs and vault entries valid."""

    return IdentityRef(key=f"{tenant}/{domain}/{name}", kind=kind)


def parts_of(identity: IdentityRef) -> IdentityParts:
    """Split a platform-composed key back into its three parts.

    Raises `ValueError` on a key this platform did not compose -- better than
    silently attributing a session to the wrong tenant, since callers use the
    result for authorization.
    """

    pieces = identity.key.split("/")
    if len(pieces) != 3:
        raise ValueError(
            f"identity key {identity.key!r} is not a platform-composed "
            "'tenant/domain/name' triple"
        )
    return IdentityParts(*pieces)


def tenant_of(identity: IdentityRef) -> str:
    """Shorthand for the authorization checks in `gateway.routes`."""
    return parts_of(identity).tenant
