"""Join codes for pre-deployed labs, and what redeeming one grants.

A lab is an already-deployed project: real VMs on the host, a saved topology
that describes them, and nothing left to build. Handing that lab to the people
it was built for used to mean handing out a share URL, which anyone who saw it
could open — so every visitor landed in the same lab and there was no way to
point one cohort at one environment and another at another. A join code is that
missing indirection: an admin mints one per lab, gives it to a specific group,
and the code — not a URL, not the project id — is what resolves to a topology.

**A code is a bearer secret with no expiry**, so the shape matters more than it
looks. Codes are drawn from an alphabet with no ``I``, ``L``, ``O``, ``0`` or
``1`` in it (:data:`CODE_ALPHABET`), which is what makes one safe to read out
loud or print on a workshop handout. Normalization therefore only uppercases
and drops separators: it deliberately does **not** fold ``0``→``O`` or
``1``→``I``, because no issued code can contain either character, so a
typo'd one is genuinely wrong and should be rejected rather than silently
mapped onto a *different* real lab.

**What a member gets is view + remote desktop, and that is enforced by
omission.** Redeeming a code adds the account to the invite's ``members`` list
and hands back the project snapshot. It grants no build surface at all: every
VM route (delete, provision, executor command) checks the caller's own
``guest-<user>-`` namespace, and a joined lab's VMs are outside it. The single
deliberate exception is the remote-desktop route, which consults
:func:`lab_grants_vm_access` — a lab you can see but not open is not a lab you
can use. The other side of that coin is :func:`joined_lab_project_ids`, which
the deploy route uses to refuse building *into* someone else's lab.

**Revoking a code revokes the access it granted.** Membership is only ever read
through the invite document, so flipping ``revoked`` cuts off everyone who
redeemed it — that is the intended way to end a cohort's access, and the reason
the member list lives on the invite rather than on the project.

The top half of this module is pure (strings and dicts in, strings and dicts
out) and is where the snapshot sanitizer lives; the bottom half reads Mongo,
like ``core/ippool.py``.
"""

import re
import secrets
from typing import TYPE_CHECKING, Any, Mapping

from fastapi import HTTPException

from app.core.db import lab_invites_col, vm_registry_col
from app.core.vm_naming import project_code

if TYPE_CHECKING:  # deferred: ``core.authz`` imports nothing from here
    from app.core.authz import AuthedUser

#: Unambiguous when read aloud or off a printed page: no ``I``/``L``/``O`` and
#: no ``0``/``1``. 30 symbols, so an 8-character code is ~39 bits.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
#: Display grouping only — the stored id never contains the separator.
CODE_GROUP = 4
#: A joiner's tab bar, not a quota on the playground: an account holding more
#: redeemed codes than this simply sees the first ones back.
MAX_JOINED_LABS = 20

_CODE_RE = re.compile(rf"^[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")
_SEPARATORS = re.compile(r"[\s\-_.]+")


def generate_join_code() -> str:
    """A fresh code. Caller retries on the (vanishingly unlikely) collision."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_join_code(raw: str) -> str | None:
    """Typed input → the stored id, or None when it cannot be a code.

    Uppercases and drops the separators people add when transcribing
    (``abcd-2345``, ``ABCD 2345``). Confusable characters are *not* folded —
    see the module docstring.
    """
    candidate = _SEPARATORS.sub("", raw or "").upper()
    return candidate if _CODE_RE.match(candidate) else None


def format_join_code(code: str) -> str:
    """``ABCD2345`` → ``ABCD-2345`` for display. Never stored."""
    return "-".join(
        code[index : index + CODE_GROUP] for index in range(0, len(code), CODE_GROUP)
    )


def lab_snapshot(project: Mapping[str, Any], project_id: str) -> dict:
    """The stored project, stripped of everything that is not the lab itself.

    A joiner receives a finished environment, so run state is not just noise:
    a snapshot carrying ``deployJobId`` or per-node ``jobId`` makes the canvas
    reattach to *someone else's* jobs, whose sockets reject them, and inherited
    ``stagedOps`` would put pending work in front of a read-only visitor. All
    of it is dropped here rather than in the client, so it is dropped for every
    client.
    """
    snapshot = {key: value for key, value in project.items() if key != "_id"}
    snapshot["id"] = project_id
    snapshot["stagedOps"] = []
    snapshot["deployJobId"] = None
    snapshot["nodes"] = [_clean_node(node) for node in snapshot.get("nodes") or []]
    return snapshot


def _clean_node(node: Any) -> Any:
    if not isinstance(node, Mapping):
        return node
    cleaned = dict(node)
    data = cleaned.get("data")
    if isinstance(data, Mapping):
        cleaned["data"] = {
            key: value
            for key, value in data.items()
            if key not in ("jobId", "teardownJobId", "progress", "phase")
        }
    return cleaned


def invite_payload(doc: Mapping[str, Any]) -> dict:
    """Wire shape of one invite — camelCase, and the code in both forms.

    ``code`` is what a client sends back; ``displayCode`` is what a human is
    shown. Keeping both here means no UI has to know the grouping rule.
    """
    return {
        "code": doc["_id"],
        "displayCode": format_join_code(doc["_id"]),
        "projectId": doc["projectId"],
        "owner": doc.get("owner"),
        "label": doc.get("label"),
        "revoked": bool(doc.get("revoked")),
        "members": sorted(doc.get("members") or []),
        "memberCount": len(doc.get("members") or []),
        "createdBy": doc.get("createdBy"),
        "createdAt": doc.get("createdAt"),
        "updatedAt": doc.get("updatedAt"),
    }


def registry_row_in_lab(
    row: Mapping[str, Any], *, project_id: str, owner: str | None
) -> bool:
    """Whether a ``vm_registry`` row belongs to the given lab.

    Two ways, because attribution arrived in two eras. A row written by the
    plan runner carries the real ``projectId`` and matching it is exact. An
    older row carries only the six-character ``projectCode`` its VM name
    encodes, which identifies a project *within one account's VMs* and nowhere
    else — so that path additionally requires the owner to match, and is never
    taken when the code is absent.
    """
    if row.get("projectId") == project_id:
        return True
    code = row.get("projectCode")
    if not code or not owner:
        return False
    return code == project_code(project_id) and row.get("owner") == owner


# --------------------------------------------------------------------------- #
# Mongo readers                                                               #
# --------------------------------------------------------------------------- #
async def active_invites_for_member(username: str) -> list[dict]:
    """Every live invite this account has redeemed. Revoked ones are invisible."""
    cursor = lab_invites_col().find({"members": username, "revoked": False})
    return await cursor.to_list(length=MAX_JOINED_LABS)


async def joined_lab_project_ids(username: str) -> set[str]:
    """Project ids the account can see *only* because it joined them.

    Excludes labs the account owns, so the deploy guard built on this never
    fires on someone building in their own project — which they may well also
    hold a code for.
    """
    return {
        invite["projectId"]
        for invite in await active_invites_for_member(username)
        if invite.get("owner") != username
    }


async def enforce_own_or_joined_vm(vm_name: str, user: "AuthedUser") -> None:
    """Refuse a guest reaching a VM that is neither its own nor in a joined lab.

    The remote-desktop door's reach, defined once because it is checked twice —
    at ticket-minting time and again when the socket redeems that ticket, since
    a ticket id is a bearer token and holding one must not be sufficient.

    This lab clause is the *only* place a join code widens a guest's reach past
    its own ``guest-<user>-`` namespace, and it is deliberate: a lab handed out
    to be looked at whose desktops cannot be opened is not usable. Everything
    else about that lab stays shut — delete, provision and executor commands all
    keep the plain namespace check — so "view and remote desktop" is enforced by
    what does *not* consult membership. Like ``enforce_guest_vm_ownership`` this
    is a check and never a rewrite: silently redirecting the name would connect
    the caller to a different machine than the one they clicked.
    """
    from app.core.authz import enforce_guest_vm_ownership

    try:
        enforce_guest_vm_ownership(vm_name, user)
    except HTTPException:
        if not await lab_grants_vm_access(vm_name, user.username):
            raise


async def lab_grants_run_access(run: Mapping[str, Any], username: str) -> bool:
    """Whether a live invite this account holds covers this deploy run.

    The evidence bundle's counterpart to ``lab_grants_vm_access``: a lab member
    is looking at the very topology this run produced, so refusing them its
    manifest and public artifacts withholds the explanation of what they can
    already see and click through. The bundle is redacted before it is stored
    (``core/evidence.redact_evidence``), so what widens here is the account's
    reach to *this run*, never the run's own secrecy.

    Membership is matched on the run's ``projectId`` — the only link a
    ``plan_runs`` document keeps back to the project an invite names. A run
    written before that field existed has no way to prove which lab it belongs
    to and is refused.
    """
    project_id = run.get("projectId")
    if not project_id:
        return False
    return project_id in await joined_lab_project_ids(username)


async def lab_grants_vm_access(vm_name: str, username: str) -> bool:
    """Whether a live invite this account holds covers this VM.

    Consulted only after the caller's own-namespace check has already failed,
    so the extra registry read happens on the joined-lab path alone.
    """
    invites = await active_invites_for_member(username)
    if not invites:
        return False

    row = await vm_registry_col().find_one(
        {"vmName": vm_name},
        projection={"projectId": 1, "projectCode": 1, "owner": 1, "status": 1},
    )
    if row is None or row.get("status") == "deleted":
        return False
    return any(
        registry_row_in_lab(
            row, project_id=invite["projectId"], owner=invite.get("owner")
        )
        for invite in invites
    )
