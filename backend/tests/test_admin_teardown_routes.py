"""The admin teardown routes: what they show, and what they refuse to do.

Two failure contracts pull in opposite directions here — the listings must
survive an unreachable ESXi host, the destructive paths must refuse to act
without it — so both are pinned.
"""

import asyncio
import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.authz import AuthedUser, Role  # noqa: E402
from app.routers import admin_teardown  # noqa: E402

NOW_ISH = 1_800_000_000_000


def _admin() -> AuthedUser:
    return AuthedUser(username="admin@corp.com", role=Role.ADMIN, auth="local")


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        async def _gen():
            for doc in self._docs:
                yield doc

        return _gen()

    async def to_list(self, length=None):
        return list(self._docs)


class _Collection:
    def __init__(self, docs=(), journal=None):
        self.docs = [dict(d) for d in docs]
        self.deleted: list[str] = []
        #: Shared across collections, so the release-before-delete ordering is
        #: observable rather than inferred.
        self.journal: list[str] = journal if journal is not None else []

    def find(self, query=None, projection=None):
        query = query or {}
        names = (query.get("vmName") or {}).get("$in")
        ids = (query.get("_id") or {}).get("$in")
        docs = self.docs
        if names is not None:
            docs = [d for d in docs if d.get("vmName") in names]
        if ids is not None:
            docs = [d for d in docs if d.get("_id") in ids]
        if query.get("status"):
            docs = [d for d in docs if d.get("status") == query["status"]]
        return _Cursor(docs)

    async def find_one(self, query, projection=None):
        return next(iter(self.find(query)._docs), None)

    async def delete_one(self, query):
        self.journal.append(f"delete:{query['vmName']}")
        self.deleted.append(query["vmName"])
        self.docs = [d for d in self.docs if d.get("vmName") != query["vmName"]]

    async def update_many(self, query, update):
        self.journal.append(f"release:{query['vmName']}")


def _registry_row(vm_name: str, **fields) -> dict:
    doc = {
        "_id": vm_name,
        "vmName": vm_name,
        "appName": vm_name,
        "status": "ready",
        "ip": None,
        "createdAt": NOW_ISH,
        "updatedAt": NOW_ISH,
    }
    doc.update(fields)
    return doc


@pytest.fixture(autouse=True)
def _reset_inventory_cache():
    admin_teardown._inventory_cache = None
    yield
    admin_teardown._inventory_cache = None


@pytest.fixture
def wired(monkeypatch):
    """Point the router at fake collections and a controllable inventory."""
    journal: list[str] = []
    registry = _Collection(journal=journal)
    pool = _Collection(journal=journal)
    shared = {
        "inventory": set(),
        "registry": registry,
        "pool": pool,
        "journal": journal,
    }

    import app.core.ippool as ippool_module

    monkeypatch.setattr(admin_teardown, "vm_registry_col", lambda: registry)
    monkeypatch.setattr(admin_teardown, "ip_pool_col", lambda: pool)
    monkeypatch.setattr(ippool_module, "ip_pool_col", lambda: pool)
    monkeypatch.setattr(admin_teardown, "users_col", lambda: _Collection())
    monkeypatch.setattr(admin_teardown, "projects_col", lambda: _Collection())

    async def _inventory(*, fresh=False):
        return shared["inventory"]

    monkeypatch.setattr(admin_teardown, "read_inventory", _inventory)
    return shared


# --------------------------------------------------------------------------- #
# Listing degrades, never 502s                                                #
# --------------------------------------------------------------------------- #
def test_environments_render_when_esxi_cannot_be_reached(wired):
    wired["registry"].docs = [_registry_row("guest-alice-cc92f3-dc01")]
    wired["inventory"] = None

    result = asyncio.run(admin_teardown.list_environments())

    assert result["esxiReachable"] is False
    assert result["count"] == 1
    vm = result["environments"][0]["vms"][0]
    # Absence is unknown, so it is neither claimed nor denied.
    assert vm["presentInEsxi"] is None
    assert vm["signals"] == []


def test_orphans_carry_the_owner_their_environment_shows(wired):
    wired["registry"].docs = [
        _registry_row("guest-alice-cc92f3-dc01", owner="alice@corp.com")
    ]
    wired["inventory"] = set()  # readable, and the VM is gone

    result = asyncio.run(admin_teardown.list_orphans())

    assert result["count"] == 1
    assert result["orphans"][0]["owner"] == "alice@corp.com"
    assert result["orphans"][0]["signals"] == ["absentFromEsxi"]


def test_read_inventory_returns_none_with_no_configured_target(monkeypatch):
    async def _no_target():
        return None

    monkeypatch.setattr(admin_teardown, "load_target", _no_target)

    assert asyncio.run(admin_teardown.read_inventory()) is None


# --------------------------------------------------------------------------- #
# Preview                                                                     #
# --------------------------------------------------------------------------- #
def _selection(*names) -> admin_teardown.SelectionRequest:
    return admin_teardown.SelectionRequest(vmNames=list(names))


def test_preview_refuses_when_esxi_is_unreachable(wired):
    wired["registry"].docs = [_registry_row("lab-dc01")]
    wired["inventory"] = None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_teardown.preview_teardown(_selection("lab-dc01")))

    assert exc.value.status_code == 503


def test_preview_fails_whole_on_an_unknown_name(wired):
    wired["registry"].docs = [_registry_row("lab-dc01")]

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            admin_teardown.preview_teardown(_selection("lab-dc01", "lab-ghost"))
        )

    assert exc.value.status_code == 404
    assert "lab-ghost" in exc.value.detail


def test_preview_reads_the_address_from_the_pool_not_the_registry(wired):
    # An errored row can carry no ip while still holding an allocation — that
    # address is exactly what the teardown reclaims.
    wired["registry"].docs = [_registry_row("lab-dc01", status="error", ip=None)]
    wired["pool"].docs = [
        {"_id": "10.0.20.51", "vmName": "lab-dc01", "status": "allocated"}
    ]
    wired["inventory"] = {"lab-dc01"}

    result = asyncio.run(admin_teardown.preview_teardown(_selection("lab-dc01")))

    assert result["vms"][0]["releasesIp"] == "10.0.20.51"
    assert result["releasesIp"] == 1
    assert (result["toDestroy"], result["alreadyAbsent"]) == (1, 0)


def test_the_confirm_token_tracks_presence_not_just_names(wired):
    wired["registry"].docs = [_registry_row("lab-dc01")]
    wired["inventory"] = {"lab-dc01"}
    present = asyncio.run(admin_teardown.preview_teardown(_selection("lab-dc01")))

    wired["inventory"] = set()
    absent = asyncio.run(admin_teardown.preview_teardown(_selection("lab-dc01")))

    assert present["confirmToken"] != absent["confirmToken"]


# --------------------------------------------------------------------------- #
# Execute                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def enqueued(monkeypatch):
    calls: list[str] = []
    revoked: list[list[str]] = []
    delayed: list[tuple] = []

    monkeypatch.setattr(
        admin_teardown.transport,
        "publish",
        lambda job_id, msg, **kw: calls.append(f"publish:{msg.type}"),
    )

    async def _revoke(names):
        calls.append("revoke")
        revoked.append(list(names))
        return []

    monkeypatch.setattr(admin_teardown.agents, "revoke_live_agent_sockets", _revoke)

    import app.tasks as tasks_module

    monkeypatch.setattr(
        tasks_module.admin_teardown_task,
        "delay",
        lambda *args: (calls.append("delay"), delayed.append(args))[0],
    )
    return {"calls": calls, "revoked": revoked, "delayed": delayed}


def _confirm(names, token) -> admin_teardown.TeardownRequest:
    return admin_teardown.TeardownRequest(vmNames=list(names), confirmToken=token)


def test_enqueue_queues_and_revokes_before_handing_off_to_the_worker(wired, enqueued):
    wired["registry"].docs = [_registry_row("lab-dc01")]
    wired["inventory"] = {"lab-dc01"}
    preview = asyncio.run(admin_teardown.preview_teardown(_selection("lab-dc01")))

    result = asyncio.run(
        admin_teardown.start_teardown(
            _confirm(["lab-dc01"], preview["confirmToken"]), _admin()
        )
    )

    assert result["count"] == 1
    assert result["vmNames"] == ["lab-dc01"]
    assert result["jobId"]
    # A client attaching immediately must find the job already queued, and the
    # agent must be cut loose before the worker starts destroying its VM.
    assert enqueued["calls"] == ["revoke", "publish:queued", "delay"]
    assert enqueued["revoked"] == [["lab-dc01"]]
    assert enqueued["delayed"][0][1] == ["lab-dc01"]


def test_enqueue_409s_when_the_selection_drifted_since_the_preview(wired, enqueued):
    wired["registry"].docs = [_registry_row("lab-dc01")]
    wired["inventory"] = {"lab-dc01"}
    preview = asyncio.run(admin_teardown.preview_teardown(_selection("lab-dc01")))

    wired["inventory"] = set()  # the VM vanished between preview and confirm

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            admin_teardown.start_teardown(
                _confirm(["lab-dc01"], preview["confirmToken"]), _admin()
            )
        )

    assert exc.value.status_code == 409
    assert "delay" not in enqueued["calls"]


# --------------------------------------------------------------------------- #
# Purge                                                                       #
# --------------------------------------------------------------------------- #
def test_purge_refuses_a_row_whose_vm_is_still_on_the_host(wired):
    wired["registry"].docs = [_registry_row("lab-dc01")]
    wired["inventory"] = {"lab-dc01"}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_teardown.purge_orphans(_selection("lab-dc01"), _admin()))

    assert exc.value.status_code == 409
    assert wired["registry"].deleted == []


def test_purge_refuses_when_the_inventory_cannot_confirm_absence(wired):
    wired["registry"].docs = [_registry_row("lab-dc01")]
    wired["inventory"] = None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_teardown.purge_orphans(_selection("lab-dc01"), _admin()))

    assert exc.value.status_code == 503


def test_purge_allows_an_already_deleted_row_even_without_the_host(wired):
    wired["registry"].docs = [_registry_row("lab-dc01", status="deleted")]
    wired["inventory"] = None

    result = asyncio.run(admin_teardown.purge_orphans(_selection("lab-dc01"), _admin()))

    assert result["purged"] == ["lab-dc01"]


def test_purge_releases_the_address_before_deleting_its_only_record(wired):
    # The row is the last link to the allocation; the reverse order leaks it.
    wired["registry"].docs = [_registry_row("lab-dc01")]
    wired["inventory"] = set()

    asyncio.run(admin_teardown.purge_orphans(_selection("lab-dc01"), _admin()))

    assert wired["journal"] == ["release:lab-dc01", "delete:lab-dc01"]
