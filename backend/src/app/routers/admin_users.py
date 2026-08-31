"""Account provisioning — admin-only (``Capability.USER_ADMIN``), backing the
``/admin`` console's Accounts section.

Accounts are admin-inserted for both employees and guests; there is no
self-serve signup. OIDC accounts appear here too (upserted at first SSO
login) — they can be disabled but hold no password, so password operations
on them are rejected.

``disabled`` remains the default revocation: it is reversible, it takes effect
on the target's next request (see ``authz.resolve_user_token``) and it keeps
``owner`` references resolvable. DELETE is the other end — for the account that
should leave no trace — and is therefore interlocked rather than cascading
freely; see ``delete_user``.
"""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.authz import (
    AuthedUser,
    Capability,
    Role,
    get_current_user,
    require_capability,
)
from app.core.db import (
    lab_invites_col,
    now_ms,
    plan_runs_col,
    project_shares_col,
    projects_col,
    to_mongo,
    users_col,
    vm_registry_col,
)
from app.core.db.models import UserDoc
from app.core.identity import hash_password
from app.core.infrastructure import PRODUCT_ROLES
from app.core.jobs.replay import PLAN_TERMINAL_STATUSES

router = APIRouter(
    prefix="/admin/users",
    tags=["admin"],
    dependencies=[Depends(require_capability(Capability.USER_ADMIN))],
)

_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")


def _validate_products(products: list[str]) -> list[str]:
    """Normalize a product grant: known ids only, deduplicated, catalogue order.

    Order is the catalogue's rather than the submitted one so two admins
    granting the same set produce the same document, and an unknown id is a 422
    rather than a silently ignored key — a typo that reads as "granted" in the
    request and "not granted" in the product palette is the failure worth
    refusing here.
    """
    unknown = sorted(set(products) - set(PRODUCT_ROLES))
    if unknown:
        raise HTTPException(
            422,
            detail=f"Unknown product template(s): {', '.join(unknown)}.",
        )
    return [product for product in PRODUCT_ROLES if product in set(products)]


def _present(doc: dict) -> dict:
    """API shape: never expose the hash, even to admins."""
    return {
        "username": doc["username"],
        "email": doc.get("email"),
        "role": doc["role"],
        "auth": doc.get("auth", "local"),
        "disabled": bool(doc.get("disabled")),
        # Stored as granted, for every role. A guest's list is what gates its
        # palette; an operator's is unread (they hold the whole catalogue), and
        # showing it back unchanged is what lets an account be demoted to guest
        # without the admin having to re-grant from memory.
        "products": list(doc.get("products") or []),
        "createdAt": doc.get("createdAt"),
        "updatedAt": doc.get("updatedAt"),
    }


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: Role = Role.OPERATOR
    email: str | None = None
    #: Product templates this account may deploy. Omitted means none, which is
    #: the point for a guest: a freshly provisioned account is limited until an
    #: admin widens it, not the other way round.
    products: list[str] = Field(default_factory=list)


class UserPatch(BaseModel):
    """Partial update; only provided fields change."""

    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)
    role: Role | None = None
    #: The whole grant, not a delta — sending ``[]`` revokes every product, and
    #: a revocation takes effect on the account's next request (the user
    #: document is re-read per request, like ``disabled`` and ``role``).
    products: list[str] | None = None


@router.get("")
async def list_users() -> dict:
    docs = [_present(doc) async for doc in users_col().find().sort("username", 1)]
    return {"users": docs, "count": len(docs)}


@router.post("", status_code=201)
async def create_user(body: UserCreate) -> dict:
    """Provision a local account. 409 (via the DuplicateKeyError handler) if
    the username or email is taken."""
    if not _USERNAME.match(body.username):
        raise HTTPException(
            422,
            detail="Username must start alphanumeric and use only letters, digits, . _ @ -",
        )
    doc = UserDoc(
        id=body.username,
        username=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role.value,  # type: ignore[arg-type]
        auth="local",
        products=_validate_products(body.products),
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    await users_col().insert_one(to_mongo(doc))
    return _present(to_mongo(doc))


@router.patch("/{username}")
async def patch_user(
    username: str, body: UserPatch, admin: AuthedUser = Depends(get_current_user)
) -> dict:
    doc = await users_col().find_one({"username": username})
    if doc is None:
        raise HTTPException(404, detail=f"No user '{username}'.")

    fields: dict = {}
    if body.disabled is not None:
        # Self-lockout guard: the last thing an admin should be able to do to
        # this deploy is disable the account they're using.
        if body.disabled and username == admin.username:
            raise HTTPException(422, detail="You cannot disable your own account.")
        fields["disabled"] = body.disabled
    if body.role is not None:
        fields["role"] = body.role.value
    if body.products is not None:
        fields["products"] = _validate_products(body.products)
    if body.password is not None:
        if doc.get("auth") == "oidc":
            raise HTTPException(
                422, detail="OIDC accounts have no password — manage them at the IdP."
            )
        fields["passwordHash"] = hash_password(body.password)

    if fields:
        fields["updatedAt"] = now_ms()
        await users_col().update_one({"username": username}, {"$set": fields})
        doc = await users_col().find_one({"username": username})
    return _present(doc)


async def _live_vm_names(username: str) -> list[str]:
    """VMs this account still owns on the host, newest state first.

    ``deleted`` is the registry's tombstone (teardown soft-deletes rather than
    dropping the row), so anything else — including ``cloning`` and ``error`` —
    is a machine that may still exist and still holds a pool address.
    """
    return sorted(
        [
            doc["vmName"]
            async for doc in vm_registry_col().find(
                {"owner": username, "status": {"$ne": "deleted"}},
                projection={"vmName": 1},
            )
        ]
    )


async def _active_job_id(username: str) -> str | None:
    """The account's first still-running deploy run, if any."""
    async for doc in plan_runs_col().find(
        {"owner": username, "scheduler.ops": {"$exists": True}},
        projection={"jobId": 1, "scheduler.ops": 1},
    ):
        ops = (doc.get("scheduler") or {}).get("ops") or {}
        if any(
            (op or {}).get("status") not in PLAN_TERMINAL_STATUSES
            for op in ops.values()
        ):
            return doc.get("jobId")
    return None


@router.delete("/{username}")
async def delete_user(
    username: str, admin: AuthedUser = Depends(get_current_user)
) -> dict:
    """Erase an account and the workspace state only it could reach.

    Deletion is for the account that should leave no trace — a finished
    evaluation, a username provisioned by mistake — where ``disabled`` would
    leave a permanent row nobody dares reuse. The session dies with the
    document, since ``resolve_user_token`` re-reads it on every request.

    Two interlocks, both refusals rather than cascades, because each names work
    an admin has to decide about rather than data to sweep up:

    * **live VMs** — the account is the only self-service door to them
      (``DELETE /api/vm/{name}``), so deleting it first strands them in the
      finite guest pool with the admin teardown console as the sole way back.
    * **an active deployment** — it is still cloning machines under this owner
      and would go on doing so after the owner no longer exists.

    What *is* swept is the state that becomes unreachable the moment the
    account is gone: its projects (every read is ``owner``-filtered, so they
    would join the legacy ``owner: None`` documents nobody can open), the
    shares it published, and the lab codes minted against it — that last one
    being the only leftover that is a live access path rather than dead data.
    ``plan_runs`` and the soft-deleted ``vm_registry`` rows stay: they are the
    deployment record the admin console reads, and they expire on their own
    TTLs.

    An OIDC account is re-created at its owner's next SSO login, so ``disabled``
    is still what keeps one of those out for good.
    """
    doc = await users_col().find_one({"username": username})
    if doc is None:
        raise HTTPException(404, detail=f"No user '{username}'.")
    # Same self-lockout guard as disabling, and for the same reason — except
    # this one has no undo.
    if username == admin.username:
        raise HTTPException(422, detail="You cannot delete your own account.")

    live_vms = await _live_vm_names(username)
    if live_vms:
        raise HTTPException(
            409,
            detail=(
                f"'{username}' still owns {len(live_vms)} VM(s): "
                f"{', '.join(live_vms)}. Tear them down first, then delete the account."
            ),
        )

    active_job = await _active_job_id(username)
    if active_job is not None:
        raise HTTPException(
            409,
            detail=(
                f"'{username}' has a deployment still running ({active_job}). "
                "Stop it first, then delete the account."
            ),
        )

    projects = await projects_col().delete_many({"owner": username})
    shares = await project_shares_col().delete_many({"owner": username})
    invites = await lab_invites_col().delete_many({"owner": username})
    await users_col().delete_one({"username": username})

    return {
        "username": username,
        "projectsDeleted": projects.deleted_count,
        "sharesDeleted": shares.deleted_count,
        "labInvitesDeleted": invites.deleted_count,
    }
