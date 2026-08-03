"""Rebuild a deploy plan's job messages from its persisted run document.

The Valkey snapshot is the live path, but it carries a TTL — 10 minutes once a
job is terminal (``transport._TERMINAL_TTL_SECONDS``). Anyone who closes the tab
during a long deploy and comes back later finds the snapshot evicted, and until
this module existed that meant a 4404: the run's outcome was unreachable even
though ``plan_runs.scheduler.ops`` still held every op's final state under a
7-day TTL, in exactly ``PlanStateMsg`` shape.

So this is the cold path behind ``ws /api/ws/jobs/{job_id}``: same messages the
worker published live, re-derived from Mongo. Pure (doc + clock in, models out)
so the decision table is unit-testable without Valkey, Mongo, or a socket.

One deliberate difference from live: a cross-user admin stop stores its reason in
Valkey alongside the cancel flag, and that reason expires with the snapshot. A
replayed cancelled run therefore surfaces the reason as the affected ops'
``detail`` (which *is* persisted) rather than as a terminal ``ErrorMsg``.
"""

from app.core.jobs.models import DoneMsg, ErrorMsg, JobStatus, Message, PlanStateMsg
from app.core.jobs.transport import ACTIVE_TTL_SECONDS

#: Op statuses after which an operation will never change again. Mirrors the
#: scheduler's own terminal set (``tasks._PLAN_TERMINAL``).
PLAN_TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})


def replay_plan_run(
    doc: dict | None, *, now_ms: int
) -> tuple[JobStatus, list[Message]] | None:
    """Messages that bring a reconnecting client up to date on *doc*'s run.

    Returns None when *doc* carries no scheduler state to replay — a job that
    isn't a plan run (a clone, a teardown) or one that died before its first
    operation was persisted. The caller answers that with 4404, same as before.

    Otherwise the returned messages are sent in order and the stream ends after
    the first terminal one:

    * every op terminal → a single ``DoneMsg``, exactly what ``_advance_plan``
      publishes when the last op lands. The client's ``finishDeploy`` reads the
      per-op statuses out of it and reports the failures.
    * still in flight, run recently touched → a non-terminal ``PlanStateMsg``;
      the socket then keeps streaming live publishes as usual.
    * still in flight, but untouched for longer than a live job's snapshot would
      have survived → the ``PlanStateMsg`` *and* a terminal ``ErrorMsg``. The
      worker is gone; without the terminal frame the client would hold its
      canvas locked forever waiting on a plan that will never publish again.
    """

    scheduler = doc.get("scheduler") if doc else None
    raw_ops = (scheduler or {}).get("ops") or {}
    if not raw_ops:
        return None

    ops = {op_id: dict(state) for op_id, state in raw_ops.items()}
    plan_state = PlanStateMsg(ops=ops)

    if all(state.get("status") in PLAN_TERMINAL_STATUSES for state in raw_ops.values()):
        return JobStatus.done, [DoneMsg(result={"ops": ops})]

    updated_at = scheduler.get("updatedAt") or doc.get("updatedAt") or 0
    if now_ms - updated_at <= ACTIVE_TTL_SECONDS * 1000:
        return JobStatus.running, [plan_state]

    return JobStatus.error, [
        plan_state,
        ErrorMsg(
            status=503,
            detail=(
                "This deployment stopped reporting and was not resumed — the "
                "operations below show how far it got."
            ),
        ),
    ]
