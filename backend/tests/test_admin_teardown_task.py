"""The bulk force-destroy task: one job, one stream, per-VM outcomes."""

import os

import pytest
from vmkit.errors import VmkitError, VmNotFoundError

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app import tasks  # noqa: E402
from app.core.jobs.models import DoneMsg, PlanStateMsg  # noqa: E402


class _Conn:
    si = object()
    content = object()


class _FakeWorkerDb:
    def __enter__(self):
        return {}

    def __exit__(self, *exc):
        return False


@pytest.fixture
def harness(monkeypatch):
    """Record every published frame and every side effect the task performs."""
    published: list = []
    released: list[str] = []
    upserts: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        tasks.transport,
        "publish",
        lambda job_id, msg, **kw: published.append(msg),
    )
    monkeypatch.setattr(tasks.transport, "cancel_mode", lambda job_id: None)
    monkeypatch.setattr(tasks, "worker_db", _FakeWorkerDb)
    monkeypatch.setattr(tasks, "_live_worker_connection", lambda conn: _Conn())
    monkeypatch.setattr(tasks, "Disconnect", lambda si: None)
    monkeypatch.setattr(
        tasks, "release_ip_sync", lambda db, name: released.append(name)
    )
    monkeypatch.setattr(
        tasks,
        "_registry_upsert_sync",
        lambda db, name, **fields: upserts.append((name, fields)),
    )
    return {
        "published": published,
        "released": released,
        "upserts": upserts,
        "monkeypatch": monkeypatch,
    }


def _destroys(harness, behaviour):
    harness["monkeypatch"].setattr(
        tasks,
        "destroy_workflow",
        lambda conn, name, progress=None: behaviour(name),
    )


def _done(published) -> DoneMsg:
    return next(msg for msg in published if isinstance(msg, DoneMsg))


def _plan_states(published) -> list[PlanStateMsg]:
    return [msg for msg in published if isinstance(msg, PlanStateMsg)]


def test_every_vm_is_destroyed_released_and_marked_deleted(harness):
    _destroys(harness, lambda name: None)

    tasks.admin_teardown_task("job-1", ["lab-dc01", "lab-ca01"], "admin@corp.com")

    assert harness["released"] == ["lab-dc01", "lab-ca01"]
    assert [name for name, _ in harness["upserts"]] == ["lab-dc01", "lab-ca01"]
    assert harness["upserts"][0][1]["status"] == "deleted"
    # The agent identity is revoked with the row, not left dangling.
    assert harness["upserts"][0][1]["agent"] is None

    result = _done(harness["published"]).result
    assert result["destroyed"] == ["lab-dc01", "lab-ca01"]
    assert result["failed"] == []


def test_a_vm_already_gone_converges_with_its_address_still_released(harness):
    # A half-failed clone leaves only registry and IP state; teardown is the
    # one path that reclaims it, so absence cannot be an error.
    _destroys(harness, lambda name: (_ for _ in ()).throw(VmNotFoundError(name)))

    tasks.admin_teardown_task("job-1", ["lab-dc01"], "admin@corp.com")

    assert harness["released"] == ["lab-dc01"]
    result = _done(harness["published"]).result
    assert result["alreadyAbsent"] == ["lab-dc01"]
    assert result["destroyed"] == []


def test_a_failure_isolates_to_one_vm_and_leaves_its_allocation_alone(harness):
    def _behaviour(name):
        if name == "lab-dc01":
            raise VmkitError("host is busy")

    _destroys(harness, _behaviour)

    tasks.admin_teardown_task("job-1", ["lab-dc01", "lab-ca01"], "admin@corp.com")

    # The failed VM may still exist and still be using its address.
    assert harness["released"] == ["lab-ca01"]
    assert [name for name, _ in harness["upserts"]] == ["lab-ca01"]

    result = _done(harness["published"]).result
    assert result["failed"] == ["lab-dc01"]
    assert result["destroyed"] == ["lab-ca01"]
    assert result["warnings"] == ["lab-dc01: host is busy"]
    # A traceback is exactly what a force-destroy failure needs to be diagnosed.
    assert result["ops"]["lab-dc01"]["trace"]


def test_a_connection_failure_on_one_vm_does_not_end_the_run(harness):
    attempts: list[int] = []

    def _connection(conn):
        attempts.append(1)
        if len(attempts) == 1:
            raise VmkitError("ESXi session died")
        return _Conn()

    harness["monkeypatch"].setattr(tasks, "_live_worker_connection", _connection)
    _destroys(harness, lambda name: None)

    tasks.admin_teardown_task("job-1", ["lab-dc01", "lab-ca01"], "admin@corp.com")

    result = _done(harness["published"]).result
    assert result["failed"] == ["lab-dc01"]
    assert result["destroyed"] == ["lab-ca01"]


def test_every_published_frame_carries_all_targets(harness):
    # The Valkey snapshot only holds the last message, so a reconnecting socket
    # has to be able to rebuild complete state from any single frame.
    _destroys(harness, lambda name: None)

    tasks.admin_teardown_task("job-1", ["lab-dc01", "lab-ca01"], "admin@corp.com")

    states = _plan_states(harness["published"])
    assert states
    for msg in states:
        assert set(msg.ops) == {"lab-dc01", "lab-ca01"}
    # Seeded pending before any work starts, so the console draws rows at once.
    assert all(op.status == "pending" for op in states[0].ops.values())


def test_a_single_vm_selection_still_streams_plan_state(harness):
    _destroys(harness, lambda name: None)

    tasks.admin_teardown_task("job-1", ["lab-dc01"], "admin@corp.com")

    assert _plan_states(harness["published"])


def test_everything_failing_still_terminates_as_a_completed_job(harness):
    # The job ran; the VMs didn't. Reporting that as a terminal error would
    # lose the per-VM detail the console renders.
    _destroys(harness, lambda name: (_ for _ in ()).throw(VmkitError("nope")))

    tasks.admin_teardown_task("job-1", ["lab-dc01", "lab-ca01"], "admin@corp.com")

    result = _done(harness["published"]).result
    assert result["failed"] == ["lab-dc01", "lab-ca01"]
    assert result["destroyed"] == []
