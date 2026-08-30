"""Directory-backed `PrototypeProvider` -- the platform's prototype catalog.

The lookup ("per-origin first, then `default/`") used to live in
`identity.profile_store.prototype_dir_for`, inside the browser layer. It is a
*catalog*: it knows that prototypes are directories, laid out on a filesystem,
named after origins. That is the consumer's data model, so it belongs here --
the browser layer only asks the question (`policy.PrototypeProvider`).

A per-origin prototype is what makes seeding worth doing at all: the cookies
that matter (`_abck`, `datadome`) are origin-scoped, so a profile warmed on one
site carries nothing useful for another -- only the generic "this browser has a
history" signal, which is what `default/` provides.
"""

from __future__ import annotations

from pathlib import Path


class DirectoryPrototypes:
    def __init__(self, root: Path | None) -> None:
        self._root = root

    def prototype_for(self, origin: str) -> Path | None:
        if self._root is None:
            return None
        for candidate in (self._root / origin, self._root / "default"):
            if candidate.is_dir():
                return candidate
        return None
