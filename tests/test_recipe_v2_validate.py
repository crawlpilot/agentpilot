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


# --- a group that opens something must be able to close it -------------------


def _group_with(steps: list[dict], teardown: list[dict] | None = None):
    return doc(
        fields={"care": {"type": {"kind": "scalar"}}},
        field_groups=[{
            "group_id": "care",
            "field_names": ["care"],
            "bindings": {"care": [{"locator": {"kind": "css", "selector": ".care"}}]},
            "steps": steps,
            **({"teardown": teardown} if teardown is not None else {}),
        }],
    )


def test_a_group_that_opens_something_and_cannot_close_it_warns() -> None:
    """Replay loads the page once and runs every group against it, so state one
    group leaves is state the next inherits -- an open drawer over the next
    group's button is the failure reloading between groups used to hide. This is
    where an author finds out, rather than from a later group reading nothing.
    """

    errors, warnings = validate_document(
        _group_with([{"op": "click", "target": {"kind": "css", "selector": "#open"}}])
    )
    assert errors == []
    assert any("no teardown" in w for w in warnings)


def test_a_group_that_closes_what_it_opened_is_quiet() -> None:
    errors, warnings = validate_document(
        _group_with(
            [{"op": "click", "target": {"kind": "css", "selector": "#open"}}],
            [{"op": "click", "target": {"kind": "css", "selector": "#close"}}],
        )
    )
    assert errors == []
    assert not any("teardown" in w for w in warnings)


def test_a_reveal_that_changes_nothing_worth_undoing_is_quiet() -> None:
    """Narrower than `REVEALING_OPS` on purpose: a scroll leaves nothing for a
    later group to trip over, and warning about it would train authors to ignore
    the warning."""

    errors, warnings = validate_document(
        _group_with([{"op": "scroll", "args": {"direction": "down"}}])
    )
    assert errors == []
    assert not any("teardown" in w for w in warnings)


# --- the never-verified chain, mirroring lint.ts ----------------------------


def _chain(*verified: int):
    return doc(
        status="approved",
        field_groups=[{
            "group_id": "core",
            "field_names": ["title"],
            "bindings": {"title": [
                {"locator": {"kind": "css", "selector": f".t{i}"}, "verified_on": v}
                for i, v in enumerate(verified)
            ]},
        }],
    )


def _unverified(document) -> list[str]:
    _errors, warnings = validate_document(document)
    return [w for w in warnings if "has ever resolved" in w]


def test_a_healthy_primary_carrying_a_field_is_not_warned_about() -> None:
    """`verified_on` counts pages a candidate actually produced the value on, so
    a fallback that was never reached reads 0 -- correctly. Asking per candidate
    would warn about almost every fallback in every approved recipe."""

    assert _unverified(_chain(2, 0)) == []


def test_a_chain_nothing_has_ever_resolved_is_warned_about_once() -> None:
    assert len(_unverified(_chain(0, 0))) == 1


def test_a_draft_has_not_been_reviewed_yet_so_says_nothing() -> None:
    document = _chain(0, 0)
    document["status"] = "draft"
    assert _unverified(document) == []


def test_a_document_with_no_status_is_treated_as_a_draft() -> None:
    """The studio omits `status` on a new document, and a brand-new draft has by
    definition never been replayed against anything."""

    document = _chain(0, 0)
    del document["status"]
    assert _unverified(document) == []
