"""Process-wide logging setup -- one colored, leveled stream handler on the
root logger, installed once at boot (`agentpilot.gateway.app` import time, the
single entrypoint both `worker` and `gateway` roles boot through).

Without this the app inherits Python's default root config (WARNING, no
handler of our own), so every `logging.getLogger(__name__).info(...)` in the
worker loops, the agent loop, egress policy, etc. is silently dropped -- which
is exactly why `docker compose logs -f worker` showed only uvicorn's own lines
and nothing about *why* an agent run failed. Here we:

- attach a single `StreamHandler` to the root logger (stdout, so Docker's json
  log driver captures it -- `docker compose logs` reads container stdout/stderr),
- default to INFO (override with `AGENTPILOT_LOG_LEVEL=DEBUG|INFO|WARNING|...`),
- color each line by level via ANSI escapes so a `logs -f` stream is scannable
  at a glance -- red ERROR/CRITICAL, yellow WARNING, etc. Color is on by
  default (the whole point of this change is a traceable `logs -f`); set
  `AGENTPILOT_LOG_COLOR=0` (or the conventional `NO_COLOR=1`) to disable, e.g.
  when piping logs into a file or a non-ANSI aggregator.

Idempotent: calling `configure_logging()` more than once (uvicorn's reloader,
tests) re-uses the handler we installed rather than stacking duplicates.
"""

from __future__ import annotations

import logging
import os
import sys

_HANDLER_MARK = "_agentpilot_configured"

# 8-bit ANSI SGR codes, keyed by level. DEBUG dim, INFO cyan, WARNING yellow,
# ERROR/CRITICAL red (CRITICAL bold) -- enough contrast to spot a failure
# scrolling past without reading every line.
_LEVEL_COLORS = {
    logging.DEBUG: "\033[90m",  # bright black / grey
    logging.INFO: "\033[36m",  # cyan
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}
_RESET = "\033[0m"
_DIM = "\033[2m"


def _color_enabled() -> bool:
    # NO_COLOR (https://no-color.org/) is the cross-tool convention; our own
    # AGENTPILOT_LOG_COLOR=0 is the explicit override. Default on -- Docker's
    # `logs -f` renders ANSI fine and coloring is the requested feature.
    if os.environ.get("NO_COLOR"):
        return False
    return os.environ.get("AGENTPILOT_LOG_COLOR", "1").lower() not in ("0", "false", "no")


class ColorFormatter(logging.Formatter):
    """Level-colored formatter: `<time> <LEVEL> <logger> <message>`, with the
    level+logger tinted per severity and the timestamp dimmed. Falls back to a
    plain (uncolored) render when color is disabled."""

    def __init__(self, *, use_color: bool) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        ts = self.formatTime(record, self.datefmt)
        level = record.levelname
        name = record.name
        if not self._use_color:
            return f"{ts} {level:<8} {name} {message}"
        color = _LEVEL_COLORS.get(record.levelno, "")
        return (
            f"{_DIM}{ts}{_RESET} "
            f"{color}{level:<8}{_RESET} "
            f"{_DIM}{name}{_RESET} "
            f"{color}{message}{_RESET}"
        )


def configure_logging() -> None:
    """Install the colored root handler and set levels from the environment.
    Idempotent -- safe to call from every process entrypoint and from tests."""

    level_name = os.environ.get("AGENTPILOT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    use_color = _color_enabled()

    root = logging.getLogger()
    root.setLevel(level)

    # Reuse the handler we previously installed rather than appending a second
    # one (which would double every line on re-entry).
    handler: logging.Handler | None = next(
        (h for h in root.handlers if getattr(h, _HANDLER_MARK, False)), None
    )
    if handler is None:
        handler = logging.StreamHandler(stream=sys.stdout)
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    handler.setLevel(level)
    handler.setFormatter(ColorFormatter(use_color=use_color))

    # uvicorn/gunicorn install their own handlers on their named loggers; clear
    # those and let records propagate to our root handler so *everything* shares
    # one colored format instead of uvicorn's plain lines next to ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
