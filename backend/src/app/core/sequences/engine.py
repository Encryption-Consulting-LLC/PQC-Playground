"""Sequence engine — walks a list of :class:`Step`\\ s, dispatching each to
the agent and handling reboots and verify-with-backoff.

Every side effect is injected, so the walk logic is unit-testable without
redis, Mongo, or a real agent:

* ``dispatch(job_key, vm_id, command, params, *, role, secret_keys, timeout_s,
  expect_disconnect) -> dict`` sends one command and blocks for its terminal
  result (raising a transport-specific exception on agent error / timeout) —
  in production this is the worker↔agent bridge
  (:func:`app.core.agentbus.dispatch_and_wait`). ``job_key``
  is the step's stable id, used to derive the deterministic per-step job id
  (idempotency key on redelivery).
* ``wait_for_reconnect(vm_id, since_ms, timeout_s)`` blocks until a connection
  newer than ``since_ms`` is currently live, or raises. It backs both a declared
  reboot step and the one-shot recovery reboot a step can ask for when its
  failure detail says the target has a restart pending
  (``Step.reboot_recovery_signatures``).
* ``sleep(seconds)`` / ``now_ms()`` are injected for deterministic tests.

Resume: a ``completed`` set (from the ``plan_runs`` cursor) skips already-run
steps on Celery redelivery; ``on_step_done(step_id, result)`` persists the
cursor + any produced artifacts after each step so a mid-sequence redelivery
picks up where it left off. Reboot waits use a *timestamp compare* (reconnect
time > dispatch time) plus the current liveness key, immune both to a fast
reboot that reconnects before polling and a reconnect that immediately drops.
"""

import logging
from collections.abc import Callable, Iterable
from typing import Any

from app.core.sequences.model import RunContext, Step, StepRuntime

logger = logging.getLogger(__name__)


class SequenceError(Exception):
    """A step failed terminally (agent error, dispatch timeout, or a verify
    probe that never reached its target state within the window)."""


class HealthGateError(SequenceError):
    """A terminal aggregate failure that retains its structured evidence."""

    def __init__(
        self,
        message: str,
        *,
        step_id: str,
        health: dict[str, Any],
        results: dict[str, dict],
    ) -> None:
        super().__init__(message)
        self.step_id = step_id
        self.health = health
        self.results = results


class SequenceCancelled(SequenceError):
    """A cooperative cancellation was observed between sequence steps."""


#: Verify-probe backoff (seconds) — geometric-ish, capped; the tail repeats the
#: cap until ``verify_window_s`` elapses. ADWS / template propagation is slow,
#: so the window (not this schedule) is the real bound.
_VERIFY_BACKOFF = (5, 10, 15, 30, 45, 60)

#: Reconnect budget for a recovery reboot (see
#: :attr:`Step.reboot_recovery_signatures`) — matches the DC tail's declared
#: reboot step, since it is the same restart of the same class of machine.
_REBOOT_RECOVERY_RECONNECT_S = 1200


def _reboot_recovery_signature(step: Step, exc: Exception) -> str | None:
    """The first of ``step``'s pending-reboot signatures present in *exc*'s
    detail, or ``None`` when the failure isn't one a restart would clear."""
    detail = str(exc).lower()
    return next(
        (sig for sig in step.reboot_recovery_signatures if sig.lower() in detail),
        None,
    )


def _combined_failure(first: Exception, last: Exception, attempts: int) -> Exception:
    """The failure to surface after *attempts* dispatches of one step.

    Reporting only the last attempt is wrong whenever the first attempt left
    state behind that makes every later one fail differently — a self-poisoning
    command's retries all lie, and the one true error gets discarded. Lead with
    the first failure and keep its class, so ``core/errors.py``'s status mapping
    still applies; append the last for context.
    """

    if str(last) == str(first):
        return last
    detail = f"{first} (retried {attempts - 1}x; last: {last})"
    try:
        return type(first)(detail)
    except Exception:  # noqa: BLE001 - an exception class with a richer ctor
        return last


class SequenceEngine:
    def __init__(
        self,
        *,
        dispatch: Callable[..., dict],
        wait_for_reconnect: Callable[[str, int, int], None],
        sleep: Callable[[float], None],
        now_ms: Callable[[], int],
        role: str = "guest",
        completed: set[str] | None = None,
        resumed_results: dict[str, dict] | None = None,
        on_step_done: Callable[[str, dict], None] | None = None,
        on_step_start: Callable[[str], None] | None = None,
        on_verify_start: Callable[[str], None] | None = None,
        on_verify_done: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._dispatch = dispatch
        self._wait_for_reconnect = wait_for_reconnect
        self._sleep = sleep
        self._now_ms = now_ms
        # The plan owner's role, forwarded on every dispatched command (the
        # agent re-checks it as its structural second gate). Both roles hold
        # VM_PROVISION, so a guest's own lab provisions under 'guest'.
        self._role = role
        self._completed = completed if completed is not None else set()
        self._resumed_results = resumed_results if resumed_results is not None else {}
        self._on_step_done = on_step_done or (lambda _s, _r: None)
        self._on_step_start = on_step_start or (lambda _s: None)
        self._on_verify_start = on_verify_start or (lambda _s: None)
        self._on_verify_done = on_verify_done or (lambda _s: None)
        self._should_stop = should_stop or (lambda: False)

    def run(self, steps: Iterable[Step], ctx: RunContext) -> dict[str, dict]:
        """Run every step in order; return {step_id: result}. Raises
        :class:`SequenceError` on the first terminal failure (the plan runner
        turns that into a failed op)."""
        results: dict[str, dict] = {}
        for step in steps:
            if self._should_stop():
                raise SequenceCancelled(
                    f"deployment cancellation requested before step '{step.id}'"
                )
            self._on_step_start(step.id)
            results[step.id] = self._run_one(step, ctx, results)
        return results

    def _run_one(
        self, step: Step, ctx: RunContext, prior_results: dict[str, dict]
    ) -> dict:
        node = ctx.node(step.target)
        if step.aggregate is None and node.agent_vm_id is None:
            raise SequenceError(
                f"step '{step.id}' targets node '{step.target}' which has no agent"
            )

        if step.id in self._completed:
            logger.info("sequence step %s already complete — skipping", step.id)
            # Its artifacts were restored into ctx by the caller from plan_runs.
            return self._resumed_results.get(step.id, {})

        if step.skip_if_artifacts and all(
            key in ctx.artifacts for key in step.skip_if_artifacts
        ):
            result = {"skipped": True, "reason": "artifact already available"}
            self._completed.add(step.id)
            self._on_step_done(step.id, result)
            return result

        if step.aggregate is not None:
            result = step.aggregate(
                StepRuntime(ctx=ctx, node=node),
                prior_results,
            )
            if result.get("healthy") is not True:
                failures = result.get("failures") or ["aggregate health check failed"]
                detail = "; ".join(str(item) for item in failures)
                raise HealthGateError(
                    f"health gate '{step.command}' failed: {detail}",
                    step_id=step.id,
                    health=result,
                    results={**prior_results, step.id: result},
                )
            self._completed.add(step.id)
            self._on_step_done(step.id, result)
            return result

        for artifact_key in step.consumes:
            if artifact_key not in ctx.artifacts:
                raise SequenceError(
                    f"step '{step.id}' requires unavailable artifact '{artifact_key}'"
                )

        params = step.resolve_params(ctx)
        # Capture *before* dispatch so a fast reboot that reconnects immediately
        # still registers as "after" the dispatch (timestamp compare).
        dispatched_at = self._now_ms()
        result = self._dispatch_with_retry(step, node.agent_vm_id, params)

        for key in step.produces:
            content = result.get("contentB64")
            if not isinstance(content, str):
                raise SequenceError(
                    f"step '{step.id}' did not report required result field "
                    "'contentB64'"
                )
            ctx.artifacts[key] = content
        artifact_defaults = step.resolve_result_artifact_defaults(ctx)
        for result_field, artifact_key in step.result_artifacts.items():
            value = result.get(result_field)
            if not isinstance(value, str) or not value:
                value = artifact_defaults.get(result_field)
            if not isinstance(value, str) or not value:
                raise SequenceError(
                    f"step '{step.id}' did not report required result field "
                    f"'{result_field}'"
                )
            ctx.artifacts[artifact_key] = value
        for result_field, artifact_key in step.optional_result_artifacts.items():
            value = result.get(result_field)
            if isinstance(value, str) and value:
                ctx.artifacts[artifact_key] = value

        if step.expects_disconnect:
            self._wait_for_reconnect(node.agent_vm_id, dispatched_at, step.timeout_s)

        if step.verify is not None:
            self._run_verify(step, node.agent_vm_id, ctx)

        self._completed.add(step.id)
        self._on_step_done(step.id, result)
        return result

    def _dispatch_with_retry(
        self, step: Step, vm_id: str, params: dict[str, str]
    ) -> dict:
        """Dispatch a convergent step with its bounded transient retry policy.

        A failure whose detail matches ``step.reboot_recovery_signatures`` gets
        one extra shot behind a reboot — the target refused the command only
        because it has a restart pending, which no amount of redispatching on the
        same boot can clear. That recovery is one-shot, and it *precedes*
        ``retry_delays_s`` rather than replacing it: a step carrying both takes
        the reboot first and then still walks its schedule if that didn't take.

        A *timeout* is never retried, whatever the schedule says: giving up on
        the wait doesn't stop the command on the guest, so a second dispatch
        would race the first (two concurrent ``Install-WindowsFeature`` runs).
        The dispatch adapter marks those failures with a ``timed_out``
        attribute; the engine stays transport-agnostic and just reads it.

        What finally surfaces after an exhausted schedule is the *first*
        attempt's failure, not the last — see ``_combined_failure``.
        """

        attempt = 0
        dispatches = 0
        recovered = False
        first_exc: Exception | None = None
        while True:
            job_key = step.id if attempt == 0 else f"{step.id}.retry.{attempt}"
            dispatches += 1
            try:
                return self._dispatch(
                    job_key,
                    vm_id,
                    step.command,
                    params,
                    role=self._role,
                    secret_keys=step.secret_keys,
                    timeout_s=step.timeout_s,
                    expect_disconnect=step.expects_disconnect,
                )
            except Exception as exc:  # noqa: BLE001 - transport/agent transient boundary
                if first_exc is None:
                    first_exc = exc
                if getattr(exc, "timed_out", False):
                    logger.warning(
                        "sequence step %s (%s) timed out after %ds; not retrying "
                        "— the command may still be running on %s",
                        step.id,
                        step.command,
                        step.timeout_s,
                        vm_id,
                    )
                    raise
                signature = _reboot_recovery_signature(step, exc)
                if signature is not None and not recovered:
                    recovered = True
                    try:
                        return self._reboot_and_redispatch(
                            step, vm_id, params, signature, exc
                        )
                    except Exception as recovery_exc:  # noqa: BLE001 - same boundary
                        # A recovery that didn't take is not terminal by itself:
                        # fall through to the step's own retry schedule, so a
                        # signature *precedes* the backoff rather than replacing
                        # it. A timed-out redispatch still stops here.
                        if getattr(recovery_exc, "timed_out", False):
                            raise
                        # A reboot that couldn't happen re-raises the *original*
                        # failure, so a distinct exception means the redispatch
                        # ran — and counts as an attempt.
                        if recovery_exc is not exc:
                            dispatches += 1
                        exc = recovery_exc
                if attempt >= len(step.retry_delays_s):
                    if first_exc is exc:
                        raise
                    raise _combined_failure(first_exc, exc, dispatches) from exc
                delay = step.retry_delays_s[attempt]
                attempt += 1
                logger.warning(
                    "sequence step %s (%s) failed; retrying attempt %d after %ds",
                    step.id,
                    step.command,
                    attempt + 1,
                    delay,
                )
                self._sleep(delay)

    def _reboot_and_redispatch(
        self,
        step: Step,
        vm_id: str,
        params: dict[str, str],
        signature: str,
        original: Exception,
    ) -> dict:
        """Reboot ``vm_id`` to clear its pending restart, then redispatch *step*.

        Raises the *original* failure (chained) when the reboot itself can't be
        made to happen, so the surfaced detail still names the real blocker
        rather than the recovery attempt.
        """
        logger.warning(
            "sequence step %s (%s) reports a pending reboot (%r); rebooting %s "
            "and redispatching once",
            step.id,
            step.command,
            signature,
            vm_id,
        )
        # Captured *before* dispatch for the same reason as a declared reboot
        # step: a reboot that reconnects immediately must still read as "after".
        since = self._now_ms()
        try:
            self._dispatch(
                f"{step.id}.rebootrecover",
                vm_id,
                "system.reboot",
                {"delaySeconds": "5"},
                role=self._role,
                secret_keys=(),
                timeout_s=120,
                expect_disconnect=True,
            )
            self._wait_for_reconnect(vm_id, since, _REBOOT_RECOVERY_RECONNECT_S)
        except Exception as exc:
            raise original from exc
        # A fresh job key: the failed attempt's terminal snapshot must not
        # short-circuit the redispatch.
        return self._dispatch(
            f"{step.id}.postreboot",
            vm_id,
            step.command,
            params,
            role=self._role,
            secret_keys=step.secret_keys,
            timeout_s=step.timeout_s,
            expect_disconnect=step.expects_disconnect,
        )

    def _run_verify(self, step: Step, vm_id: str, ctx: RunContext) -> None:
        probe = step.verify
        assert probe is not None
        predicate = step.verify_predicate or (lambda _r: True)
        deadline = self._now_ms() + step.verify_window_s * 1000
        attempt = 0
        verify_id = f"{step.id}.verify"
        self._on_verify_start(verify_id)
        last_detail = "no probe ran"
        while True:
            try:
                params = probe.resolve_params(ctx)
                # Verify probes are read-only, so a fresh job key per attempt is
                # fine (no idempotency concern) and avoids a stale terminal
                # snapshot short-circuiting a retry.
                result = self._dispatch(
                    f"{step.id}.verify.{attempt}",
                    vm_id,
                    probe.command,
                    params,
                    role=self._role,
                    secret_keys=probe.secret_keys,
                    timeout_s=probe.timeout_s,
                )
                if predicate(result):
                    self._on_verify_done(verify_id)
                    return
                last_detail = f"{probe.command} not ready yet"
            except Exception as exc:  # noqa: BLE001 - readiness boundary
                # Production dispatch raises agentbus DispatchError /
                # AgentUnreachableError, while unit-test adapters commonly use
                # SequenceError. Any probe failure is a not-ready-yet signal;
                # keep retrying until the verify window closes.
                last_detail = str(exc)

            if self._now_ms() >= deadline:
                raise SequenceError(
                    f"verify '{probe.command}' for step '{step.id}' did not "
                    f"succeed within {step.verify_window_s}s: {last_detail}"
                )
            backoff = _VERIFY_BACKOFF[min(attempt, len(_VERIFY_BACKOFF) - 1)]
            attempt += 1
            self._sleep(backoff)


def deterministic_step_job_id(plan_job_id: str, op_id: str, step_id: str) -> str:
    """The per-step job id — both the dispatch idempotency key and a future
    Inspector drill-down handle. Deterministic so a redelivered task reuses the
    same id and the bridge finds the already-terminal snapshot."""
    return f"{plan_job_id}-{op_id}-{step_id}"


#: Job-key suffixes this module mints for a *re*dispatch of a step the client
#: already has one row for. ``.retry.{n}`` / ``.verify.{n}`` are matched on the
#: marker, since the attempt number varies.
_REDISPATCH_MARKERS = (".retry.", ".rebootrecover", ".postreboot")
_VERIFY_MARKER = ".verify."


def visible_step_id(job_key: str) -> str:
    """The execution-manifest row a dispatch *job key* belongs to.

    Every attempt at a step needs its own job key — the key is the dispatch
    idempotency key, so a retry, a post-reboot redispatch, or the n-th verify
    probe must not reuse the one whose terminal snapshot already exists
    (``_dispatch_with_retry`` / ``_reboot_and_redispatch`` / ``_run_verify``).
    But the UI has exactly one row per *step*, plus one for its verify probe, so
    forwarding those keys as step ids reported progress against rows that don't
    exist: the step actually running stayed grey while a phantom entry
    accumulated its state, and the op's phase text named a step
    (``iis-share.postreboot``) that wasn't in the list.

    Collapsing back to the row id here also restores the duration prior for a
    retried step, which ``load_step_medians`` keys by real step id.
    """
    for marker in _REDISPATCH_MARKERS:
        index = job_key.find(marker)
        if index != -1:
            return job_key[:index]
    index = job_key.find(_VERIFY_MARKER)
    if index != -1:
        # The probe's own row is `{step}.verify`; the attempt number is not shown.
        return job_key[: index + len(_VERIFY_MARKER) - 1]
    return job_key


def redact_params(params: dict[str, Any], secret_keys: Iterable[str]) -> dict[str, Any]:
    """A copy of *params* with every secret key masked — for logging."""
    secret = set(secret_keys)
    return {k: ("***" if k in secret else v) for k, v in params.items()}
