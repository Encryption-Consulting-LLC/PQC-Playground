"""Rebuilding a job's messages from its persisted plan run, once Valkey forgets.

The incident these cover: a deploy finished with one failed operation while the
browser was closed. The terminal snapshot evicted 10 minutes later, so the
reconnecting tab got a 4404 and the failure was never reported — even though
``plan_runs`` still held every op's outcome.
"""

from app.core.jobs.models import JobStatus
from app.core.jobs.replay import replay_plan_run
from app.core.jobs.transport import ACTIVE_TTL_SECONDS

NOW = 1_785_757_000_000
IIS_FAILURE = (
    "agent command 'iis.setup_certenroll' failed: powershell execution failed: "
    "script exited with code 1: Install-WindowsFeature : The request for the "
    "server to discover installed features timed out."
)


def _run(ops: dict, *, updated_at: int = NOW) -> dict:
    return {
        "jobId": "5fd68414aff44884b9d77f2b6d319400",
        "owner": "guest",
        "updatedAt": updated_at,
        "scheduler": {"ops": ops, "updatedAt": updated_at},
    }


def test_a_finished_plan_replays_as_one_done_message() -> None:
    doc = _run(
        {
            "clone-dc": {"status": "done", "percent": 100},
            "iis-web": {"status": "error", "detail": IIS_FAILURE},
        }
    )

    status, messages = replay_plan_run(doc, now_ms=NOW)

    assert status is JobStatus.done
    assert [msg.type for msg in messages] == ["done"]
    # The client's finishDeploy reads per-op statuses straight out of `result`.
    assert messages[0].result["ops"]["iis-web"]["detail"] == IIS_FAILURE
    assert messages[0].result["ops"]["clone-dc"]["status"] == "done"


def test_a_cancelled_op_still_counts_as_terminal() -> None:
    doc = _run(
        {
            "clone-dc": {"status": "error", "detail": "boom"},
            "join-ca": {
                "status": "cancelled",
                "detail": "Skipped: a dependency failed.",
            },
        }
    )

    status, messages = replay_plan_run(doc, now_ms=NOW)

    assert status is JobStatus.done
    assert [msg.type for msg in messages] == ["done"]


def test_a_live_plan_replays_as_non_terminal_state_and_keeps_streaming() -> None:
    doc = _run(
        {
            "clone-dc": {"status": "done"},
            "provision-dc": {"status": "running", "percent": 40},
        },
        updated_at=NOW - 30_000,
    )

    status, messages = replay_plan_run(doc, now_ms=NOW)

    assert status is JobStatus.running
    # Non-terminal on purpose: the socket goes on to stream live publishes.
    assert [msg.type for msg in messages] == ["plan-state"]
    assert messages[0].ops["provision-dc"].percent == 40


def test_an_abandoned_plan_gets_a_terminal_frame_so_the_canvas_unlocks() -> None:
    doc = _run(
        {
            "clone-dc": {"status": "done"},
            "provision-dc": {"status": "running", "percent": 40},
        },
        updated_at=NOW - (ACTIVE_TTL_SECONDS * 1000 + 1),
    )

    status, messages = replay_plan_run(doc, now_ms=NOW)

    assert status is JobStatus.error
    # State first (so the rows show how far it got), then the terminal frame.
    assert [msg.type for msg in messages] == ["plan-state", "error"]
    assert messages[1].status == 503


def test_a_job_with_nothing_persisted_is_not_replayable() -> None:
    # A clone or teardown job: no plan run at all, so the caller still 4404s.
    assert replay_plan_run(None, now_ms=NOW) is None
    # A plan run that died before its first op was persisted.
    assert replay_plan_run({"jobId": "x", "owner": "guest"}, now_ms=NOW) is None
    assert replay_plan_run(_run({}), now_ms=NOW) is None
