"""A minimal path evaluator for `FieldLocator.path` against
`extract_structured_data()`'s output (`json_ld` is a list, `hydration`/`meta`
are dicts) -- not a full JSONPath implementation, just dict-key/list-index
traversal, which is all a `FieldLocator` needs.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN_RE = re.compile(r"[^.\[\]]+")


def resolve_path(data: Any, path: str) -> Any:
    if not path:
        return data
    current = data
    for tok in _TOKEN_RE.findall(path):
        if current is None:
            return None
        if isinstance(current, list):
            if not re.fullmatch(r"-?\d+", tok):
                return None
            idx = int(tok)
            current = current[idx] if -len(current) <= idx < len(current) else None
        elif isinstance(current, dict):
            current = current.get(tok)
        else:
            return None
    return current
