"""Human-timing delay policy -- a direct port of Pulsar's `InteractSettings`
delay presets (`pulsar-browser/.../api/InteractSettings.kt:374-445`).

WAFs that score pointer/keystroke telemetry (Akamai's `_abck` sensor among
them) flag pixel-perfect, fixed-cadence automation. Pulsar's answer is a small
table of named delay presets, each mapping a low-level action to an
`IntRange(ms)` that every action samples from before it fires. This is that
table, translated 1:1, plus the global clamp and the defensive range-fallback
from `AbstractWebDriver.randomDelayMillis` (`:550-559`).

The `STEALTH` preset is the slow, most-human one -- use it for protected
targets. `DEFAULT` is the everyday preset; `FAST` trades realism for speed on
sites that don't score behaviour. The ladder from an aggressiveness level to a
preset mirrors Pulsar's `InteractLevel -> preset` factory
(`InteractSettings.kt:464-489`).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

# Global clamp from InteractSettings.kt:71-76 -- no sampled delay is ever
# shorter than this or longer than this, whatever a preset range says.
MIN_DELAY_MS = 50
MAX_DELAY_MS = 2_000

# Fallback when a range is malformed (first <= 0 or last > 10_000), matching
# AbstractWebDriver.randomDelayMillis's guard (:550-559) -- a bad range must
# degrade to a sane human delay, never raise.
_FALLBACK_RANGE = (500, 1_000)

# Every action name Pulsar's table keys on. "gap" is the pause *between*
# high-level actions; the rest pace the sub-steps of one action. "default"
# backs any action name not otherwise present.
_ACTIONS = (
    "gap",
    "click",
    "delete",
    "press",
    "type",
    "fill",
    "mouseWheel",
    "dragAndDrop",
    "waitForNavigation",
    "waitForSelector",
    "default",
)

# The three ported presets (ms IntRange per action), InteractSettings.kt:374-445.
_DEFAULT: dict[str, tuple[int, int]] = {
    "gap": (650, 1_100),
    "click": (90, 180),
    "delete": (80, 180),
    "press": (120, 260),
    "type": (90, 240),
    "fill": (130, 280),
    "mouseWheel": (180, 420),
    "dragAndDrop": (260, 650),
    "waitForNavigation": (250, 700),
    "waitForSelector": (200, 600),
    "default": (200, 600),
}

_FAST: dict[str, tuple[int, int]] = {
    "gap": (350, 700),
    "click": (60, 120),
    "delete": (50, 120),
    "press": (90, 180),
    "type": (60, 160),
    "fill": (80, 180),
    "mouseWheel": (120, 260),
    "dragAndDrop": (180, 420),
    "waitForNavigation": (120, 380),
    "waitForSelector": (100, 320),
    "default": (120, 320),
}

_STEALTH: dict[str, tuple[int, int]] = {
    "gap": (900, 1_600),
    "click": (120, 260),
    "delete": (110, 260),
    "press": (180, 360),
    "type": (140, 320),
    "fill": (220, 450),
    "mouseWheel": (260, 700),
    "dragAndDrop": (420, 1_100),
    "waitForNavigation": (350, 1_100),
    "waitForSelector": (280, 900),
    "default": (280, 900),
}


@dataclass(frozen=True)
class DelayPolicy:
    """A named delay table. `sample(action)` returns a clamped millisecond
    delay; `pause(action)` sleeps for it. Both fall back to a sane range for
    an unknown or malformed action rather than raising, so a caller can pass
    any Patchright verb name freely."""

    name: str
    ranges: dict[str, tuple[int, int]]

    def sample(self, action: str = "default") -> int:
        lo, hi = self.ranges.get(action) or self.ranges.get("default", _FALLBACK_RANGE)
        if lo <= 0 or hi > 10_000 or lo > hi:
            lo, hi = _FALLBACK_RANGE
        return max(MIN_DELAY_MS, min(MAX_DELAY_MS, random.randint(lo, hi)))

    async def pause(self, action: str = "default") -> None:
        await asyncio.sleep(self.sample(action) / 1_000)


DEFAULT = DelayPolicy("default", _DEFAULT)
FAST = DelayPolicy("fast", _FAST)
STEALTH = DelayPolicy("stealth", _STEALTH)

_BY_NAME = {p.name: p for p in (DEFAULT, FAST, STEALTH)}

# Map the request `tier` knob (gateway/schemas.py) to a delay preset. `basic`
# never reaches the browser (httpx path), so it isn't here; `stealth` and
# `enhanced` both want the slowest, most-human timing.
_TIER_TO_POLICY = {
    "auto": DEFAULT,
    "stealth": STEALTH,
    "enhanced": STEALTH,
}


def for_tier(tier: str) -> DelayPolicy:
    """Resolve a scrape `tier` to its delay preset (defaults to `DEFAULT`)."""
    return _TIER_TO_POLICY.get(tier, DEFAULT)


def by_name(name: str) -> DelayPolicy:
    """Resolve a preset by name (defaults to `DEFAULT`)."""
    return _BY_NAME.get(name, DEFAULT)
