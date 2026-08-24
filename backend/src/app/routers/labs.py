"""Pre-deployed labs: issue join codes (admin) and redeem them (canvas roles).

Two doors again, and for the familiar reason. ``/admin/labs`` is the issuing
side (``Capability.LAB_ADMIN``, admin-only): list the labs the playground
actually has, mint a code for one, revoke it. ``/labs`` is the redeeming side
(``Capability.LAB_JOIN``, operator + guest): hand back a code, receive the
lab's topology. Minting is admin-only on purpose — a code lets strangers see
somebody else's VMs, which is a platform decision rather than one for whichever
account happens to own the project.

**A lab is a deployed project, and the two halves are read separately.** VMs
come from ``vm_registry`` (grouped into environments by ``core/environments.py``,
the same grouping the teardown console shows) and the topology comes from the
project snapshot — either the owner's ``projects`` document or, for a lab built
under the guest-sharing flow, its ``project_shares`` document. A lab with VMs
but no retrievable snapshot cannot be joined at all: there would be nothing to
render. That is reported as ``snapshotAvailable: false`` in the listing and a
422 at mint time, rather than a code that resolves to an empty canvas.

**Deliberately no ESXi read.** The teardown console needs the host to say which
VMs are already gone; this one only answers "which labs exist and what is their
code", so it never fails when the host is unreachable. Absence signals are
simply not computed (``inventory=None``), which the grouping module treats as
"unknown", never as "gone".

**Refusals are specific**, because a code that "doesn't work" is the least
useful thing this feature could say: 422 for input that cannot be a code at
all, 404 for one that is well-formed and unknown, 410 for one that was issued
and has since been revoked, and 409 for a lab whose topology has disappeared.
Telling the holder of a revoked code that it was revoked leaks nothing they did
not already have, and is the difference between "ask for a new code" and
"retype it".
"""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.authz import AuthedUser, Capability, get_current_user, require_capability
from app.core.db import (
    LabInviteDoc,
    lab_invites_col,
    now_ms,
    project_shares_col,
    projects_col,
    to_mongo,
    users_col,
    vm_registry_col,
)
from app.core.environments import group_environments
from app.core.labs import (
    MAX_JOINED_LABS,
    generate_join_code,
    invite_payload,
    lab_snapshot,
    normalize_join_code,
)
from app.core.vm_naming import user_slug

router = APIRouter(prefix="/labs", tags=["labs"])
admin_router = APIRouter(
    prefix="/admin/labs",
    tags=["admin"],
    dependencies=[Depends(require_capability(Capability.LAB_ADMIN))],
)

#: Registry rows are tiny; this bounds a runaway read, not a real playground
#: (the teardown console uses the same ceiling).
_MAX_ROWS = 1000
#: Retries on a code collision. At 30^8 codes this loop effectively never runs
#: twice; it exists so a collision is a retry rather than a 500.
_MINT_ATTEMPTS = 5

JoinCode = Annotated[str, Path(min_length=1, max_length=32)]


class InviteCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project_id: str = Field(alias="projectId", min_length=8, max_length=64)
    label: str | None = Field(default=None, max_length=120)


class InvitePatch(BaseModel):
    """Revoke (or restore) a code, or retitle it. Every field optional."""

    model_config = ConfigDict(populate_by_name=True)

    revoked: bool | None = None
    label: str | None = Field(default=None, max_length=120)


class JoinRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


# --------------------------------------------------------------------------- #
# Shared resolution                                                           #
# --------------------------------------------------------------------------- #
async def _snapshot_source(project_id: str) -> dict | None:
    """The stored project behind a lab, from whichever collection holds it.

    ``projects`` first: it is the live document its owner keeps editing, so a
    joiner sees the lab as it is now. ``project_shares`` is the fallback for a
    lab built and published through the guest-sharing flow, where the snapshot
    is the share's own copy.
    """
    doc = await projects_col().find_one({"_id": project_id})
    if doc is not None:
        return {"project": doc, "owner": doc.get("owner"), "source": "project"}
    share = await project_shares_col().find_one({"_id": project_id})
    if share is not None:
        # A share's stored ``project`` is a client payload, which carries no
        # timestamps of its own (``ProjectIn`` drops them and the server stamps
        # the envelope instead). Lift the envelope's, so a joined lab's tab has
        # the same fields as one sourced from ``projects``.
        return {
            "project": {
                **(share.get("project") or {}),
                "createdAt": share.get("createdAt"),
                "updatedAt": share.get("updatedAt"),
            },
            "owner": share.get("owner"),
            "source": "share",
        }
    return None


def _joined_payload(invite: dict, project: dict) -> dict:
    """What a joiner gets: the invite's identity plus the sanitized snapshot."""
    snapshot = lab_snapshot(project, invite["projectId"])
    return {
        **invite_payload(invite),
        "name": snapshot.get("name") or "Lab",
        "project": snapshot,
    }


# --------------------------------------------------------------------------- #
# Admin: list labs, mint and revoke codes                                     #
# --------------------------------------------------------------------------- #
async def _slug_owners() -> dict[str, list[str]]:
    """Name slug → the accounts producing it, for owner reconciliation."""
    slug_owners: dict[str, list[str]] = {}
    async for doc in users_col().find({}, projection={"username": 1}):
        slug_owners.setdefault(user_slug(doc["username"]), []).append(doc["username"])
    return slug_owners


@admin_router.get("")
async def list_labs() -> dict:
    """Every deployed environment, with its join code when it has one."""
    rows = await vm_registry_col().find({}).to_list(length=_MAX_ROWS)
    projects = {
        doc["_id"]: doc["name"]
        async for doc in projects_col().find({}, projection={"name": 1})
    }
    environments = group_environments(
        rows,
        # No ESXi read here — see the module docstring.
        inventory=None,
        slug_owners=await _slug_owners(),
        project_names=projects,
    )
    invites = {
        doc["projectId"]: doc
        async for doc in lab_invites_col().find({"revoked": False})
    }
    # One query for the fallback source rather than a per-lab lookup: the
    # answer is only ever "is there a snapshot", and the ids are already known.
    project_ids = [env.project_id for env in environments if env.project_id]
    shared_ids = {
        doc["_id"]
        async for doc in project_shares_col().find(
            {"_id": {"$in": project_ids}}, projection={"_id": 1}
        )
    }

    labs = []
    for env in environments:
        summary = env.model_dump(by_alias=True)
        project_id = env.project_id
        invite = invites.get(project_id) if project_id else None
        labs.append(
            {
                **summary,
                # Dropped: an admin choosing a lab to share needs its identity
                # and size, not a per-VM orphan classification (that is the
                # teardown console's job, and it reads ESXi to do it).
                "vms": [vm["vmName"] for vm in summary.get("vms", [])],
                "snapshotAvailable": bool(
                    project_id and (project_id in projects or project_id in shared_ids)
                ),
                "invite": invite_payload(invite) if invite else None,
            }
        )
    return {"labs": labs, "count": len(labs)}


@admin_router.post("/invites", status_code=201)
async def create_invite(
    body: InviteCreate, user: AuthedUser = Depends(get_current_user)
) -> dict:
    """Mint the lab's join code, or return the one it already has.

    Idempotent rather than 409, because "this lab already has a code" is not an
    error an admin can act on — the code itself is what they came for. Rotating
    one is therefore explicit: revoke, then mint again.
    """
    source = await _snapshot_source(body.project_id)
    if source is None:
        raise HTTPException(
            404, detail=f"No saved project '{body.project_id}' to share as a lab."
        )
    if not source["owner"]:
        raise HTTPException(
            422,
            detail=(
                "This project has no owner on record, so its VMs cannot be "
                "matched to it. Redeploy the lab from an account before sharing it."
            ),
        )

    existing = await lab_invites_col().find_one(
        {"projectId": body.project_id, "revoked": False}
    )
    if existing is not None:
        return invite_payload(existing)

    now = now_ms()
    for _ in range(_MINT_ATTEMPTS):
        doc = to_mongo(
            LabInviteDoc(
                id=generate_join_code(),
                project_id=body.project_id,
                owner=source["owner"],
                label=body.label
                or (source["project"].get("name") if source["project"] else None),
                created_by=user.username,
                created_at=now,
                updated_at=now,
            )
        )
        try:
            await lab_invites_col().insert_one(doc)
        except DuplicateKeyError:
            # Either the code collided or another admin minted this lab's code
            # first. The second is the one worth resolving into a result.
            concurrent = await lab_invites_col().find_one(
                {"projectId": body.project_id, "revoked": False}
            )
            if concurrent is not None:
                return invite_payload(concurrent)
            continue
        return invite_payload(doc)
    raise HTTPException(503, detail="Could not allocate a join code. Try again.")


@admin_router.get("/invites")
async def list_invites() -> dict:
    """Every code ever minted, newest first — revoked ones included.

    Revoked invites are kept rather than deleted: they are the record of who
    joined which lab, and re-minting a lab's code should not silently look like
    nobody ever had the old one.
    """
    cursor = lab_invites_col().find({}).sort("createdAt", -1)
    docs = await cursor.to_list(length=500)
    return {"invites": [invite_payload(doc) for doc in docs], "count": len(docs)}


@admin_router.patch("/invites/{code}")
async def patch_invite(code: JoinCode, body: InvitePatch) -> dict:
    """Revoke, restore or retitle a code.

    Revoking is the cohort-wide off switch: membership is only ever read
    through this document, so every account that redeemed the code loses the
    lab (and its desktops) on the next request. Restoring can collide with a
    code minted in the meantime — one live code per lab is an index invariant,
    so that comes back as a 409 rather than two codes for one environment.
    """
    normalized = normalize_join_code(code)
    if normalized is None:
        raise HTTPException(422, detail="That is not a join code.")

    update: dict = {"updatedAt": now_ms()}
    if body.revoked is not None:
        update["revoked"] = body.revoked
    if body.label is not None:
        update["label"] = body.label
    try:
        doc = await lab_invites_col().find_one_and_update(
            {"_id": normalized},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        raise HTTPException(
            409,
            detail=(
                "This lab already has a live join code. Revoke that one before "
                "restoring this one."
            ),
        ) from exc
    if doc is None:
        raise HTTPException(404, detail="No such join code.")
    return invite_payload(doc)


# --------------------------------------------------------------------------- #
# Canvas roles: redeem a code, list what was redeemed                         #
# --------------------------------------------------------------------------- #
@router.post("/join", dependencies=[Depends(require_capability(Capability.LAB_JOIN))])
async def join_lab(
    body: JoinRequest = Body(...), user: AuthedUser = Depends(get_current_user)
) -> dict:
    """Redeem a code and receive the lab's topology.

    Membership is recorded before the snapshot is returned, so a joiner whose
    browser drops the response still holds the lab on their next request (and
    still shows up in the admin console's member list).
    """
    normalized = normalize_join_code(body.code)
    if normalized is None:
        raise HTTPException(
            422, detail="That doesn't look like a join code. Codes are 8 characters."
        )

    invite = await lab_invites_col().find_one({"_id": normalized})
    if invite is None:
        raise HTTPException(404, detail="That join code isn't recognised.")
    if invite.get("revoked"):
        raise HTTPException(410, detail="That join code is no longer active.")

    source = await _snapshot_source(invite["projectId"])
    if source is None:
        raise HTTPException(
            409,
            detail="This lab is no longer available. Ask for a new join code.",
        )

    await lab_invites_col().update_one(
        {"_id": normalized}, {"$addToSet": {"members": user.username}}
    )
    invite.setdefault("members", [])
    if user.username not in invite["members"]:
        invite["members"].append(user.username)
    return _joined_payload(invite, source["project"])


@router.get("", dependencies=[Depends(require_capability(Capability.LAB_JOIN))])
async def list_joined_labs(user: AuthedUser = Depends(get_current_user)) -> dict:
    """The caller's joined labs, with current snapshots.

    This is what makes a joined lab survive a reload: it is not the caller's
    project, so it is deliberately absent from ``/projects`` and from their
    browser storage, and the membership record is the only place it lives.
    """
    cursor = lab_invites_col().find({"members": user.username, "revoked": False})
    invites = await cursor.to_list(length=MAX_JOINED_LABS)

    labs = []
    for invite in invites:
        source = await _snapshot_source(invite["projectId"])
        # A lab whose project was deleted is skipped rather than reported: the
        # caller cannot act on it, and a tab that can't render is worse than no
        # tab. The invite stays put for the admin console to clean up.
        if source is None:
            continue
        labs.append(_joined_payload(invite, source["project"]))
    return {"labs": labs, "count": len(labs)}
