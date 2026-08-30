"""The single owner of what a scrape tier *means*.

Before this package the tier vocabulary and its consequences were spread over
six modules -- `spi.actions` (Runtime-free flag, interaction-profile mapping),
`session.stealth_profile` (protected set, `auto` ladder entry),
`session.ephemeral` (the escalation ladder, and the `"residential"` proxy
decision open-coded four times), `driver.humanize` (tier -> delay table),
`identity.proxy_config` (tier -> proxy pool) and `gateway.schemas` (the wire
`Literal`, declared three separate times). Adding a tier meant editing six
files and keeping three descriptions in sync by hand.

`TierPolicy.for_tier()` resolves a requested tier to every decision that
follows from it, once. Callers take a `TierPolicy`, never a bare `str`.

Deliberately a **pure leaf**: it imports nothing else from `agentpilot`, so
both the `driver` and `session` branches -- which sit above `spi` on separate
arms of the layer graph and may not import each other -- can depend on it. An
import-linter `forbidden` contract enforces that purity.
"""

from crawlpilot.tiers.policy import (
    ESCALATION,
    PROTECTED,
    Tier,
    TierName,
    TierPolicy,
)

__all__ = ["ESCALATION", "PROTECTED", "Tier", "TierName", "TierPolicy"]
