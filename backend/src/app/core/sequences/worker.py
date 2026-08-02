"""Worker-side glue that runs a :class:`SequenceEngine` with concrete effects:
the agentbus dispatch bridge, Mongo-backed reboot waits, and ``plan_runs``
cursor/artifact persistence for redelivery-safe resume.

Kept separate from :mod:`app.core.sequences.engine` (pure) and
:mod:`app.tasks` (Celery/vmkit) so the wiring has one home. Everything here
runs in the Celery worker over a short-lived sync Mongo client.
"""

import datetime
import logging
import statistics
import time

from app.core import agentbus
from app.core.db.models import now_ms
from app.core.sequences.engine import (
    HealthGateError,
    SequenceEngine,
    deterministic_step_job_id,
)
from app.core.sequences.model import RunContext, Step

logger = logging.getLogger(__name__)

#: plan_runs TTL horizon — 7 days after the last write (matches the plan).
_PLAN_RUN_TTL_DAYS = 7

#: How many recent runs per command feed the duration median.
_STEP_MEDIAN_SAMPLE = 20


def _record_step_metric(
    db, *, command: str, step_id: str, vm_id: str, duration_ms: int
) -> None:
    """Insert one ``step_metrics`` duration sample. Observational only — a
    metrics write must never fail a step that just succeeded."""
    try:
        db["step_metrics"].insert_one(
            {
                "command": command,
                "stepId": step_id,
                "vmId": vm_id,
                "durationMs": duration_ms,
                "at": now_ms(),
            }
        )
    except Exception:  # noqa: BLE001 — observational only
        logger.exception("failed to record step metric for %s", command)


def _median_duration_ms(db, query: dict) -> float | None:
    """Median ``durationMs`` of the newest ``_STEP_MEDIAN_SAMPLE`` matches, or
    ``None`` when nothing has been recorded (priors are optional — a failed
    lookup must never fail the run that asked for it)."""
    try:
        docs = (
            db["step_metrics"]
            .find(query, {"durationMs": 1})
            .sort("at", -1)
            .limit(_STEP_MEDIAN_SAMPLE)
        )
        values = [
            d["durationMs"]
            for d in docs
            if isinstance(d.get("durationMs"), (int, float))
        ]
    except Exception:  # noqa: BLE001 — priors are optional
        logger.exception("failed to load step medians for %r", query)
        return None
    return float(statistics.median(values)) if values else None


def load_step_medians(db, steps) -> dict[str, float]:
    """``step_id -> median durationMs`` of the last ``_STEP_MEDIAN_SAMPLE``
    recorded runs *of that step* — the duration priors behind the UI's
    estimated intra-step percent.

    Keyed per step, not per command, because one command can be two very
    different jobs: ``iis.setup_certenroll`` takes ~20s to publish a share
    (``iis-share``) and minutes to stand up IIS (``iis-web``), and a
    command-wide median hands the slow one the fast one's estimate.

    Falls back to the command-wide median for a step with no samples of its
    own. That fallback is load-bearing rather than cosmetic: metrics are
    recorded under the *job key*, so a step that always takes a retry path
    banks its history elsewhere (every ``install-forest`` sample is filed under
    ``install-forest.postreboot``) and would otherwise look brand new. A
    command nobody has ever run is simply absent — that step gets elapsed text
    with no estimate.
    """
    medians: dict[str, float] = {}
    by_command: dict[str, float | None] = {}
    for step in steps:
        step_id, command = step.id, step.command
        if step_id in medians:
            continue
        own = _median_duration_ms(db, {"command": command, "stepId": step_id})
        if own is not None:
            medians[step_id] = own
            continue
        if command not in by_command:
            by_command[command] = _median_duration_ms(db, {"command": command})
        if by_command[command] is not None:
            medians[step_id] = by_command[command]
    return medians


def _load_run_state(
    db, plan_job_id: str
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, dict[str, dict]]]:
    """Return ``(cursor, artifacts, results)`` for a plan run.

    Results are retained per op so a redelivered sequence can rebuild a local
    aggregate from already-completed remote probes instead of silently feeding
    it empty placeholders.
    """
    doc = db["plan_runs"].find_one({"jobId": plan_job_id})
    if doc is None:
        return {}, {}, {}
    return (
        doc.get("cursor") or {},
        doc.get("artifacts") or {},
        doc.get("results") or {},
    )


def _persist_step(
    db,
    plan_job_id: str,
    op_id: str,
    step_id: str,
    result: dict,
    artifact_updates: dict[str, str],
) -> None:
    """Persist a completed step and only the artifacts it newly produced.

    Plan operations fan out across multiple workers.  Replacing the complete
    ``artifacts`` document here lets a worker holding an older snapshot erase
    artifacts concurrently produced by another operation.  Dotted ``$set``
    updates merge each new relay value atomically instead.
    """
    ttl_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        days=_PLAN_RUN_TTL_DAYS
    )
    set_values = {
        f"results.{op_id}.{step_id}": result,
        "updatedAt": now_ms(),
        "ttlAt": ttl_at,
    }
    set_values.update(
        {f"artifacts.{key}": value for key, value in artifact_updates.items()}
    )
    db["plan_runs"].update_one(
        {"jobId": plan_job_id},
        {
            "$addToSet": {f"cursor.{op_id}": step_id},
            "$set": set_values,
            "$setOnInsert": {"jobId": plan_job_id, "createdAt": now_ms()},
        },
        upsert=True,
    )


def _persist_failed_health(
    db,
    plan_job_id: str,
    op_id: str,
    step_id: str,
    result: dict,
) -> None:
    """Retain a failed aggregate without advancing the resumable cursor."""
    ttl_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        days=_PLAN_RUN_TTL_DAYS
    )
    db["plan_runs"].update_one(
        {"jobId": plan_job_id},
        {
            "$set": {
                f"results.{op_id}.{step_id}": result,
                "updatedAt": now_ms(),
                "ttlAt": ttl_at,
            },
            "$setOnInsert": {"jobId": plan_job_id, "createdAt": now_ms()},
        },
        upsert=True,
    )


def run_op_sequence(
    db,
    steps: list[Step],
    ctx: RunContext,
    *,
    plan_job_id: str,
    op_id: str,
    role: str,
    on_step_complete=None,
    on_step_progress=None,
    on_step_tick=None,
    on_step_start=None,
    on_verify_start=None,
    on_verify_done=None,
    should_stop=None,
) -> dict[str, dict]:
    """Run one op's step sequence to completion (or raise
    :class:`~app.core.sequences.engine.SequenceError`).

    Wires the engine's injected effects to production:

    * ``dispatch`` → :func:`agentbus.dispatch_and_wait` under a deterministic
      per-step job id, so a redelivered task reuses the already-terminal result
      instead of re-running a side-effecting command; the agent's own progress
      frames are forwarded to ``on_step_progress(step_id, phase, percent)`` and
      the frameless-poll heartbeat to ``on_step_tick(step_id, elapsed_s)``;
    * every successful dispatch's duration is logged and sampled into
      ``step_metrics`` (the priors :func:`load_step_medians` reads);
    * ``wait_for_reconnect`` → the Mongo ``lastConnectedAt`` + live-key gate;
    * completed steps come from the ``plan_runs`` cursor and each step's
      completion is persisted before the next runs.
    """
    completed_by_op, artifacts, results_by_op = _load_run_state(db, plan_job_id)
    completed = set(completed_by_op.get(op_id, []))
    resumed_results = results_by_op.get(op_id, {})
    ctx.artifacts.update(artifacts)
    persisted_artifacts = dict(ctx.artifacts)

    def dispatch(
        job_key,
        vm_id,
        command,
        params,
        *,
        role,
        secret_keys,
        timeout_s,
        expect_disconnect=False,
    ):
        step_job_id = deterministic_step_job_id(plan_job_id, op_id, job_key)
        on_progress = None
        if on_step_progress is not None:
            on_progress = lambda phase, pct: on_step_progress(job_key, phase, pct)  # noqa: E731
        on_tick = None
        if on_step_tick is not None:
            on_tick = lambda elapsed_s: on_step_tick(job_key, elapsed_s)  # noqa: E731
        started = time.monotonic()
        result = agentbus.dispatch_and_wait(
            vm_id,
            command,
            params,
            job_id=step_job_id,
            role=role,
            timeout_s=timeout_s,
            secret_keys=secret_keys,
            expect_disconnect=expect_disconnect,
            on_progress=on_progress,
            on_tick=on_tick,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "sequence step %s/%s (%s) on %s took %.1fs",
            op_id,
            job_key,
            command,
            vm_id,
            duration_ms / 1000,
        )
        _record_step_metric(
            db,
            command=command,
            step_id=job_key,
            vm_id=vm_id,
            duration_ms=duration_ms,
        )
        return result

    def wait_for_reconnect(vm_id, since_ms, timeout_s):
        agentbus.wait_for_reconnect(vm_id, since_ms, timeout_s, db=db)

    def on_step_done(step_id, result):
        artifact_updates = {
            key: value
            for key, value in ctx.artifacts.items()
            if persisted_artifacts.get(key) != value
        }
        _persist_step(db, plan_job_id, op_id, step_id, result, artifact_updates)
        persisted_artifacts.update(artifact_updates)
        if on_step_complete is not None:
            on_step_complete(step_id)

    engine = SequenceEngine(
        dispatch=dispatch,
        wait_for_reconnect=wait_for_reconnect,
        sleep=time.sleep,
        now_ms=now_ms,
        role=role,
        completed=completed,
        resumed_results=resumed_results,
        on_step_done=on_step_done,
        on_step_start=on_step_start,
        on_verify_start=on_verify_start,
        on_verify_done=on_verify_done,
        should_stop=should_stop,
    )
    try:
        return engine.run(steps, ctx)
    except HealthGateError as exc:
        _persist_failed_health(db, plan_job_id, op_id, exc.step_id, exc.health)
        raise
