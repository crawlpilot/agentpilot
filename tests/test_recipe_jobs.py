"""Extraction jobs -- the runtime half of the recipe marketplace.

`GET /v1/recipes/templates` is how a caller finds a scraper somebody else
built; `POST /v1/recipes/{id}/jobs` is how they apply it to their own URLs.
This covers the parts that need no database: how a submission is cleaned up
before it costs a browser session each, and how N run outcomes roll up into
one answer about the batch.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agentpilot.gateway.routes.recipes import _normalize_urls
from agentpilot.jobs.recipe_store import RecipeJobOut


def _job(*, total: int, queued: int = 0, running: int = 0, completed: int = 0,
         failed: int = 0) -> RecipeJobOut:
    return RecipeJobOut(
        job_id="j", recipe_id="r", recipe_name="Zara PDP", tenant="t",
        recipe_version=3, total=total, queued=queued, running=running,
        completed=completed, failed=failed,
        created_at=datetime.now(UTC), finished_at=None,
    )


# --- what a submission is reduced to ----------------------------------------


def test_blank_and_whitespace_urls_are_dropped() -> None:
    assert _normalize_urls(["  https://a.test/1  ", "", "   ", "https://a.test/2"]) == [
        "https://a.test/1",
        "https://a.test/2",
    ]


def test_duplicates_are_dropped_keeping_submission_order() -> None:
    """A caller pasting from a spreadsheet repeats URLs routinely, and each
    duplicate would otherwise cost a full browser session to produce a row
    identical to one already in the job."""

    urls = ["https://a.test/2", "https://a.test/1", "https://a.test/2", " https://a.test/1 "]
    assert _normalize_urls(urls) == ["https://a.test/2", "https://a.test/1"]


def test_a_submission_of_only_blanks_reduces_to_nothing() -> None:
    # The route turns this into a 422 rather than an empty job that reports
    # `completed` having done nothing.
    assert _normalize_urls(["", "  ", "\t"]) == []


# --- how N runs become one answer -------------------------------------------


def test_a_job_is_running_while_any_url_is_outstanding() -> None:
    assert _job(total=3, queued=1, completed=2).status == "running"
    assert _job(total=3, running=1, completed=1, failed=1).status == "running"


def test_some_failing_is_partial_not_failed() -> None:
    """`partial` is a real outcome for a batch, not a rounding of `failed`.

    Nine of ten URLs yielding is a useful result and a caller can act on it;
    reporting that as `failed` would throw away nine pages of data on the
    strength of one bad URL.
    """

    assert _job(total=10, completed=9, failed=1).status == "partial"


def test_every_url_failing_is_a_failed_job() -> None:
    assert _job(total=4, failed=4).status == "failed"


def test_all_completed_is_completed() -> None:
    assert _job(total=4, completed=4).status == "completed"
