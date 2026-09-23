"""What a build tried, and why each attempt was rejected.

A build that produces a wrong selector -- or none -- left nothing behind to look
at. `progress` narrates what the run is *doing*; `data` holds what it *got*.
Neither says what was proposed on the way, so "it is failing" was reported
against a run whose reasoning had already been discarded.

**The expensive part already exists.** `verify_locators` and
`rows._problems_with` write their rejections for a person to read, deliberately:

    reads 203 values but this field is one value -- the selector is matching a
    whole set rather than a single element

    column 'value' is empty in 9 of 10 rows, so it is not being resolved inside
    each row

    every one of the 10 rows came back identical -- the column selectors are
    resolving against the whole page rather than inside a row

Those strings are the diagnosis. They were logged to a worker's stdout and then
thrown away, which is why a failed build could only be investigated by running
it again and watching.

**Optional, and inert when absent.** `record` is a no-op on `None`, so every
call site passes a trace through without a branch and every existing test keeps
working with no trace at all. A collector that had to be constructed would have
made the browser-free unit tests carry a persistence concern they have nothing
to do with.

**Bounded.** A build proposes a handful of locators per field over a handful of
attempts; the cap exists for the pathological case rather than the normal one,
and values are truncated because a trace is for reading, not for replaying.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any

# A read can be a whole rendered table. The point of recording it is to see what
# the selector actually grabbed -- the first line of that is enough to tell a
# spec sheet from a recommended-products carousel.
_MAX_VALUE_CHARS = 400

# One build, one field, a handful of attempts. Past this something is looping,
# and a trace that grows without limit is a second bug rather than a diagnosis
# of the first.
_MAX_ENTRIES = 400


@dataclass(frozen=True)
class TraceEntry:
    """One thing that was tried."""

    field: str
    stage: str
    """Where in the build this happened: `propose`, `page_json`, `rows`,
    `within`, `transform_repair`, `dom_fallback`. Which stage rejected a field
    is most of the diagnosis -- "the model never proposed anything" and "every
    proposal read the wrong element" need opposite fixes."""

    outcome: str
    """`bound` or `rejected`."""

    locator: dict[str, Any] | None = None
    read: str | None = None
    """What the locator actually returned, truncated. The difference between a
    selector that found nothing and one that found the wrong thing."""

    transform: list[str] = field(default_factory=list)
    """The op names of the pipeline that ran, if any. An empty read after a
    pipeline is a different problem from an empty read before one."""

    reason: str | None = None
    """Verbatim from `verify_locators` / `_problems_with` / the judge. Written
    to be read by a person, so it is passed through rather than summarised."""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "field": self.field,
            "stage": self.stage,
            "outcome": self.outcome,
        }
        if self.locator is not None:
            out["locator"] = self.locator
        if self.read is not None:
            out["read"] = self.read
        if self.transform:
            out["transform"] = self.transform
        if self.reason:
            out["reason"] = self.reason
        return out


def show(value: Any) -> str:
    """A value as a trace records it: readable, and never large."""

    if isinstance(value, str):
        text = value
    else:
        try:
            text = _json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text[:_MAX_VALUE_CHARS] + "..." if len(text) > _MAX_VALUE_CHARS else text


@dataclass(frozen=True)
class Exchange:
    """One question put to the model, and what came back.

    The trace above says which locators were tried and why each was turned down.
    It cannot say whether the model was ASKED the right question -- and that is
    the difference between "the selector is wrong" and "the model never saw the
    section, so of course it guessed". On a build where an accordion never
    yielded a table, the two need opposite responses: fix the prompt, or fix the
    reveal.
    """

    stage: str
    fields: list[str]
    prompt: str
    response: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "fields": self.fields,
            "prompt": self.prompt,
            "response": self.response,
        }


# A prompt carries the page snapshot and the JSON outline, so it is tens of
# kilobytes. Kept whole -- a truncated prompt cannot answer "was the section in
# what the model saw?", which is the only reason to keep it at all -- but capped
# in COUNT, and stored only when asked for.
_MAX_EXCHANGES = 40


@dataclass
class BuildTrace:
    """Everything a build tried, in the order it tried it."""

    entries: list[TraceEntry] = field(default_factory=list)
    dropped: int = 0
    exchanges: list[Exchange] = field(default_factory=list)

    def add(
        self,
        field_name: str,
        stage: str,
        outcome: str,
        *,
        locator: Any = None,
        read: Any = None,
        transform: Any = None,
        reason: str | None = None,
    ) -> None:
        if len(self.entries) >= _MAX_ENTRIES:
            self.dropped += 1
            return
        self.entries.append(
            TraceEntry(
                field=field_name,
                stage=stage,
                outcome=outcome,
                locator=locator.to_dict() if hasattr(locator, "to_dict") else locator,
                read=None if read is None else show(read),
                transform=[t.op for t in (transform or []) if hasattr(t, "op")],
                reason=reason,
            )
        )

    def exchanged(
        self, stage: str, fields: list[str], prompt: str, response: Any
    ) -> None:
        """Keep one model call, prompt and reply.

        Always collected, never always stored: by the time anyone wants to know
        what the model was shown, the browser is gone and the page has moved on,
        so there is no second chance to capture it. Whether it reaches the
        database is `AGENTPILOT_RECIPE_TRACE_PROMPTS`.
        """

        if len(self.exchanges) >= _MAX_EXCHANGES:
            return
        try:
            body = _json.dumps(response, ensure_ascii=False, indent=1, default=str)
        except (TypeError, ValueError):
            body = str(response)
        self.exchanges.append(
            Exchange(stage=stage, fields=list(fields), prompt=prompt, response=body)
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"attempts": [e.to_dict() for e in self.entries]}
        if self.dropped:
            out["dropped"] = self.dropped
        return out

    def exchanges_to_dict(self) -> dict[str, Any]:
        """The model calls, separately from the attempts. Separate because these
        are large and optional and the attempts are neither."""

        return {"exchanges": [e.to_dict() for e in self.exchanges]}

    def for_field(self, name: str) -> list[TraceEntry]:
        return [e for e in self.entries if e.field == name]

    def explain(self, name: str) -> str:
        """Why this field is not bound, as a person would want it put.

        This is what turns an ask from "find this" into "here is what I tried",
        which is a different question to be handed -- a person who can see that
        the accordion click never ran, or that the selector read the breadcrumb
        trail, knows which of the two things to do about it.
        """

        lines = [
            f"{e.stage}: {e.reason}"
            + (f" (read {e.read})" if e.read and e.reason and "read" not in e.reason else "")
            for e in self.for_field(name)
            if e.outcome == "rejected" and e.reason
        ]
        return "\n".join(dict.fromkeys(lines))


def record(
    trace: BuildTrace | None,
    field_name: str,
    stage: str,
    outcome: str,
    **kwargs: Any,
) -> None:
    """Add an entry if anyone is collecting. A no-op otherwise.

    The reason every call site can be a single unconditional line: tracing is a
    diagnostic concern and the code doing the work should not have to know
    whether it is switched on.
    """

    if trace is not None:
        trace.add(field_name, stage, outcome, **kwargs)
