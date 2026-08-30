"""Declarative value normalization for recipe fields -- turns a raw resolved
value (text/href/attribute) into the caller's requested shape via the
directives on `schema.FieldNormalization`. Pure and stdlib-only (no new deps).

Structural port of Pulsar's `select -> regex/number coerce -> default`
composition (`str_first_float(dom_first_text(...), 0.0)`), reusing the same
concrete techniques as its `Strings.java` (first-number/price extraction,
control-char/whitespace cleanup, accent stripping) and `RegexExtractor.re1`
(regex-group extract with default). Directives apply in a fixed order:

    regex-extract (group) -> replace -> trim/collapse_ws/case/strip_accents
    -> type-coerce -> default -> required-gate

`normalize_value` returns `(value, ok)`: `ok=False` means a `required` field
came out empty (a quality-gate failure, mirroring Pulsar's `isQualified`),
which callers surface as a field failure.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import urljoin

from agentpilot.recipe.schema import FieldNormalization

# First signed number with optional thousands separators / decimal, e.g.
# "$1,299.00" -> "1,299.00", "-4.5%" -> "-4.5". Mirrors Strings.getFirstFloatNumber.
_NUMBER_RE = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")

_TRUE_TOKENS = frozenset({"true", "yes", "y", "1", "on", "in stock", "available"})
_FALSE_TOKENS = frozenset({"false", "no", "n", "0", "off", "out of stock", "unavailable"})

# A few common date layouts, tried in order; best-effort, stdlib-only (no
# dateutil dependency). ISO forms are handled first via datetime.fromisoformat.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _first_number(text: str) -> str | None:
    match = _NUMBER_RE.search(text)
    return match.group(0).replace(",", "") if match else None


def _coerce(text: str, spec: FieldNormalization, *, base_url: str | None) -> Any:
    """Coerce a cleaned string to `spec.value_type`. Returns `None` when the
    text can't represent that type (caller then falls back to `default`)."""

    vt = spec.value_type
    if vt == "string":
        return text
    if vt in ("number", "price"):
        num = _first_number(text)
        return float(num) if num is not None else None
    if vt == "integer":
        num = _first_number(text)
        if num is None:
            return None
        try:
            return int(float(num))
        except ValueError:
            return None
    if vt == "boolean":
        low = text.strip().lower()
        if low in _TRUE_TOKENS:
            return True
        if low in _FALSE_TOKENS:
            return False
        return None
    if vt == "url":
        candidate = text.strip()
        if not candidate:
            return None
        return urljoin(base_url, candidate) if base_url else candidate
    if vt in ("date", "datetime"):
        return _parse_date(text.strip(), vt)
    return text


def _parse_date(text: str, vt: str) -> str | None:
    from datetime import datetime

    iso_candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        return parsed.isoformat() if vt == "datetime" else parsed.date().isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.isoformat() if vt == "datetime" else parsed.date().isoformat()
    return None


def normalize_value(
    raw: Any, spec: FieldNormalization, *, base_url: str | None = None
) -> tuple[Any, bool]:
    """Apply `spec` to `raw`. `base_url` is used only to absolutize a `url`
    value_type. Returns `(value, ok)` where `ok=False` iff `spec.required` and
    the normalized value is empty/None."""

    if raw is None:
        return _finish(spec.default, spec)

    text = raw if isinstance(raw, str) else str(raw)

    if spec.regex:
        try:
            match = re.search(spec.regex, text)
        except re.error:  # invalid user-supplied pattern -> no extraction
            match = None
        if match is None:
            return _finish(spec.default, spec)
        try:
            text = match.group(spec.regex_group) or ""
        except IndexError:  # group index out of range -> treat as no match
            return _finish(spec.default, spec)

    for pattern, replacement in spec.replace:
        try:
            text = re.sub(pattern, replacement, text)
        except re.error:  # invalid replace rule -> leave text unchanged
            continue

    text = _CONTROL_RE.sub("", text)
    if spec.collapse_ws:
        text = _WS_RE.sub(" ", text)
    if spec.trim:
        text = text.strip()
    if spec.strip_accents:
        text = _strip_accents(text)
    if spec.case == "lower":
        text = text.lower()
    elif spec.case == "upper":
        text = text.upper()

    coerced = _coerce(text, spec, base_url=base_url)
    if coerced is None or (isinstance(coerced, str) and not coerced):
        return _finish(spec.default, spec)
    return coerced, True


def _finish(value: Any, spec: FieldNormalization) -> tuple[Any, bool]:
    """Terminal step: a `required` field with an empty/None value fails the
    quality gate; otherwise the (possibly-default) value passes."""

    empty = value is None or (isinstance(value, str) and not value.strip())
    if empty and spec.required:
        return value, False
    return value, True
