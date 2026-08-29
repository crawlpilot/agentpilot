"""Driver-agnostic identity types: who a browsing session is acting as."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

_SLUG_UNSAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


class ProfileKind(Enum):
    DEFAULT = "default"
    PROTOTYPE = "prototype"
    GROUP = "group"
    TEMPORARY = "temporary"
    PERMANENT = "permanent"


def _sanitize_segment(segment: str) -> str:
    if not segment or segment in (".", "..") or "/" in segment or "\\" in segment:
        raise ValueError(f"unsafe identity path segment: {segment!r}")
    cleaned = _SLUG_UNSAFE.sub("_", segment)
    if not cleaned:
        raise ValueError(f"identity path segment sanitizes to empty: {segment!r}")
    return cleaned


@dataclass(frozen=True)
class IdentityRef:
    """An opaque handle for "these requests share cookies, profile and exit IP".

    `key` is **never parsed by the browser layer** -- it is validated for
    filesystem safety and otherwise used only for equality and scoping. What it
    means is the caller's business: the multi-tenant platform composes
    `f"{tenant}/{domain}/{name}"` (see `control.identity.identity_for`), a
    single-tenant crawler may use `"example.com"`, and neither shape is
    privileged.

    This replaced `IdentityKey(tenant, domain, name)`, whose `.slug()` rendered
    the SaaS tenancy model directly as the on-disk profile layout -- so the
    library's storage *was* the billing model (plan D10). The rendered slug is
    byte-identical across that change, so existing profile dirs and vault
    entries stay valid; only the owner of the composition moved up a layer.

    A profile dir on a node's local disk exists for exactly one `IdentityRef`.
    `.slug()` is the canonical filesystem-safe path fragment for that dir, and
    every segment is validated here, once, rather than at each call site.
    """

    key: str
    kind: ProfileKind = field(default=ProfileKind.TEMPORARY)

    def slug(self) -> str:
        return "/".join(_sanitize_segment(part) for part in self.key.split("/"))

    @property
    def scope_root(self) -> str:
        """The first slug segment -- the containment root a profile dir may not
        escape. Generic on purpose: the browser layer must not know that the
        platform happens to put a tenant there."""

        return self.slug().split("/", 1)[0]

    @property
    def is_temporary(self) -> bool:
        return self.kind is ProfileKind.TEMPORARY

    @property
    def is_permanent(self) -> bool:
        return self.kind in (ProfileKind.DEFAULT, ProfileKind.PROTOTYPE, ProfileKind.PERMANENT)
