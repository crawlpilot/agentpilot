"""Env-tunable knobs for the recipe pipeline -- same `os.environ.get` +
dataclass discipline as `agentpilot.llm.client.LLMConfig.from_env()`."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class RecipeConfig:
    max_heal_attempts: int
    min_schedule_interval_s: float
    field_verify_max_retries: int
    max_repeat_iterations: int
    max_judge_repairs: int
    """How many times an onboarding build may re-bind fields the judge
    rejected. Each round costs a page load, the group's step sequence and a
    proposal call, and a model that cannot find a better locator will happily
    propose the same one forever -- so the budget is small and the loop also
    stops as soon as a round changes nothing."""

    onboard_sample_runs: int
    """How many sample URLs a draft is replayed against before judging. Two
    catches the common "it only worked on the page it was built against"; the
    third is mostly confirmation, at the cost of another full page load."""

    assist_timeout_s: float
    """How long a parked run waits for a human before resuming without them.
    A parked run holds one of `_IDENTITY_SLOTS` warm identities plus a browser
    and a proxy pin, so a forgotten tab must not starve the pool."""

    @classmethod
    def from_env(cls) -> RecipeConfig:
        return cls(
            max_heal_attempts=int(os.environ.get("AGENTPILOT_RECIPE_MAX_HEAL_ATTEMPTS", "3")),
            min_schedule_interval_s=float(
                os.environ.get("AGENTPILOT_RECIPE_DEFAULT_MIN_SCHEDULE_INTERVAL_S", "300")
            ),
            field_verify_max_retries=int(
                os.environ.get("AGENTPILOT_RECIPE_FIELD_VERIFY_MAX_RETRIES", "1")
            ),
            max_repeat_iterations=int(
                os.environ.get("AGENTPILOT_RECIPE_MAX_REPEAT_ITERATIONS", "20")
            ),
            max_judge_repairs=int(
                os.environ.get("AGENTPILOT_RECIPE_MAX_JUDGE_REPAIRS", "2")
            ),
            onboard_sample_runs=int(
                os.environ.get("AGENTPILOT_RECIPE_ONBOARD_SAMPLE_RUNS", "2")
            ),
            assist_timeout_s=float(
                os.environ.get("AGENTPILOT_RECIPE_ASSIST_TIMEOUT_S", "900")
            ),
        )
