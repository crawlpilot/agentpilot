"""Independent completion judge (D5): a second, deliberately skeptical LLM
pass that re-checks the agent's self-reported `done(success=True)` against the
task and the observed page state. Mirrors Browser4's `judge.py` ("be initially
doubtful of the agent's self-reported success"; ground truth takes precedence).

Fail-open: if the judge call errors, the agent's own verdict stands. The judge
may *veto* a success it can't justify from the evidence, but a judge outage
must never fabricate a failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json

_PAGE_STATE_CAP = 4_000
"""Bound the final-page-state evidence handed to the judge -- a full snapshot
can be enormous, and the judge only needs enough to corroborate completion."""

_JUDGE_SYSTEM = (
    "You are a strict, skeptical verifier of a web agent's work. The agent has "
    "reported that it completed a task. Be initially doubtful of that claim: treat "
    "the observed page state and action history as ground truth, not the agent's "
    "self-report. Decide whether the task was actually accomplished. Respond with a "
    'JSON object {"passed": boolean, "reason": string}. Set passed=true only if the '
    "evidence genuinely supports completion; otherwise passed=false with a concrete "
    "reason."
)

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"passed": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["passed", "reason"],
}


@dataclass
class JudgeVerdict:
    passed: bool
    reason: str
    errored: bool = False
    """True when the judge call itself failed -- the caller must fail open
    (keep the agent's verdict) rather than treat this as a rejection."""


async def judge_completion(
    *,
    task: str,
    claimed_result: str,
    extracted_data: dict[str, Any] | None,
    page_state: str,
    history_summary: str,
    config: LLMConfig,
) -> JudgeVerdict:
    """One skeptical verification call. Returns a fail-open verdict
    (`passed=True, errored=True`) if the LLM call raises."""

    user = "\n\n".join(
        [
            f"<task>\n{task}\n</task>",
            f"<agent_claimed_result>\n{claimed_result}\n</agent_claimed_result>",
            f"<agent_extracted_data>\n{json.dumps(extracted_data, default=str)}\n"
            "</agent_extracted_data>",
            f"<final_page_state>\n{page_state[:_PAGE_STATE_CAP]}\n</final_page_state>",
            f"<action_history>\n{history_summary}\n</action_history>",
        ]
    )
    try:
        raw = await chat_json(_JUDGE_SYSTEM, user, config=config, json_schema=_JUDGE_SCHEMA)
    except Exception as exc:
        return JudgeVerdict(passed=True, reason=f"judge unavailable: {exc}", errored=True)
    return JudgeVerdict(passed=bool(raw.get("passed", True)), reason=str(raw.get("reason", "")))
