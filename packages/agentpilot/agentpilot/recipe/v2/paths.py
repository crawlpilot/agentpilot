"""Addressing into the structured-data containers (contract 2, and
docs/recipe-json-extraction.md for why the dialects are these two).

`simple` is v1's dotted/bracket traversal, kept as the default because most
paths are `[0].offers.price` and a path a reviewer can read at a glance is
worth more than one that can express anything.

`jmespath` is opt-in per locator, for the pages where the data is a haystack:
Walmart's product page ships a 352 KB `__NEXT_DATA__` blob whose specification
rows are an unordered array of `{name, value}`, and picking one by its `name`
is not something dotted traversal can do.

**Neither dialect offers recursive descent, and that is the load-bearing
decision.** Measured on that same Walmart page, an unanchored `$..name` matches
203 nodes, and among the first distinct strings it returns is a *sponsored
competitor's* product name, carried in the page's own JSON under
`contentLayout.modules[1].configs.ad`. An extraction language whose shortest
expression silently scrapes the wrong brand is the wrong default, so descent is
simply not offered -- an anchored path is the only kind either dialect can say.
"""

from __future__ import annotations

import re
from typing import Any, Literal

PathLang = Literal["simple", "jmespath"]

_TOKEN_RE = re.compile(r"[^.\[\]]+")


class PathError(Exception):
    """A path could not be compiled. A path that compiles but matches nothing
    is NOT an error -- that is an empty field, which is a normal outcome the
    candidate list exists to handle."""


def resolve_path(data: Any, path: str, lang: PathLang = "simple") -> Any:
    """Resolve `path` against `data`, returning `None` when it matches nothing.

    Raises `PathError` only for a malformed expression, which is an authoring
    bug worth surfacing rather than a page that happens not to have the value.
    """

    if not path:
        return data
    if lang == "jmespath":
        return _resolve_jmespath(data, path)
    return _resolve_simple(data, path)


def _resolve_simple(data: Any, path: str) -> Any:
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


_JMESPATH_CACHE: dict[str, Any] = {}


def _resolve_jmespath(data: Any, path: str) -> Any:
    try:
        import jmespath
        from jmespath.exceptions import JMESPathError
    except ImportError as exc:  # pragma: no cover - jmespath is a direct dependency
        raise PathError("jmespath paths require the `jmespath` package") from exc

    compiled = _JMESPATH_CACHE.get(path)
    if compiled is None:
        try:
            compiled = jmespath.compile(path)
        except JMESPathError as exc:
            raise PathError(f"invalid jmespath expression {path!r}: {exc}") from exc
        _JMESPATH_CACHE[path] = compiled
    try:
        return compiled.search(data)
    except JMESPathError as exc:
        raise PathError(f"jmespath evaluation failed for {path!r}: {exc}") from exc
