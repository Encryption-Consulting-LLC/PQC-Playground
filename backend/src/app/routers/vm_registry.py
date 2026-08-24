"""VM registry — app-side VM identity ↔ real ESXi identity, plus a status cache.

Entries are keyed by the real ESXi inventory name (``vmName``, unique index) —
the natural stable identity; app names ("WS-1") repeat across projects. The
deploy worker writes here on every clone (``app.tasks._run_clone_op``), the
sequence engine resolves cross-node context from it, and the ``/admin``
console reads it for oversight and teardown.

That last consumer is why ``delete_entry`` is guarded. A registry row is the
only remaining link between a VM and its allocated address; deleting one for a
VM that still exists leaks the address and hides the machine from every view
that could reclaim it. So the row may only be removed when it provably backs
nothing — the same rule the teardown console's purge action enforces.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


from app.core.authz import Capability, require_capability
from app.core.db import from_mongo, now_ms, vm_registry_col
from app.core.environments import is_purgeable

router = APIRouter(prefix="/vm-registry", tags=["vm-registry"])


class VmRegistryUpsert(BaseModel):
    """Everything but the key (``vmName``, from the path) and server-owned fields."""

    model_config = ConfigDict(populate_by_name=True)

    app_name: str = Field(min_length=1, max_length=120, alias="appName")
    owner: str | None = None
    project_id: str | None = Field(default=None, alias="projectId")
    project_code: str | None = Field(default=None, alias="projectCode")
    node_id: str | None = Field(default=None, alias="nodeId")
    moid: str | None = None
    status: Literal["cloning", "ready", "error", "deleted"] = "ready"
    power_state: str | None = Field(default=None, alias="powerState")
    job_id: str | None = Field(default=None, alias="jobId")
    ip: str | None = None
    # Deliberately absent: ``localAdminPasswordEnc``. It is server-written (by the
    # clone task, from the password firstboot actually applied), so accepting one
    # here would let a REGISTRY_WRITE holder plant a credential the guest never
    # had — and the console would then hand it to guacd.


@router.get("", dependencies=[Depends(require_capability(Capability.REGISTRY_READ))])
async def list_entries(project_id: str | None = None) -> dict:
    """Full entries (they're tiny), optionally filtered to one project."""
    query = {"projectId": project_id} if project_id is not None else {}
    # ``localAdminPasswordEnc`` is projected away rather than filtered after the
    # fact: it is a guest's console credential, and the only code with any reason
    # to read it is ``core.console.credentials`` on the way to guacd. It is
    # ciphertext, not plaintext, but a registry listing has no business carrying
    # it to a browser at all.
    cursor = (
        vm_registry_col()
        .find(query, {"localAdminPasswordEnc": 0})
        .sort("updatedAt", -1)
    )
    docs = await cursor.to_list(length=500)
    return {"entries": [from_mongo(d) for d in docs], "count": len(docs)}


@router.put(
    "/{vm_name}",
    dependencies=[Depends(require_capability(Capability.REGISTRY_WRITE))],
)
async def upsert_entry(vm_name: str, body: VmRegistryUpsert) -> dict:
    """Upsert keyed on the unique ``vmName`` index; document identity survives
    repeated upserts ($setOnInsert pins _id/createdAt on first write)."""
    fields = body.model_dump(by_alias=True)
    fields["updatedAt"] = now_ms()
    fields["schemaVersion"] = 1
    await vm_registry_col().update_one(
        {"vmName": vm_name},
        {
            "$set": fields,
            "$setOnInsert": {"_id": uuid.uuid4().hex, "createdAt": now_ms()},
        },
        upsert=True,
    )
    doc = await vm_registry_col().find_one({"vmName": vm_name})
    return from_mongo(doc)


@router.delete(
    "/{vm_name}",
    status_code=204,
    dependencies=[Depends(require_capability(Capability.REGISTRY_WRITE))],
)
async def delete_entry(vm_name: str) -> None:
    """Remove a registry entry — bookkeeping only, never a VM.

    409 unless the entry is already marked ``deleted`` or its VM is provably
    absent from ESXi. Destroying the VM (and reclaiming its address) is a
    different verb with a different route.
    """
    from app.routers.admin_teardown import read_inventory

    doc = await vm_registry_col().find_one({"vmName": vm_name})
    if doc is None:
        raise HTTPException(404, detail=f"VM registry entry '{vm_name}' not found.")
    if not is_purgeable(doc, inventory=await read_inventory(fresh=True)):
        raise HTTPException(
            409,
            detail=(
                f"'{vm_name}' still has a VM on the host (or the host could not be "
                "reached to confirm otherwise). Tear the VM down via "
                "POST /api/admin/teardown instead of removing its registry entry."
            ),
        )
    await vm_registry_col().delete_one({"vmName": vm_name})
