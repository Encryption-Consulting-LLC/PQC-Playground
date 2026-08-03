"""Project CRUD — the persistence surface behind the frontend's project tabs.

A project document is the frontend's ``Project`` snapshot (see
``frontend/src/store/projects.ts``): the canvas graph, counters, viewport, and
staged ops. The graph payloads are stored as opaque validated blobs — React
Flow internals are not modeled server-side.

**Every route is scoped to ``owner``**, with no per-role exception: a project
belongs to the account that created it, and one account's projects are neither
listed, readable, writable, nor deletable by another. That is the whole of the
authorization model here — ``PROJECT_READ``/``PROJECT_WRITE`` decide *whether*
you have projects at all (every canvas role does), and the owner filter decides
*which*. Collaboration is a separate surface with its own collection and its own
rules (``routers/project_shares.py``); nothing here consults it.

A document written before ownership landed has ``owner: None`` and therefore
matches no caller — unattributable data stays unreachable rather than falling to
whoever asks first.

Concurrency is last-write-wins — one browser tab per account writes. A
rev/If-Match check can slot into PUT when concurrent editing lands.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.authz import (
    AuthedUser,
    Capability,
    get_current_user,
    require_capability,
)
from app.core.db import ProjectDoc, Viewport, from_mongo, now_ms, projects_col, to_mongo

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectIn(BaseModel):
    """Client project snapshot.

    ``dirty`` and client ``updatedAt`` are intentionally absent — pydantic
    drops unknown keys, so the frontend can send its ``Project`` object
    verbatim and the server stamps its own timestamps.
    """

    model_config = ConfigDict(populate_by_name=True)

    # POST only; ignored on PUT (the path param wins). The frontend generates
    # crypto.randomUUID() ids, so tabs render synchronously before the create
    # round-trips.
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{8,64}$")
    name: str = Field(min_length=1, max_length=120)
    nodes: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    edges: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    counters: dict[str, int] = Field(default_factory=dict)
    viewport: Viewport = Field(default_factory=Viewport)
    staged_ops: list[dict[str, Any]] = Field(
        default_factory=list, max_length=50, alias="stagedOps"
    )
    deploy_job_id: str | None = Field(default=None, alias="deployJobId")


def owned(project_id: str, user: AuthedUser) -> dict:
    """The only query shape this module reads or writes a single project with.

    Ownership is part of the *filter*, never a check performed after fetching:
    a miss is indistinguishable from a nonexistent project, so a 404 never
    confirms that someone else's project id is real.
    """
    return {"_id": project_id, "owner": user.username}


@router.get("", dependencies=[Depends(require_capability(Capability.PROJECT_READ))])
async def list_projects(user: AuthedUser = Depends(get_current_user)) -> dict:
    """The caller's own projects, summaries only, newest first."""
    cursor = (
        projects_col()
        .find(
            {"owner": user.username},
            projection={"name": 1, "createdAt": 1, "updatedAt": 1, "schemaVersion": 1},
        )
        .sort("updatedAt", -1)
    )
    docs = await cursor.to_list(length=200)
    return {"projects": [from_mongo(d) for d in docs], "count": len(docs)}


@router.post(
    "",
    status_code=201,
    dependencies=[Depends(require_capability(Capability.PROJECT_WRITE))],
)
async def create_project(
    body: ProjectIn, user: AuthedUser = Depends(get_current_user)
) -> dict:
    """Create a project owned by the caller. A duplicate id 409s.

    The id is client-generated, so a duplicate may well be *another account's*
    project. The insert fails identically either way and the message names no
    owner, which is what keeps this from being a probe for foreign ids.
    """
    now = now_ms()
    doc = ProjectDoc(
        id=body.id or uuid.uuid4().hex,
        name=body.name,
        nodes=body.nodes,
        edges=body.edges,
        counters=body.counters,
        viewport=body.viewport,
        staged_ops=body.staged_ops,
        deploy_job_id=body.deploy_job_id,
        owner=user.username,
        created_at=now,
        updated_at=now,
    )
    stored = to_mongo(doc)
    await projects_col().insert_one(stored)
    return from_mongo(stored)


@router.get(
    "/{project_id}",
    dependencies=[Depends(require_capability(Capability.PROJECT_READ))],
)
async def get_project(
    project_id: str, user: AuthedUser = Depends(get_current_user)
) -> dict:
    doc = await projects_col().find_one(owned(project_id, user))
    if doc is None:
        raise HTTPException(404, detail=f"Project '{project_id}' not found.")
    return from_mongo(doc)


@router.put(
    "/{project_id}",
    dependencies=[Depends(require_capability(Capability.PROJECT_WRITE))],
)
async def update_project(
    project_id: str, body: ProjectIn, user: AuthedUser = Depends(get_current_user)
) -> dict:
    """Full-snapshot replace (matches the frontend's checkpoint semantics).

    No upsert — creation stays explicit via POST; 404 if the project is gone or
    is not the caller's. ``createdAt``/``schemaVersion`` are preserved from the
    stored doc; ``owner`` is re-asserted from the session rather than carried
    over, so the field can only ever name the account that passed the filter.
    """
    existing = await projects_col().find_one(
        owned(project_id, user),
        projection={"createdAt": 1, "schemaVersion": 1},
    )
    if existing is None:
        raise HTTPException(404, detail=f"Project '{project_id}' not found.")

    doc = ProjectDoc(
        id=project_id,
        name=body.name,
        nodes=body.nodes,
        edges=body.edges,
        counters=body.counters,
        viewport=body.viewport,
        staged_ops=body.staged_ops,
        deploy_job_id=body.deploy_job_id,
        owner=user.username,
        schema_version=existing.get("schemaVersion", 1),
        created_at=existing["createdAt"],
        updated_at=now_ms(),
    )
    stored = to_mongo(doc)
    # Filtered again on replace: the find and the write must agree on ownership
    # even if the document changed hands in between.
    await projects_col().replace_one(owned(project_id, user), stored)
    return from_mongo(stored)


@router.delete(
    "/{project_id}",
    status_code=204,
    dependencies=[Depends(require_capability(Capability.PROJECT_WRITE))],
)
async def delete_project(
    project_id: str, user: AuthedUser = Depends(get_current_user)
) -> None:
    result = await projects_col().delete_one(owned(project_id, user))
    if result.deleted_count == 0:
        raise HTTPException(404, detail=f"Project '{project_id}' not found.")
