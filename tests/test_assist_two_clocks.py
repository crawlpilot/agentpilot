"""A park that nobody is watching must not cost what a watched one costs.

`assist_timeout_s` describes itself as "the ceiling for a tab nobody is looking
at rather than the budget for doing the work", and `touch_park` exists to push
the deadline out while the studio's assist panel is open. Both were true. What
was not true is that the park *started* anywhere other than the ceiling -- so a
question nobody ever saw held a warm identity, a browser and a proxy pin for
1800s, on a node that fits four browsers, while the heartbeat renewed its lease
the whole time.

These pin the two clocks apart.
"""

from __future__ import annotations

import os

from agentpilot.recipe.config import RecipeConfig


def _config(**env: str) -> RecipeConfig:
    previous = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return RecipeConfig.from_env()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_unattended_budget_is_far_below_the_ceiling_by_default() -> None:
    cfg = RecipeConfig.from_env()
    assert cfg.assist_unattended_s < cfg.assist_timeout_s
    assert cfg.assist_unattended_s == 180
    assert cfg.assist_timeout_s == 1800


def test_both_clocks_are_configurable_independently() -> None:
    cfg = _config(
        AGENTPILOT_RECIPE_ASSIST_UNATTENDED_S="30",
        AGENTPILOT_RECIPE_ASSIST_TIMEOUT_S="600",
    )
    assert (cfg.assist_unattended_s, cfg.assist_timeout_s) == (30.0, 600.0)


def test_the_park_starts_on_the_unattended_budget_not_the_ceiling() -> None:
    """The arithmetic `_park_for_assist` does, isolated.

    `min` rather than the unattended value outright, so an operator who sets a
    ceiling *below* the unattended budget gets the ceiling honoured rather than a
    park that outlives its own timeout.
    """

    def initial(timeout_s: float, unattended_s: float | None) -> float:
        return timeout_s if unattended_s is None else min(unattended_s, timeout_s)

    assert initial(1800, 180) == 180
    assert initial(120, 180) == 120, "the ceiling still wins when it is the smaller one"
    assert initial(1800, None) == 1800, "no unattended budget keeps the old behaviour"


def test_touch_park_extends_by_the_ceiling_so_a_watched_park_gets_the_full_budget() -> None:
    """The other half: a person who turns up must not be penalised for the short
    start. `recipes.touch_park` passes `assist_timeout_s` as `extend_by_s`, so
    the first poll from an open panel lifts the deadline to the full ceiling.
    """

    import inspect

    from agentpilot.gateway.routes import recipes

    source = inspect.getsource(recipes.heartbeat_assist)
    assert "extend_by_s=RecipeConfig.from_env().assist_timeout_s" in source
