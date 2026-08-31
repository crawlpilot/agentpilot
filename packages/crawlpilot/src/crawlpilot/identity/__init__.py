"""P2's identity layer: profile-dir path safety, encrypted-at-rest state
vaulting, and assign-once proxy pinning. Never imports `crawlpilot.driver` -- same
composition-root rule as `agentpilot.gateway`/`crawlpilot.session`.

Every module here holds *policy* over an injectable `StateStore` (see
`crawlpilot.policy`): the scoring, the retirement caps and the sticky-pick
algorithm live in this package, and only the storage is someone else's.

`fingerprint` and `profile_store` are absent from `__all__` deliberately. They
are what a *driver* consults while launching, not something a consumer composes;
their shapes track Chrome's, and they should be free to.
"""

from __future__ import annotations

from crawlpilot.identity import burn_tracker, proxy_health, proxy_pinning, vault

__all__ = ["burn_tracker", "proxy_health", "proxy_pinning", "vault"]
