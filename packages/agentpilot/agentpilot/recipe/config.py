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
        )
