"""Human-shaped mouse paths.

Everything in this module exists because a straight line is worse than no line.
Playwright's `mouse.move(x, y, steps=n)` interpolates linearly at uniform
spacing: constant velocity, zero curvature, no overshoot. Both PulsarRPA
(`Emulation.kt:179`) and Browser4 (`EmulationHandler.kt:248-271`) do exactly
that, and it is the one place worth *improving* on them rather than porting --
a perfectly straight constant-velocity path scores worse on a mouse-entropy
model than a single teleport does, because a teleport at least looks like a
programmatic focus rather than a human who moves like a machine.

Three properties a real pointer has that linear interpolation lacks:

- **Curvature.** A hand pivots around the wrist/elbow, so the path bows. Here:
  a cubic Bezier whose control points are offset perpendicular to the straight
  line by a distance that scales with the travel.
- **Non-uniform velocity.** Fitts's-law movement accelerates, then decelerates
  sharply into the target. Here: an ease-in-out reparameterisation of `t`, so
  the sampled points bunch up at both ends.
- **Overshoot.** Fast movements to a small target overshoot and correct. Here:
  an occasional short reversal past the endpoint.

The output is a list of points; dispatching them (and the delay between them)
is the caller's job, so this module stays pure and testable without a browser.
"""

from __future__ import annotations

import math
import random
from typing import TypedDict

Point = tuple[float, float]


class Box(TypedDict):
    """An element's bounding box.

    Structurally identical to Playwright's `FloatRect`, which is what
    `Locator.bounding_box()` returns -- declaring our own keeps this module
    free of a Playwright import (it is pure geometry and unit-tested without a
    browser) while still type-checking at the call site, since mypy compares
    TypedDicts structurally.
    """

    x: float
    y: float
    width: float
    height: float

_OVERSHOOT_PROBABILITY = 0.25
"""How often a movement overshoots and corrects. Every movement overshooting is
as unnatural as none doing so."""

_MAX_BOW_FRACTION = 0.18
"""Peak perpendicular deviation as a fraction of travel distance. Larger than
this and the pointer visibly loops rather than bows."""


def _ease_in_out(t: float) -> float:
    """Smoothstep. Bunches sampled points near t=0 and t=1, which is what makes
    the dispatched path accelerate away and decelerate into the target."""

    return t * t * (3.0 - 2.0 * t)


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1.0 - t
    x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
    return (x, y)


def path(start: Point, end: Point, *, steps: int | None = None) -> list[Point]:
    """A curved, eased sequence of points from `start` to `end`, inclusive.

    `steps` defaults to a distance-dependent count: a short nudge does not need
    as many samples as a cross-viewport sweep, and a fixed count would make
    short movements suspiciously dense and long ones suspiciously coarse.
    """

    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance < 1.0:
        return [end]

    if steps is None:
        steps = max(8, min(40, int(distance / 12) + random.randint(3, 8)))

    # Control points pushed perpendicular to the straight line, by a fraction
    # of the travel, in a randomly chosen direction.
    nx, ny = -dy / distance, dx / distance
    bow = distance * random.uniform(0.04, _MAX_BOW_FRACTION) * random.choice((-1.0, 1.0))
    c1 = (
        start[0] + dx * 0.3 + nx * bow,
        start[1] + dy * 0.3 + ny * bow,
    )
    c2 = (
        start[0] + dx * 0.7 + nx * bow * random.uniform(0.4, 1.0),
        start[1] + dy * 0.7 + ny * bow * random.uniform(0.4, 1.0),
    )

    points = [_cubic(start, c1, c2, end, _ease_in_out(i / steps)) for i in range(steps + 1)]

    if random.random() < _OVERSHOOT_PROBABILITY and distance > 60:
        # Carry a little past the target, then settle back onto it.
        over = (
            end[0] + dx / distance * random.uniform(3.0, 12.0),
            end[1] + dy / distance * random.uniform(3.0, 12.0),
        )
        points.extend([over, end])

    return points


def jittered_point_in(box: Box) -> Point:
    """A point inside `box` that is deliberately *not* its centre.

    Clicking the exact geometric centre every time is a fingerprint in its own
    right (Browser4 `EmulationHandler.kt:868-924` makes the same point). The
    margin keeps the point comfortably inside the element so the click still
    lands on it.
    """

    margin_x = min(box["width"] * 0.3, 8.0)
    margin_y = min(box["height"] * 0.3, 8.0)
    return (
        box["x"] + random.uniform(margin_x, max(margin_x, box["width"] - margin_x)),
        box["y"] + random.uniform(margin_y, max(margin_y, box["height"] - margin_y)),
    )


def approach_from_outside(box: Box, *, distance: float = 50.0) -> Point:
    """A point just outside `box` to approach from.

    Moving in from outside makes the browser emit a real `mouseover`/`mouseenter`
    transition, which a pointer that materialises inside the element never
    produces. Ported from Browser4's `hover` (`EmulationHandler.kt:829-866`),
    which uses the same ~50 px standoff.
    """

    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    angle = random.uniform(0, 2 * math.pi)
    return (
        max(0.0, cx + math.cos(angle) * (box["width"] / 2 + distance)),
        max(0.0, cy + math.sin(angle) * (box["height"] / 2 + distance)),
    )
