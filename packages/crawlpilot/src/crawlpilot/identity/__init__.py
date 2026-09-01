"""P2's identity layer: profile-dir path safety, encrypted-at-rest state
vaulting, and assign-once proxy pinning. Never imports `crawlpilot.driver` -- same
composition-root rule as `agentpilot.gateway`/`crawlpilot.session`.

Every module here holds *policy* over an injectable `StateStore` (see
`crawlpilot.policy`): the scoring, the retirement caps and the sticky-pick
algorithm live in this package, and only the storage is someone else's.

`fingerprint` and `profile_store` are absent from `__all__` deliberately. They
are what a *driver* consults while launching, not something a consumer composes;
their shapes track Chrome's, and they should be free to.

`vault` is in `__all__` but is NOT imported eagerly: it needs `cryptography`,
which only the `[vault]`/`[all]` extras install. Eager-importing it here made
*every* `import crawlpilot` require that extra -- `crawlpilot/__init__.py`
imports `crawlpilot.api`, which imports `crawlpilot.identity.proxy_pinning`,
which runs this module -- so the Chrome-free, vault-free gateway image
(`docker/gateway.Dockerfile`, which syncs only `--extra postgres`) died at boot
with `ModuleNotFoundError: No module named 'cryptography'`. Same reason
`session/interactive.py` keeps its `Vault` import under `TYPE_CHECKING`; this
was the remaining eager path to the same module. Access it lazily
(`crawlpilot.identity.vault`) and the extra stays genuinely optional.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from crawlpilot.identity import burn_tracker, proxy_health, proxy_pinning

if TYPE_CHECKING:
    from crawlpilot.identity import vault

__all__ = ["burn_tracker", "proxy_health", "proxy_pinning", "vault"]


def __getattr__(name: str) -> Any:
    # PEP 562: keeps `crawlpilot.identity.vault` working as an attribute after a
    # bare `import crawlpilot.identity`, without paying for `cryptography` on
    # installs that never touch the vault.
    #
    # `import_module`, not `from crawlpilot.identity import vault`: the `from`
    # form goes through `_handle_fromlist`, which -- when the submodule import
    # fails, i.e. exactly the no-cryptography case -- swallows the
    # ModuleNotFoundError and falls back to `getattr` on this module, landing
    # back here and recursing until the stack blows. `import_module` propagates
    # the real error.
    if name == "vault":
        return importlib.import_module("crawlpilot.identity.vault")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
