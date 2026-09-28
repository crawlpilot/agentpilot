"""The recipe-run processing loop: claims queued `recipe_runs` rows and
dispatches each to `agentpilot.recipe.v2`. Folded into the existing
`AGENTPILOT_ROLE=worker` process next to `CrawlWorkerLoop`/`AgentWorkerLoop`,
same claim/lock/retry shape. `codegen` needs no browser session at all (a pure
LLM call over the already-built document); every other kind opens one
`InteractiveSession` per run, mirroring `AgentWorkerLoop`.

**One engine.** `build`, `heal` and `onboard` are all "work out where these
fields live on this page", so they are one code path that differs only in
whether a document already exists to take the field list from. `replay` and
`codegen` read that document.

The v1 engine this loop used to dispatch to is gone. It read `global_setup`
and `field_groups` -- columns nothing has written since the studio shipped, and
which `_process_onboard` writes as `[]` on purpose. Sending a real recipe to it
replayed zero field groups and reported success with `data = {}`, so every
"the recipe returns nulls" report traced back to that one branch.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

from agentpilot.jobs.recipe_store import ClaimedRecipeRun, PostgresRecipeStore
from agentpilot.llm.client import LLMConfig
from agentpilot.observability.metrics import runs_deferred_total, runs_timed_out_total
from agentpilot.recipe.config import RecipeConfig
from agentpilot.recipe.v2.onboard import DEFAULT_ONBOARD_MAX_STEPS
from crawlpilot.config import DEFAULTS, BrowserConfig
from crawlpilot.identity.proxy_pinning import ProxyPinner
from crawlpilot.policy import NullPrototypes, PrototypeProvider
from crawlpilot.session.interactive import (
    InteractiveSession,
    open_interactive_session,
    release_interactive_session,
)
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi.driver import BrowserDriver
from crawlpilot.spi.errors import LeaseConflict, NodeAtCapacity

if TYPE_CHECKING:
    from agentpilot.placement.placer import SessionPlacer

log = structlog.get_logger(__name__)

_NO_PROTOTYPES = NullPrototypes()


def _domain_from_url(url: str) -> str:
    return urlparse(url).hostname or url


# How many warm identities a recipe run may cycle between, per (tenant, domain).
#
# The identity is `{tenant}/{domain}/{name}`, and `name` decides whether a run
# gets a *warm* browser profile or a brand-new one. This loop used to pass
# `recipe-run-{run_id}` -- unique per run -- which meant every single run opened
# a cookie-less, history-less, first-visit Chrome behind a freshly picked proxy.
#
# That is not a neutral default, it is the thing WAFs look for.
# `session/ephemeral.py` says so outright: a throwaway identity is "a
# cookie-less, first-visit browser every time, which is itself a bot signal to
# WAFs like Akamai" -- which is what Walmart and Zara run. It also leaked a
# profile directory per run, forever, at ~90 MB of Chrome profile each -- so a
# single 500-URL marketplace job would have left 45 GB behind it.
#
# A *single* stable name would warm perfectly and then serialise everything --
# `Registry.acquire` raises `LeaseConflict` when an identity already holds an
# active lease, so two concurrent runs on the same domain would collide. A
# small pool is the middle: each slot is reused across runs (warm profile, warm
# Chrome, sticky proxy), and there are enough slots that concurrent runs get
# their own. Sized above `max_concurrent` so the common case never contends.
_IDENTITY_SLOTS = 8


class RecipeWorkerLoop:
    def __init__(
        self,
        store: PostgresRecipeStore,
        registry: RegistryProtocol,
        driver: BrowserDriver,
        profiles_root: Path,
        proxy_pinner: ProxyPinner | None,
        *,
        lease_ttl_seconds: float = 300.0,
        batch_size: int = 5,
        max_concurrent: int = 3,
        poll_interval_seconds: float = 2.0,
        stale_after_seconds: float = 120.0,
        build_max_steps: int = DEFAULT_ONBOARD_MAX_STEPS,
        browser_config: BrowserConfig = DEFAULTS,
        prototype_provider: PrototypeProvider = _NO_PROTOTYPES,
        assist_poll_seconds: float = 3.0,
        sessions: dict[str, Any] | None = None,
        placer: SessionPlacer | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._driver = driver
        self._profiles_root = profiles_root
        self._browser_config = browser_config
        self._prototype_provider = prototype_provider
        self._proxy_pinner = proxy_pinner
        self._lease_ttl_seconds = lease_ttl_seconds
        self._batch_size = batch_size
        self._max_concurrent = max_concurrent
        self._poll_interval_seconds = poll_interval_seconds
        self._stale_after_seconds = stale_after_seconds
        self._build_max_steps = build_max_steps
        # Live-view plumbing, the same pair `AgentWorkerLoop` uses.
        #
        # A worker opens its browser in-process, which never goes through the
        # gateway's placer -- so the session is invisible to `/v1/sessions`
        # and the live-view proxy has no route to it. For an agent run that is
        # a missing nicety; for an onboarding build it is the difference
        # between watching the page and watching a spinner, and the assist
        # loop's whole premise is that a person can pick on the live page.
        #
        # `sessions` is the in-process dict this worker's own live-view route
        # reads; `placer` writes the redis route so a gateway process can
        # proxy through to here.
        self._sessions = sessions if sessions is not None else {}
        self._placer = placer
        # run_id -> (live_session_id, session, tier), so `_heartbeat` can keep
        # the redis route alive for runs that outlive its TTL. See `_heartbeat`.
        self._live_routes: dict[str, tuple[str, Any, str]] = {}
        self._deadlines: dict[str, float] = {}
        """run_id -> monotonic instant past which this run is presumed wedged.

        Shared with `_heartbeat` so the two halves of the same policy cannot
        drift: the deadline stops the run, and the heartbeat stops *vouching*
        for it. Either alone leaves a hole -- see `_heartbeat`."""
        # How often a parked run checks whether a person has answered. The
        # answer is written by the gateway, not passed in memory, so this is a
        # database poll -- cheap, and a few seconds of latency is nothing next
        # to how long a person takes to look at a page.
        self._assist_poll_seconds = assist_poll_seconds
        self._task: asyncio.Task[None] | None = None
        # Runs this worker is currently processing. Capacity is counted from here
        # rather than from a per-tick semaphore, because the tick no longer waits
        # for the work it started -- see `tick`.
        self._inflight: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        # The tick no longer awaits what it claimed, so shutting down the loop
        # alone would leave runs executing against a closing process.
        for task in tuple(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*tuple(self._inflight), return_exceptions=True)
        self._inflight.clear()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("recipe_worker_loop.tick_failed")
            await asyncio.sleep(self._poll_interval_seconds)

    async def tick(self) -> None:
        """Reclaim, then claim up to remaining capacity. Does not wait for work.

        **The claim loop must not be blocked by the work it claimed.** It used to
        `await asyncio.gather(...)` over the whole batch, which made every tick a
        barrier: `_run` could not come round again until the slowest run in the
        batch finished. `max_concurrent` limited parallelism *inside* a tick and
        did nothing across them, so a worker that claimed one long run claimed
        nothing else for that run's entire duration.

        A build is up to 15 agent steps, and a run parked for a person waits up
        to `assist_timeout_s` -- 1800s by default. One of those is enough to stop
        a worker picking up anything at all, which is how a queued build sat
        untouched while two workers idled inside a park.

        Capacity is now enforced where the work is taken on rather than after:
        only as many runs are claimed as there are free slots, so a row is left
        `queued` for another worker instead of being locked by one that has no
        room for it.
        """

        await self._store.reclaim_stale_runs(self._stale_after_seconds)
        # A live worker resumes its own park on time. A row still parked well
        # past its deadline means the process holding it died -- and with it the
        # browser session the park existed to keep open, so there is nothing to
        # resume into.
        await self._store.reclaim_expired_parks()

        free = self._max_concurrent - len(self._inflight)
        if free <= 0:
            return
        claimed = await self._store.claim_runs_batch(min(self._batch_size, free))
        for run in claimed:
            task = asyncio.create_task(self._process(run))
            self._inflight.add(task)
            # Discard on completion, so capacity is released the moment a run
            # ends rather than at the next tick.
            task.add_done_callback(self._inflight.discard)

    async def drain(self) -> None:
        """Wait for everything in flight. For tests and shutdown, not the loop."""

        while self._inflight:
            await asyncio.gather(*tuple(self._inflight), return_exceptions=True)

    async def _process(self, run: ClaimedRecipeRun) -> None:
        # Keep this run's lease fresh for its whole (potentially long) duration
        # so `reclaim_stale_runs` can't hand it to a second worker -- a build is
        # up to 15 agent steps, well past `stale_after`.
        # The deadline is shared with the heartbeat rather than held here, so
        # the two cannot disagree about when this run stopped being healthy.
        cfg = RecipeConfig.from_env()
        budget = cfg.replay_deadline_s if run.kind == "replay" else cfg.build_deadline_s
        deadline = time.monotonic() + budget
        self._deadlines[run.run_id] = deadline

        heartbeat = asyncio.create_task(self._heartbeat(run))
        try:
            # A wall clock over the whole run, because a hung await has no other
            # end. MEASURED: a Walgreens replay crashed its renderer ten seconds
            # in and sat `running` indefinitely -- no error, no step trace -- with
            # no timeout anywhere between `page.content()` and here.
            await asyncio.wait_for(self._process_run(run), timeout=budget)
        except TimeoutError:
            log.warning(
                "recipe_worker_loop.run_deadline_exceeded",
                run_id=run.run_id, kind=run.kind, budget_s=budget,
            )
            runs_timed_out_total.inc()
            await self._store.fail_run(
                run.run_id,
                run.lock,
                f"run exceeded its {budget:.0f}s deadline and was stopped; the "
                "page or a model call stopped responding",
            )
        except NodeAtCapacity as exc:
            # Nothing is wrong with this run: the node was full at the instant it
            # was claimed, and the usual reason is a neighbouring build that is
            # about to finish and hand back the slot. Deferring costs a delay;
            # failing costs the run.
            #
            # Deliberately BEFORE the `Exception` arm, because `NodeAtCapacity`
            # is an ordinary exception and would otherwise be failed like any
            # other -- which is exactly what happened to three Walgreens
            # onboards, each dead within 0.1s of being claimed.
            delay = float(exc.retry_after_seconds or 5)
            log.info(
                "recipe_worker_loop.run_deferred",
                run_id=run.run_id, retry_after_s=delay, reason=str(exc),
            )
            runs_deferred_total.inc()
            await self._store.defer_run(run.run_id, run.lock, str(exc), delay)
        except Exception as exc:
            log.warning("recipe_worker_loop.run_failed", run_id=run.run_id, error=str(exc))
            await self._store.fail_run(run.run_id, run.lock, str(exc))
        finally:
            self._deadlines.pop(run.run_id, None)
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, run: ClaimedRecipeRun) -> None:
        """Keep this run's claim lock *and* its live-view route fresh.

        The two used to be one line, and the missing half broke the assist
        panel outright. `commit_route` stamps `session:{id}` with
        `lease_ttl_seconds` -- 300s by default -- and the only other thing that
        refreshes it is a *successful* execute through the gateway proxy. A
        build runs for minutes and then parks for up to `assist_timeout_s`
        (1800s), so the route lapsed long before anybody opened the panel;
        `resolve_route` then 404s every picker injection, and because the TTL is
        only extended after a successful execute, nothing can revive it. The
        session still appears in `/v1/sessions` -- that listing fans out to the
        workers' in-process dicts and never consults the route -- so it looked
        healthy and silently refused to pick.

        `AgentWorkerLoop` re-publishes on every agent step for exactly this
        reason; a recipe run has no comparable step boundary during a park, so
        the heartbeat is the cadence. `stale_after/3` (40s by default) is
        comfortably inside any sane TTL.
        """

        interval = max(self._stale_after_seconds / 3.0, 1.0)
        while True:
            await asyncio.sleep(interval)
            deadline = self._deadlines.get(run.run_id)
            if deadline is not None and time.monotonic() > deadline:
                # Past its budget, so stop renewing -- and keep not renewing.
                #
                # This is the half that makes the deadline reliable. `wait_for`
                # cancels the run, but cancellation lands on an `await` that has
                # to be reachable, and the failure this exists for is precisely
                # an await that never returns. If the cancel wedges too, the
                # heartbeat is the only thing left, and while it keeps renewing
                # `locked_at` the run is invisible to `reclaim_stale_runs`:
                #
                #   WHERE status = 'running' AND locked_at < now() - stale_after
                #
                # A heartbeat that renews unconditionally proves the WORKER is
                # alive, which it is, and says nothing about the RUN, which is
                # not. Measured on a Walgreens replay: eight minutes `running`,
                # heartbeat renewing every 40s, so the reclaim it needed could
                # never fire and nothing would ever have freed it.
                log.warning(
                    "recipe_worker_loop.heartbeat_withdrawn",
                    run_id=run.run_id,
                    reason="run is past its deadline; letting the lock go stale",
                )
                return
            try:
                await self._store.renew_lock(run.run_id, run.lock)
            except Exception:
                log.warning("recipe_worker_loop.renew_failed", run_id=run.run_id)
            route = self._live_routes.get(run.run_id)
            if route is not None:
                # Already best-effort and idempotent; a redis hiccup costs the
                # live view, never the run.
                await self._publish_live_route(*route)
                # And the session lease, which nothing else renews on its own.
                #
                # `execute_on_session` renews it before every dispatch, so the
                # lease tracks PAGE activity -- and a build spends most of its
                # time in model calls, not page calls. One Bedrock request that
                # stalled for 127 seconds, inside a `_look` batch of several,
                # was enough to leave the lease un-renewed past its 300s TTL.
                # The reaper then force-released it (it cannot distinguish a
                # slow owner from a crashed one), and every observation after
                # that failed with "lease was reclaimed" until the run aborted.
                #
                # The heartbeat is the right cadence for the same reason it is
                # for the route: it is the only thing in the run that ticks
                # independently of what the run happens to be waiting on.
                await self._renew_session_lease(run, route[1])

    async def _process_run(self, run: ClaimedRecipeRun) -> None:
        if run.kind == "codegen":
            await self._process_codegen(run)
            return

        if run.kind == "heal":
            cfg = RecipeConfig.from_env()
            if run.recipe.heal_attempts >= cfg.max_heal_attempts:
                # Exhausted the heal budget -- stop burning LLM/browser cycles;
                # only a fresh build (which resets the streak) recovers it.
                await self._store.mark_recipe_broken(run.recipe_id)
                await self._store.complete_run(
                    run.run_id,
                    run.lock,
                    data=None,
                    field_failures=None,
                    error=(
                        f"max heal attempts ({cfg.max_heal_attempts}) exhausted; "
                        "recipe marked broken, rebuild required"
                    ),
                )
                return

        # Every remaining kind needs a page, and the page needs a domain to
        # open the session against. A recipe with no `target.match` has an
        # empty `url_pattern`, which is legitimate -- it is applied to URLs a
        # caller submits -- but leaves a *scheduled* replay with nowhere to go.
        # `open_interactive_session` rejects the empty domain with "unsafe
        # identity path segment", which is true and says nothing about the
        # actual problem, so it is caught here where the cause is known.
        target_url = run.url or run.recipe.url_pattern
        if not target_url:
            await self._store.complete_run(
                run.run_id,
                run.lock,
                data=None,
                field_failures=None,
                error=(
                    "onboarding needs a URL to build the recipe against"
                    if run.kind == "onboard"
                    else "this recipe has no URL of its own to run against; submit URLs "
                    "to it as a job, or give it a target matcher to make it schedulable"
                ),
            )
            return

        # For a submitted run, the domain is the *submitted URL's* -- the
        # profile, the proxy pin and the cookie jar are all domain-scoped, so
        # taking them from the recipe's own `url_pattern` would open a session
        # pinned to a site this run never visits.
        session = await self._open_warm_session(run, _domain_from_url(target_url))
        # Named after the run, which is how the UI finds it among whatever else
        # is open -- and how the assist panel points a person at the page this
        # run is stuck on.
        live_session_id = f"recipe-run-{run.run_id}"
        await self._publish_live_route(live_session_id, session, "auto")
        # Hand the route to `_heartbeat`, which re-commits it for as long as the
        # run lives. Publishing once is not enough: the route's TTL is shorter
        # than a build, let alone a park. See `_heartbeat`.
        self._live_routes[run.run_id] = (live_session_id, session, "auto")
        try:
            # `build` and `heal` are both "work out where these fields live on
            # this page", which is what onboarding does -- so they are the same
            # code path, differing only in whether a document already exists to
            # take the field list from. The v1 engine that used to serve them
            # read `field_groups`, a column nothing has written since the studio
            # shipped.
            if run.kind in ("onboard", "build", "heal"):
                await self._process_onboard(run, session, target_url)
            elif run.kind == "replay":
                await self._process_replay(run, session, target_url)
            else:
                raise AssertionError(f"unhandled recipe run kind: {run.kind!r}")
        finally:
            self._live_routes.pop(run.run_id, None)
            await self._forget_live_route(live_session_id, session)
            try:
                await release_interactive_session(
                    session, registry=self._registry, driver=self._driver, vault=None
                )
            except Exception:
                log.warning("recipe_worker_loop.release_failed", run_id=run.run_id)

    async def _renew_session_lease(self, run: ClaimedRecipeRun, session: Any) -> None:
        """Keep the browser lease alive across a long model call.

        A `KeyError` means the reaper already took it, and renewing is then
        neither possible nor useful -- the run is going to fail on its next
        page read and say so properly. Logged once at that point rather than
        every 40s, because the heartbeat outlives the failure.
        """

        try:
            await self._registry.renew(session.lease_id)
        except KeyError:
            log.warning(
                "recipe_worker_loop.lease_already_reclaimed",
                run_id=run.run_id, lease=str(getattr(session, "lease_id", "")),
            )
            self._live_routes.pop(run.run_id, None)
        except Exception:
            log.warning("recipe_worker_loop.lease_renew_failed", run_id=run.run_id)

    async def _publish_live_route(self, session_id: str, session: Any, tier: str) -> None:
        """Make this worker's session reachable by the live view.

        Two registrations, because there are two ways a viewer arrives: the
        in-process `sessions` dict serves this worker's own live-view route, and
        the redis route lets a `gateway`-role process proxy through to here.

        Best-effort throughout, and a no-op with no placer wired: a redis hiccup
        must cost the run its live view, never the run.
        """

        self._sessions[session_id] = session
        if self._placer is None:
            return
        with contextlib.suppress(Exception):
            await self._placer.commit_route(
                session_id,
                session.ctx.node_id,
                session.identity,
                tier,
                self._lease_ttl_seconds,
            )

    async def _forget_live_route(self, session_id: str, session: Any) -> None:
        """Counterpart, once the session is gone. The redis route also carries a
        TTL, so failing here degrades to a stale entry that expires rather than
        to a failed run."""

        self._sessions.pop(session_id, None)
        if self._placer is None:
            return
        with contextlib.suppress(Exception):
            await self._placer.forget_route(session_id, session.ctx.node_id)

    async def _open_warm_session(
        self, run: ClaimedRecipeRun, domain: str
    ) -> InteractiveSession:
        """A session on a *reused* identity, so the browser is not a stranger.

        See `_IDENTITY_SLOTS`. The slot is claimed by trying to open one and
        moving on if it is taken, rather than by counting: two worker processes
        share a registry and neither can know what the other holds, so the only
        reliable claim is the acquire itself.

        Starting the scan at a per-run offset keeps concurrent runs from all
        queueing behind slot 0 -- without it, three parallel runs would each
        collide on 0, then on 1, doing the work of six opens to reach three.
        """

        start = hash(run.run_id) % _IDENTITY_SLOTS
        for offset in range(_IDENTITY_SLOTS):
            slot = (start + offset) % _IDENTITY_SLOTS
            try:
                return await open_interactive_session(
                    browser_config=self._browser_config,
                    prototype_provider=self._prototype_provider,
                    # Unique: this names the *session*, not the identity, and
                    # it is what live-view and the logs address.
                    session_id=f"recipe-run-{run.run_id}",
                    scope=run.tenant,
                    domain=domain,
                    # Stable: this names the *identity*, and decides whether
                    # the profile is warm.
                    name=f"recipe-{slot}",
                    tier="auto",
                    headful=True,
                    block_popups=True,
                    enable_cdp=False,
                    registry=self._registry,
                    driver=self._driver,
                    profiles_root=self._profiles_root,
                    proxy_pinner=self._proxy_pinner,
                    vault=None,
                    lease_ttl_seconds=self._lease_ttl_seconds,
                )
            except LeaseConflict:
                # This identity is busy; another may be free.
                continue
            # `NodeAtCapacity` is deliberately NOT caught here. A lease conflict
            # is about one identity, so trying the next slot is the right move;
            # capacity is about the whole node, so the next seven slots would be
            # refused for the same reason and the cold-identity fallback below
            # would be refused too. It propagates to `_process`, which defers the
            # run rather than spending eight refusals to fail it.

        # Every slot busy. A cold identity still runs, and is far more likely
        # to be blocked -- but a run that executes and reports being blocked
        # beats one that never starts, and `classify.py` can tell the two
        # apart in the result.
        log.warning(
            "recipe_worker_loop.no_warm_identity", run_id=run.run_id, domain=domain
        )
        return await open_interactive_session(
            browser_config=self._browser_config,
            prototype_provider=self._prototype_provider,
            session_id=f"recipe-run-{run.run_id}",
            scope=run.tenant,
            domain=domain,
            name=f"recipe-run-{run.run_id}",
            tier="auto",
            headful=False,
            block_popups=True,
            enable_cdp=False,
            registry=self._registry,
            driver=self._driver,
            profiles_root=self._profiles_root,
            proxy_pinner=self._proxy_pinner,
            vault=None,
            lease_ttl_seconds=self._lease_ttl_seconds,
        )

    async def _process_onboard(
        self, run: ClaimedRecipeRun, session: InteractiveSession, url: str
    ) -> None:
        """The v2 build: a contract and a URL in, a saved v2 document out.

        This writes `document`, which is the whole
        point -- a recipe without one is refused by `_process_job_run` and
        cannot be opened by the studio, so the v1 build could never produce
        something the marketplace would run.

        The recipe is left as a `draft`: the judge and a human review it before
        it is publishable. Fields the agent could not locate are reported
        rather than dropped, and are what the assist loop will ask about.
        """

        from agentpilot.recipe.v2.assertions import propose_assertions, with_assertions
        from agentpilot.recipe.v2.assist import build_asks
        from agentpilot.recipe.v2.contract import (
            fields_from_description,
            fields_from_output_schema,
        )
        from agentpilot.recipe.v2.onboard import BlockedError, onboard_recipe
        from agentpilot.recipe.v2.review import merge_field_values, verify_and_judge
        from agentpilot.recipe.v2.schema import fields_to_dict, parse_fields
        from agentpilot.recipe.v2.validate import validate_document

        params: dict[str, Any] = run.params or {}
        llm_config = LLMConfig.from_env()
        cfg = RecipeConfig.from_env()

        # Three ways to say what you want, in descending order of how exactly
        # the caller stated it. An explicit v2 field map is taken as-is; a JSON
        # Schema or example payload is converted deterministically; only a
        # plain-English description needs a model, because only it is ambiguous.
        declared = run.recipe.field_schema or {}
        output_schema = params.get("output_schema") or None
        try:
            if declared:
                fields = parse_fields(declared)
            elif isinstance(output_schema, dict) and output_schema:
                fields = fields_from_output_schema(output_schema)
                if not fields:
                    raise ValueError(
                        "could not read any fields out of that output schema -- it "
                        "should be a JSON Schema with `properties`, or an example "
                        "of the JSON object you want back"
                    )
            else:
                fields = await fields_from_description(
                    str(params.get("description") or ""), llm_config=llm_config, url=url
                )
        except ValueError as exc:
            await self._store.complete_run(
                run.run_id, run.lock, data=None, field_failures=None, error=str(exc)
            )
            return

        sample_urls = [str(u) for u in (params.get("sample_urls") or []) if u] or [url]

        async def publish(progress: dict[str, Any]) -> None:
            # What the build is doing, so a run that takes minutes is not a
            # spinner. Best-effort: `update_run_progress` swallows its own
            # failures, and a status line must never be able to fail a build.
            await self._store.update_run_progress(run.run_id, run.lock, progress)

        try:
            recipe, outcome = await onboard_recipe(
                recipe_id=run.recipe_id,
                tenant=run.tenant,
                name=run.recipe.name,
                url=url,
                fields=fields,
                sample_urls=sample_urls,
                session=session,
                registry=self._registry,
                driver=self._driver,
                llm_config=llm_config,
                max_steps=self._build_max_steps,
                on_progress=publish,
            )
        except BlockedError as exc:
            # Distinct from "nothing resolved" on purpose: a wall needs a
            # different identity or a proxy, not a different schema, and
            # `classify.py` exists so the two are never confused again.
            await self._store.complete_run(
                run.run_id, run.lock, data=None, field_failures=None,
                error=f"blocked before anything could be built: {exc}",
            )
            return


        # Stages 6 and 7: run what was built, judge what it collected, repair
        # what the judge rejected. A draft reaches a human only once the judge
        # passes -- their time is the scarce resource this whole gate protects.
        recipe, review = await verify_and_judge(
            recipe,
            sample_urls=sample_urls,
            session=session,
            registry=self._registry,
            driver=self._driver,
            llm_config=llm_config,
            max_repairs=cfg.max_judge_repairs,
            sample_limit=cfg.onboard_sample_runs,
            # So the judge cannot call a field absent for content it was never
            # shown -- see `OnboardOutcome.revealed_page_text`.
            revealed_page_text=outcome.revealed_page_text,
            on_progress=publish,
        )

        # Anything the agent could not find, and anything the judge rejected
        # that repair could not fix, now goes to a person -- on the page the run
        # is still sitting on. See `assist.py` for why the session staying open
        # is the whole point.
        # Everything the build proposed and every reason one was turned down,
        # saved before the assist park rather than after: a run that nobody
        # answers, or that dies in the park, is exactly the one whose reasoning
        # somebody will want to read.
        await self._store.save_artifact(
            run.run_id, run.tenant, "trace", outcome.trace.to_dict()
        )
        if cfg.trace_prompts:
            # Large, and they carry page content, so they are opt-in. Between
            # them they answer the thing the attempts cannot: whether the model
            # was asked the right question at all. A build whose accordion never
            # yielded a table looks identical either way until you can see
            # whether the section was in what the model was shown.
            if outcome.page_json_outline:
                await self._store.save_artifact(
                    run.run_id, run.tenant, "outline",
                    {"page_json": outcome.page_json_outline},
                )
            for exchange in outcome.trace.exchanges:
                await self._store.save_artifact(
                    run.run_id, run.tenant, "prompt", exchange.to_dict(),
                    field=", ".join(exchange.fields) or None,
                )

        # Anything the agent could not find, and anything the judge rejected
        # that repair could not fix, now goes to a person -- on the page the run
        # is still sitting on. See `assist.py` for why the session staying open
        # is the whole point.
        # What the draft actually read, hoisted above the asks so a `rejected`
        # ask can show the value it is asking about. `review.runs` is not
        # re-run by the assist loop, so this is the same thing the assertion
        # proposal below used to recompute for itself.
        collected = merge_field_values(review.runs)
        asks = build_asks(
            outcome.unresolved,
            review.unrepaired,
            # Kept distinct all the way to the person: a field the page does
            # not contain needs a decision, not another search.
            absent=review.absent,
            step_trace=review.step_trace,
            # The rejected values themselves. A person cannot sensibly overrule
            # "this looked wrong" without being shown what it read.
            collected=collected,
            # What was tried for each field, so the panel can show it. An ask
            # that says "here is what I tried and why each attempt failed" is a
            # different question to be handed than "find this".
            tried={
                name: outcome.trace.explain(name)
                for name in {
                    *outcome.unresolved, *review.unrepaired, *review.absent,
                }
                if outcome.trace.explain(name)
            },
        )

        # A person may want to look even when nothing is unresolved, and that is
        # the case that used to be impossible: a build that bound every field
        # -- correctly or not -- finished and saved without ever offering. On a
        # page whose own JSON carries a sponsored competitor under the same key
        # names, every field resolves and one of them is the wrong product;
        # nothing mechanical catches it.
        #
        # `params` is re-read rather than using the copy taken at the top,
        # because `request_assist` writes it *while this build is running*.
        if await self._assist_wanted(run) and not asks:
            asks = build_asks(
                {},
                # `rejected` rather than `unresolved`: these bound to something,
                # and the question is whether it is the right something -- which
                # is what a `rejected` ask puts to a person.
                {
                    name: "bound -- check it is the right value before this is saved"
                    for group in recipe.field_groups
                    for name in group.bindings
                },
                step_trace=review.step_trace,
                collected=collected,
                tried={
                    name: outcome.trace.explain(name)
                    for group in recipe.field_groups
                    for name in group.bindings
                    if outcome.trace.explain(name)
                },
            )
            log.info(
                "recipe_worker_loop.assist_on_request",
                run_id=run.run_id, fields=[a.field for a in asks],
            )
        if asks and cfg.assist_timeout_s > 0:
            recipe, unsettled = await self._await_assist(
                run, recipe, asks, session=session, url=url,
                llm_config=llm_config, timeout_s=cfg.assist_timeout_s,
                unattended_s=cfg.assist_unattended_s,
            )
            outcome.unresolved = {**outcome.unresolved, **unsettled}
            review.unrepaired = {
                k: v for k, v in review.unrepaired.items() if k in unsettled
            }

        # The model-proposed assertions, now that real values exist to justify a
        # bound. The mechanical ones were attached during the build; these are
        # the ones that need to know what the value means.
        if collected:
            recipe.fields = with_assertions(
                recipe.fields,
                await propose_assertions(
                    recipe.fields, samples=collected, llm_config=llm_config
                ),
            )

        document = recipe.to_dict()
        errors, warnings = validate_document(document)
        if errors:
            # The agent is held to the same lint as a human author. Saving a
            # document the studio would reject leaves a recipe nobody can edit
            # and the marketplace cannot run.
            await self._store.complete_run(
                run.run_id, run.lock, data=None,
                field_failures={"unresolved": outcome.unresolved},
                error="the built recipe did not validate: " + "; ".join(errors),
            )
            return

        await self._store.apply_recipe_update(
            run.recipe_id,
            version=run.recipe.version + 1,
            # The v1 columns are left empty rather than filled with v2-shaped
            # data. A v2 group has `bindings`/`steps` and a v1 reader expects
            # `field_locators`/`reveal_steps`; writing one into the other is the
            # exact shape confusion that crashed the recipe detail page. Empty
            # says "this recipe has no v1 representation", which is true.
            global_setup=[],
            field_groups=[],
            health_status=recipe.health_status,
            diff_summary="onboarding build",
            heal_attempts="reset",
            document=document,
            field_schema=fields_to_dict(fields),
        )
        await self._store.complete_run(
            run.run_id,
            run.lock,
            data={
                "recipe_id": run.recipe_id,
                "version": run.recipe.version + 1,
                # What the draft actually collected, per sample URL, plus the
                # judge's verdict. This is what the studio shows the reviewer:
                # a draft is worth looking at because it ran, not because it
                # parsed.
                "review": review.to_dict(),
                "ready_for_review": review.ready_for_review,
            },
            field_failures={
                "unresolved": outcome.unresolved,
                # Judged wrong and not repairable. Distinct from `unresolved`,
                # which is "never found" -- these resolved to something, it was
                # just the wrong thing, and the two need different questions put
                # to a human.
                "rejected": review.unrepaired,
                "warnings": warnings,
                "landed_url": outcome.landed_url,
                "steps_taken": outcome.steps_taken,
            },
        )

    async def _assist_wanted(self, run: ClaimedRecipeRun) -> bool:
        """Whether this build should stop for a person even with nothing to ask.

        Two ways to say so, and they are the same intent at different times:
        `mode: "assisted"` decided before the build started, and
        `POST .../assist/request` decided while it was running. The run row is
        re-read for the second -- the `params` captured when the run was claimed
        predate the request by minutes.
        """

        if (run.params or {}).get("mode") == "assisted":
            return True
        try:
            current = await self._store.get_run(run.run_id, run.tenant)
        except Exception:  # noqa: BLE001 - a failed read is not a reason to park
            return False
        return bool((current.params or {}).get("assist_requested")) if current else False

    async def _await_assist(
        self,
        run: ClaimedRecipeRun,
        recipe: Any,
        asks: list[Any],
        *,
        session: InteractiveSession,
        url: str,
        llm_config: LLMConfig,
        timeout_s: float,
        unattended_s: float | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """Park, wait for a person, apply what they said.

        The browser session is deliberately **not** released around this wait.
        That is the entire value of the mechanism: the person sees the page the
        agent gave up on, with whatever accordion the run had opened still open,
        and picks the element directly. Releasing and re-opening would lose that
        state and reduce the ask to a guess from a description.

        The cost is real and is why the wait is bounded: this holds a worker
        slot (`max_concurrent`), one of `_IDENTITY_SLOTS` warm identities, a
        browser and a proxy pin for the duration.
        """

        from datetime import UTC, datetime, timedelta

        from agentpilot.recipe.v2.assist import apply_resolutions, parse_resolutions

        # Two clocks. The park STARTS on the unattended budget and only reaches
        # `timeout_s` if somebody actually turns up -- the studio calls
        # `touch_park` on a timer while the assist panel is open, and that
        # extends it to the full ceiling.
        #
        # It used to start at the ceiling, which made the two the same number
        # and contradicted `assist_timeout_s`'s own docstring ("the ceiling for a
        # tab nobody is looking at rather than the budget for doing the work").
        # A question nobody ever saw held a warm identity, a browser and a proxy
        # pin for 1800s on a node that fits four browsers -- and the heartbeat
        # dutifully renewed the lease the whole time, because the worker was
        # alive and waiting, which is exactly what it looks like when nobody is
        # coming.
        initial_s = timeout_s if unattended_s is None else min(unattended_s, timeout_s)
        deadline = datetime.now(UTC) + timedelta(seconds=initial_s)
        parked = await self._store.park_run(
            run.run_id,
            run.lock,
            pending_asks=[a.to_dict() for a in asks],
            parked_until=deadline,
        )
        if not parked:
            # The row was not `running` any more -- cancelled, or reclaimed.
            # Carrying on would write results for a run somebody else owns.
            log.info("recipe_worker_loop.park_refused", run_id=run.run_id)
            return recipe, {a.field: a.reason for a in asks}

        log.info(
            "recipe_worker_loop.parked",
            run_id=run.run_id, fields=[a.field for a in asks],
            unattended_s=initial_s, ceiling_s=timeout_s,
        )

        raw: list[dict[str, Any]] | None = None
        while True:
            # The deadline is re-read rather than held, because the studio pushes
            # it out through `touch_park` while somebody has the panel open.
            # Holding the value computed at park time is what dropped a person
            # halfway through recording a route: answering an ask properly takes
            # a reload, a recording, a pick and a look at what it read.
            current = await self._store.get_run(run.run_id, run.tenant)
            if current is None:
                # The row is gone -- cancelled, or the history was cleared out
                # from under this worker. Nobody can answer a run that does not
                # exist, so waiting out the remaining park is pure cost: it
                # holds a warm identity, a browser, a proxy pin and -- because
                # `tick` does not return until everything it claimed finishes --
                # this worker's entire claiming loop. Measured after a `TRUNCATE`
                # of the run tables: two workers sat on deleted rows for the full
                # 1800s and no queued build was picked up by anyone.
                log.info("recipe_worker_loop.park_vanished", run_id=run.run_id)
                return recipe, {a.field: a.reason for a in asks}
            # Poll BEFORE testing the deadline. `submit_assist` sets
            # `parked_until = NULL` and `status = 'running'` in the same
            # statement, so a row that has just been answered reports no
            # deadline at all -- and `or deadline` then falls back to the
            # *original* park deadline. Answering in the last poll interval
            # therefore broke straight out of this loop with `raw is None`,
            # called `resume_run`, and dropped answers the API had already
            # accepted with a 200.
            raw = await self._store.poll_assist(run.run_id, run.lock)
            if raw is not None:
                break
            if current.status != "needs_input":
                # The row left `needs_input` and yet `poll_assist` -- which
                # reads by `lock`, not by tenant -- cannot see it. This worker
                # no longer owns the run: reclaimed, or resumed under a newer
                # lock. Waiting out the remaining park would hold a browser and
                # a warm identity for somebody else's run.
                log.info("recipe_worker_loop.park_lock_lost", run_id=run.run_id)
                return recipe, {a.field: a.reason for a in asks}
            until = current.parked_until or deadline
            if datetime.now(UTC) >= until:
                break
            await asyncio.sleep(self._assist_poll_seconds)

        if raw is None:
            # Nobody answered. Resuming beats holding the slot indefinitely, and
            # the fields are reported unresolved exactly as they would have been
            # without an assist loop at all.
            await self._store.resume_run(run.run_id, run.lock)
            log.info("recipe_worker_loop.park_expired", run_id=run.run_id)
            return recipe, {a.field: a.reason for a in asks}

        resolutions = parse_resolutions(raw, asks)
        recipe, unsettled = await apply_resolutions(
            recipe,
            resolutions,
            url=url,
            session=session,
            registry=self._registry,
            driver=self._driver,
            llm_config=llm_config,
        )
        # An ask nobody answered stays unresolved.
        for ask in asks:
            if ask.field not in resolutions:
                unsettled.setdefault(ask.field, ask.reason)
        return recipe, unsettled

    async def _process_replay(
        self, run: ClaimedRecipeRun, session: InteractiveSession, url: str
    ) -> None:
        """Run the recipe once and report what it collected.

        Three paths, and which one is taken is decided by the recipe's *shape*,
        not by preference:

        - A submitted job run carries its own URL and goes to `_process_job_run`.
        - A recipe with a v2 `document` must use the v2 engine. The v1 engine
          reads `global_setup`/`field_groups`, the v1 columns, which are empty
          for everything the studio and the onboarding agent produce -- so it
          replayed a recipe with no groups at all and completed successfully
          with `data = {}`. A run that collects nothing and calls itself healthy
          is the worst of both outcomes: the caller gets nulls and the recipe
          gets marked fine.
        - Only a genuinely v1 recipe falls through to the v1 engine.
        """

        if run.job_id is not None and run.url:
            await self._process_job_run(run, session)
            return

        document = run.recipe.document
        if not document:
            # Nothing to replay. A recipe with no document was never built --
            # its onboarding run failed, or it is a shell created by a request
            # whose build never finished. Saying so beats the v1 engine's old
            # answer, which was to replay zero field groups and report success
            # with `data = {}`.
            await self._store.complete_run(
                run.run_id, run.lock, data=None, field_failures=None,
                error="this recipe has no document yet -- its build did not finish",
            )
            return
        await self._process_replay_v2(run, session, document, url)

    async def _process_replay_v2(
        self,
        run: ClaimedRecipeRun,
        session: InteractiveSession,
        document: dict[str, Any],
        url: str,
    ) -> None:
        """A scheduled or manual replay of a v2 recipe.

        Differs from `_process_job_run` in exactly one way, and it is the
        documented one: this DOES move the recipe's health. A scheduled replay
        failing means the recipe is degraded; a submitted run failing usually
        means the caller's URL was wrong, and letting one bad paste mark a
        public template broken would take a working scraper out of the
        catalogue for every tenant that can see it.
        """

        from agentpilot.recipe.v2.models import Recipe as RecipeV2
        from agentpilot.recipe.v2.models import RunInput
        from agentpilot.recipe.v2.replay import replay_recipe as replay_v2

        result = await replay_v2(
            RecipeV2.from_dict(document, recipe_id=run.recipe_id, tenant=run.tenant),
            RunInput(url=url, metadata=(run.params or {}).get("metadata") or {}),
            session=session,
            registry=self._registry,
            driver=self._driver,
        )
        # `blocked` is not `degraded`. A recipe that could not be read because a
        # wall stood in front of it has not drifted, and healing it against the
        # wall is how `classify.py` says recipes get destroyed.
        if result.outcome != "blocked":
            await self._store.mark_replay_result(
                run.recipe_id,
                health_status="healthy" if result.success else "degraded",
            )
        await self._store.complete_run(
            run.run_id,
            run.lock,
            data=result.data,
            field_failures={
                "field_status": result.field_status,
                "truncated": result.truncated,
                "outcome": result.outcome,
                "step_trace": [s.to_dict() for s in result.step_trace],
                # Which selector actually produced each value, and what the
                # candidates ahead of it did. `replay` has always computed this
                # -- it is the drift signal the operational model turns on --
                # and it was dropped on the floor here, so a run that collected
                # the wrong value could not be asked which selector collected
                # it.
                "provenance": result.provenance,
                "assertions": {
                    name: [c.to_dict() for c in checks]
                    for name, checks in result.assertions.items()
                },
            },
            error=result.error,
        )

    async def _process_job_run(
        self, run: ClaimedRecipeRun, session: InteractiveSession
    ) -> None:
        """One URL of a submitted job, through the v2 engine.

        **v2, not v1, and not by preference.** The older `replay_recipe` reads
        the page to visit off the recipe's own `url_pattern`; there is nowhere
        to put a caller's URL. `recipe/v2/replay.py` takes it as `RunInput`,
        which is the whole reason a catalogue of recipes can be applied to
        somebody else's list of pages. A recipe with no v2 document was built
        by the agent against one URL pattern and cannot answer for another, so
        it is refused rather than silently run against the wrong page.

        **The result never touches recipe health.** A scheduled replay failing
        means the recipe is degraded; a submitted run failing usually means the
        URL was wrong, and letting one bad paste mark a public template broken
        would take a working scraper out of the catalogue for every tenant that
        can see it.
        """

        from agentpilot.recipe.v2.models import Recipe as RecipeV2
        from agentpilot.recipe.v2.models import RunInput
        from agentpilot.recipe.v2.replay import replay_recipe as replay_v2

        document = run.recipe.document
        if not document:
            await self._store.complete_run(
                run.run_id,
                run.lock,
                data=None,
                field_failures=None,
                error=(
                    "this recipe has no v2 document, so it can only run against "
                    "the URL pattern it was built for"
                ),
            )
            return

        result = await replay_v2(
            RecipeV2.from_dict(document),
            RunInput(url=run.url or "", metadata=(run.params or {}).get("metadata") or {}),
            session=session,
            registry=self._registry,
            driver=self._driver,
        )
        await self._store.complete_run(
            run.run_id,
            run.lock,
            data=result.data,
            # `field_status` is v2's per-field verdict (resolved / fallback /
            # suspect / empty / failed) and is what the job view colours each
            # URL by -- richer than v1's failures list, and the same shape a
            # caller reading the API needs to judge a partial result.
            #
            # `step_trace` is here because without it an empty field behind a
            # reveal click is unattributable: the selector may be wrong, or the
            # click may never have run, and the two need opposite fixes. Every
            # reveal step is `optional: true, on_error: continue` by
            # construction (see `RecipeWizardPage::pickStepTarget`), so a step
            # that matched nothing is *silent* -- the run completes, the group
            # reads an unrevealed page, and every field in it comes back empty
            # with nothing saying why.
            field_failures={
                "field_status": result.field_status,
                "truncated": result.truncated,
                "outcome": result.outcome,
                "step_trace": [s.to_dict() for s in result.step_trace],
                # Same reason as `_process_replay`: a job run is the one most
                # likely to be looked at when a value comes back wrong, because
                # it ran against a URL the caller chose rather than the one the
                # recipe was built on. Which selector won, out of how many, is
                # the first question.
                "provenance": result.provenance,
                "assertions": {
                    name: [c.to_dict() for c in checks]
                    for name, checks in result.assertions.items()
                },
            },
            error=result.error,
        )

    async def _process_codegen(self, run: ClaimedRecipeRun) -> None:
        """Write a standalone scraper for this recipe.

        Routed by which shape the recipe actually has. A v2 document carries
        `bindings`/`steps`/`repeat`, which the v1 generator cannot read at all
        -- it looks for `field_locators` and `locator.source` and would emit a
        script for an empty recipe rather than fail. Since every recipe the
        studio or the onboarding agent produces is v2, that is the common case,
        not the exotic one.
        """

        params: dict[str, Any] = run.params or {}
        language = params.get("language", "python-playwright")
        document = run.recipe.document

        if not document:
            await self._store.complete_run(
                run.run_id, run.lock, data=None, field_failures=None,
                error="this recipe has no document yet -- its build did not finish",
            )
            return
        await self._process_codegen_v2(run, document, language)

    async def _process_codegen_v2(
        self, run: ClaimedRecipeRun, document: dict[str, Any], language: str
    ) -> None:
        from agentpilot.recipe.v2.codegen import (
            generate_scraper_code as generate_v2,
        )
        from agentpilot.recipe.v2.models import Recipe as RecipeV2

        recipe = RecipeV2.from_dict(document, recipe_id=run.recipe_id, tenant=run.tenant)
        try:
            code, problems = await generate_v2(
                recipe, language=language, llm_config=LLMConfig.from_env()
            )
        except ValueError as exc:
            # A recipe this cannot be expressed as a standalone script at all --
            # a Lua transform, say. Refusing beats emitting something that runs
            # and silently collects less.
            await self._store.complete_run(
                run.run_id, run.lock, data=None, field_failures=None, error=str(exc)
            )
            return

        await self._store.complete_run(
            run.run_id,
            run.lock,
            data={"code": code, "language": language, "problems": problems},
            field_failures=None,
            # Handed over WITH its problems rather than withheld: a script with
            # one missing field is still worth having, as long as what is wrong
            # with it is said out loud rather than discovered in production.
            error=(
                "the generated script did not fully check out: " + "; ".join(problems)
                if problems
                else None
            ),
        )
