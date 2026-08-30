"""Unit tests for `crawlpilot.driver.mouse` -- pure geometry, no browser.

These assert the three properties that separate a human pointer path from
Playwright's `mouse.move(steps=n)` linear interpolation, which is what both
PulsarRPA and Browser4 use and which is the one place worth improving on them.
"""

from __future__ import annotations

import math
import random

from crawlpilot.driver import mouse


def _straightness(points: list[tuple[float, float]]) -> float:
    """Max perpendicular deviation from the straight start->end line, as a
    fraction of the travel distance. Exactly 0 for linear interpolation."""

    (x0, y0), (x1, y1) = points[0], points[-1]
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length == 0:
        return 0.0
    worst = max(abs((px - x0) * dy - (py - y0) * dx) / length for px, py in points)
    return worst / length


def test_path_starts_and_ends_on_target() -> None:
    points = mouse.path((10.0, 10.0), (400.0, 300.0))

    assert points[0] == (10.0, 10.0)
    assert points[-1] == (400.0, 300.0)


def test_path_is_curved_not_linear() -> None:
    """A straight constant-velocity path is a stronger tell than a teleport."""

    random.seed(7)
    deviations = [_straightness(mouse.path((0.0, 0.0), (500.0, 400.0))) for _ in range(30)]

    assert min(deviations) > 0.005, "some path was effectively straight"
    assert max(deviations) < 0.5, "a path bowed so far it would look like a loop"


def test_path_velocity_is_not_uniform() -> None:
    """Fitts's-law movement accelerates then decelerates into the target, so
    the sampled points bunch at both ends rather than being evenly spaced."""

    random.seed(11)
    points = mouse.path((0.0, 0.0), (600.0, 0.0), steps=40)
    gaps = [
        math.hypot(b[0] - a[0], b[1] - a[1])
        for a, b in zip(points, points[1:], strict=False)
    ]
    mid = gaps[len(gaps) // 2]

    assert mid > gaps[0] * 1.5, "path did not accelerate away from the start"
    assert mid > gaps[-1] * 1.5, "path did not decelerate into the target"


def test_path_handles_a_zero_length_move() -> None:
    assert mouse.path((5.0, 5.0), (5.0, 5.0)) == [(5.0, 5.0)]
    assert mouse.path((5.0, 5.0), (5.4, 5.4)) == [(5.4, 5.4)]


def test_step_count_scales_with_distance() -> None:
    random.seed(3)
    short = len(mouse.path((0.0, 0.0), (40.0, 0.0)))
    long = len(mouse.path((0.0, 0.0), (900.0, 0.0)))

    assert long > short


def test_jittered_point_is_inside_the_box_but_not_its_centre() -> None:
    box = {"x": 100.0, "y": 200.0, "width": 80.0, "height": 40.0}
    centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    points = [mouse.jittered_point_in(box) for _ in range(200)]

    for x, y in points:
        assert box["x"] <= x <= box["x"] + box["width"]
        assert box["y"] <= y <= box["y"] + box["height"]
    assert any(abs(x - centre[0]) > 1 or abs(y - centre[1]) > 1 for x, y in points)


def test_jittered_point_copes_with_a_tiny_box() -> None:
    """Elements smaller than twice the margin must not produce an inverted
    range -- `random.uniform` would happily return a point outside the box."""

    box = {"x": 10.0, "y": 10.0, "width": 3.0, "height": 2.0}

    for _ in range(100):
        x, y = mouse.jittered_point_in(box)
        assert box["x"] <= x <= box["x"] + box["width"] + 1e-9
        assert box["y"] <= y <= box["y"] + box["height"] + 1e-9


def test_approach_point_is_outside_the_box() -> None:
    """Approaching from outside is what makes the browser emit a real
    mouseover/mouseenter transition."""

    box = {"x": 300.0, "y": 300.0, "width": 100.0, "height": 50.0}

    for _ in range(100):
        x, y = mouse.approach_from_outside(box, distance=50.0)
        inside = (
            box["x"] <= x <= box["x"] + box["width"]
            and box["y"] <= y <= box["y"] + box["height"]
        )
        assert not inside


def test_approach_point_is_never_negative() -> None:
    """A box near the viewport origin must not produce off-screen coordinates
    that the browser would reject."""

    box = {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0}

    for _ in range(200):
        x, y = mouse.approach_from_outside(box)
        assert x >= 0.0 and y >= 0.0
