"""The Lua escape hatch and its sandbox.

Recipe Lua is authored by a model reading an untrusted page, so these are not
hygiene tests -- they are the security boundary. Every failure mode must be a
`LuaError` (which the pipeline turns into a field failure), never a crash and
never a successful escape.
"""

from __future__ import annotations

import pytest

from agentpilot.recipe.v2.lua import LuaError, LuaLimits, lua_available, run_lua_transform
from agentpilot.recipe.v2.transform import (
    Transform,
    TransformContext,
    TransformError,
    apply_transforms,
)

pytestmark = pytest.mark.skipif(not lua_available(), reason="requires the recipe-lua extra (lupa)")


# --- it does the job --------------------------------------------------------


def test_parses_a_dimension_string_into_typed_components() -> None:
    """The real Amazon case: '3.15 x 3.54 x 1.18 inches' -> four typed fields.
    Three numbers and a unit cannot be produced by an ordered list of scalar
    ops without inventing a dozen of them."""

    src = (
        "local l, w, h, u = v:match('([%d%.]+)%s*x%s*([%d%.]+)%s*x%s*([%d%.]+)%s*(%a+)')\n"
        "if not l then return nil end\n"
        "return { length = tonumber(l), width = tonumber(w), height = tonumber(h), unit = u }"
    )
    assert run_lua_transform(src, "3.15 x 3.54 x 1.18 inches") == {
        "length": 3.15, "width": 3.54, "height": 1.18, "unit": "inches",
    }


def test_returns_a_list_for_a_contiguous_integer_keyed_table() -> None:
    assert run_lua_transform("return {10, 20, 30}", None) == [10, 20, 30]


def test_returns_a_dict_for_a_string_keyed_table() -> None:
    assert run_lua_transform("return {a = 1, b = 2}", None) == {"a": 1, "b": 2}


def test_empty_table_is_a_list() -> None:
    assert run_lua_transform("return {}", None) == []


def test_receives_the_value_and_context() -> None:
    src = "return v .. '|' .. ctx.url .. '|' .. ctx.meta.sku"
    got = run_lua_transform("return tostring(v)", 42)
    assert got == "42"
    got = run_lua_transform(src, "x", {"url": "u", "meta": {"sku": "s"}})
    assert got == "x|u|s"


def test_nested_input_structures_convert() -> None:
    src = "return v.rows[1].name"
    assert run_lua_transform(src, {"rows": [{"name": "Scent"}]}) == "Scent"


def test_returning_nil_is_none_not_an_error() -> None:
    assert run_lua_transform("return nil", "x") is None


# --- the sandbox ------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "return os.time()",
        "return io.open('/etc/passwd')",
        "return require('os')",
        "return dofile('/etc/passwd')",
        "return loadstring('return 1')()",
        "return debug.getinfo(1)",
        "return package.path",
    ],
)
def test_forbidden_names_are_refused(src: str) -> None:
    with pytest.raises(LuaError):
        run_lua_transform(src, None)


def test_python_bridge_is_not_reachable() -> None:
    """lupa injects a `python` table into the real global environment. The
    chunk runs against an allowlist that does not contain it."""

    with pytest.raises(LuaError):
        run_lua_transform("return python.eval('1+1')", None)


def test_globals_are_confined_to_the_allowlist() -> None:
    keys = run_lua_transform(
        "local out = {} for k in pairs(_G) do out[#out+1] = k end table.sort(out) return out",
        None,
    )
    assert "os" not in keys and "io" not in keys and "debug" not in keys
    assert "string" in keys and "math" in keys


def test_one_script_cannot_affect_the_next() -> None:
    run_lua_transform("_G.leaked = 'yes' return 1", None)
    assert run_lua_transform("return _G.leaked", None) is None


# --- the limits -------------------------------------------------------------


def test_infinite_loop_hits_the_instruction_budget() -> None:
    with pytest.raises(LuaError, match="budget"):
        run_lua_transform("while true do end", None, limits=LuaLimits(instruction_budget=50_000))


def test_raising_inside_lua_is_a_field_failure() -> None:
    with pytest.raises(LuaError, match="raised"):
        run_lua_transform("error('nope')", None)


def test_syntax_error_is_a_field_failure() -> None:
    with pytest.raises(LuaError, match="compile"):
        run_lua_transform("this is not lua", None)


def test_a_lua_failure_surfaces_as_a_transform_error_not_a_crash() -> None:
    """What the pipeline actually sees: one bad recipe must not take down a
    worker processing nineteen good ones."""

    with pytest.raises(TransformError):
        apply_transforms(
            "x", [Transform(op="lua", source="error('boom')")], TransformContext()
        )
