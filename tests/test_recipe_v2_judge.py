"""`agentpilot.recipe.v2.judge` -- is the collected value the RIGHT value?

The direction of every default here is deliberate and is the thing under test:
this gate stands in front of a human's attention, so a false rejection costs a
person's time for nothing while a false acceptance costs only what the human
was going to catch anyway. It leans toward accepting.
"""

from __future__ import annotations

from agentpilot.recipe.v2 import judge as judge_mod
from agentpilot.recipe.v2.judge import (
    DataVerdict,
    FieldVerdict,
    build_user_message,
    judge_collection,
    parse_verdict,
)
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec

FIELDS = {
    "name": FieldSpec(name="name", description="the product title"),
    "price": FieldSpec(name="price", type=TypeSpec(kind="scalar", value_type="price")),
}


# --- parsing ----------------------------------------------------------------


def test_a_rejection_carries_a_reason_that_can_be_acted_on() -> None:
    verdict = parse_verdict(
        {"fields": [{
            "field": "name", "ok": False,
            "reason": "this is the breadcrumb trail; the product title is in the h1 below it",
        }]},
        FIELDS,
    )
    assert verdict.passed is False
    assert "breadcrumb" in verdict.rejected["name"]


def test_a_rejection_with_no_reason_is_not_acted_on() -> None:
    """The reason IS the repair instruction -- it is fed to `propose_locators`
    as `failures`. A bare "wrong" tells the next round nothing, so acting on it
    would burn a repair budget re-asking the identical question."""

    verdict = parse_verdict({"fields": [{"field": "name", "ok": False}]}, FIELDS)
    assert verdict.passed is True
    assert verdict.rejected == {}


def test_a_field_that_was_not_asked_about_is_ignored() -> None:
    verdict = parse_verdict(
        {"fields": [{"field": "invented", "ok": False, "reason": "x"}]}, FIELDS
    )
    assert verdict.verdicts == {}
    assert verdict.passed is True


def test_a_malformed_reply_passes_rather_than_blocking() -> None:
    """Rejecting on a malformed reply sends a working recipe to a human with
    nothing to tell them -- the cost of a false positive and none of the
    benefit."""

    assert parse_verdict({}, FIELDS).passed is True
    assert parse_verdict({"fields": ["nonsense"]}, FIELDS).passed is True


# --- the prompt -------------------------------------------------------------


def test_the_page_text_is_included_as_ground_truth() -> None:
    """Handed only the values, a model has nothing to check them against and
    will ratify whatever it is shown."""

    message = build_user_message(
        FIELDS, data={"name": "Ribbed top"}, page_text="RIBBED TOP  Rs 2,290"
    )
    assert "RIBBED TOP" in message
    assert "Ribbed top" in message


def test_the_winning_source_is_shown_with_each_value() -> None:
    """Which source a value came from is a real signal about how it could be
    wrong: `hydration` picking up a recommended product is a different mistake
    from a css selector drifting one element left."""

    message = build_user_message(
        FIELDS,
        data={"name": "X"},
        page_text="",
        provenance={"name": {"source": "hydration"}},
    )
    assert "hydration" in message


def test_a_huge_value_is_truncated_and_says_so() -> None:
    message = build_user_message(
        FIELDS, data={"name": "x" * 5000}, page_text=""
    )
    assert "truncated" in message
    assert len(message) < 5000


def test_the_prompt_forbids_grading_and_demands_a_statement() -> None:
    assert "not that it is wrong" in judge_mod._SYSTEM_PROMPT
    assert "ground truth" in judge_mod._SYSTEM_PROMPT


# --- the call ---------------------------------------------------------------


async def test_a_judge_outage_fails_open(monkeypatch) -> None:
    """A judge outage must never fabricate a failure. Here that matters twice
    over: failing closed would park the run waiting on a human for a question
    the judge could not even ask."""

    async def boom(*a, **k):
        raise RuntimeError("503 from the model")

    monkeypatch.setattr(judge_mod, "chat_json_conversation", boom)

    verdict = await judge_collection(
        FIELDS, data={"name": "X"}, page_text="X", llm_config=None
    )
    assert verdict.passed is True
    assert verdict.errored is True
    assert "unavailable" in verdict.verdicts["name"].reason


async def test_nothing_collected_is_not_a_correctness_failure(monkeypatch) -> None:
    """Absence is a completeness problem and is reported elsewhere. Judging it
    here would put the wrong question to the human: the field was never found,
    not found-and-wrong."""

    called = False

    async def spy(*a, **k):
        nonlocal called
        called = True
        return {"fields": []}

    monkeypatch.setattr(judge_mod, "chat_json_conversation", spy)

    verdict = await judge_collection(
        FIELDS, data={"name": None, "price": ""}, page_text="x", llm_config=None
    )
    assert verdict.passed is True
    assert called is False


async def test_only_fields_with_a_value_are_judged(monkeypatch) -> None:
    seen: dict = {}

    async def spy(messages, **k):
        seen["user"] = messages[1]["content"]
        return {"fields": [{"field": "name", "ok": True}]}

    monkeypatch.setattr(judge_mod, "chat_json_conversation", spy)

    await judge_collection(
        FIELDS, data={"name": "Ribbed top", "price": None}, page_text="p", llm_config=None
    )
    assert "name" in seen["user"]
    assert "- price =" not in seen["user"]


# --- the verdict object -----------------------------------------------------


def test_rejected_is_the_failures_map_a_repair_round_feeds_back() -> None:
    verdict = DataVerdict(
        passed=False,
        verdicts={
            "name": FieldVerdict("name", False, "it is the breadcrumb"),
            "price": FieldVerdict("price", True, ""),
        },
    )
    assert verdict.rejected == {"name": "it is the breadcrumb"}


# --- absence, as distinct from being wrong -----------------------------------


def test_absence_is_parsed_and_kept_out_of_the_repair_list() -> None:
    """The distinction the loop turned on. A rejection sends the selector agent
    back for a better locator; absence has to tell it to stop looking, or it
    returns a different wrong element for ever."""

    verdict = parse_verdict(
        {"fields": [
            {"field": "name", "ok": False, "reason": "that is the breadcrumb"},
            {"field": "price", "ok": False, "absent": True,
             "reason": "this page shows no price at all"},
        ]},
        FIELDS,
    )
    assert verdict.rejected == {"name": "that is the breadcrumb"}
    assert verdict.absent == {"price": "this page shows no price at all"}
    assert verdict.passed is False


def test_absent_only_counts_on_a_rejection() -> None:
    """Present and absent at once is a confused reply, and the safe reading is
    that the value stands."""

    verdict = parse_verdict(
        {"fields": [{"field": "name", "ok": True, "absent": True}]}, FIELDS
    )
    assert verdict.absent == {}
    assert verdict.passed is True


def test_an_absent_claim_with_no_reason_is_not_acted_on() -> None:
    """Same rule as any rejection: the reason IS what the next step uses, and
    dropping a field on an unexplained claim is worse than keeping it."""

    verdict = parse_verdict(
        {"fields": [{"field": "name", "ok": False, "absent": True}]}, FIELDS
    )
    assert verdict.absent == {}
    assert verdict.rejected == {}


def test_the_prompt_teaches_the_difference() -> None:
    assert "absent=true" in judge_mod._SYSTEM_PROMPT
    assert "tells it to stop looking" in judge_mod._SYSTEM_PROMPT
