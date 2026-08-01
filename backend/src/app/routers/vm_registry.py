"""VM registry — app-side VM identity ↔ real ESXi identity, plus a status cache.

Minimal surface (list / upsert / delete): nothing consumes registry
data yet. Entries are keyed by the real ESXi inventory name (``vmName``,
unique index) — the natural stable identity; app names ("WS-1") repeat across
projects. The deploy worker does not write here yet — see the marker
in ``app.tasks._run_clone_op``.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


from app.core.authz import Capability, require_capability
from app.core.db import from_mongo, now_ms, vm_registry_col

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


@router.get("", dependencies=[Depends(require_capability(Capability.REGISTRY_READ))])
async def list_entries(project_id: str | None = None) -> dict:
    """Full entries (they're tiny), optionally filtered to one project."""
    query = {"projectId": project_id} if project_id is not None else {}
    cursor = vm_registry_col().find(query).sort("updatedAt", -1)
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
    result = await vm_registry_col().delete_one({"vmName": vm_name})
    if result.deleted_count == 0:
        raise HTTPException(404, detail=f"VM registry entry '{vm_name}' not found.")
