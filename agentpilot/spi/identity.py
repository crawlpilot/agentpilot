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
class IdentityKey:
    """Identifies a single warm browsing identity: `(tenant, domain, name)`.

    A profile dir on a node's local disk exists for exactly one `IdentityKey`.
    `.slug()` is the canonical filesystem-safe path fragment for that dir, and
    is reused by P2's `profile_store.py` path-traversal guard — every segment
    is validated here, once, rather than re-validated at each call site.
    """

    tenant: str
    domain: str
    name: str
    kind: ProfileKind = field(default=ProfileKind.TEMPORARY)

    def slug(self) -> str:
        return "/".join(
            _sanitize_segment(part) for part in (self.tenant, self.domain, self.name)
        )

    @property
    def is_temporary(self) -> bool:
        return self.kind is ProfileKind.TEMPORARY

    @property
    def is_permanent(self) -> bool:
        return self.kind in (ProfileKind.DEFAULT, ProfileKind.PROTOTYPE, ProfileKind.PERMANENT)
