"""Join codes: what a code is, what redeeming one grants, and what revoking takes back.

Four things are pinned here.

*The code shape*, because it is transcribed by hand off a handout or a chat
message: separators are forgiven, case is forgiven, and a character that no
issued code can contain is *not* silently folded onto one that can — that would
map a typo onto a different real lab.

*The snapshot handed to a joiner*, because it is somebody else's project. Run
state (job ids, staged ops) belongs to the account that deployed it, and a
canvas that inherits it reattaches to sockets that reject it.

*The reach a member gains*, which is exactly one thing: the remote-desktop
check stops refusing the lab's VMs. Deleting, provisioning and deploying stay
refused, and that is the whole "view and remote desktop" contract.

*Revocation*, which is the cohort-wide off switch — it must take back the
desktop access it granted, not merely hide the lab from a list.
"""

import asyncio
import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import labs  # noqa: E402
from app.core.authz import AuthedUser, Role  # noqa: E402
from app.routers import deploy as deploy_router  # noqa: E402
from app.routers import labs as labs_router  # noqa: E402

PROJECT_ID = "9e4edb21-6d5f-4d0e-9a4c-0f2b7c8d1e33"
LAB_VM = "guest-operator-9e4edb-ca01"  # deliberately not the joiner's namespace


class _Collection:
    """The fragment of the Mongo surface these routes touch."""

    def __init__(self, docs: list[dict] | None = None) -> None:
        self.docs: dict[str, dict] = {doc["_id"]: dict(doc) for doc in docs or []}

    def _matches(self, doc: dict, query: dict) -> bool:
        for key, expected in query.items():
            value = doc.get(key)
            if isinstance(expected, dict) and "$in" in expected:
                if value not in expected["$in"]:
                    return False
            elif key == "members":
                if expected not in (value or []):
                    return False
            elif key == "vmName":
                if doc.get("vmName") != expected:
                    return False
            elif value != expected:
                return False
        return True

    def find(self, query: dict | None = None, projection: dict | None = None):
        return _Cursor(
            [dict(d) for d in self.docs.values() if self._matches(d, query or {})]
        )

    async def find_one(self, query: dict, projection: dict | None = None):
        for doc in self.docs.values():
            if self._matches(doc, query):
                return dict(doc)
        return None

    async def insert_one(self, doc: dict):
        self.docs[doc["_id"]] = dict(doc)

    async def update_one(self, query: dict, update: dict):
        doc = await self.find_one(query)
        if doc is None:
            return
        stored = self.docs[doc["_id"]]
        stored.update(update.get("$set", {}))
        for field, value in update.get("$addToSet", {}).items():
            if value not in stored.setdefault(field, []):
                stored[field].append(value)

    async def find_one_and_update(self, query: dict, update: dict, **_kwargs):
        await self.update_one(query, update)
        return await self.find_one({"_id": query["_id"]})


class _Cursor:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs

    def sort(self, *_args):
        return self

    async def to_list(self, length=None):
        return self.docs

    def __aiter__(self):
        async def _gen():
            for doc in self.docs:
                yield doc

        return _gen()


def _project(owner="operator") -> dict:
    return {
        "_id": PROJECT_ID,
        "name": "Workshop PKI",
        "owner": owner,
        "nodes": [
            {
                "id": "n1",
                "data": {
                    "name": "CA01",
                    "vmName": LAB_VM,
                    "ip": "192.0.2.10",
                    "jobId": "job-owned-by-someone-else",
                    "progress": 100,
                },
            }
        ],
        "edges": [],
        "stagedOps": [{"id": "op-1", "kind": "createVm"}],
        "deployJobId": "job-owned-by-someone-else",
    }


def _wire(monkeypatch, *, invites=None, projects=None, registry=None):
    invites_col = _Collection(invites)
    projects_col = _Collection(projects if projects is not None else [_project()])
    shares_col = _Collection([])
    registry_col = _Collection(registry or [])

    monkeypatch.setattr(labs, "lab_invites_col", lambda: invites_col)
    monkeypatch.setattr(labs, "vm_registry_col", lambda: registry_col)
    monkeypatch.setattr(labs_router, "lab_invites_col", lambda: invites_col)
    monkeypatch.setattr(labs_router, "projects_col", lambda: projects_col)
    monkeypatch.setattr(labs_router, "project_shares_col", lambda: shares_col)
    return invites_col


def _admin() -> AuthedUser:
    return AuthedUser(username="root", role=Role.ADMIN, auth="local")


def _guest(username="carol") -> AuthedUser:
    return AuthedUser(username=username, role=Role.GUEST, auth="local")


def _registry_row(**overrides) -> dict:
    row = {
        "_id": "reg-1",
        "vmName": LAB_VM,
        "status": "ready",
        "owner": "operator",
        "projectId": PROJECT_ID,
        "projectCode": "9e4edb",
    }
    row.update(overrides)
    return row


# --- the code itself ---------------------------------------------------------
def test_a_code_survives_being_transcribed_by_hand():
    code = labs.generate_join_code()
    assert labs.normalize_join_code(labs.format_join_code(code).lower()) == code
    assert labs.normalize_join_code(f" {code[:4]} {code[4:]} ") == code


def test_confusable_characters_are_rejected_rather_than_folded():
    # No issued code contains I/L/O/0/1, so a code carrying one is a typo. It
    # must fail rather than resolve to a *different* lab.
    assert labs.normalize_join_code("ABCD-234I") is None
    assert labs.normalize_join_code("") is None
    assert labs.normalize_join_code("ABCD") is None


# --- what a joiner receives --------------------------------------------------
def test_joining_returns_the_lab_without_its_owner_s_run_state(monkeypatch):
    invites = _wire(monkeypatch)
    minted = asyncio.run(
        labs_router.create_invite(
            labs_router.InviteCreate(projectId=PROJECT_ID), _admin()
        )
    )

    joined = asyncio.run(
        labs_router.join_lab(
            labs_router.JoinRequest(code=minted["displayCode"]), _guest()
        )
    )

    assert joined["projectId"] == PROJECT_ID
    assert joined["name"] == "Workshop PKI"
    # The lab itself survives; the previous deploy's run state does not.
    assert joined["project"]["nodes"][0]["data"]["vmName"] == LAB_VM
    assert "jobId" not in joined["project"]["nodes"][0]["data"]
    assert "progress" not in joined["project"]["nodes"][0]["data"]
    assert joined["project"]["stagedOps"] == []
    assert joined["project"]["deployJobId"] is None
    assert invites.docs[minted["code"]]["members"] == ["carol"]


def test_minting_twice_returns_one_code_for_the_lab(monkeypatch):
    _wire(monkeypatch)
    first = asyncio.run(
        labs_router.create_invite(
            labs_router.InviteCreate(projectId=PROJECT_ID), _admin()
        )
    )
    second = asyncio.run(
        labs_router.create_invite(
            labs_router.InviteCreate(projectId=PROJECT_ID), _admin()
        )
    )
    assert first["code"] == second["code"]


def test_an_unknown_code_and_a_revoked_one_read_differently(monkeypatch):
    _wire(monkeypatch)
    minted = asyncio.run(
        labs_router.create_invite(
            labs_router.InviteCreate(projectId=PROJECT_ID), _admin()
        )
    )

    with pytest.raises(HTTPException) as unknown:
        asyncio.run(
            labs_router.join_lab(labs_router.JoinRequest(code="ZZZZ-2345"), _guest())
        )
    assert unknown.value.status_code == 404

    with pytest.raises(HTTPException) as malformed:
        asyncio.run(
            labs_router.join_lab(labs_router.JoinRequest(code="nope"), _guest())
        )
    assert malformed.value.status_code == 422

    asyncio.run(
        labs_router.patch_invite(minted["code"], labs_router.InvitePatch(revoked=True))
    )
    with pytest.raises(HTTPException) as revoked:
        asyncio.run(
            labs_router.join_lab(labs_router.JoinRequest(code=minted["code"]), _guest())
        )
    # 410, not 404: "ask for a new code" is a different action from "retype it".
    assert revoked.value.status_code == 410


# --- the reach a member gains, and loses -------------------------------------
def test_membership_opens_the_lab_s_desktops_and_revoking_closes_them(monkeypatch):
    _wire(monkeypatch, registry=[_registry_row()])
    minted = asyncio.run(
        labs_router.create_invite(
            labs_router.InviteCreate(projectId=PROJECT_ID), _admin()
        )
    )

    # Before joining, the lab's VM is simply another account's.
    with pytest.raises(HTTPException):
        asyncio.run(labs.enforce_own_or_joined_vm(LAB_VM, _guest()))

    asyncio.run(
        labs_router.join_lab(labs_router.JoinRequest(code=minted["code"]), _guest())
    )
    asyncio.run(labs.enforce_own_or_joined_vm(LAB_VM, _guest()))  # no raise

    # Revoking is the cohort-wide off switch — it takes the desktops with it.
    asyncio.run(
        labs_router.patch_invite(minted["code"], labs_router.InvitePatch(revoked=True))
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(labs.enforce_own_or_joined_vm(LAB_VM, _guest()))
    assert exc.value.status_code == 403


def test_membership_does_not_reach_a_vm_outside_the_lab(monkeypatch):
    _wire(
        monkeypatch,
        registry=[
            _registry_row(),
            _registry_row(
                _id="reg-2",
                vmName="guest-dan-aaaaaa-ca01",
                projectId="another-project",
                projectCode="aaaaaa",
                owner="dan",
            ),
        ],
    )
    minted = asyncio.run(
        labs_router.create_invite(
            labs_router.InviteCreate(projectId=PROJECT_ID), _admin()
        )
    )
    asyncio.run(
        labs_router.join_lab(labs_router.JoinRequest(code=minted["code"]), _guest())
    )

    with pytest.raises(HTTPException):
        asyncio.run(labs.enforce_own_or_joined_vm("guest-dan-aaaaaa-ca01", _guest()))


def test_a_member_cannot_deploy_into_the_lab_it_joined(monkeypatch):
    _wire(monkeypatch)
    minted = asyncio.run(
        labs_router.create_invite(
            labs_router.InviteCreate(projectId=PROJECT_ID), _admin()
        )
    )
    asyncio.run(
        labs_router.join_lab(labs_router.JoinRequest(code=minted["code"]), _guest())
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(deploy_router._refuse_joined_lab_deploy(PROJECT_ID, _guest()))
    assert exc.value.status_code == 403

    # The caller's own projects are untouched by the guard.
    asyncio.run(deploy_router._refuse_joined_lab_deploy("some-other-project", _guest()))


def test_owning_a_lab_you_also_hold_a_code_for_still_deploys(monkeypatch):
    _wire(monkeypatch, projects=[_project(owner="carol")])
    minted = asyncio.run(
        labs_router.create_invite(
            labs_router.InviteCreate(projectId=PROJECT_ID), _admin()
        )
    )
    asyncio.run(
        labs_router.join_lab(labs_router.JoinRequest(code=minted["code"]), _guest())
    )

    asyncio.run(deploy_router._refuse_joined_lab_deploy(PROJECT_ID, _guest()))


def test_a_lab_with_no_saved_project_cannot_be_shared(monkeypatch):
    _wire(monkeypatch, projects=[])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            labs_router.create_invite(
                labs_router.InviteCreate(projectId=PROJECT_ID), _admin()
            )
        )
    assert exc.value.status_code == 404
