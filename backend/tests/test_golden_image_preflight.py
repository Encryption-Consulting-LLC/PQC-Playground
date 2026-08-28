"""Read-only Windows golden-image ESXi preflight."""

import os
import asyncio
from types import SimpleNamespace

import pytest

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import golden_image  # noqa: E402
from app.core.datastore_image import DatastoreVmxFacts  # noqa: E402
from app import tasks  # noqa: E402
from app.routers import settings as settings_router  # noqa: E402
from app.routers.deploy import PlanOp  # noqa: E402


def _config(**overrides):
    values = {
        "base": "ws-2025-base",
        "datastore": "datastore1",
        "expectedGuestOs": "windows2022srvNext-64",
        "network": "VM Network",
        "maxUsagePct": 80,
    }
    values.update(overrides)
    return golden_image.GoldenImageConfig(**values)


def _patch_inventory(
    monkeypatch,
    *,
    guest_os="windows2022srvNext_64Guest",
    names=None,
    capacity=1000,
    free=600,
    vmdk=100,
):
    monkeypatch.setattr(
        golden_image,
        "read_datastore_vmx",
        lambda _conn, datastore, base: DatastoreVmxFacts(
            path=f"[{datastore}] {base}/{base}.vmx",
            revision="vmx-sha256:" + "a" * 64,
            guest_os=guest_os,
            networks=frozenset({"VM Network"}),
        ),
    )
    monkeypatch.setattr(
        golden_image, "list_vm_names", lambda _content: set(names or {"ws-2025-base"})
    )
    datastore = SimpleNamespace(
        summary=SimpleNamespace(capacity=capacity, freeSpace=free),
    )
    monkeypatch.setattr(
        golden_image, "get_datastore", lambda _content, _name: datastore
    )
    monkeypatch.setattr(golden_image, "get_base_vmdk_size", lambda _ds, _base: vmdk)


def _run(names=None, count=None):
    return golden_image.preflight_golden_image(
        SimpleNamespace(
            content=SimpleNamespace(about=SimpleNamespace(instanceUuid="esxi-1"))
        ),
        _config(),
        requested_vm_names=names,
        clone_count=count,
    )


def test_ready_image_returns_capacity_and_identity_snapshot(monkeypatch):
    _patch_inventory(monkeypatch)

    result = _run(["DC01", "CA01"], 2)

    assert result.ready is True
    assert result.base_moid is None
    assert result.base_change_version == "vmx-sha256:" + "a" * 64
    assert result.esxi_instance_uuid == "esxi-1"
    assert result.actual_guest_os == "windows2022srvNext_64Guest"
    assert result.required_bytes == 200
    assert result.projected_usage_pct == 60
    assert len(result.snapshot_id) == 64
    assert all(check.ok for check in result.checks)


def test_missing_image_and_wrong_os_are_reported(monkeypatch):
    _patch_inventory(monkeypatch)
    monkeypatch.setattr(
        golden_image,
        "read_datastore_vmx",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("HTTP 404")),
    )

    result = _run()

    assert result.ready is False
    assert next(check for check in result.checks if check.key == "image").ok is False
    assert next(check for check in result.checks if check.key == "guestOs").ok is False


def test_non_windows_or_mismatched_guest_os_fails(monkeypatch):
    _patch_inventory(monkeypatch, guest_os="ubuntu64Guest")

    result = _run()

    guest = next(check for check in result.checks if check.key == "guestOs")
    assert guest.ok is False
    assert "ubuntu64Guest" in guest.detail


def test_capacity_is_reserved_for_every_requested_clone(monkeypatch):
    _patch_inventory(monkeypatch, capacity=1000, free=300, vmdk=100)

    result = _run(["DC01", "CA01", "CA02", "SRV1"], 4)

    assert result.ready is False
    capacity = next(check for check in result.checks if check.key == "capacity")
    assert capacity.ok is False
    assert result.projected_usage_pct == 110


def test_existing_requested_vm_name_fails(monkeypatch):
    _patch_inventory(monkeypatch, names={"ws-2025-base", "CA02"})

    result = _run(["DC01", "CA02"], 2)

    collision = next(check for check in result.checks if check.key == "vmNames")
    assert collision.ok is False
    assert "CA02" in collision.detail


def test_legacy_settings_document_uses_environment_defaults():
    config = golden_image.golden_image_config_from_doc({})

    assert config.base == golden_image.settings.clone_base
    assert config.datastore == golden_image.settings.clone_datastore


def test_settings_validation_endpoint_returns_wire_snapshot(monkeypatch):
    class Collection:
        async def find_one(self, _query):
            return {
                "cloneBase": "ws-2025-base",
                "cloneDatastore": "datastore1",
                "cloneGuestOs": "windows2022srvNext-64",
                "cloneMaxUsagePct": 80,
            }

    _patch_inventory(monkeypatch)
    monkeypatch.setattr(settings_router, "settings_col", lambda: Collection())

    async def run_now(call):
        return call()

    monkeypatch.setattr(settings_router, "run_in_threadpool", run_now)

    async def fake_acquire():
        return SimpleNamespace(content=object()), None

    monkeypatch.setattr(settings_router, "_acquire_esxi", fake_acquire)

    response = asyncio.run(
        settings_router.validate_golden_image(
            settings_router.GoldenImageValidationRequest(
                requestedVmNames=["DC01", "CA01"],
            ),
        )
    )

    assert response["ready"] is True
    assert response["cloneCount"] == 2
    assert response["baseMoid"] is None
    assert len(response["snapshotId"]) == 64


def test_snapshot_changes_when_esxi_host_identity_changes(monkeypatch):
    _patch_inventory(monkeypatch)
    first = _run(["DC01"], 1)
    second = golden_image.preflight_golden_image(
        SimpleNamespace(
            content=SimpleNamespace(about=SimpleNamespace(instanceUuid="esxi-2"))
        ),
        _config(),
        requested_vm_names=["DC01"],
        clone_count=1,
    )

    assert first.snapshot_id != second.snapshot_id


class _SettingsCollection:
    def find_one(self, _query):
        return {
            "cloneBase": "ws-2025-base",
            "cloneDatastore": "datastore1",
            "cloneGuestOs": "windows2022srvNext-64",
            "cloneMaxUsagePct": 80,
        }


class _WorkerDb:
    def __getitem__(self, name):
        assert name == "settings"
        return _SettingsCollection()


def test_worker_accepts_an_unchanged_preflight(monkeypatch):
    _patch_inventory(monkeypatch)
    accepted = _run(["DC01"], 1)
    monkeypatch.setattr(
        tasks, "preflight_golden_image", lambda *_args, **_kwargs: accepted
    )
    op = PlanOp(
        id="create-dc",
        kind="createVm",
        target="dc",
        params={"vmName": "DC01", "template": "domainController"},
    )

    config = tasks._verify_worker_preflight(
        SimpleNamespace(content=object()),
        _WorkerDb(),
        [op],
        accepted.model_dump(by_alias=True),
    )

    assert config.base == "ws-2025-base"
    assert tasks._plan_clone_defaults(config)["datastore"] == "datastore1"


def test_worker_rejects_changed_preflight_before_clone(monkeypatch):
    _patch_inventory(monkeypatch)
    current = _run(["DC01"], 1)
    accepted = current.model_copy(update={"snapshot_id": "0" * 64})
    monkeypatch.setattr(
        tasks, "preflight_golden_image", lambda *_args, **_kwargs: current
    )
    op = PlanOp(
        id="create-dc",
        kind="createVm",
        target="dc",
        params={"vmName": "DC01", "template": "domainController"},
    )

    with pytest.raises(RuntimeError, match="changed after preflight"):
        tasks._verify_worker_preflight(
            SimpleNamespace(content=object()),
            _WorkerDb(),
            [op],
            accepted.model_dump(by_alias=True),
        )


def test_worker_rejects_clone_job_without_snapshot():
    with pytest.raises(RuntimeError, match="missing"):
        tasks._verify_worker_preflight(
            SimpleNamespace(content=object()), _WorkerDb(), [], None
        )
