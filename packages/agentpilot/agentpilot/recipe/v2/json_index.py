"""What is actually in this page's JSON, and where.

Every locator the selector agent proposes against structured data is a path the
model has to write from memory of what it was shown -- and what it was shown
used to be `json.dumps(structured_data)[:12_000]`. On a Walmart product page
that blob is ~352 KB, so the model saw 3.4% of it, cut mid-structure, and the
answer to both of the questions being asked -- `idml.specifications` and
`idml.productHighlights`, each an array of `{name, value}` keyed exactly as the
caller asked -- sat far past the cut. The model was left guessing CSS at a page
that contains no `<table>` and no `<tr>`.

`rows.find_row_arrays` was written for that exact blindness and worked, but only
for `table` fields. This module generalises it into three deterministic
questions, none of which needs a model:

- `outline` -- what paths does this blob have, described densely enough that a
  16 KB budget covers a 352 KB blob.
- `find_row_arrays` -- which arrays already look like the wanted rows. Moved
  here verbatim from `rows.py`; that module still re-exports it.
- `find_value_arrays` -- which arrays already look like the wanted list. The new
  one, and the reason `highlights` could not bind: a list of strings whose
  values are one key of an array of objects needs
  `productHighlights[*].value` with `path_lang="jmespath"`, and no model writes
  that for a path it has never seen.

**Ad and telemetry subtrees are pruned from all three.** `paths.py` documents
the trap: on that same page an unanchored `$..name` matches 203 nodes, and among
the first is a *sponsored competitor's* product carried under
`contentLayout.modules[].configs.ad`. The prompts already warn about it. Not
showing it is stronger than warning about it, and costs nothing -- nobody wants
the ad.

Deterministic on purpose, all of it. "Which arrays have these keys" is a
question about the data, and running it through a model could only lose.
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass
from typing import Any

from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec

# The locator kind that addresses each container of `PageReader.structured_data`.
# `json_ld` is a list, the other two are dicts, so a json_ld path starts `[0].`.
_SOURCE_OF = {"json_ld": "json_ld", "hydration": "hydration", "meta": "metadata"}

# How deep to walk, and how many candidates to keep.
_MAX_SCAN_DEPTH = 12
_MAX_ROW_CANDIDATES = 6
_MAX_VALUE_CANDIDATES = 6
_MIN_ARRAY_ROWS = 2

# Sibling elements of an array are homogeneous, so descending into all of them
# buys nothing and costs the walk. `find_row_arrays` looks at five because it is
# sampling rows; the outline looks at two because it is describing shape.
_ROW_WALK_WIDTH = 5
_OUTLINE_WALK_WIDTH = 2

# A ceiling on the walk itself. A 352 KB blob has tens of thousands of leaves and
# the outline can only show a few hundred.
_MAX_ENTRIES = 4_000

# How much of one value to show.
_SAMPLE_CHARS = 160
_SCALAR_CHARS = 120

# How deep to keep emitting an object's scalar leaves. Past this the arrays are
# what matter and a leaf-per-key listing would crowd them out.
_MAX_LEAF_DEPTH = 6

_SCALARS = (str, int, float, bool)


# ---------------------------------------------------------------------------
# Noise
# ---------------------------------------------------------------------------

# Whole keys that are never wanted data. Matched on the normalised key, so
# `adsEnabled` and `ads_enabled` are the same thing.
_NOISE_KEYS = frozenset({
    "ad", "ads", "adunit", "adunits", "adsenabled", "adconfig", "adslot",
    "telemetry", "beacon", "beacons", "tracking", "analytics",
    # GraphQL's `__typename`, after normalisation strips the underscores.
    "typename",
    "pixel", "pixels", "impression", "impressions",
})

# Substrings that identify a noise subtree wherever they appear in a key.
# Deliberately not a bare "ad": `shadeName` and `gradeLabel` are real product
# data, and dropping them would be a worse bug than showing an advertisement.
_NOISE_SUBSTRINGS = (
    "advertis", "sponsor", "telemetry", "beacon", "analytic", "tracking",
    "adcontent", "admodule", "adsenabled", "adserver",
)


def is_noise_key(key: Any) -> bool:
    """Whether a subtree under this key is advertising or telemetry."""

    normalised = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if not normalised:
        return False
    return normalised in _NOISE_KEYS or any(s in normalised for s in _NOISE_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Affinity
# ---------------------------------------------------------------------------


def name_affinity(path: str, field_name: str) -> int:
    """How much the site's own name for an array agrees with the caller's name
    for the field.

    Not decoration -- it is the tiebreak that decides correctness. On the page
    this was written against, `idml.indications`, `idml.specifications` and
    `idml.productHighlights` are all `[{name, value}]` arrays that all verify;
    only one of them is the specification sheet. Without this the first one
    found wins, which is the plausible-but-wrong answer this module exists to
    avoid.
    """

    wanted = re.sub(r"[^a-z0-9]", "", field_name.lower())
    if not wanted:
        return 0
    last = re.sub(r"[^a-z0-9]", "", path.rsplit(".", 1)[-1].lower())
    if last == wanted:
        return 60
    if wanted in last or last in wanted:
        return 40
    if wanted in re.sub(r"[^a-z0-9]", "", path.lower()):
        return 20
    return 0


def _best_affinity(path: str, names: list[str]) -> int:
    return max((name_affinity(path, name) for name in names), default=0)


# ---------------------------------------------------------------------------
# Walking
# ---------------------------------------------------------------------------


def _walk(
    structured: dict[str, Any],
    visit: Any,
    *,
    width: int,
    max_depth: int = _MAX_SCAN_DEPTH,
) -> None:
    """Depth-first over every container, skipping noise subtrees.

    `visit(kind, path, node, depth)` is called for every list and dict reached.
    Shared by all three public functions so they agree exactly on what is in the
    blob -- an outline that shows a path `find_value_arrays` skipped would be an
    invitation to write a locator nothing can find.
    """

    budget = [_MAX_ENTRIES]

    def go(node: Any, kind: str, path: str, depth: int) -> None:
        if depth > max_depth or budget[0] <= 0:
            return
        budget[0] -= 1
        visit(kind, path, node, depth)
        if isinstance(node, list):
            for index, item in enumerate(node[:width]):
                if isinstance(item, (list, dict)):
                    go(item, kind, f"{path}[{index}]" if path else f"[{index}]", depth + 1)
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if is_noise_key(key):
                    continue
                if isinstance(value, (list, dict)):
                    go(value, kind, f"{path}.{key}" if path else str(key), depth + 1)

    for kind, container in _SOURCE_OF.items():
        node = structured.get(container)
        if isinstance(node, (list, dict)):
            go(node, kind, "", 0)


def _rows_of(node: Any) -> list[dict[str, Any]]:
    """The node as a list of row dicts, or empty if it is not one."""

    if not isinstance(node, list):
        return []
    rows = [r for r in node if isinstance(r, dict)]
    if len(rows) < _MIN_ARRAY_ROWS or len(rows) != len(node):
        return []
    return rows


def _scalars_of(node: Any) -> list[Any]:
    """The node as a list of scalar values, or empty if it is not one."""

    if not isinstance(node, list):
        return []
    values = [v for v in node if isinstance(v, _SCALARS)]
    if len(values) < _MIN_ARRAY_ROWS or len(values) != len(node):
        return []
    return values


# ---------------------------------------------------------------------------
# Rows -- moved from rows.py, behaviour unchanged apart from noise pruning
# ---------------------------------------------------------------------------


def find_row_arrays(
    structured: dict[str, Any], wanted: dict[str, TypeSpec], field_name: str = ""
) -> list[tuple[str, str, list[Any]]]:
    """Arrays in the page's own JSON that already look like the wanted rows.

    Returns `(locator_kind, path, sample)`, best first.
    """

    if not wanted:
        return []

    want = {name.lower() for name in wanted}
    found: list[tuple[int, str, str, list[Any]]] = []

    def visit(kind: str, path: str, node: Any, _depth: int) -> None:
        rows = _rows_of(node)
        if not rows:
            return
        keys = {str(k).lower() for k in rows[0]}
        hits = len(want & keys)
        if not hits:
            return
        # Every wanted column present beats a partial match, and a tight row
        # (few extra keys) beats a fat one -- a product object with `name` and
        # `value` among thirty other fields is not a specification row.
        score = hits * 100 + name_affinity(path, field_name) - min(len(keys), 50)
        found.append((score, kind, path, rows[:3]))

    _walk(structured, visit, width=_ROW_WALK_WIDTH)
    found.sort(key=lambda f: -f[0])
    return [(kind, path, sample) for _score, kind, path, sample in found[:_MAX_ROW_CANDIDATES]]


# ---------------------------------------------------------------------------
# Value arrays -- the new one
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueArray:
    """An array in the page's JSON that could be a `list` field's values."""

    kind: str
    """`json_ld`, `hydration` or `meta` -- the locator kind that addresses it."""

    path: str
    """Already in its final form: `a.b.c` for an array of scalars, or
    `a.b.c[*].value` for one key plucked out of an array of objects."""

    path_lang: str
    """`simple` for a direct array, `jmespath` for a pluck -- the projection is
    the one thing dotted traversal cannot express."""

    sample: list[Any]
    affinity: int
    """`name_affinity` of the containing array's path against the field name.
    Zero means the site's own name for this array says nothing about whether it
    is the wanted one -- see `confident_value_arrays`."""

    score: int = 0
    """Total rank. Compared only against candidates from the SAME array, where
    it decides whether one key is a better answer than its siblings or whether
    the choice between them is arbitrary."""

    root: str = ""
    """The containing array's path, without the projection. Two candidates
    sharing a root are two readings of one array."""

    def as_locator_args(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "path_lang": self.path_lang}


_TYPE_MATCH = {
    "url": lambda v: isinstance(v, str) and (v.startswith(("http://", "https://", "//", "/"))),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def _type_bonus(values: list[Any], value_type: str) -> int:
    """How well these values match the declared item type.

    A `list of url` field should prefer the array of image URLs over the array
    of colour names, even when both sit under equally plausible keys.
    """

    check = _TYPE_MATCH.get(value_type)
    if check is None:
        # `string`/`text` match anything; a non-empty string still beats a
        # number rendered as one.
        return 10 if any(isinstance(v, str) and v.strip() for v in values) else 0
    matched = sum(1 for v in values if check(v))
    return int(30 * matched / len(values)) if values else 0


def find_value_arrays(
    structured: dict[str, Any], spec: FieldSpec, field_name: str = ""
) -> list[ValueArray]:
    """Arrays in the page's own JSON that could be this `list` field.

    Two shapes, because pages use both:

    - an array of scalars, addressed directly;
    - an array of objects where one key holds the values -- Walmart's
      `productHighlights` is `[{"name": "Skin type", "value": "All"}, ...]` and
      the caller asked for a list of strings, so the answer is
      `productHighlights[*].value`.

    Ranked by `name_affinity` first and decisively: the type and shape bonuses
    only order arrays the site names equally well, because a well-typed array
    under the wrong key is exactly the plausible-but-wrong answer to avoid.
    """

    if spec.type.kind != "list":
        return []
    value_type = spec.type.items.value_type if spec.type.items else "string"
    name = field_name or spec.name
    found: list[tuple[int, ValueArray]] = []

    def visit(kind: str, path: str, node: Any, _depth: int) -> None:
        if not path:
            return
        affinity = name_affinity(path, name)

        direct = _scalars_of(node)
        if direct:
            score = affinity * 100 + _type_bonus(direct, value_type) + 20
            found.append((score, ValueArray(
                kind=kind, path=path, path_lang="simple",
                sample=direct[:3], affinity=affinity, score=score, root=path,
            )))
            return

        rows = _rows_of(node)
        if not rows:
            return
        for key in rows[0]:
            if is_noise_key(key):
                continue
            values = [r.get(key) for r in rows]
            present = [v for v in values if isinstance(v, _SCALARS)]
            # A key missing from half the rows is not the column of values --
            # the same fill rule `rows._problems_with` applies to a column.
            if len(present) < max(_MIN_ARRAY_ROWS, len(rows) // 2):
                continue
            # The key's own name matters far less than the array's: `value` is
            # the right key under `productHighlights` and says nothing on its
            # own, which is why it is weighted a tenth of the path's affinity.
            score = (
                affinity * 100
                + _type_bonus(present, value_type)
                + name_affinity(str(key), name) // 10
            )
            found.append((score, ValueArray(
                kind=kind, path=f"{path}[*].{key}", path_lang="jmespath",
                sample=present[:3], affinity=affinity, score=score, root=path,
            )))

    _walk(structured, visit, width=_ROW_WALK_WIDTH)
    found.sort(key=lambda f: -f[0])

    # At most two readings per array. One is the usual case; two exist because
    # an array of `{name, value}` is genuinely ambiguous -- for a field called
    # `highlights`, "Skin type" and "All" are each half of the same fact, and
    # nothing in the data says which half was wanted. Showing both is how that
    # question reaches something that can answer it. A third is noise.
    per_root: dict[str, int] = {}
    out: list[ValueArray] = []
    for _score, candidate in found:
        seen = per_root.get(candidate.root, 0)
        if seen >= 2:
            continue
        per_root[candidate.root] = seen + 1
        out.append(candidate)
        if len(out) >= _MAX_VALUE_CANDIDATES:
            break
    return out


# How much one reading of an array must beat its sibling by to be called the
# answer rather than a guess between two. `_type_bonus` spans 0-30, so this is
# "the values are meaningfully more like the declared type", and a tie on a
# `{name, value}` array lands well under it.
_DECISIVE_MARGIN = 15


def confident_value_arrays(
    structured: dict[str, Any], spec: FieldSpec, field_name: str = ""
) -> list[ValueArray]:
    """The candidates good enough to bind without asking a model.

    Two gates, and both are load-bearing:

    **The site's own naming has to agree.** `verify_locators` checks that a
    locator reads something and that the result survives its transform, which a
    list field passes on any non-empty array anywhere in the blob -- so for this
    path verification is not a sufficient gate on its own, and affinity is what
    keeps a well-shaped array under an unrelated key from binding silently.

    **The choice within the array has to be decisive.** `productHighlights` is
    `[{name, value}]`, and for a field called `highlights` the two keys score
    identically: "Skin type" and "All" are each half of one fact and the data
    does not say which half was asked for. Binding the winner of a tie is a coin
    flip that looks like a measurement. Those go to the model instead, which has
    the field's description and the rendered page to judge with.

    Everything rejected here still reaches the model as a hint, so the
    invisibility this module exists to fix is fixed either way -- what differs
    is only whether a model is asked.
    """

    candidates = find_value_arrays(structured, spec, field_name)
    runner_up: dict[str, int] = {}
    for candidate in candidates:
        if candidate.root in runner_up:
            runner_up[candidate.root] = max(runner_up[candidate.root], candidate.score)
        else:
            runner_up[candidate.root] = -1

    out: list[ValueArray] = []
    for candidate in candidates:
        if candidate.affinity <= 0:
            continue
        rival = runner_up.get(candidate.root, -1)
        if rival >= 0 and candidate.score - rival < _DECISIVE_MARGIN:
            continue
        out.append(candidate)
    return out


# ---------------------------------------------------------------------------
# Outline
# ---------------------------------------------------------------------------


def _brief(value: Any, limit: int) -> str:
    try:
        text = _json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:limit] + "..." if len(text) > limit else text


def _describe(node: Any) -> str | None:
    """One line describing this array, or None when the node is only a waypoint.

    Objects are waypoints: their keys show up as the paths of whatever is under
    them, so a line per object would double the outline to say nothing. Their
    scalar leaves are emitted separately, each with its own path -- that is how
    a `title` or a `price` binds, and a leaf bundled into its parent's line
    would not tell the model how to address it.
    """

    rows = _rows_of(node)
    if rows:
        keys = ", ".join(list(rows[0])[:12])
        return f"array[{len(node)}] of {{{keys}}}  e.g. {_brief(rows[0], _SAMPLE_CHARS)}"

    scalars = _scalars_of(node)
    if scalars:
        kinds = {type(v).__name__ for v in scalars}
        return (
            f"array[{len(node)}] of {'/'.join(sorted(kinds))}  "
            f"e.g. {_brief(scalars[:3], _SAMPLE_CHARS)}"
        )

    if isinstance(node, list) and node:
        return f"array[{len(node)}] (mixed)"
    return None


def outline(
    structured: dict[str, Any],
    *,
    wanted: dict[str, FieldSpec] | None = None,
    max_chars: int = 16_000,
) -> str:
    """The blob's shape, dense enough to fit a 352 KB page in a prompt.

    Every line is `kind=<k> path='<p>'` followed by what is there, and the path
    is in the dialect `paths.resolve_path` speaks and rooted at the container --
    so a line can be copied straight into a locator. That is the whole point:
    the model's job is to choose among paths that exist, not to remember one out
    of a truncated dump.

    Ordering is by `name_affinity` against the declared field names, then by
    depth. Under a budget the interesting paths have to come first, and the
    caller's own words for what they want are the only signal available about
    which those are.
    """

    names = [spec.name for spec in (wanted or {}).values()]
    entries: list[tuple[int, int, str, str]] = []

    def add(kind: str, path: str, depth: int, described: str) -> None:
        head = f"kind={kind} path={path!r}"
        entries.append((_best_affinity(path, names), depth, head, described))

    def visit(kind: str, path: str, node: Any, depth: int) -> None:
        described = _describe(node)
        if described is not None and path:
            add(kind, path, depth, described)
        # Scalar leaves, each with its own addressable path. The root container
        # is reached with `path == ""`, so this is also what makes a top-level
        # `hydration.someKey` visible at all -- the walk itself only descends
        # into containers.
        if isinstance(node, dict) and depth <= _MAX_LEAF_DEPTH:
            for key, value in node.items():
                if is_noise_key(key) or not isinstance(value, _SCALARS):
                    continue
                if value == "" or value is None:
                    continue
                leaf = f"{path}.{key}" if path else str(key)
                add(kind, leaf, depth + 1, _brief(value, _SCALAR_CHARS))

    _walk(structured, visit, width=_OUTLINE_WALK_WIDTH)
    if not entries:
        return ""

    entries.sort(key=lambda e: (-e[0], e[1], e[2]))

    lines: list[str] = []
    used = 0
    dropped = 0
    elided = False
    for _affinity, _depth, head, described in entries:
        line = f"{head}\n    {described}"
        if used + len(line) + 1 > max_chars:
            dropped += 1
            continue
        elided = elided or described.endswith("...")
        lines.append(line)
        used += len(line) + 1
    # Say so when anything was left out, in both senses -- a path that did not
    # fit, and a value shown only in part. A model told nothing was elided will
    # conclude a key is absent when it was merely off the end, which is the
    # failure the raw-prefix version of this produced on every large page.
    if dropped:
        lines.append(
            f"... {dropped} more paths TRUNCATED -- deeper keys exist than are shown here"
        )
    elif elided:
        lines.append("... some values above are TRUNCATED; their paths are complete")
    return "\n".join(lines)
