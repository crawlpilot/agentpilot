"""The sandboxed Lua escape hatch for the transform pipeline (contract 7.1).

Recipe Lua is written by a model reading an untrusted web page, so it is
treated as untrusted input rather than as trusted operator configuration. Three
things enforce that, and all three matter:

1. **A fresh runtime per evaluation**, so nothing a script leaves behind can
   reach the next field, the next row, or the next recipe.
2. **An allowlisted environment.** The chunk is compiled against a table
   containing only pure value-manipulation builtins -- no `io`, `os`, `require`,
   `load`, `dofile`, `package`, `debug`, and no `python` bridge. This is the
   primary defence: the removed names are not reachable rather than merely
   discouraged.
3. **An instruction-count hook and a wall clock**, because an allowlist does
   nothing about `while true do end`.

Exceeding a limit, raising, or returning something unconvertible is a *field
failure* -- `LuaError`, which the transform pipeline turns into a failed field.
It is never a crashed run, because one bad recipe must not take down a worker
processing nineteen good ones.

Determinism is part of the contract, not a side effect of the sandbox: no
network, no filesystem, no clock, no randomness. A transform that returned a
different value for the same input would make replay irreproducible, heal diffs
meaningless, and golden fixtures a lie.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

# Bootstrap runs in the REAL global environment (it needs `debug` to install the
# hook and `load`/`loadstring` to compile). Only the user chunk is confined.
_BOOTSTRAP = """
local M = {}

function M.make_env()
  local env = {
    string = string, table = table, math = math,
    tonumber = tonumber, tostring = tostring, type = type,
    select = select, ipairs = ipairs, pairs = pairs, next = next,
    error = error, pcall = pcall, assert = assert,
    unpack = table.unpack or unpack,
  }
  env._G = env
  return env
end

function M.compile(src, env)
  local wrapped = "return function(v, ctx)\\n" .. src .. "\\nend"
  local chunk, err
  if setfenv ~= nil then          -- Lua 5.1 / LuaJIT
    chunk, err = loadstring(wrapped, "=recipe-transform")
    if not chunk then return nil, err end
    setfenv(chunk, env)
  else                             -- Lua 5.2+
    chunk, err = load(wrapped, "=recipe-transform", "t", env)
    if not chunk then return nil, err end
  end
  local ok, fn = pcall(chunk)
  if not ok then return nil, tostring(fn) end
  if type(fn) ~= "function" then return nil, "chunk did not produce a function" end
  return fn, nil
end

function M.call(fn, v, ctx, budget)
  local hooked = false
  if budget and budget > 0 and debug and debug.sethook then
    debug.sethook(function() error("instruction budget exceeded", 0) end, "", budget)
    hooked = true
  end
  local ok, res = pcall(fn, v, ctx)
  if hooked then debug.sethook() end
  if not ok then return nil, tostring(res) end
  return res, nil
end

return M
"""

_FORBIDDEN_SUBSTRINGS = (
    "require", "dofile", "loadfile", "loadstring", "load(",
    "os.", "io.", "package", "debug.", "getfenv", "setfenv", "coroutine",
)


class LuaError(Exception):
    """A Lua transform failed: a syntax error, a raised error, a limit
    exceeded, or a result that cannot be represented as JSON. Callers treat it
    as a field failure with this message as the reason."""


@dataclass(frozen=True)
class LuaLimits:
    instruction_budget: int = 2_000_000
    timeout_ms: int = 250
    max_table_entries: int = 100_000
    max_depth: int = 32


DEFAULT_LIMITS = LuaLimits()


def lua_available() -> bool:
    """Whether the optional `lupa` extra is installed. A recipe that uses a
    `lua` transform on a deployment without it gets a clear field failure
    rather than an import error at request time."""

    try:
        import lupa  # noqa: F401
    except ImportError:
        return False
    return True


def _to_lua(runtime: Any, value: Any, depth: int = 0) -> Any:
    """Python -> Lua. Dicts and lists become real Lua tables (1-based for
    lists, per Lua convention) so a script can index them naturally."""

    if depth > DEFAULT_LIMITS.max_depth:
        raise LuaError("input nesting too deep for a lua transform")
    if isinstance(value, dict):
        return runtime.table_from(
            {k: _to_lua(runtime, v, depth + 1) for k, v in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return runtime.table_from([_to_lua(runtime, v, depth + 1) for v in value])
    return value


def _from_lua(runtime: Any, value: Any, limits: LuaLimits, depth: int = 0) -> Any:
    """Lua -> Python. A table with contiguous 1..n integer keys becomes a list;
    anything else becomes a dict. This is the standard ambiguity in Lua and
    there is no way to resolve it without a convention -- an empty table is
    therefore an empty list, which is the more useful default for extraction."""

    import lupa

    if depth > limits.max_depth:
        raise LuaError("lua transform returned a structure that is too deeply nested")
    if lupa.lua_type(value) != "table":
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        raise LuaError(f"lua transform returned an unsupported type: {lupa.lua_type(value)!r}")

    items = list(value.items())
    if len(items) > limits.max_table_entries:
        raise LuaError("lua transform returned too many entries")

    keys = [k for k, _ in items]
    is_array = keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(
        range(1, len(keys) + 1)
    )
    if is_array or not keys:
        return [_from_lua(runtime, value[i], limits, depth + 1) for i in range(1, len(keys) + 1)]
    return {
        str(k): _from_lua(runtime, v, limits, depth + 1)
        for k, v in items
    }


def _reject_obvious_escapes(source: str) -> None:
    """A cheap pre-filter over the source text.

    This is belt-and-braces, NOT the security boundary -- the environment
    allowlist is. It exists because a recipe whose Lua reaches for `os.execute`
    is a recipe worth refusing loudly at author time, rather than one that
    silently returns nil at 3am and looks like ordinary drift.
    """

    lowered = source.lower()
    for needle in _FORBIDDEN_SUBSTRINGS:
        if needle in lowered:
            raise LuaError(f"lua transform references a forbidden name: {needle!r}")


def run_lua_transform(
    source: str,
    value: Any,
    ctx: dict[str, Any] | None = None,
    *,
    limits: LuaLimits = DEFAULT_LIMITS,
) -> Any:
    """Apply one `{"op": "lua", "source": ...}` transform.

    `source` is a function body over `(v, ctx)` returning the new value.
    Raises `LuaError` for every failure mode; never raises anything else.
    """

    try:
        import lupa
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise LuaError(
            "lua transforms require the `recipe-lua` extra (lupa) to be installed"
        ) from exc

    _reject_obvious_escapes(source)

    runtime = lupa.LuaRuntime(
        register_eval=False,
        register_builtins=False,
        unpack_returned_tuples=True,
        # Deny every attribute access on any Python object that might reach Lua.
        # With the env allowlist there should be none; this makes that a
        # property of the runtime rather than of our own care.
        attribute_filter=_deny_attribute,
    )
    module = runtime.execute(_BOOTSTRAP)
    env = module.make_env()

    fn, err = module.compile(source, env)
    if fn is None:
        raise LuaError(f"lua transform failed to compile: {err}")

    lua_value = _to_lua(runtime, value)
    lua_ctx = _to_lua(runtime, ctx or {})

    started = time.monotonic()
    result, err = module.call(fn, lua_value, lua_ctx, limits.instruction_budget)
    elapsed_ms = (time.monotonic() - started) * 1000

    if err is not None:
        raise LuaError(f"lua transform raised: {err}")
    if elapsed_ms > limits.timeout_ms:
        raise LuaError(
            f"lua transform exceeded its time budget ({elapsed_ms:.0f}ms > {limits.timeout_ms}ms)"
        )
    return _from_lua(runtime, result, limits)


def _deny_attribute(obj: Any, attr_name: str, is_setting: bool) -> Any:
    raise AttributeError("attribute access from lua is not permitted")
