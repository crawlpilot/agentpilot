"""The closed set of driver-level errors, and how each one crosses a network.

Each class carries its own `code` (the stable string a client matches on) and
`http_status`. They used to live in a 14-entry table in
`agentpilot.gateway.errors` instead, which put the taxonomy on the *server* side
of a distribution boundary: adding an exception meant editing the class here,
the `ErrorCode` enum there, the mapping beside it, and -- once a client existed
-- a fourth copy of the inverse mapping that the client could not import from
`agentpilot` at all.

Carrying it on the class removes all four edits but one. The exception is the
single declaration, both directions are derived from it (`code_of` /
`error_for_code`), and the two tables that could disagree stop existing. What
stays in `gateway.errors` is what is genuinely HTTP: `Retry-After`, the
response envelope, and handler registration.
"""

from __future__ import annotations

from typing import ClassVar


class DriverError(Exception):
    """Base class for all crawlpilot.spi driver errors.

    The defaults are what an unclassified failure gets, and are deliberately the
    pessimistic pair: a 500 says "this was our fault and you should not simply
    retry it", which is the right thing to say about an error nobody has
    classified yet.
    """

    code: ClassVar[str] = "INTERNAL_ERROR"
    """The stable, wire-visible identifier. A client matches on this, so it is
    part of the published contract -- renaming one is a breaking change even
    though the class name is not."""

    http_status: ClassVar[int] = 500

    retry_after_seconds: ClassVar[int | None] = None
    """Set on the errors where retrying is the *expected* response rather than a
    gamble -- a busy fleet and a contended lease both resolve on their own. It
    becomes a `Retry-After` header, so it lives with the error rather than in a
    separate tuple the server keeps in step by hand."""


class NavigationTimeout(DriverError):
    code: ClassVar[str] = "NAVIGATION_TIMEOUT"
    http_status: ClassVar[int] = 504


class ContextCrashed(DriverError):
    code: ClassVar[str] = "CONTEXT_CRASHED"
    http_status: ClassVar[int] = 500


class ChallengeDetected(DriverError):
    """A hard bot wall (CAPTCHA / robot check / Access Denied) was detected on
    the navigated page. Carries the classifier `verdict` (a `block_detect.Verdict`
    value, as a plain string to keep `spi` free of a `driver` import), its
    `weight` for the session layer's burn accounting, and its `scope`
    (`"privacy"` -- the only scope that raises; soft CRAWL-scope verdicts are
    surfaced on `ActionResult` instead of raised). The driver computes all three
    since it owns the classifier. `scope` defaults to `"privacy"` so existing
    call sites (and tests) that raise a bare wall keep full-rotation semantics."""

    code: ClassVar[str] = "CHALLENGE_UNRESOLVED"
    http_status: ClassVar[int] = 422

    def __init__(
        self,
        message: str,
        *,
        verdict: str | None = None,
        weight: int = 0,
        scope: str = "privacy",
    ) -> None:
        super().__init__(message)
        self.verdict = verdict
        self.weight = weight
        self.scope = scope


class StaleRefError(DriverError):
    """A ref does not resolve against the current capture.

    Refs are minted by a snapshot and dropped on the next snapshot or
    navigation, so the overwhelmingly common cause is acting on a ref taken
    before the page changed. The answer is always the same -- snapshot again and
    use a fresh ref -- which is why the message says that rather than
    speculating about the cause.

    It used to carry an `epoch_superseded` flag meant to distinguish "the
    capture that minted this is gone" from "this ref was never real". All three
    raise sites passed `False`, and the counter that would have answered the
    question was incremented in six places and read in none, so the flag only
    ever produced one branch of its own message. browser-use, whose ref model
    this is (`browser/session.py`), keeps no such distinction either:
    `get_dom_element_by_index` returns the node or `None`, and the caller says
    "page may have changed. Try refreshing browser state." One actionable
    sentence beats two that a caller cannot act on differently.
    """

    code: ClassVar[str] = "STALE_REF"
    http_status: ClassVar[int] = 409

    def __init__(self, ref: str) -> None:
        super().__init__(
            f"ref {ref!r} is not available -- the page may have changed. "
            "Take a fresh snapshot and use a ref from it."
        )
        self.ref = ref


class TabNotFound(DriverError):
    """A `page_id`/tab reference (execute's `page_id` arg, or
    `SwitchTabAction`/`CloseTabAction`'s) that doesn't resolve to a currently
    tracked tab -- already closed, or never existed. Same NOT_FOUND family as
    "no such session", not a distinct `ErrorCode` (see `gateway/errors.py`'s
    mapping)."""

    code: ClassVar[str] = "NOT_FOUND"
    http_status: ClassVar[int] = 404

    def __init__(self, page_id: str) -> None:
        super().__init__(f"no such tab {page_id!r}")
        self.page_id = page_id


class LeaseConflict(DriverError):
    code: ClassVar[str] = "SESSION_LEASE_CONFLICT"
    http_status: ClassVar[int] = 409
    retry_after_seconds: ClassVar[int | None] = 5


class NodeLost(DriverError):
    code: ClassVar[str] = "NODE_LOST"
    http_status: ClassVar[int] = 502


class CapacityExhausted(DriverError):
    code: ClassVar[str] = "CAPACITY_EXHAUSTED"
    http_status: ClassVar[int] = 503
    retry_after_seconds: ClassVar[int | None] = 5


class EgressBlocked(DriverError):
    code: ClassVar[str] = "EGRESS_BLOCKED"
    http_status: ClassVar[int] = 403


class CdpNotAvailable(DriverError):
    """No CDP endpoint exists for this session: either the deployed driver
    doesn't implement `CdpEndpointCapable` at all, or this session's
    underlying context wasn't opened with `enable_cdp=True`. Same "this
    specific thing isn't available" family as `TabNotFound`/`StaleRefError`,
    not a server fault."""

    code: ClassVar[str] = "CDP_NOT_AVAILABLE"
    http_status: ClassVar[int] = 409


class JobNotFound(DriverError):
    """No `crawl`/`batch_scrape` job with this id, or it exists but belongs
    to a different tenant -- same NOT_FOUND family as `TabNotFound`, and
    deliberately not distinguished from "wrong tenant" in the response, for
    the same reason `gateway/routes/sessions.py` doesn't leak session
    existence across tenants."""

    code: ClassVar[str] = "JOB_NOT_FOUND"
    http_status: ClassVar[int] = 404

    def __init__(self, job_id: str) -> None:
        super().__init__(f"no such job {job_id!r}")
        self.job_id = job_id


class JobCancelled(DriverError):
    """Raised when an operation (e.g. claiming a task) discovers its job was
    cancelled after the operation started -- distinct from `JobNotFound`
    (the job exists, it just won't accept further work)."""

    code: ClassVar[str] = "JOB_CANCELLED"
    http_status: ClassVar[int] = 409

    def __init__(self, job_id: str) -> None:
        super().__init__(f"job {job_id!r} was cancelled")
        self.job_id = job_id


class NoDialogOpen(DriverError):
    """A dialog verb was called while no JavaScript dialog was open.

    Its own type rather than a bare error so a caller can turn it into a
    "nothing to answer" observation: calling `dialog_dismiss` speculatively,
    without knowing whether the last click raised a `confirm()`, is a reasonable
    thing for an agent to do and is not a failure.
    """

    def __init__(self) -> None:
        super().__init__("no JavaScript dialog is currently open on this page")


class SelectorNotFound(DriverError):
    """A CSS selector scoping a snapshot matched no element.

    Raised rather than scoping the observation to the empty set: a snapshot that
    came back blank because of a typo in the selector should say so, not look
    like an empty page.
    """

    def __init__(self, selector: str) -> None:
        self.selector = selector
        super().__init__(f"selector {selector!r} matched no element on this page")


class WaitTimeout(DriverError):
    """A `wait_for_*` condition did not come true in time.

    Raised rather than reported, because a wait that silently gave up is worse
    than no wait: the next action in the batch would run against the state the
    caller was waiting *not* to see, and report success.
    """

    def __init__(self, condition: str, timeout_ms: int) -> None:
        self.condition = condition
        self.timeout_ms = timeout_ms
        super().__init__(f"timed out after {timeout_ms}ms waiting for {condition}")


# ------------------------------------------------------------- both directions
#
# Derived from the classes above, never written out. A table would be a second
# declaration to keep in step -- exactly what carrying `code` on the class
# removes.


def all_error_types() -> list[type[DriverError]]:
    """Every `DriverError` subclass, depth-first.

    Public because the taxonomy is worth enumerating -- a client listing what it
    can catch, a test asserting the wire vocabulary has not drifted. Walks
    `__subclasses__` rather
    than a registry so a class declared anywhere is found without registering
    itself, and so this cannot fall out of step with the module."""

    found: list[type[DriverError]] = []
    stack: list[type[DriverError]] = [DriverError]
    while stack:
        cls = stack.pop()
        if cls is not DriverError:
            found.append(cls)
        stack.extend(cls.__subclasses__())
    return found


def code_of(error: DriverError | type[DriverError]) -> str:
    """The wire code for an error or its class."""

    cls = error if isinstance(error, type) else type(error)
    return cls.code


def error_for_code(code: str) -> type[DriverError]:
    """The exception class a wire code names -- the direction a *client* needs.

    Only classes that **declare** a `code` of their own are candidates. Matching
    on the inherited attribute instead would resolve `"INTERNAL_ERROR"` to
    whichever unclassified subclass happened to be walked first -- `WaitTimeout`,
    say -- and hand a caller an exception type that has nothing to do with what
    went wrong. Unclassified errors are meant to land on `DriverError`, and
    checking `__dict__` is what makes "declares a code" the actual question.

    Falls back to `DriverError` for a code this build has never heard of, which
    is what lets a client one release behind its server still raise something
    catchable rather than crashing on an unknown string. The message carries the
    server's own text either way.

    Ambiguity between two classes that declare the *same* code is real and
    deliberate: `TabNotFound` and any future `NOT_FOUND` share one, so the first
    match wins. A client that needs to tell them apart is asking the wire a
    question it was never designed to answer -- the codes are a closed
    vocabulary for *handling*, not a class serializer.
    """

    for cls in all_error_types():
        if cls.__dict__.get("code") == code:
            return cls
    return DriverError


def error_from_wire(code: str, message: str) -> DriverError:
    """Rebuild an exception from a `{code, error}` envelope.

    Constructed via `__new__`, deliberately: several of these classes take
    structured arguments (`StaleRefError(ref, epoch_superseded=...)`,
    `WaitTimeout(condition, timeout_ms)`) that the wire does not carry, so
    calling `__init__` would raise a `TypeError` about a missing argument
    instead of the error the server actually reported -- turning a clean
    "your ref went stale" into a confusing client-side crash.

    So the caller gets the right *type* to catch and the server's own message,
    without pretending the structured fields survived the trip. They did not:
    the envelope is `{code, error, details}`.
    """

    cls = error_for_code(code)
    error = cls.__new__(cls)
    Exception.__init__(error, message)
    return error


def raise_for_code(code: str, message: str) -> None:
    """Raise what a `{code, error}` envelope describes. See `error_from_wire`."""

    raise error_from_wire(code, message)
