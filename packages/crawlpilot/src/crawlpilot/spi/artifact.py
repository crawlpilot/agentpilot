"""Reference to a downloaded artifact, stored in the tenant-scoped artifact store (P2).

Defined now for `ActionResult.downloads` shape completeness; download handling
(the actual store) isn't implemented until P2.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ArtifactRef:
    artifact_id: str
    tenant: str
    kind: str
    size: int
    sha256: str
