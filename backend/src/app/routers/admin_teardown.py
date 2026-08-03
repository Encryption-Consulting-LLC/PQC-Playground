"""Cross-user VM teardown — admin-only (``Capability.VM_TEARDOWN_ADMIN``),
backing the ``/admin`` console's VM Registry section.

Deploying a lab is self-service; reclaiming one was not. Operators and guests
hold ``VM_DELETE``, scoped to their own namespace, which leaves nobody able to
clear an abandoned environment out of a finite guest IP pool. These routes are
that missing half: see the playground grouped as the environments it was
actually deployed as, see which entries are orphaned and why, and force-destroy
an environment, a single VM, or a set of dead registry rows.

**Force destroy only.** A VM is destroyed on the host, its address returned to
the pool, and its registry row marked deleted. Nothing runs inside the guest,
so domain accounts, DNS records, issued certificates and CA state are left
behind in Active Directory. The graceful in-guest path exists
(``core/teardown.py``) but is not wired here.

**Two deliberately opposite ESXi failure contracts.** The listing endpoints
must never fail on an unreachable host — an admin investigating a broken
playground is exactly who needs them — so they degrade to
``esxiReachable: false`` and simply omit the "absent from ESXi" signal rather
than asserting it false. The preview and purge endpoints do the opposite and
503, because both answer "which of these are already gone", and answering that
wrong is how an admin confirms the wrong destroy or erases the last record of a
live VM.

That split is also why the preview is its own endpoint rather than a field on
the listing: the confirmation dialog must act on data computed at confirm time,
the selection is arbitrary and crosses groups, and the list is polled while the
preview forces an uncached inventory read.

**Selection is always a list of VM names**, never an environment key, so the
server never re-derives membership between preview and execution. The
``confirmToken`` closes that gap from the other side: a stateless sha256 of the
sorted ``(vmName, presentInEsxi)`` pairs, re-computed at execution time and
409'd on drift, which makes "you saw the dry run" structural rather than a UI
convention.

Responses are camelCase like every other ``/admin`` route.
"""

import hashlib
import logging
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from vmkit.esxi import list_vm_names

from app.core import agents
from app.core.authz import AuthedUser, Capability, get_current_user, require_capability
from app.core.db import ip_pool_col, projects_col, users_col, vm_registry_col
from app.core.environments import (
    classify_vm,
    group_environments,
    is_purgeable,
)
from app.core.esxi import load_target, manager
from app.core.ippool import release_ip_async
from app.core.jobs import transport
from app.core.jobs.models import JobStatus, QueuedMsg
from app.core.settings import settings
from app.core.vm_naming import user_slug

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/teardown",
    tags=["admin"],
    dependencies=[Depends(require_capability(Capability.VM_TEARDOWN_ADMIN))],
)

#: Registry rows are tiny; this bounds a runaway read, not a real playground.
_MAX_ROWS = 1000

# --------------------------------------------------------------------------- #
# ESXi inventory — read defensively, cached for the polled endpoints           #
# --------------------------------------------------------------------------- #
_inventory_lock = threading.Lock()
_inventory_cache: tuple[float, set[str]] | None = None


def _cached_inventory() -> set[str] | None:
    with _inventory_lock:
        if _inventory_cache is None:
            return None
        read_at, names = _inventory_cache
        if time.monotonic() - read_at >= settings.teardown_inventory_cache_s:
            return None
        return names


def _store_inventory(names: set[str]) -> None:
    global _inventory_cache
    with _inventory_lock:
        _inventory_cache = (time.monotonic(), names)


async def read_inventory(*, fresh: bool = False) -> set[str] | None:
    """Every VM name in ESXi inventory, or None when the host can't be read.

    Deliberately not a route dependency: ``get_esxi`` raises 502/503 *before*
    the handler body, which would take the listing endpoints down with the host
    they exist to diagnose. Callers decide what an unreadable host means.

    Cached because ``list_vm_names`` rebuilds a container view over the whole
    inventory on every call and the console polls; ``fresh=True`` bypasses it
    so a confirmation is never shown stale data.
    """
    if not fresh:
        cached = _cached_inventory()
        if cached is not None:
            return cached
    target = await load_target()
    if target is None:
        return None
    try:
        conn = await run_in_threadpool(manager.get, target)
        names = set(await run_in_threadpool(list_vm_names, conn.content))
    except Exception as exc:  # noqa: BLE001 — any failure means "can't tell"
        logger.warning("Teardown console could not read ESXi inventory: %s", exc)
        return None
    _store_inventory(names)
    return names


# --------------------------------------------------------------------------- #
# Shared lookups                                                              #
# --------------------------------------------------------------------------- #
async def _registry_rows(vm_names: list[str] | None = None) -> list[dict]:
    query = {"vmName": {"$in": vm_names}} if vm_names is not None else {}
    return await vm_registry_col().find(query).to_list(length=_MAX_ROWS)


async def _slug_owners() -> dict[str, list[str]]:
    """``name slug -> every account producing it`` — what turns a VM name back
    into a real account, and what makes a collision visible instead of guessed."""
    owners: dict[str, list[str]] = {}
    async for doc in users_col().find({}, {"username": 1}):
        owners.setdefault(user_slug(doc["username"]), []).append(doc["username"])
    return owners


async def _project_names(docs: list[dict]) -> dict[str, str]:
    """Names for the projects persisted in Mongo, across every owner.

    Deliberately unscoped, like the rest of this admin console: naming the lab
    an abandoned VM came from is the point. A project that was deleted (or
    predates ownership) resolves to nothing and falls back to its project code.
    """
    ids = sorted({doc["projectId"] for doc in docs if doc.get("projectId")})
    if not ids:
        return {}
    return {
        doc["_id"]: doc["name"]
        async for doc in projects_col().find({"_id": {"$in": ids}}, {"name": 1})
    }


async def _allocated_addresses(vm_names: list[str]) -> dict[str, str]:
    """``vmName -> address`` straight from the pool.

    The authoritative source, not the registry's display copy: an errored row
    can carry ``ip: None`` while still holding an allocation, and that address
    is exactly what a teardown has to reclaim.
    """
    return {
        doc["vmName"]: doc["_id"]
        async for doc in ip_pool_col().find(
            {"vmName": {"$in": vm_names}, "status": "allocated"}
        )
    }


def _thresholds() -> dict:
    return {
        "agent_dead_after_s": settings.teardown_agent_dead_after_s,
        "stuck_cloning_after_s": settings.teardown_stuck_cloning_after_s,
    }


# --------------------------------------------------------------------------- #
# Request models                                                              #
# --------------------------------------------------------------------------- #
class SelectionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    vm_names: list[str] = Field(alias="vmNames", min_length=1, max_length=200)


class TeardownRequest(SelectionRequest):
    #: The ``confirmToken`` from the preview this confirmation is based on.
    confirm_token: str = Field(alias="confirmToken")


def _confirm_token(pairs: list[tuple[str, bool]]) -> str:
    """A stateless digest of what the preview showed.

    Hashes presence as well as names, so an admin who previewed "3 VMs, 1
    already gone" and confirms after the other two disappeared gets a 409
    rather than a silently different operation.
    """
    payload = "\n".join(f"{name}:{int(present)}" for name, present in sorted(pairs))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Listing (degrades when ESXi is unreachable)                                 #
# --------------------------------------------------------------------------- #
@router.get("/environments")
async def list_environments() -> dict:
    """The playground grouped into the labs it was deployed as."""
    inventory = await read_inventory()
    docs = await _registry_rows()
    environments = group_environments(
        docs,
        inventory=inventory,
        slug_owners=await _slug_owners(),
        project_names=await _project_names(docs),
        **_thresholds(),
    )
    return {
        "environments": [env.model_dump(by_alias=True) for env in environments],
        "count": len(environments),
        "esxiReachable": inventory is not None,
    }


@router.get("/orphans")
async def list_orphans() -> dict:
    """Every flagged registry row, flat, with the reasons it was flagged.

    Grouped first and then flattened so each row carries the same owner the
    Environments tab shows it under — an orphan with no attribution is only
    half a finding.
    """
    inventory = await read_inventory()
    docs = await _registry_rows()
    environments = group_environments(
        docs,
        inventory=inventory,
        slug_owners=await _slug_owners(),
        project_names=await _project_names(docs),
        **_thresholds(),
    )
    orphans = [
        {
            **vm.model_dump(by_alias=True),
            "owner": env.owner,
            "ownerResolved": env.owner_resolved,
            "projectCode": env.project_code,
            "environmentKey": env.key,
        }
        for env in environments
        for vm in env.vms
        if vm.signals
    ]
    return {
        "orphans": orphans,
        "count": len(orphans),
        "esxiReachable": inventory is not None,
    }


# --------------------------------------------------------------------------- #
# Preview / execute (503 when ESXi is unreachable)                            #
# --------------------------------------------------------------------------- #
async def _preview(vm_names: list[str]) -> dict:
    """Dry run for an arbitrary selection — what confirming would actually do."""
    inventory = await read_inventory(fresh=True)
    if inventory is None:
        raise HTTPException(
            503,
            detail=(
                "Cannot reach the ESXi host, so there is no way to tell which of "
                "these VMs still exist. Nothing has been destroyed."
            ),
        )
    docs = await _registry_rows(vm_names)
    found = {doc["vmName"]: doc for doc in docs}
    # Fail whole rather than silently narrowing the selection: a preview that
    # quietly drops a name is a preview that doesn't describe the confirmation.
    missing = sorted(set(vm_names) - set(found))
    if missing:
        raise HTTPException(404, detail=f"No registry entry for: {', '.join(missing)}.")

    allocations = await _allocated_addresses(vm_names)
    vms = []
    for name in sorted(vm_names):
        detail = classify_vm(found[name], inventory=inventory, **_thresholds())
        entry = detail.model_dump(by_alias=True)
        entry["releasesIp"] = allocations.get(name)
        vms.append(entry)

    return {
        "vms": vms,
        "vmNames": sorted(vm_names),
        "toDestroy": sum(1 for vm in vms if vm["presentInEsxi"]),
        "alreadyAbsent": sum(1 for vm in vms if not vm["presentInEsxi"]),
        "releasesIp": sum(1 for vm in vms if vm["releasesIp"]),
        "confirmToken": _confirm_token(
            [(vm["vmName"], bool(vm["presentInEsxi"])) for vm in vms]
        ),
    }


@router.post("/preview")
async def preview_teardown(body: SelectionRequest) -> dict:
    return await _preview(body.vm_names)


@router.post("", status_code=202)
async def start_teardown(
    body: TeardownRequest,
    user: AuthedUser = Depends(get_current_user),
) -> dict:
    """Force-destroy the selected VMs as one job; stream progress over
    ws /api/ws/jobs/{jobId}."""
    from app.tasks import (
        admin_teardown_task,
    )  # local import: avoids loading Celery for every route

    current = await _preview(body.vm_names)
    if current["confirmToken"] != body.confirm_token:
        raise HTTPException(
            409,
            detail=(
                "These VMs changed since the preview was shown. Nothing has been "
                "destroyed — close this and try again."
            ),
        )

    # Cut the agents loose first: a VM must not keep taking commands while it
    # is being destroyed. Runs here because the live socket map is per-process.
    await agents.revoke_live_agent_sockets(body.vm_names)

    job_id = uuid.uuid4().hex
    logger.info(
        "admin %s requested force teardown of %d VM(s): %s",
        user.username,
        len(body.vm_names),
        ", ".join(sorted(body.vm_names)),
    )
    transport.publish(job_id, QueuedMsg(), status=JobStatus.queued)
    admin_teardown_task.delay(job_id, sorted(body.vm_names), user.username)
    return {
        "jobId": job_id,
        "vmNames": sorted(body.vm_names),
        "count": len(body.vm_names),
    }


# --------------------------------------------------------------------------- #
# Registry row purge (bookkeeping only — destroys nothing)                    #
# --------------------------------------------------------------------------- #
@router.post("/orphans/purge")
async def purge_orphans(
    body: SelectionRequest,
    user: AuthedUser = Depends(get_current_user),
) -> dict:
    """Delete registry rows that provably back no VM.

    The one place degrading on an unreachable host would be wrong: "this row
    describes nothing" is the entire justification for deleting it, and an
    unreadable inventory cannot establish that.
    """
    inventory = await read_inventory(fresh=True)
    docs = await _registry_rows(body.vm_names)
    found = {doc["vmName"]: doc for doc in docs}
    missing = sorted(set(body.vm_names) - set(found))
    if missing:
        raise HTTPException(404, detail=f"No registry entry for: {', '.join(missing)}.")

    if inventory is None and any(
        (doc.get("status") or "ready") != "deleted" for doc in docs
    ):
        raise HTTPException(
            503,
            detail=(
                "Cannot reach the ESXi host, so there is no way to confirm these "
                "entries back no VM. No entries were removed."
            ),
        )

    backed = sorted(
        name
        for name, doc in found.items()
        if not is_purgeable(doc, inventory=inventory)
    )
    if backed:
        raise HTTPException(
            409,
            detail=(
                f"These entries still have a VM on the host: {', '.join(backed)}. "
                "Destroy the VM instead of removing its registry entry."
            ),
        )

    for name in sorted(found):
        # Release BEFORE deleting: the row is the only thing left linking the
        # address to anything, so the reverse order leaks it permanently.
        await release_ip_async(name)
        await vm_registry_col().delete_one({"vmName": name})

    logger.info(
        "admin %s purged %d registry entr%s: %s",
        user.username,
        len(found),
        "y" if len(found) == 1 else "ies",
        ", ".join(sorted(found)),
    )
    return {"purged": sorted(found), "count": len(found)}
