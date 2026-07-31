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
        same boot can clear. That recovery is one-shot and independent of
        ``retry_delays_s`` (which covers transient service failures).
        """

        attempt = 0
        recovered = False
        while True:
            job_key = step.id if attempt == 0 else f"{step.id}.retry.{attempt}"
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
                signature = _reboot_recovery_signature(step, exc)
                if signature is not None and not recovered:
                    recovered = True
                    return self._reboot_and_redispatch(
                        step, vm_id, params, signature, exc
                    )
                if attempt >= len(step.retry_delays_s):
                    raise
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


def redact_params(params: dict[str, Any], secret_keys: Iterable[str]) -> dict[str, Any]:
    """A copy of *params* with every secret key masked — for logging."""
    secret = set(secret_keys)
    return {k: ("***" if k in secret else v) for k, v in params.items()}
