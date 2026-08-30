"""Stage 2: generalizes a single representative option (found during
exploration, e.g. clicking size "M") into a `RepeatSpec` matching the whole
option set (S/M/L/XL) -- a structural heuristic, no LLM call.

The generalization descriptor is accessible role + name, not a DOM selector,
so "the same option set" is re-identified at replay time by the fixed name
set rather than a container selector. At generalization time (this module),
find the clicked option's direct siblings sharing its role under its
immediate parent in *this* snapshot, and freeze their exact accessible names
into `Locator.name_in`. `evaluate.py`/`replay.py` then match that fixed name
set against the whole tree at replay time. A same-named element unrelated to
the option set elsewhere on the page is a known, accepted v1 false-positive
risk.
"""

from __future__ import annotations

from agentpilot.recipe.models import Locator, RepeatSpec
from agentpilot.recipe.tree import find_matches, find_node, find_parent
from agentpilot.spi.dom_tree import EnhancedDOMTreeNode

_MIN_SIBLINGS_TO_GENERALIZE = 2


def generalize_option_locator(
    *,
    snapshot: EnhancedDOMTreeNode,
    clicked_ref: str,
    array_field: str,
    max_iterations: int,
) -> RepeatSpec | None:
    """Returns `None` when generalization isn't possible (no resolvable
    parent, or fewer than 2 same-role siblings found) -- the caller
    (`build.py`) falls back to a single-iteration `RepeatSpec` covering just
    the one option already found, rather than failing the whole build."""

    clicked = find_node(snapshot, clicked_ref)
    if clicked is None or not clicked.ax_name:
        return None
    parent = find_parent(snapshot, clicked_ref)
    if parent is None:
        return None

    siblings = [c for c in parent.children if c.ax_role == clicked.ax_role and c.ax_name]
    if len(siblings) < _MIN_SIBLINGS_TO_GENERALIZE:
        return None

    names = [s.ax_name for s in siblings]
    locator = Locator(source="ax_role", role=clicked.ax_role, name_in=names)

    # Mechanical verify: does this name set actually resolve back to (at
    # least) the same sibling count against THIS snapshot? Guards against a
    # role that turns out to be too generic (e.g. duplicated names elsewhere
    # under a different, unrelated parent still count towards this, but
    # under-matching here would mean the locator is somehow broken).
    matched = find_matches(snapshot, clicked.ax_role, name_in=names)
    if len(matched) < len(siblings):
        return None

    return RepeatSpec(
        option_locator=locator, max_iterations=max_iterations, array_field=array_field
    )


def single_option_fallback(
    *, clicked_ref: str, snapshot: EnhancedDOMTreeNode, array_field: str
) -> RepeatSpec | None:
    """When generalization fails, degrade to a `RepeatSpec` covering just the
    one option already found (max_iterations=1) rather than dropping the
    array field entirely -- a partial result the caller/heal cycle can
    improve on later."""

    clicked = find_node(snapshot, clicked_ref)
    if clicked is None or not clicked.ax_name:
        return None
    locator = Locator(source="ax_role", role=clicked.ax_role, name_in=[clicked.ax_name])
    return RepeatSpec(option_locator=locator, max_iterations=1, array_field=array_field)
