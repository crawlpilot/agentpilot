"""Structural validation for a hand-authored v2 document.

This is the server-side twin of `frontend/src/lib/recipe/lint.ts`, and it
exists because a save endpoint that accepts anything is not a contract. It is
deliberately the *same rules*, phrased the same way: two validators that
disagree about what is valid are worse than one, because an author gets a green
studio and a red API and no way to tell which is wrong.

It checks structure only -- no browser, no page. "Does this selector resolve?"
is what `dry-run` and the studio's preview answer; this answers "is this a
recipe at all?".

Errors block the save. Warnings are returned to the caller and do not.
"""

from __future__ import annotations

from typing import Any

def validate_document(doc: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return `(errors, warnings)` for a v2 document."""

    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(doc, dict):
        return ["recipe must be an object"], []

    if not str(doc.get("name") or "").strip():
        errors.append("name: the recipe needs a name")

    fields = doc.get("fields")
    if not isinstance(fields, dict) or not fields:
        errors.append("fields: no fields declared")
        fields = {}

    groups = doc.get("field_groups")
    if not isinstance(groups, list) or not groups:
        errors.append("field_groups: a recipe needs at least one group")
        groups = []

    # `sample_urls` is not checked. It used to warn below three, on the
    # reasoning that a recipe fitted to one page is a guess -- true of
    # authoring, and wrong as a property of the saved artefact. A recipe is
    # chosen by a caller and applied to the URLs *they* submit; the page it was
    # built against constrains nothing. The field remains as optional
    # provenance. Mirrors the same decision in `frontend/src/lib/recipe/lint.ts`.

    if not (doc.get("target") or {}).get("match"):
        warnings.append("target.match: empty, so this recipe accepts any URL")

    variant_ids = {v.get("variant_id") for v in (doc.get("variants") or [])}
    seen_groups: set[str] = set()
    bound: set[str] = set()

    for group in groups:
        if not isinstance(group, dict):
            errors.append("field_groups: each group must be an object")
            continue
        group_id = str(group.get("group_id") or "")
        where = f"field_groups.{group_id}"
        if group_id in seen_groups:
            errors.append(f"{where}: duplicate group_id")
        seen_groups.add(group_id)

        bindings = group.get("bindings") or {}
        for name in group.get("field_names") or []:
            bound.add(name)
            spec = fields.get(name)
            if spec is None:
                errors.append(
                    f"{where}.{name}: group collects a field the schema does not declare"
                )
                continue

            # A `table` field is bound one COLUMN at a time: `field_names`
            # carries the field, `bindings` is keyed by the column names in
            # `type.columns`. Looking for `bindings[name]` on a table finds
            # nothing and reports a correctly-bound table as unresolvable --
            # which is exactly the bug `lint.ts` shipped with.
            type_spec = spec.get("type") or {}
            if type_spec.get("kind") == "table":
                keys = list((type_spec.get("columns") or {}).keys())
                if not keys:
                    errors.append(f"{where}.{name}: declared as a table but has no columns")
                    continue
            else:
                keys = [name]

            unbound = [k for k in keys if not bindings.get(k)]
            if len(unbound) == len(keys):
                errors.append(
                    f"{where}.{name}: no candidates bound -- this field can never resolve"
                )
                continue
            for key in unbound:
                errors.append(f'{where}.{name}.{key}: column "{key}" has no candidates bound')

            for key in keys:
                for index, candidate in enumerate(bindings.get(key) or []):
                    at = f"{key} -> candidate {index + 1}"
                    locator = candidate.get("locator")
                    if not isinstance(locator, dict) or not locator.get("kind"):
                        errors.append(f"{at}: candidate has no locator")
                        continue
                    variant = candidate.get("variant_id")
                    if variant and variant not in variant_ids:
                        errors.append(
                            f'{at}: scoped to variant "{variant}", which no variant declares'
                        )

        repeat = group.get("repeat")
        if repeat:
            row_field = repeat.get("row_field")
            if row_field not in (group.get("field_names") or []):
                errors.append(
                    f'{where}.repeat: row_field "{row_field}" is not collected by this group'
                )
            kind = repeat.get("kind")
            if kind == "dom":
                if not repeat.get("option_locator"):
                    errors.append(f"{where}.repeat: a dom repeat needs an option_locator")
            elif kind in ("json", "dom_rows"):
                if not repeat.get("rows_locator"):
                    errors.append(f"{where}.repeat: a {kind} repeat needs a rows_locator")
            else:
                errors.append(f"{where}.repeat: unknown kind {kind!r}")

    for name in fields:
        if name not in bound:
            warnings.append(f"fields.{name}: declared but no group collects it")

    return errors, warnings
