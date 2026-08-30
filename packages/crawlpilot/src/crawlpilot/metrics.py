"""A metrics seam with a no-op default.

The browser layer emitted counters by importing `observability.metrics`
directly, which imports `prometheus_client`. That made prometheus a transitive
dependency of the browser core -- contradicting the extraction's promise about
the shipped wheel's closure, and, more concretely, blocking the split outright:
`observability` is used by fourteen platform files as well, so it cannot simply
move across with the browser layer.

`tests/test_public_api_surface.py::test_observability_coupling_does_not_grow`
pinned that debt precisely so it would surface here rather than at packaging
time. This pays it.

The counters themselves are unchanged -- the platform still exports the same
Prometheus metrics with the same names and labels. What changed is who owns the
client: the browser layer emits through this `Recorder`, and the platform
installs one that forwards to `prometheus_client`.

**The module-level recorder is a deliberate global**, and the only one in the
browser layer. Threading a recorder through every constructor to reach four call
sites would be a worse trade, and this is the shape the standard library already
uses for the same problem (`logging.getLogger`). It defaults to a no-op, so a
library consumer that installs nothing pays nothing and sees nothing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Recorder(Protocol):
    """Counters only -- the browser layer emits nothing else today, and a
    narrow seam is easier to implement than a general metrics API."""

    def incr(self, name: str, amount: float = 1.0, **labels: str) -> None: ...


class NullRecorder:
    """The default. Emitting is free and silent when nobody is listening."""

    def incr(self, name: str, amount: float = 1.0, **labels: str) -> None:
        return None


_recorder: Recorder = NullRecorder()


def set_recorder(recorder: Recorder | None) -> None:
    """Install a recorder, or `None` to go back to silence.

    Called once from a composition root (`gateway.wiring`), never from library
    code.
    """

    global _recorder  # noqa: PLW0603 -- the documented single global, see module docstring
    _recorder = recorder or NullRecorder()


def incr(name: str, amount: float = 1.0, **labels: str) -> None:
    """Emit a counter increment. Never raises: a metrics backend must not be
    able to fail a crawl."""

    try:
        _recorder.incr(name, amount, **labels)
    except Exception:  # noqa: BLE001, S110 -- see docstring
        pass
