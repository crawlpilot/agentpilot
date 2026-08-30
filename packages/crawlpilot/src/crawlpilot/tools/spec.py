"""One definition per browser verb, projected onto every surface that needs it.

The action vocabulary used to exist three times (plan D5): the `spi.actions`
dataclasses the driver dispatches, a hand-written Pydantic union in
`agent.actions` for the LLM, and a second hand-written union in
`gateway.schemas` for the HTTP boundary -- plus a 17-entry converter dict
mapping the third back to the first. `agent/actions.py`'s own docstring
conceded the duplication. Adding a verb meant editing four files and keeping
three descriptions in sync by hand.

A `ToolSpec` is the single declaration. The dataclass stays the source of truth
for *types and defaults* -- they are read off it, never restated -- and the spec
adds what a dataclass cannot carry: a human description, per-field descriptions,
a safety class, and which fields each surface exposes.

The surfaces genuinely differ, and that is deliberate rather than accidental:
the HTTP boundary exposes nearly the full dataclass, while the agent surface is
narrower on purpose (fewer knobs for a model to get wrong -- `navigate` takes a
url and not `wait_until`, `referer` or `timeout_ms`). One declaration, two
projections, instead of two hand-maintained copies that drift.

**Vendor-neutral by construction.** A spec carries a Pydantic model and plain
JSON Schema; nothing here knows what an LLM provider is. Provider shapes live in
`tools.adapters` as pure dict re-shaping, and `crawlpilot.tools` imports no LLM
SDK -- asserted by a test.
"""

from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Literal, get_type_hints
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, create_model

Safety = Literal["safe", "sensitive"]

REF_DESCRIPTION = (
    "The exact element ref from the current page state, shown in square brackets "
    'e.g. \'e12\' in `[e12]<button "Add to cart"/>`. Copy it verbatim (an `e` '
    "followed by digits). Never use a human-readable name/label, a description, "
    "or a URL as a ref -- to open a URL use the navigate action."
)
"""Deliberately NOT a JSON-schema `pattern`: llama.cpp / Ollama structured
outputs compile the schema to a GBNF grammar and reject any `pattern` (400),
which would break every local-model run. The loop's `valid_refs` guard rejects a
malformed or stale ref instead."""


@dataclass(frozen=True)
class ToolSpec:
    """One browser verb."""

    name: str
    """The wire discriminator (`type`) and the tool name within its namespace."""

    description: str
    action_cls: type
    """The `spi.actions` dataclass this builds. Types and defaults come from it."""

    wire_fields: tuple[str, ...] = ()
    """Fields exposed on the HTTP boundary."""

    agent_fields: tuple[str, ...] | None = None
    """Fields exposed to an LLM, or `None` when the verb is not offered to
    agents at all (`execute_js` -- arbitrary JS is security-sensitive; tab
    management -- complexity without a clear need)."""

    field_descriptions: dict[str, str] = field(default_factory=dict)

    wire_overrides: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    """`{field: (annotation, default)}` where the HTTP surface deliberately
    differs from the dataclass. Only `extract`'s tag filters do today: the
    dataclass takes `tuple[str, ...] | None = None` (absent means "no filter"),
    while the wire has always taken `list[str]` defaulting to empty. Both mean
    the same thing; changing either would be a visible API change, so the
    difference is declared rather than papered over."""

    agent_overrides: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    """Where the *agent* surface deliberately narrows the dataclass. `extract`
    is the case: the driver and the HTTP API accept `html` and
    `structured_data`, but an agent reading a page wants markdown or text --
    raw HTML burns context for no gain, and JSON-LD is not what the loop asks
    for. Narrowing the enum is what stops a model choosing them at all."""

    validators: dict[str, Any] = field(default_factory=dict)
    """Pydantic validators applied to the agent projection, by name. `navigate`
    carries the http/https check: without it a model can steer the browser to
    `file://` or `javascript:`, so it is a security guard rather than
    tidiness."""

    safety: Safety = "safe"

    domains: tuple[str, ...] | None = None
    """Host glob patterns this verb applies to (`("*.walmart.com",)`), or `None`
    for every site.

    Ported from browser-use's `RegisteredAction.domains`. It is what makes the
    namespacing in this registry useful rather than decorative: a
    `walmart.solve_wall` can be registered permanently and still only reach a
    model's schema on Walmart, instead of every run paying context for a verb
    that cannot work where it is."""

    # ------------------------------------------------------------ applicability

    def applies_to(self, page_url: str | None) -> bool:
        """Whether this verb should be offered while `page_url` is open.

        Fails closed on an unknown URL, as browser-use does
        (`tools/registry/views.py:107-111`): a domain-restricted verb exists
        precisely because it is unsafe or meaningless elsewhere, so "we don't
        know where we are" is not a reason to offer it.
        """

        if self.domains is None:
            return True
        if not page_url:
            return False
        host = urlparse(page_url).hostname or ""
        return any(fnmatch(host, pattern) for pattern in self.domains)

    # ------------------------------------------------------------ projections

    def wire_model(self) -> type[BaseModel]:
        """The HTTP projection, deliberately **without** descriptions.

        Descriptions here would land in the published OpenAPI, and this
        consolidation is meant to be invisible on the wire -- a golden-schema
        test asserts the generated union is byte-identical to the hand-written
        one it replaces. Enriching the API docs is a real improvement, but a
        separate, deliberate change rather than a side effect of a refactor.
        """

        return _build_model(
            self,
            self.wire_fields,
            suffix="ActionIn",
            overrides=self.wire_overrides,
            with_descriptions=False,
        )

    def agent_model(self) -> type[BaseModel]:
        """The LLM projection, **with** descriptions -- they are what steer the
        model, and `REF_DESCRIPTION` in particular is the difference between a
        model copying a ref token and inventing a label."""

        if self.agent_fields is None:
            raise ValueError(f"{self.name!r} is not exposed to agents")
        return _build_model(
            self,
            self.agent_fields,
            suffix="ActionIn",
            overrides=self.agent_overrides,
            with_docstring=False,
            validators=self.validators,
        )

    def json_schema(self) -> dict[str, Any]:
        """Plain JSON Schema for the agent projection -- the vendor-neutral
        artifact every adapter re-shapes."""

        return self.agent_model().model_json_schema()

    def build(self, **kwargs: Any) -> Any:
        """Instantiate the underlying `spi.actions` dataclass.

        Replaces `gateway.action_conversion`'s hand-written converter table:
        the field names are the same on both sides by construction, so there is
        nothing to keep in sync.
        """

        return self.action_cls(**kwargs)

    def from_model(self, parsed: BaseModel) -> Any:
        data = parsed.model_dump(exclude={"type"})
        return self.build(**data)


def _dataclass_fields(action_cls: type) -> dict[str, tuple[Any, Any]]:
    """`{name: (type, default)}` read off the dataclass, so a field's type and
    default have exactly one definition.

    A `default_factory` is passed through to Pydantic as a factory rather than
    called here. Resolving it to a literal would emit `"default": []` into the
    JSON Schema, where the hand-written model emitted no `default` at all --
    a visible wire-schema change for what should be a pure refactor.
    """

    hints = get_type_hints(action_cls)
    out: dict[str, tuple[Any, Any]] = {}
    for f in dataclasses.fields(action_cls):
        if f.default is not dataclasses.MISSING:
            default: Any = f.default
        elif f.default_factory is not dataclasses.MISSING:
            default = Field(default_factory=f.default_factory)
        else:
            default = ...  # required
        out[f.name] = (hints[f.name], default)
    return out


def _build_model(
    spec: ToolSpec,
    names: tuple[str, ...],
    *,
    suffix: str,
    overrides: dict[str, tuple[Any, Any]] | None = None,
    with_descriptions: bool = True,
    with_docstring: bool = True,
    validators: dict[str, Any] | None = None,
) -> type[BaseModel]:
    available = {**_dataclass_fields(spec.action_cls), **(overrides or {})}
    unknown = set(names) - available.keys()
    if unknown:
        raise ValueError(f"{spec.name!r} exposes unknown field(s) {sorted(unknown)}")

    fields: dict[str, Any] = {
        "type": (Literal[spec.name], ...),
    }
    for n in names:
        annotation, default = available[n]
        description = spec.field_descriptions.get(n) if with_descriptions else None
        if description is not None:
            fields[n] = (annotation, Field(default, description=description))
        else:
            fields[n] = (annotation, default)

    model: type[BaseModel] = create_model(
        _class_name(spec.name, suffix),
        __config__=ConfigDict(extra="forbid"),
        __validators__=validators or None,
        **fields,
    )
    if with_descriptions and with_docstring:
        model.__doc__ = spec.description
    return model


def _class_name(name: str, suffix: str) -> str:
    return "".join(part.title() for part in name.split("_")) + suffix


def union_of(models: typing.Sequence[type[BaseModel]]) -> Any:
    """A discriminated union over `type`, matching the hand-written
    `Annotated[A | B | ..., Field(discriminator="type")]`."""

    from typing import Annotated, Union  # noqa: PLC0415

    return Annotated[Union[tuple(models)], Field(discriminator="type")]  # noqa: UP007
