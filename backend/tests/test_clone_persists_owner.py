"""A cloned VM records who deployed it, and into which project.

The registry is the only durable link between a VM and the lab it belongs to.
Attribution is written on the FIRST (``cloning``) upsert on purpose: a clone
that then fails leaves an errored row, and an errored row nobody can attribute
is exactly what the admin teardown console has to be able to group.
"""

import os

import pytest

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app import tasks  # noqa: E402
from app.core.golden_image import GoldenImageConfig  # noqa: E402
from app.core.ippool import GuestNetwork  # noqa: E402
from app.routers.deploy import PlanOp  # noqa: E402

PROJECT_ID = "467893ab-cdef-4321-abcd-123456789abc"


class _Conn:
    content = object()
    si = object()


def _image() -> GoldenImageConfig:
    return GoldenImageConfig(
        base="ws-2025-base",
        datastore="datastore1",
        expectedGuestOs="windows2022srvNext-64",
        network="VM Network",
        maxUsagePct=80.0,
    )


def _run_clone(monkeypatch, **kwargs) -> list[tuple[str, dict]]:
    """Drive ``_run_clone_op`` far enough to reach the registry write, then
    abort at ISO build — everything after it is unrelated to attribution."""
    upserts: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        tasks,
        "load_guest_network_sync",
        lambda db: GuestNetwork(
            ip_start="10.0.20.50",
            ip_end="10.0.20.60",
            prefix=24,
            gateway="10.0.20.1",
            dns1="10.0.20.1",
        ),
    )
    monkeypatch.setattr(
        tasks, "allocate_ip_sync", lambda db, name, job_id: "10.0.20.51"
    )
    monkeypatch.setattr(
        tasks,
        "_registry_upsert_sync",
        lambda db, name, **fields: upserts.append((name, fields)),
    )
    monkeypatch.setattr(tasks.settings, "executor_agent_path", None)
    monkeypatch.setattr(tasks, "get_vm_by_name", lambda content, name: None)
    monkeypatch.setattr(tasks, "_cleanup_failed_clone", lambda *a, **kw: None)

    def _boom(**_kwargs):
        raise RuntimeError("stop after the registry write")

    monkeypatch.setattr(tasks, "build_firstboot_iso", _boom)

    op = PlanOp(id="clone-dc", kind="createVm", target="dc-node")
    op.params = {"vmName": "guest-alice-467893-dc01", "template": "domainController"}

    ok = tasks._run_clone_op(
        _Conn(),
        {},
        op,
        "job-1",
        {},
        lambda: None,
        "guest",
        _image(),
        **kwargs,
    )
    assert ok is False
    return upserts


def test_the_cloning_upsert_carries_owner_project_and_node(monkeypatch):
    upserts = _run_clone(monkeypatch, owner="alice@corp.com", project_id=PROJECT_ID)

    name, fields = upserts[0]
    assert name == "guest-alice-467893-dc01"
    assert fields["status"] == "cloning"
    assert fields["owner"] == "alice@corp.com"
    assert fields["projectId"] == PROJECT_ID
    # The code is what a name-parsed legacy row can also produce, so grouping
    # keys on it — and it must match what the VM name was built with.
    assert fields["projectCode"] == "467893"
    # Reviving the long-dormant (projectId, nodeId) index.
    assert fields["nodeId"] == "dc-node"


def test_a_clone_with_no_project_context_stores_no_fabricated_code(monkeypatch):
    upserts = _run_clone(monkeypatch, owner="ops@corp.com")

    _, fields = upserts[0]
    assert fields["owner"] == "ops@corp.com"
    assert fields["projectId"] is None
    assert fields["projectCode"] is None


def test_attribution_threads_through_from_the_plan_operation_task(monkeypatch):
    """The task holds both values already — this is the wiring that hands them on."""
    captured: dict = {}

    monkeypatch.setattr(
        tasks,
        "_run_clone_op",
        lambda *args, **kwargs: captured.update(kwargs) or True,
    )
    monkeypatch.setattr(tasks, "_open_worker_connection", lambda: _Conn())
    monkeypatch.setattr(tasks, "Disconnect", lambda si: None)
    monkeypatch.setattr(tasks, "deployment_profiles_from_doc", lambda doc: _Profiles())
    monkeypatch.setattr(tasks, "role_for_template", lambda template, ca_type: "dc")
    monkeypatch.setattr(
        tasks,
        "_scheduler_states",
        lambda db, job_id: {"clone-dc": tasks.OpRunState(status="running")},
    )
    monkeypatch.setattr(tasks, "_persist_scheduler_op", lambda *a, **kw: None)
    monkeypatch.setattr(tasks, "_publish_scheduler_states", lambda db, job_id: {})
    monkeypatch.setattr(tasks, "_advance_plan", lambda *a, **kw: None)
    monkeypatch.setattr(tasks.transport, "cancel_mode", lambda job_id: None)
    monkeypatch.setattr(tasks, "worker_db", _FakeWorkerDb)

    import app.core.topology as topology_module

    plan = {
        "projectId": PROJECT_ID,
        "topology": {
            "version": 1,
            "nodes": [{"id": "dc-node", "name": "DC-01", "role": "domainController"}],
            "edges": [],
        },
        "ops": [
            {
                "id": "clone-dc",
                "kind": "createVm",
                "target": "dc-node",
                "params": {
                    "vmName": "guest-alice-467893-dc01",
                    "template": "domainController",
                },
            }
        ],
    }
    from app.routers.deploy import DeployRequest

    compiled_ops = DeployRequest(**plan).ops
    monkeypatch.setattr(
        topology_module,
        "compile_plan",
        lambda topology, ops: _Compiled(compiled_ops),
    )

    tasks.run_plan_operation_task(
        "job-1", "clone-dc", plan, "guest", None, "alice@corp.com"
    )

    assert captured == {"owner": "alice@corp.com", "project_id": PROJECT_ID}


class _Compiled:
    def __init__(self, operations):
        self.operations = operations


class _Profiles(dict):
    def __missing__(self, key):
        return _image()


class _FakeWorkerDb:
    """Just the plan_runs claim and the settings read the task performs."""

    def __enter__(self):
        return {
            "plan_runs": _FakeClaim(),
            "settings": _FakeSettings(),
        }

    def __exit__(self, *exc):
        return False


class _FakeClaim:
    def update_one(self, query, update):
        return _Claimed()


class _Claimed:
    modified_count = 1


class _FakeSettings:
    def find_one(self, query):
        return {"_id": "global"}


@pytest.fixture(autouse=True)
def _quiet_transport(monkeypatch):
    monkeypatch.setattr(tasks.transport, "publish", lambda *a, **kw: None)
