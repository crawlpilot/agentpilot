"""The ordered transform pipeline (contract 7).

v1 applied a fixed order -- regex, replace, trim/case, coerce, default,
required-gate -- which was a good default and a bad constraint: it could not
express "cast, then default", or "split, then trim each part". v2 makes the
order explicit and the op set open.

The *semantics* underneath are v1's, deliberately. First-float extraction,
price and accent and control-character handling, the token vocabularies for
booleans -- all of it was ported from Pulsar's `Strings`/`RegexExtractor` and
is the accumulated answer to a decade of real pages. `tests/test_recipe_v2_transform.py`
carries every case from the v1 suite for exactly that reason: the rewrite is
allowed to change the shape, not to quietly lose the behaviour.

**List semantics.** Scalar ops map over the elements of a list; list ops
(`join`, `index`, `unique`, `filter_empty`, `to_object`, `to_pairs`) act on the
list itself. Ops that turn one value into several (`split`, `html_select`)
flatten when their input is already a list. This is the only implicit behaviour
in the pipeline, and it is stated here because it would otherwise be a
surprise.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urljoin

from agentpilot.recipe.v2.lua import LuaError, run_lua_transform
from agentpilot.recipe.v2.paths import PathError, resolve_path

TransformOp = Literal[
    "regex_extract", "regex_replace", "trim", "collapse_ws", "strip_control",
    "strip_accents", "case", "split", "join", "slice", "index", "unique",
    "filter_empty", "map_lookup", "template", "url_resolve", "json_parse",
    "json_path", "strip_html", "html_select", "to_object", "to_pairs",
    "cast", "default", "lua",
]

ValueType = Literal[
    "string", "text", "number", "float", "integer", "price",
    "boolean", "url", "date", "datetime", "json",
]

# First signed number with optional thousands separators / decimal:
# "$1,299.00" -> "1,299.00", "-4.5%" -> "-4.5". Mirrors Pulsar's
# Strings.getFirstFloatNumber, and v1's normalize.py.
_NUMBER_RE = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
# Zero-width and bidi marks. Amazon pads its feature bullets with these, and
# they survive every whitespace-based cleanup because they are not whitespace.
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")

_TRUE_TOKENS = frozenset({"true", "yes", "y", "1", "on", "in stock", "available", "instock"})
_FALSE_TOKENS = frozenset(
    {"false", "no", "n", "0", "off", "out of stock", "unavailable", "outofstock"}
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
)

_FLAG_MAP = {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "a": re.A}

# Ops that act on a list as a whole rather than on each element.
_LIST_OPS = frozenset({"join", "index", "unique", "filter_empty", "to_object", "to_pairs"})
# Ops that turn one value into many; flattened when the input is already a list.
_EXPANDING_OPS = frozenset({"split", "html_select"})


class TransformError(Exception):
    """A transform could not be applied. Callers turn this into a field
    failure with this message as the reason -- never a crashed run."""


@dataclass(frozen=True)
class TransformContext:
    """What a transform can see besides its input value. Deliberately small:
    the run's URL (for `url_resolve`), the caller's metadata (for `{{meta.*}}`
    templating), and identifying context for error messages."""

    url: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    field_name: str = ""
    variant: str | None = None
    raw: Any = None


@dataclass(frozen=True)
class Transform:
    """One step of the pipeline. Field names mirror the JSON schema exactly, so
    a stored recipe round-trips without a translation layer."""

    op: TransformOp
    pattern: str | None = None
    group: int = 0
    flags: str = ""
    repl: str | None = None
    count: int = 0
    mode: str | None = None
    sep: str | None = None
    limit: int = -1
    start: int | None = None
    end: int | None = None
    i: int | None = None
    table: dict[str, Any] | None = None
    default: Any = None
    format: str | None = None
    path: str | None = None
    path_lang: str = "simple"
    to: str | None = None
    value: Any = None
    source: str | None = None
    selector: str | None = None
    attribute: str = "text"
    all: bool = False
    key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"op": self.op}
        for name, default in (
            ("pattern", None), ("group", 0), ("flags", ""), ("repl", None), ("count", 0),
            ("mode", None), ("sep", None), ("limit", -1), ("start", None), ("end", None),
            ("i", None), ("table", None), ("default", None), ("format", None),
            ("path", None), ("path_lang", "simple"), ("to", None), ("value", None),
            ("source", None), ("selector", None), ("attribute", "text"), ("all", False),
            ("key", None),
        ):
            got = getattr(self, name)
            if got != default:
                out[name] = got
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Transform:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def parse_transforms(raw: list[dict[str, Any]] | None) -> list[Transform]:
    return [Transform.from_dict(item) for item in (raw or [])]


# --- the pipeline -----------------------------------------------------------


def apply_transforms(
    value: Any,
    transforms: list[Transform],
    ctx: TransformContext | None = None,
) -> Any:
    """Apply `transforms` left to right. Raises `TransformError` on the first
    op that cannot be applied."""

    context = ctx or TransformContext()
    current = value
    for step in transforms:
        current = _apply_one(current, step, context)
    return current


def _apply_one(value: Any, t: Transform, ctx: TransformContext) -> Any:
    if t.op == "default":
        return t.value if _is_empty(value) else value
    if t.op in _LIST_OPS:
        return _apply_list_op(value, t, ctx)
    if isinstance(value, list):
        mapped = [_apply_scalar(v, t, ctx) for v in value]
        if t.op in _EXPANDING_OPS:
            flat: list[Any] = []
            for item in mapped:
                flat.extend(item) if isinstance(item, list) else flat.append(item)
            return flat
        return [m for m in mapped if m is not None]
    return _apply_scalar(value, t, ctx)


def _apply_list_op(value: Any, t: Transform, ctx: TransformContext) -> Any:
    items = value if isinstance(value, list) else ([] if value is None else [value])
    if t.op == "filter_empty":
        return [v for v in items if not _is_empty(v)]
    if t.op == "unique":
        seen: list[Any] = []
        for v in items:
            if v not in seen:
                seen.append(v)
        return seen
    if t.op == "join":
        if t.sep is None:
            raise TransformError("join requires `sep`")
        return t.sep.join(_as_text(v) for v in items if not _is_empty(v))
    if t.op == "index":
        if t.i is None:
            raise TransformError("index requires `i`")
        return items[t.i] if -len(items) <= t.i < len(items) else None
    if t.op == "to_object":
        return _to_object(items, t)
    if t.op == "to_pairs":
        return _to_pairs(value, t)
    raise TransformError(f"unhandled list op {t.op!r}")


def _to_object(items: list[Any], t: Transform) -> dict[str, Any]:
    """`[{name, value}, ...] -> {name: value}` -- the near-universal
    specification shape, turned into the map a caller actually asked for.

    A later duplicate key wins, matching dict-literal semantics; a row missing
    either side is skipped rather than producing a `None` key, because a spec
    sheet with an empty-string key is worse than one with a missing row."""

    key_name = t.key or "name"
    val_name = t.value if isinstance(t.value, str) else "value"
    out: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        k = item.get(key_name)
        if k is None or (isinstance(k, str) and not k.strip()):
            continue
        v = item.get(val_name)
        if v is None:
            continue
        out[str(k).strip()] = v
    return out


def _to_pairs(value: Any, t: Transform) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        raise TransformError("to_pairs expects an object")
    key_name = t.key or "key"
    val_name = t.value if isinstance(t.value, str) else "value"
    return [{key_name: k, val_name: v} for k, v in value.items()]


def _apply_scalar(value: Any, t: Transform, ctx: TransformContext) -> Any:  # noqa: C901
    op = t.op

    if op == "lua":
        if not t.source:
            raise TransformError("lua requires `source`")
        try:
            return run_lua_transform(
                t.source,
                value,
                {
                    "url": ctx.url,
                    "meta": ctx.meta,
                    "field": ctx.field_name,
                    "variant": ctx.variant,
                    "raw": ctx.raw,
                },
            )
        except LuaError as exc:
            raise TransformError(str(exc)) from exc

    if op == "json_parse":
        if value is None:
            return None
        try:
            return json.loads(_as_text(value))
        except (TypeError, ValueError) as exc:
            raise TransformError(f"json_parse failed: {exc}") from exc

    if op == "json_path":
        if not t.path:
            raise TransformError("json_path requires `path`")
        try:
            return resolve_path(value, t.path, t.path_lang)  # type: ignore[arg-type]
        except PathError as exc:
            raise TransformError(str(exc)) from exc

    if op == "cast":
        if not t.to:
            raise TransformError("cast requires `to`")
        return _coerce(value, t.to, ctx)  # type: ignore[arg-type]

    if op == "html_select":
        return _html_select(value, t)

    if op == "strip_html":
        return _strip_html(value)

    if op == "map_lookup":
        if t.table is None:
            raise TransformError("map_lookup requires `table`")
        if value is None:
            return t.default
        return t.table.get(_as_text(value), t.default)

    if op == "template":
        if t.format is None:
            raise TransformError("template requires `format`")
        return _render_template(t.format, value, ctx)

    if op == "url_resolve":
        if _is_empty(value):
            return None
        candidate = _as_text(value).strip()
        return urljoin(ctx.url, candidate) if ctx.url else candidate

    if op == "split":
        if t.sep is None:
            raise TransformError("split requires `sep`")
        if value is None:
            return []
        return _as_text(value).split(t.sep, t.limit if t.limit is not None else -1)

    if value is None:
        return None
    text = _as_text(value)

    if op == "regex_extract":
        if not t.pattern:
            raise TransformError("regex_extract requires `pattern`")
        match = _search(t.pattern, text, t.flags)
        if match is None:
            return None
        try:
            return match.group(t.group)
        except (IndexError, re.error) as exc:
            raise TransformError(f"regex_extract group {t.group} out of range") from exc

    if op == "regex_replace":
        if not t.pattern or t.repl is None:
            raise TransformError("regex_replace requires `pattern` and `repl`")
        return _compile(t.pattern, t.flags).sub(t.repl, text, count=t.count or 0)

    if op == "trim":
        return text.strip()
    if op == "collapse_ws":
        return _WS_RE.sub(" ", text).strip()
    if op == "strip_control":
        return _INVISIBLE_RE.sub("", _CONTROL_RE.sub("", text))
    if op == "strip_accents":
        return "".join(
            c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
        )
    if op == "case":
        if t.mode == "lower":
            return text.lower()
        if t.mode == "upper":
            return text.upper()
        if t.mode == "title":
            return text.title()
        raise TransformError(f"case requires mode lower|upper|title, got {t.mode!r}")
    if op == "slice":
        return text[t.start:t.end]

    raise TransformError(f"unknown transform op {t.op!r}")


error_types = re.error


def _search(pattern: str, text: str, flags: str) -> re.Match[str] | None:
    return _compile(pattern, flags).search(text)


def _compile(pattern: str, flags: str) -> re.Pattern[str]:
    bits = 0
    for ch in flags or "":
        if ch not in _FLAG_MAP:
            raise TransformError(f"unknown regex flag {ch!r}")
        bits |= _FLAG_MAP[ch]
    try:
        return re.compile(pattern, bits)
    except re.error as exc:
        raise TransformError(f"invalid regex {pattern!r}: {exc}") from exc


def _render_template(fmt: str, value: Any, ctx: TransformContext) -> str:
    out = fmt.replace("{{v}}", _as_text(value) if value is not None else "")

    def _meta(match: re.Match[str]) -> str:
        return _as_text(ctx.meta.get(match.group(1), ""))

    return re.sub(r"\{\{meta\.([A-Za-z0-9_]+)\}\}", _meta, out)


def _coerce(value: Any, to: str, ctx: TransformContext) -> Any:  # noqa: C901
    if value is None:
        return None
    if to == "json":
        return value
    if to in ("string", "text"):
        return _as_text(value)
    if to == "boolean":
        if isinstance(value, bool):
            return value
        low = _as_text(value).strip().lower()
        if low in _TRUE_TOKENS:
            return True
        if low in _FALSE_TOKENS:
            return False
        return None

    if isinstance(value, bool):
        return None
    if to in ("number", "float", "price") and isinstance(value, (int, float)):
        return float(value)
    if to == "integer" and isinstance(value, (int, float)):
        return int(value)

    text = _as_text(value)
    if to in ("number", "float", "price"):
        num = _first_number(text)
        return float(num) if num is not None else None
    if to == "integer":
        num = _first_number(text)
        if num is None:
            return None
        try:
            return int(float(num))
        except ValueError:
            return None
    if to == "url":
        candidate = text.strip()
        if not candidate:
            return None
        return urljoin(ctx.url, candidate) if ctx.url else candidate
    if to in ("date", "datetime"):
        return _parse_date(text.strip(), to)
    raise TransformError(f"unknown cast target {to!r}")


def _first_number(text: str) -> str | None:
    match = _NUMBER_RE.search(text)
    return match.group(0).replace(",", "") if match else None


def _parse_date(text: str, to: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.isoformat() if to == "datetime" else parsed.date().isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.isoformat() if to == "datetime" else parsed.date().isoformat()
    return None


def _strip_html(value: Any) -> Any:
    if value is None:
        return None
    text = _as_text(value)
    if "<" not in text:
        return text
    root = _parse_fragment(text)
    if root is None:
        return text
    return _WS_RE.sub(" ", root.text_content()).strip()


def _html_select(value: Any, t: Transform) -> Any:
    """Parse an HTML *string* -- one that arrived as a JSON value, not as the
    page -- and select from it. Commerce JSON is full of these: Walmart's
    `idml.longDescription` is a JSON string containing `<ul><li>...</li></ul>`,
    and the caller wants the bullets as a list."""

    if not t.selector:
        raise TransformError("html_select requires `selector`")
    if value is None:
        return [] if t.all else None
    root = _parse_fragment(_as_text(value))
    if root is None:
        return [] if t.all else None
    try:
        from lxml.cssselect import CSSSelector
    except ImportError as exc:  # pragma: no cover - lxml is a crawlpilot dependency
        raise TransformError("html_select requires lxml with cssselect") from exc
    try:
        matches = CSSSelector(t.selector)(root)
    except Exception as exc:  # noqa: BLE001 - cssselect raises several types
        raise TransformError(f"invalid css selector {t.selector!r}: {exc}") from exc

    values = [_element_value(el, t.attribute) for el in matches]
    values = [v for v in values if v is not None]
    if t.all:
        return values
    return values[0] if values else None


def _element_value(el: Any, attribute: str) -> Any:
    if attribute in ("text", "visible_text"):
        return _WS_RE.sub(" ", el.text_content()).strip()
    if attribute == "html":
        from lxml import etree

        return etree.tostring(el, encoding="unicode")
    return el.get(attribute)


def _parse_fragment(text: str) -> Any:
    try:
        from lxml import html as lxml_html
    except ImportError as exc:  # pragma: no cover - lxml is a crawlpilot dependency
        raise TransformError("html transforms require lxml") from exc
    try:
        return lxml_html.fragment_fromstring(text, create_parent="div")
    except Exception:  # noqa: BLE001 - malformed markup is data, not a crash
        try:
            return lxml_html.fromstring(text)
        except Exception:  # noqa: BLE001
            return None


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False
