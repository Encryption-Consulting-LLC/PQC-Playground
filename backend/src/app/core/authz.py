"""Role and capability model, and the request-level identity dependency.

``ROLE_CAPABILITIES`` is the single allowlist, and ``require_capability`` the
authoritative server-side gate. A role is a property of the authenticated
**user** (``get_current_user``). Login is always required: admins, operators,
and guests are all accounts in the users collection, and the account's ``role``
decides the feature set. There is no anonymous session.

Admin is a platform-management role, deliberately disjoint from operator:
admins configure the shared ESXi target, base images, and accounts (the
``/admin`` console), but never touch what happens inside a deployed VM —
that surface (canvas builds, deploys, provisioning) belongs to operators and
guests. The frontend enforces this split further by refusing to render the
canvas app at all for an admin account (a cosmetic mirror of the same
disjoint-capability design, not a substitute for it).

The ``X-Session-Token`` header (and ``?token=`` on WebSockets) now carries a
backend-signed JWT (``core/identity.py``). For account-backed tokens the user
document is re-read on every request, so ``disabled: true`` or a role edit
revokes/changes access immediately despite the stateless token.

The frontend reads capabilities from ``GET /auth/me`` and uses them to
conditionally render UI, but backend enforcement is the authoritative gate —
a guest with a valid token calling an operator-only route gets 403 regardless
of what the UI shows.

To expand or restrict a role's surface, edit ROLE_CAPABILITIES only. No other
code needs to change for allowlist adjustments.
"""

from enum import Enum

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel

from app.core.identity import AuthProvenance, decode_token
from app.core.vm_naming import (
    GUEST_MACHINE_MAX,
    guest_namespace,
    name_slug,
    project_code,
)


class Role(str, Enum):
    ADMIN = "admin"
    OPERATOR = "operator"
    GUEST = "guest"


class Capability(str, Enum):
    VM_LIST = "vm:list"
    VM_READ = "vm:read"
    VM_CLONE = "vm:clone"
    VM_UPDATE = "vm:update"
    VM_POWER = "vm:power"
    VM_DELETE = "vm:delete"
    VM_PROVISION = "vm:provision"  # run a template's role provisioning on one's own VM
    VM_CONSOLE = "vm:console"  # open a remote-desktop session to one's own VM
    # admin cross-user VM/environment teardown (the /admin console)
    VM_TEARDOWN_ADMIN = "vm:teardown-admin"
    # admin cross-user remote desktop (the /admin console)
    VM_CONSOLE_ADMIN = "vm:console-admin"
    CONFIG_GENERATE = "config:generate"
    ISO_AUTHOR = "iso:author"
    VM_EXEC_ARBITRARY = "vm:exec-arbitrary"  # reserved — operator-only escape hatch
    DEPLOY = "deploy"
    DEPLOY_ADMIN = (
        "deploy:admin"  # admin cross-user deployment stop (the /admin console)
    )
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    SETTINGS_READ = "settings:read"
    SETTINGS_WRITE = "settings:write"
    REGISTRY_READ = "registry:read"
    REGISTRY_WRITE = "registry:write"
    USER_ADMIN = "user:admin"


# Tune the allowlist here.
# Admin    → platform management only (the /admin console): accounts, the
#            shared ESXi target, base images, IP pool and VM-registry
#            oversight. Deliberately excludes every VM/PKI-building
#            capability — admins configure the playground, they don't build
#            in it.
# Operator → everything that isn't admin's — the canvas, deploys, VM ops.
# Guest    → read/guided VM subset only.
#   CONFIG_GENERATE is operator-only: config is produced server-side on the
#     guest's behalf (not via a guest-invoked endpoint).
#   ISO_AUTHOR is operator-only: authored/uploaded config ISOs run
#     arbitrary scripts as SYSTEM on first boot and bypass the guest IP pool —
#     never a shared-playground surface. Gates the /iso routes and any
#     createVm op carrying authored content (checked in validate_plan).
#   VM_EXEC_ARBITRARY is reserved for the firstboot executor (future phase).
#   DEPLOY is guest-eligible: the plan runner only does what a guest can already
#     trigger directly (clones) plus simulated stub ops.
#   VM_DELETE is guest-eligible: self-service teardown is the point —
#     safety comes from ``enforce_guest_vm_ownership`` (a guest can only delete
#     inside its own name namespace), not from withholding the capability.
#   VM_PROVISION is guest-eligible for the same reason: a guest
#     provisioning its *own* throwaway CA/DC is the point; the executor
#     command route enforces per-VM ownership so it can't target another VM.
#   PROJECT_* (Mongo project persistence) is held by every canvas role,
#     including guests. It grants no cross-account reach: ``routers/projects.py``
#     filters every read and write by ``owner``, so the capability decides
#     *whether* you have saved projects and the filter decides *which* — that is
#     the entire limit on a guest's project access. Guests were localStorage-only
#     before, which sounded safer but wasn't: one browser-wide storage key leaked
#     each guest's projects to the next visitor, and a guest could deploy from a
#     project the server had never heard of. Explicit opaque-id snapshots remain a
#     separate surface (the guest-only /project-shares API).
#   SETTINGS_* / REGISTRY_* / USER_ADMIN are admin-only: the shared ESXi
#     target, base-image profiles, and account provisioning are platform
#     concerns, not something an operator building a topology touches.
#   DEPLOY_ADMIN is admin-only: the cross-user deployment kill-switch (stop a
#     whole user's or every user's active deployments from the /admin console)
#     is platform oversight, distinct from operators' own-job DEPLOY cancel.
#   VM_TEARDOWN_ADMIN is admin-only for the same reason, and is deliberately
#     NOT VM_DELETE: operators and guests hold that one, scoped to their own
#     VMs by ``enforce_guest_vm_ownership``. This capability crosses the
#     ownership boundary — reclaiming a dead lab someone else abandoned is
#     platform housekeeping, and an admin with no way to do it can only watch
#     the guest IP pool fill up. Admins still get no VM_DELETE, so the
#     boundary-crossing surface is exactly the /admin teardown routes.
#   VM_CONSOLE (remote desktop) is granted to operators *and* guests: a session
#     only reaches a VM the holder could already clone, provision and destroy,
#     and it is ownership-scoped by ``enforce_guest_vm_ownership`` exactly like
#     VM_DELETE. It is deliberately not VM_EXEC_ARBITRARY's peer — that one is a
#     backend-dispatched shell on any VM, this one is a screen on your own.
#   VM_CONSOLE_ADMIN is the admin-only boundary-crossing twin, for the same
#     reason VM_TEARDOWN_ADMIN exists: an admin diagnosing an abandoned lab
#     before reclaiming it has no other way in. Admins hold no VM_CONSOLE, so
#     the crossing surface is exactly the /admin console routes.
ROLE_CAPABILITIES: dict[Role, set[Capability]] = {
    Role.ADMIN: {
        Capability.SETTINGS_READ,
        Capability.SETTINGS_WRITE,
        Capability.REGISTRY_READ,
        Capability.REGISTRY_WRITE,
        Capability.USER_ADMIN,
        Capability.DEPLOY_ADMIN,
        Capability.VM_TEARDOWN_ADMIN,
        Capability.VM_CONSOLE_ADMIN,
    },
    # NOTE: the admin-only set is written out twice — once granted above and
    # once subtracted here. ``test_authz_admin_capabilities`` asserts the two
    # stay in step, because forgetting the subtraction silently grants an
    # admin-only capability to every operator.
    Role.OPERATOR: set(Capability)
    - {
        Capability.SETTINGS_READ,
        Capability.SETTINGS_WRITE,
        Capability.REGISTRY_READ,
        Capability.REGISTRY_WRITE,
        Capability.USER_ADMIN,
        Capability.DEPLOY_ADMIN,
        Capability.VM_TEARDOWN_ADMIN,
        Capability.VM_CONSOLE_ADMIN,
    },
    Role.GUEST: {
        Capability.VM_LIST,
        Capability.VM_READ,
        Capability.VM_CLONE,
        Capability.VM_DELETE,
        Capability.VM_PROVISION,
        Capability.VM_CONSOLE,
        Capability.DEPLOY,
        Capability.PROJECT_READ,
        Capability.PROJECT_WRITE,
    },
}


class AuthedUser(BaseModel):
    """The resolved identity behind a request — what ``get_current_user`` yields."""

    username: str
    role: Role
    auth: AuthProvenance


def capabilities_for(role: Role) -> list[str]:
    """Sorted list of capability strings for the given role — used in API responses."""
    return sorted(c.value for c in ROLE_CAPABILITIES[role])


async def resolve_user_token(token: str | None) -> AuthedUser | None:
    """Token string → AuthedUser, or None if invalid/expired/disabled.

    Shared by the header dependency below and the WebSocket routes (browsers
    can't set custom headers on the upgrade request, so those authenticate via
    a query param).

    Every token is account-backed now — the user document is re-read on each
    request so ``disabled`` and role edits apply immediately; the token's own
    role claim is deliberately ignored (the doc is authoritative).
    """
    if not token:
        return None
    payload = decode_token(token)
    if payload is None:
        return None

    from app.core.db import (
        users_col,
    )  # deferred: keep authz importable without Mongo init

    doc = await users_col().find_one({"username": payload["sub"]})
    if doc is None or doc.get("disabled"):
        return None
    return AuthedUser(
        username=doc["username"], role=Role(doc["role"]), auth=payload["auth"]
    )


#: Marks the one 401 that means "the token you sent is no good". The frontend
#: clears its stored session on this header alone, so a 401 raised for any other
#: reason (a rejected login attempt, an SSO exchange) leaves the caller signed
#: in. Blanket "any 401 logs you out" is what let unrelated failures end a
#: session mid-deploy.
SESSION_REJECTED_HEADERS = {"WWW-Authenticate": "Session"}


async def get_current_user(x_session_token: str = Header(...)) -> AuthedUser:
    """FastAPI dependency: resolve X-Session-Token → AuthedUser (401 if invalid)."""
    user = await resolve_user_token(x_session_token)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session token.",
            headers=SESSION_REJECTED_HEADERS,
        )
    return user


def require_capability(cap: Capability):
    """FastAPI dependency factory.

    Usage::
        @router.get("/thing", dependencies=[Depends(require_capability(Capability.VM_LIST))])
        def list_things(...): ...

    Authenticates the request (via ``get_current_user`` — FastAPI caches it
    per-request, so pairing this with other identity-consuming dependencies
    costs one resolution) and raises HTTP 403 if the user's role lacks ``cap``.
    """

    def _dep(user: AuthedUser = Depends(get_current_user)) -> None:
        if cap not in ROLE_CAPABILITIES[user.role]:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Role '{user.role.value}' does not have capability '{cap.value}'."
                ),
            )

    return _dep


# The name scheme itself (segment caps, slugging, parsing) lives in
# ``core/vm_naming`` — a stdlib-only leaf module the Celery worker and the
# backfill CLI can import without the auth stack. These wrappers only add the
# ``AuthedUser`` binding, so the identity a name is built from is always the
# authenticated one.
def _guest_namespace(user: AuthedUser) -> str:
    """The caller's own VM-name prefix — also the ownership boundary enforced
    by ``enforce_guest_vm_ownership``."""
    return guest_namespace(user.username)


def enforce_guest_vm_name(
    name: str, user: AuthedUser, project_id: str | None = None
) -> str:
    """Derive the authoritative VM name for a clone, server-side.

    Operators are trusted and keep free-form names (returned unchanged).

    For guests the whole name is rebuilt from the caller's *own* authenticated
    identity, so a guest can never name a real VM outside its namespace nor
    spoof another user's::

        guest-<user>-<project>-<machine>   # project context known (deploy plan)
        guest-<user>-<machine>             # no project context (direct clone)

    ``name`` is treated purely as the requested machine segment — the frontend
    sends the plain canvas label, and a defensively-included namespace prefix
    is stripped first. Raises 422 if the machine segment (or, when a project is
    supplied, the project segment) slugs to nothing.
    """
    if user.role != Role.GUEST:
        return name
    prefix = _guest_namespace(user)
    raw = name[len(prefix) :] if name.startswith(prefix) else name
    machine = name_slug(raw, GUEST_MACHINE_MAX)
    if not machine:
        raise HTTPException(422, detail="Invalid VM name.")
    if project_id is None:
        return f"{prefix}{machine}"
    code = project_code(project_id)
    if not code:
        raise HTTPException(422, detail="Invalid project id for VM naming.")
    return f"{prefix}{code}-{machine}"


def enforce_guest_vm_ownership(name: str, user: AuthedUser) -> None:
    """Refuse a guest operating on a VM outside its own namespace (403).

    A *check*, never a rewrite — silently redirecting a destructive operation
    (the way ``enforce_guest_vm_name`` redirects clone names) could aim it at
    a different VM than the caller named. Operators pass unchecked.
    """
    if user.role != Role.GUEST:
        return
    if not name.startswith(_guest_namespace(user)):
        raise HTTPException(
            status_code=403, detail="Guests can only manage their own VMs."
        )
