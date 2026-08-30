"""Extension identity and host-compatibility gating.

Ported in spirit from Browser4's `skeleton/plugin/PluginManifest.kt` and
`boot/plugin/PluginCompatibility.kt`. The verdict table below is theirs, and it
is well judged: an extension built against an *older* major keeps working
(the host only added things), while one built against a *newer* major is refused
(it may call APIs this host does not have). Silently loading the latter would
surface as an `AttributeError` deep inside a hook, on some page, some day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

API_VERSION = "0.1"
"""The extension API version this host implements. Bump the major when a hook is
removed or its signature changes incompatibly."""

_VERSION = re.compile(r"^(\d+)\.(\d+)")


def _major(version: str | None) -> int | None:
    if not version:
        return None
    m = _VERSION.match(version.strip())
    return int(m.group(1)) if m else None


class Compatibility(Enum):
    LOAD = "load"
    LOAD_WITH_WARNING = "load_with_warning"
    REFUSE = "refuse"


@dataclass(frozen=True)
class ExtensionManifest:
    name: str
    version: str = "0.0"
    api_version: str = API_VERSION
    description: str = ""
    requires: tuple[str, ...] = field(default_factory=tuple)
    """Mount names this extension expects the host to support -- advisory, for
    a clearer error than a missing-attribute failure at dispatch time."""

    default_enabled: bool = True


@dataclass(frozen=True)
class CompatibilityVerdict:
    compatibility: Compatibility
    reason: str = ""

    @property
    def loads(self) -> bool:
        return self.compatibility is not Compatibility.REFUSE


def check_compatibility(
    manifest: ExtensionManifest, host_api_version: str = API_VERSION
) -> CompatibilityVerdict:
    theirs, ours = _major(manifest.api_version), _major(host_api_version)
    if ours is None:
        return CompatibilityVerdict(Compatibility.LOAD, "host api version unknown")
    if theirs is None:
        return CompatibilityVerdict(
            Compatibility.LOAD_WITH_WARNING,
            f"extension {manifest.name!r} declares no parseable api_version "
            f"({manifest.api_version!r}); loading best-effort",
        )
    if theirs == ours:
        return CompatibilityVerdict(Compatibility.LOAD)
    if theirs < ours:
        return CompatibilityVerdict(
            Compatibility.LOAD_WITH_WARNING,
            f"extension {manifest.name!r} targets api {manifest.api_version} but the "
            f"host implements {host_api_version}; loading, but it may miss newer hooks",
        )
    return CompatibilityVerdict(
        Compatibility.REFUSE,
        f"extension {manifest.name!r} targets api {manifest.api_version}, newer than the "
        f"host's {host_api_version}; refusing rather than failing inside a hook later",
    )
