"""Job-lifecycle webhook config -- attached to a `Job` at creation time,
delivered by `agentpilot.webhook` (HMAC-signed, egress-guarded; see that
package's docstring for why the guard is mandatory, not optional).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

WebhookEvent = Literal["started", "page", "completed", "failed"]


@dataclass
class WebhookConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    events: tuple[WebhookEvent, ...] = ("completed", "failed")
    secret: str = ""
    """Server-generated at job creation, returned once in the create-job
    response, never re-exposed afterward -- same "shown once" discipline as
    `auth.keygen`'s API-key plaintext."""
