"""Configuration read from the environment **once, at a composition root**.

Before this module, seven leaf modules called `os.environ.get` at the point of
use -- `identity.fingerprint` (at *import* time, so the value froze on first
import), `identity.profile_store`, `identity.proxy_health`, `egress.policy`
and `extraction.site_checkers`. That makes the library unconfigurable by a
caller who is not the process owner: a crawler embedding it could not change
the pinned Chrome version, point at a different prototype root, or exempt its
own LLM endpoint from the SSRF fence without mutating global process state.

The rule this module establishes: **leaves take values as arguments; only a
composition root reads the environment.** `gateway.wiring` calls
`BrowserConfig.from_env()` once and passes the result down.

Deliberately a **pure leaf**, like `agentpilot.tiers`: it imports nothing from
`agentpilot`, so every branch of the layer graph can depend on it. An
import-linter `forbidden` contract enforces that.

`DEFAULTS` is a fully-populated `BrowserConfig` with no environment in it, so a
caller that supplies nothing gets the documented defaults rather than whatever
the host process happens to export.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_CHROME_VERSION = "131.0.6778.86"
"""Must track the Chrome actually deployed. Patchright launches the real system
Chrome, so a stale pin here while the browser reports a newer build is a hard,
deterministic bot tell for WAFs that cross-check UA against Client Hints."""

DEFAULT_PROXY_MAX_SUCCESS = 100
PROTOTYPE_ENV = "AGENTPILOT_PROTOTYPE_PROFILE_DIR"


@dataclass(frozen=True)
class FingerprintConfig:
    chrome_version: str = DEFAULT_CHROME_VERSION

    @classmethod
    def from_env(cls) -> FingerprintConfig:
        raw = os.environ.get("AGENTPILOT_CHROME_VERSION", DEFAULT_CHROME_VERSION).strip()
        return cls(chrome_version=raw or DEFAULT_CHROME_VERSION)


@dataclass(frozen=True)
class ProfileConfig:
    prototype_root: Path | None = None
    """Root of the per-domain prototype profile tree, or `None` to disable
    seeding entirely (the previous behaviour when the env var was unset)."""

    @classmethod
    def from_env(cls) -> ProfileConfig:
        configured = os.environ.get(PROTOTYPE_ENV)
        if not configured or not configured.strip():
            return cls()
        return cls(prototype_root=Path(configured.strip()))


@dataclass(frozen=True)
class ProxyHealthConfig:
    max_success: int = DEFAULT_PROXY_MAX_SUCCESS

    @classmethod
    def from_env(cls) -> ProxyHealthConfig:
        return cls(
            max_success=int(
                os.environ.get("AGENTPILOT_PROXY_MAX_SUCCESS", str(DEFAULT_PROXY_MAX_SUCCESS))
            )
        )


@dataclass(frozen=True)
class EgressConfig:
    llm_endpoint_host: str | None = None
    """Hostname of the worker's own LLM endpoint, exempted from the container
    egress fence. The browser is fenced off RFC1918, but a local LLM often
    *lives* there (Ollama via `host.docker.internal` sits in 192.168/16), so
    without this exemption the baseline severs the control plane too."""

    @classmethod
    def from_env(cls) -> EgressConfig:
        # `ANTHROPIC_BASE_URL` is the fallback the Bedrock provider resolves its
        # endpoint from, so honour it too rather than silently losing the
        # exemption when only that one is set.
        base = os.environ.get("AGENTPILOT_LLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
        if not base:
            return cls()
        return cls(llm_endpoint_host=urlparse(base).hostname)


@dataclass(frozen=True)
class ContentConfig:
    amazon_expect_district: str = ""
    """Opt-in delivery-district check: when set and Amazon's delivery block is
    present without this district, the egress geo is wrong. Depends on the
    proxy's exit country, so it is deployment config, not a library constant.
    (Phase 4 moves this out of the library entirely -- see plan D12.)"""

    @classmethod
    def from_env(cls) -> ContentConfig:
        return cls(
            amazon_expect_district=os.environ.get(
                "AGENTPILOT_AMAZON_EXPECT_DISTRICT", ""
            ).strip().lower()
        )


@dataclass(frozen=True)
class BrowserConfig:
    """Everything the browser layer needs that used to come from the ambient
    process environment. Phase 5's `Browser` facade takes this directly."""

    fingerprint: FingerprintConfig = field(default_factory=FingerprintConfig)
    profiles: ProfileConfig = field(default_factory=ProfileConfig)
    proxy_health: ProxyHealthConfig = field(default_factory=ProxyHealthConfig)
    egress: EgressConfig = field(default_factory=EgressConfig)
    content: ContentConfig = field(default_factory=ContentConfig)

    @classmethod
    def from_env(cls) -> BrowserConfig:
        return cls(
            fingerprint=FingerprintConfig.from_env(),
            profiles=ProfileConfig.from_env(),
            proxy_health=ProxyHealthConfig.from_env(),
            egress=EgressConfig.from_env(),
            content=ContentConfig.from_env(),
        )


DEFAULTS = BrowserConfig()
"""The documented defaults, with no environment consulted. Used as the default
argument wherever config is threaded, so an un-configured caller is explicit
rather than accidentally inheriting the host's exports."""
