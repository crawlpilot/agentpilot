"""Job-lifecycle webhook config -- attached to a `Job` at creation time,
delivered HMAC-signed and egress-guarded.

Lives beside `jobs.types` rather than in `crawlpilot.spi` because nothing in
the browser library ever referenced it; a job's delivery callback belongs to
the service that owns the job.
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
