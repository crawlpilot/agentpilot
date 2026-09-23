"""Server-side validation of a hand-authored v2 document.

The rules here must match `frontend/src/lib/recipe/lint.ts`. Two validators
that disagree give an author a green studio and a red API with no way to tell
which is right, so the cases that pin the shared rules are the important ones.
"""

from __future__ import annotations

from agentpilot.recipe.v2.validate import validate_document


def doc(**kw):
    base = {
        "name": "n",
        "sample_urls": ["https://e.com/1", "https://e.com/2", "https://e.com/3"],
        "target": {"match": [{"kind": "glob", "pattern": "https://e.com/*"}]},
        "fields": {"title": {"type": {"kind": "scalar", "value_type": "string"}}},
        "field_groups": [{
            "group_id": "core",
            "field_names": ["title"],
            "bindings": {"title": [{"locator": {"kind": "css", "selector": ".t"}}]},
        }],
    }
    base.update(kw)
    return base


def test_a_well_formed_document_has_no_errors() -> None:
    errors, warnings = validate_document(doc())
    assert errors == []
    assert warnings == []


def test_a_table_binds_by_column_not_by_field_name() -> None:
    """The rule `lint.ts` shipped wrong, pinned on both sides.

    A table's `field_names` carries the field; `bindings` is keyed by the
    column names in `type.columns`. Looking for `bindings[field]` reports a
    correctly-bound table as unresolvable -- which is what happened to the
    shipped Zara example.
    """

    errors, _ = validate_document(doc(
        fields={"items": {"type": {"kind": "table", "columns": {
            "title": {"kind": "scalar", "value_type": "string"},
            "price": {"kind": "scalar", "value_type": "price"},
        }}}},
        field_groups=[{
            "group_id": "core",
            "field_names": ["items"],
            "bindings": {
                "title": [{"locator": {"kind": "css", "selector": ".t"}}],
                "price": [{"locator": {"kind": "css", "selector": ".p"}}],
            },
            "repeat": {
                "kind": "dom_rows", "row_field": "items", "max_iterations": 100,
                "rows_locator": {"kind": "css", "selector": "li.card"},
            },
        }],
    ))
    assert errors == []


def test_a_column_with_no_candidates_is_named() -> None:
    errors, _ = validate_document(doc(
        fields={"items": {"type": {"kind": "table", "columns": {
            "title": {"kind": "scalar", "value_type": "string"},
            "price": {"kind": "scalar", "value_type": "price"},
        }}}},
        field_groups=[{
            "group_id": "core",
            "field_names": ["items"],
            "bindings": {"title": [{"locator": {"kind": "css", "selector": ".t"}}]},
            "repeat": {
                "kind": "dom_rows", "row_field": "items", "max_iterations": 100,
                "rows_locator": {"kind": "css", "selector": "li"},
            },
        }],
    ))
    assert any("price" in e for e in errors)


def test_every_repeat_kind_needs_its_own_locator() -> None:
    for kind, needed in [("dom", "option_locator"), ("json", "rows_locator"),
                         ("dom_rows", "rows_locator")]:
        errors, _ = validate_document(doc(
            field_groups=[{
                "group_id": "core",
                "field_names": ["title"],
                "bindings": {"title": [{"locator": {"kind": "css", "selector": ".t"}}]},
                "repeat": {"kind": kind, "row_field": "title", "max_iterations": 10},
            }],
        ))
        assert any(needed in e for e in errors), kind


def test_an_unknown_repeat_kind_is_rejected() -> None:
    errors, _ = validate_document(doc(
        field_groups=[{
            "group_id": "core", "field_names": ["title"],
            "bindings": {"title": [{"locator": {"kind": "css", "selector": ".t"}}]},
            "repeat": {"kind": "dom_pages", "row_field": "title", "max_iterations": 10},
        }],
    ))
    assert any("unknown kind" in e for e in errors)


def test_a_candidate_scoped_to_an_undeclared_variant_is_dead() -> None:
    errors, _ = validate_document(doc(
        field_groups=[{
            "group_id": "core", "field_names": ["title"],
            "bindings": {"title": [
                {"locator": {"kind": "css", "selector": ".t"}, "variant_id": "ghost"}
            ]},
        }],
    ))
    assert any("ghost" in e for e in errors)


def test_a_field_no_group_collects_is_a_warning_not_an_error() -> None:
    # Authorable and sometimes intentional mid-edit; not a reason to refuse.
    errors, warnings = validate_document(doc(
        fields={
            "title": {"type": {"kind": "scalar", "value_type": "string"}},
            "orphan": {"type": {"kind": "scalar", "value_type": "string"}},
        },
    ))
    assert errors == []
    assert any("orphan" in w for w in warnings)


def test_sample_urls_are_not_a_constraint_on_the_recipe() -> None:
    """A recipe is applied to the URLs a caller submits, not to the ones it was
    built on -- so neither having few of them nor having none says anything
    about whether the recipe is valid. This used to warn below three, which
    made scaffolding look like a defect in the artefact."""

    for urls in ([], ["https://e.com/1"]):
        errors, warnings = validate_document(doc(sample_urls=urls))
        assert errors == []
        assert not any("sample_urls" in w for w in warnings)


def test_the_obvious_refusals() -> None:
    assert any("name" in e for e in validate_document(doc(name=" "))[0])
    assert any("fields" in e for e in validate_document(doc(fields={}))[0])
    assert any("field_groups" in e for e in validate_document(doc(field_groups=[]))[0])
    assert validate_document("not a document")[0]  # type: ignore[arg-type]


def test_a_group_collecting_an_undeclared_field_is_refused() -> None:
    errors, _ = validate_document(doc(
        field_groups=[{
            "group_id": "core", "field_names": ["ghost"],
            "bindings": {"ghost": [{"locator": {"kind": "css", "selector": ".g"}}]},
        }],
    ))
    assert any("does not declare" in e for e in errors)


def _care(selector: str, **extra):
    """A document whose one field is bound to `selector`."""

    locator = {"kind": "xpath", "selector": selector, "all": True, **extra}
    return doc(
        fields={"care": {"type": {"kind": "list", "items": {"kind": "scalar"}}}},
        field_groups=[{
            "group_id": "care",
            "field_names": ["care"],
            "bindings": {"care": [{"locator": locator}]},
        }],
    )


def test_an_xpath_that_cannot_be_contained_is_an_error() -> None:
    """The agent is held to this at proposal time. A hand-authored or
    hand-edited recipe reaches replay without passing through any of that, so
    the rule is stated once more here -- an expression on a document-order axis
    makes the `within` beside it a comment rather than a constraint.

    The selector is the one a real Zara build froze.
    """

    errors, _warnings = validate_document(
        _care("//*[contains(translate(text(),'CARE','care'),'care')]/following::ul[1]/li")
    )
    assert any("following::" in e for e in errors)


def test_a_relative_xpath_passes() -> None:
    errors, _warnings = validate_document(_care(".//ul[@class='care-list']/li"))
    assert errors == []


def test_an_absolute_xpath_beside_a_within_is_an_error() -> None:
    """The scope would be recorded and then ignored: `document.evaluate` honours
    a context node only for a relative expression."""

    errors, _warnings = validate_document(
        _care("//li", within={"kind": "css", "selector": "#specs"})
    )
    assert any("absolute expression" in e for e in errors)


def test_a_partial_case_fold_is_a_warning_not_an_error() -> None:
    """A short `translate()` can be deliberate, so it does not block a save --
    unlike an escaping axis, which is never salvageable."""

    errors, warnings = validate_document(
        _care(".//*[contains(translate(.,'CARE','care'),'care')]")
    )
    assert errors == []
    assert any("only those 4 letters" in w for w in warnings)
