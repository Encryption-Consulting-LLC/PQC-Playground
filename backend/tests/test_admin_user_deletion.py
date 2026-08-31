"""Deleting an account: what it refuses, and what it takes with it.

Disabling was the only revocation for a long time, and the reason given for
that was that a deleted account leaves ``owner`` references dangling. Deletion
therefore has to answer that objection rather than ignore it, which it does in
two halves — and both halves are what this pins.

The *refusals* cover the references that still point at something real: a VM
the account owns (deleting the account removes the only self-service door to
it, stranding it in a finite pool) and a deployment still running under that
owner (it would go on cloning machines for an account that no longer exists).

The *cascade* covers the references that become unreachable the moment the
document is gone — projects, shares, and the lab codes minted against them,
that last one being a live access path for strangers rather than dead data.
Anything not in one of those two lists is deliberately left alone.
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
from app.routers import admin_users  # noqa: E402


class _FakeCollection:
    """Enough Mongo to exercise the owner filters this route is made of."""

    def __init__(self, docs: list[dict] | None = None) -> None:
        self.docs: list[dict] = [dict(d) for d in docs or []]

    def _matches(self, doc: dict, query: dict) -> bool:
        for key, expected in query.items():
            actual = doc
            for part in key.split("."):
                actual = (actual or {}).get(part) if isinstance(actual, dict) else None
            if isinstance(expected, dict):
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
                if (
                    "$exists" in expected
                    and (actual is not None) != expected["$exists"]
                ):
                    return False
            elif actual != expected:
                return False
        return True

    async def find_one(self, query: dict, projection: dict | None = None):
        return next((dict(d) for d in self.docs if self._matches(d, query)), None)

    def find(self, query: dict | None = None, projection: dict | None = None):
        matched = [dict(d) for d in self.docs if self._matches(d, query or {})]

        class _Cursor:
            def __aiter__(self):
                async def gen():
                    for doc in matched:
                        yield doc

                return gen()

        return _Cursor()

    async def delete_one(self, query: dict):
        return await self.delete_many(query, limit=1)

    async def delete_many(self, query: dict, limit: int | None = None):
        kept, removed = [], 0
        for doc in self.docs:
            if self._matches(doc, query) and (limit is None or removed < limit):
                removed += 1
            else:
                kept.append(doc)
        self.docs = kept
        return type("R", (), {"deleted_count": removed})()


@pytest.fixture
def db(monkeypatch) -> dict[str, _FakeCollection]:
    cols = {
        "users": _FakeCollection(
            [
                {"_id": "guest-a", "username": "guest-a", "role": "guest"},
                {"_id": "root", "username": "root", "role": "admin"},
            ]
        ),
        "vm_registry": _FakeCollection(),
        "plan_runs": _FakeCollection(),
        "projects": _FakeCollection(),
        "project_shares": _FakeCollection(),
        "lab_invites": _FakeCollection(),
    }
    for name, col in cols.items():
        monkeypatch.setattr(admin_users, f"{name}_col", lambda col=col: col)
    return cols


def _admin(username: str = "root") -> AuthedUser:
    return AuthedUser(username=username, role=Role.ADMIN, auth="local")


def _delete(username: str, admin: AuthedUser | None = None) -> dict:
    return asyncio.run(admin_users.delete_user(username, admin or _admin()))


def test_deleting_an_account_removes_the_document(db) -> None:
    result = _delete("guest-a")

    assert [d["username"] for d in db["users"].docs] == ["root"]
    assert result["username"] == "guest-a"


def test_an_unknown_account_is_a_404(db) -> None:
    with pytest.raises(HTTPException) as excinfo:
        _delete("nobody")
    assert excinfo.value.status_code == 404


def test_an_admin_cannot_delete_the_account_it_is_signed_in_with(db) -> None:
    """Same self-lockout guard as disabling, except this one has no undo."""
    with pytest.raises(HTTPException) as excinfo:
        _delete("root")

    assert excinfo.value.status_code == 422
    assert [d["username"] for d in db["users"].docs] == ["guest-a", "root"]


def test_an_account_that_still_owns_a_vm_is_refused(db) -> None:
    """The account is the only self-service teardown door to its own VMs."""
    db["vm_registry"].docs = [
        {"vmName": "guest-guest-a-abc123-dc01", "owner": "guest-a", "status": "ready"}
    ]

    with pytest.raises(HTTPException) as excinfo:
        _delete("guest-a")

    assert excinfo.value.status_code == 409
    assert "guest-guest-a-abc123-dc01" in excinfo.value.detail
    assert db["users"].docs != []


def test_a_half_cloned_vm_counts_as_owned(db) -> None:
    """``deleted`` is the tombstone — every other status may still be a machine
    holding a pool address, including one whose clone failed."""
    db["vm_registry"].docs = [
        {
            "vmName": "guest-guest-a-abc123-ca01",
            "owner": "guest-a",
            "status": "cloning",
        },
        {"vmName": "guest-guest-a-abc123-ws01", "owner": "guest-a", "status": "error"},
    ]

    with pytest.raises(HTTPException) as excinfo:
        _delete("guest-a")
    assert excinfo.value.status_code == 409


def test_torn_down_vms_do_not_block_deletion(db) -> None:
    db["vm_registry"].docs = [
        {"vmName": "guest-guest-a-abc123-dc01", "owner": "guest-a", "status": "deleted"}
    ]

    _delete("guest-a")

    # The row itself stays: it is the deployment record the admin console reads.
    assert len(db["vm_registry"].docs) == 1


def test_a_running_deployment_is_refused(db) -> None:
    db["plan_runs"].docs = [
        {
            "jobId": "job-1",
            "owner": "guest-a",
            "scheduler": {"ops": {"a": {"status": "done"}, "b": {"status": "running"}}},
        }
    ]

    with pytest.raises(HTTPException) as excinfo:
        _delete("guest-a")

    assert excinfo.value.status_code == 409
    assert "job-1" in excinfo.value.detail


def test_a_finished_deployment_does_not_block_deletion(db) -> None:
    """Every terminal status counts as finished, failures included."""
    db["plan_runs"].docs = [
        {
            "jobId": "job-1",
            "owner": "guest-a",
            "scheduler": {
                "ops": {
                    "a": {"status": "done"},
                    "b": {"status": "error"},
                    "c": {"status": "cancelled"},
                }
            },
        }
    ]

    _delete("guest-a")

    # Kept on purpose — the run is the audit record and expires on its own TTL.
    assert db["plan_runs"].docs != []


def test_another_accounts_running_deployment_is_not_this_accounts_problem(db) -> None:
    db["plan_runs"].docs = [
        {
            "jobId": "job-1",
            "owner": "someone-else",
            "scheduler": {"ops": {"a": {"status": "running"}}},
        }
    ]

    _delete("guest-a")


def test_the_accounts_own_workspace_state_goes_with_it(db) -> None:
    db["projects"].docs = [{"_id": "p1", "owner": "guest-a"}]
    db["project_shares"].docs = [{"_id": "p1", "owner": "guest-a"}]
    db["lab_invites"].docs = [{"_id": "ABCD2345", "owner": "guest-a", "revoked": False}]

    result = _delete("guest-a")

    assert db["projects"].docs == []
    assert db["project_shares"].docs == []
    # A live join code outliving its account would let strangers keep walking in.
    assert db["lab_invites"].docs == []
    assert result == {
        "username": "guest-a",
        "projectsDeleted": 1,
        "sharesDeleted": 1,
        "labInvitesDeleted": 1,
    }


def test_the_cascade_is_scoped_to_the_deleted_account(db) -> None:
    db["projects"].docs = [{"_id": "p1", "owner": "guest-b"}]
    db["project_shares"].docs = [{"_id": "p1", "owner": "guest-b"}]
    db["lab_invites"].docs = [{"_id": "ABCD2345", "owner": "guest-b"}]

    result = _delete("guest-a")

    assert len(db["projects"].docs) == 1
    assert len(db["project_shares"].docs) == 1
    assert len(db["lab_invites"].docs) == 1
    assert result["projectsDeleted"] == 0
