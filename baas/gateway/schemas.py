"""Pydantic mirror of `spi` types for HTTP-boundary validation only.

Every model is `extra="forbid"` (strict, friendly schemas). This module never
defines a shape `spi` doesn't already own -- it only adds JSON-Schema-shaped
validation on top of it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- session lifecycle ---


class SessionOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    domain: str
    name: str
    tier: Literal["basic", "stealth", "enhanced", "auto"] = "auto"
    headful: bool = False
    block_popups: bool = False
    live_view: bool = False


class SessionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier_used: str
    node_id: str
    duration_ms: float


class SessionOpenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    metadata: SessionMetadata


# --- actions (closed, tagged union mirroring spi.actions) ---


class NavigateActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["navigate"]
    url: str
    timeout_ms: int = 30_000


class GoBackActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["go_back"]


class SnapshotActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["snapshot"]
    viewport_only: bool = False
    max_nodes: int | None = None
    roles: list[str] | None = None


class ExtractActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["extract"]
    format: Literal["markdown", "text", "html"] = "markdown"
    main_content: bool = True


class ScreenshotActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["screenshot"]
    full_page: bool = False


class WaitActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["wait"]
    ms: int | None = None
    ref: str | None = None


class ExecuteJsActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["execute_js"]
    script: str


class ClickActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["click"]
    ref: str
    all: bool = False


class FillActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["fill"]
    ref: str
    text: str


class SelectOptionActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["select_option"]
    ref: str
    values: list[str] = Field(default_factory=list)


class HoverActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hover"]
    ref: str


class PressActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["press"]
    key: str


class ScrollActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["scroll"]
    direction: Literal["up", "down", "left", "right"]
    ref: str | None = None


ActionIn = Annotated[
    NavigateActionIn
    | GoBackActionIn
    | SnapshotActionIn
    | ExtractActionIn
    | ScreenshotActionIn
    | WaitActionIn
    | ExecuteJsActionIn
    | ClickActionIn
    | FillActionIn
    | SelectOptionActionIn
    | HoverActionIn
    | PressActionIn
    | ScrollActionIn,
    Field(discriminator="type"),
]


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[ActionIn]


# --- action results ---


class SnapshotNodeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    epoch: int
    ref: str
    role: str
    name: str
    children: list[SnapshotNodeOut] = Field(default_factory=list)


class AXSnapshotOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    epoch: int
    root: SnapshotNodeOut


class ArtifactRefOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    tenant: str
    kind: str
    size: int
    sha256: str


class ActionResultOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshots: list[AXSnapshotOut] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    """Base64-encoded PNG bytes."""
    extracts: list[str] = Field(default_factory=list)
    js_returns: list[Any] = Field(default_factory=list)
    downloads: list[ArtifactRefOut] = Field(default_factory=list)
    sequence_aborted: bool = False
    page_changed: bool = False


# --- sessions list (enterprise UI) ---


class SessionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    tenant: str
    domain: str
    name: str
    tier: str
    headful: bool
    node_id: str
    pid: int | None
    rss_mb: float | None
    state: Literal["active", "expired"]
    lease_expires_at: float | None
    """Unix timestamp; `None` when `state == "expired"` (no live lease)."""


class SessionListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessions: list[SessionOut]


# --- API keys (enterprise UI control plane, admin-gated) ---


class ApiKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: str
    name: str


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_id: str
    tenant: str
    name: str
    prefix: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


class ApiKeyCreateOut(ApiKeyOut):
    api_key: str
    """Plaintext, returned exactly once -- never retrievable again."""


class ApiKeyListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_keys: list[ApiKeyOut]
