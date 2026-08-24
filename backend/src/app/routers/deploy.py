"""Deploy-plan routes — compile a semantic topology and run it as one plan job.

Mirrors ``vm.py``'s clone route: ``POST /deploy`` enqueues and returns immediately
with a ``job_id``; a Celery coordinator fans ready dependency-graph operations
out to the worker pool, streaming per-op state over the existing
``/api/ws/jobs/{job_id}`` transport as one ``PlanStateMsg`` per transition.

Vocabulary is the five op kinds the frontend staging store can produce
(see ``frontend/src/lib/staging.ts``) plus the backend-synthesized
``provision`` companion ``compile_plan`` adds per createVm. Every ``createVm``
is a real clone — the server decides, never a client flag — booted from a
per-VM firstboot ISO carrying a pool-allocated guest IP; op kinds without a
real command sequence remain simulated stubs (see ``app.tasks._simulate_op``).
"""

import logging
import re
import uuid
import io
import datetime
from typing import Literal, Mapping
from enum import Enum
from functools import partial

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.core.authz import (
    ROLE_CAPABILITIES,
    AuthedUser,
    Capability,
    Role,
    enforce_guest_vm_name,
    get_current_user,
    require_capability,
)
from app.core.db import (
    SETTINGS_DOC_ID,
    get_db,
    plan_runs_col,
    settings_col,
    vm_registry_col,
)
from app.core.evidence import build_evidence_bundle, redact_evidence
from app.core.esxi import _target_from_doc, connect_to_target
from app.core.firstboot import TEMPLATE_IDS
from app.core.golden_image import (
    golden_image_config_from_doc,
)
from app.core.ippool import guest_network_from_doc
from app.core.labs import joined_lab_project_ids
from app.core.infrastructure import (
    LINUX_PRODUCT_BASE,
    LINUX_PRODUCT_TEMPLATES,
    deployment_profiles_from_doc,
    infrastructure_profiles_from_doc,
    role_for_template,
)
from app.core.infrastructure_preflight import PlannedMachine, preflight_infrastructure
from app.core.environment_preflight import preflight_control_plane
from app.core.settings import settings
from app.core.template_config import validate_template_config
from app.core.jobs import transport
from app.core.jobs.models import JobStatus, QueuedMsg
from app.core.topology import (
    CompiledPlan,
    PlanCompilationError,
    TopologyDocument,
    TopologyValidationError,
    compile_plan,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/deploy", tags=["deploy"])

# Authored-ISO caps. Files ride inline in the deploy payload (and
# through the Celery broker), so they are text-only and tightly bounded.
ISO_MAX_FILES = 20
ISO_FILE_MAX_BYTES = 256 * 1024
ISO_OP_MAX_BYTES = 512 * 1024
ISO_PLAN_MAX_BYTES = 2 * 1024 * 1024
#: .ps1/.sh only for now: the deployed golden image's runner dispatches every
#: manifest entry it can execute; .cmd/.bat wait on the v2 runner rollout
#: (VM-Setup-Scripts) being promoted to the default base image.
_ISO_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.(ps1|sh)$")


class PlanOpKind(str, Enum):
    create_vm = "createVm"
    #: Backend-synthesized companion of a createVm (id ``{createVmOpId}::provision``):
    #: agent phone-home + boot settle + the template's role install, split off
    #: the clone so it runs on the provision queue. Client-supplied provision
    #: ops are stripped and re-synthesized by ``compile_plan``.
    provision = "provision"
    domain_join = "domainJoin"
    domain_leave = "domainLeave"
    ca_connect = "caConnect"
    web_server_cert = "webServerCert"


class IsoFile(BaseModel):
    """One operator-authored firstboot script riding inline in a createVm op."""

    name: str
    # max_length counts characters; validate_plan re-checks encoded bytes for
    # the per-op and per-plan sums.
    content: str = Field(max_length=ISO_FILE_MAX_BYTES)


class PlanOp(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    kind: PlanOpKind
    target: str
    #: The second node an op involves: the DC a member joins, the
    #: parent CA an issuing CA connects to, the CA issuing a web/client cert.
    #: The backend resolves both nodes' real guest-namespaced identities from
    #: the registry when expanding the op into agent commands.
    secondary: str | None = None
    params: dict[str, str] = Field(default_factory=dict)
    files: list[IsoFile] = Field(default_factory=list, max_length=ISO_MAX_FILES)
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")


class DeployRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # 50 is the client-authored ceiling; the doubled bound leaves headroom for
    # the worker to re-parse a compiled plan carrying one backend-synthesized
    # provision op per createVm.
    ops: list[PlanOp] = Field(min_length=1, max_length=100)
    topology: TopologyDocument
    #: The project the canvas belongs to. For guests it supplies the ``<project>``
    #: segment of every derived VM name (``guest-<user>-<project>-<machine>``) and
    #: is required when the plan clones. Operators keep free-form names and ignore
    #: it. As a *name* it needs no membership check — the result stays inside the
    #: caller's own ``guest-<user>-`` namespace regardless — but the deploy route
    #: still refuses a project the caller only holds a join code for; see
    #: ``_refuse_joined_lab_deploy``.
    project_id: str | None = Field(default=None, alias="projectId")


class CancelRequest(BaseModel):
    mode: Literal["step", "operation"] = "step"


def _compile_or_422(req: DeployRequest) -> CompiledPlan:
    """Translate semantic validation/compiler failures into one API shape."""

    try:
        return compile_plan(req.topology, req.ops)
    except (TopologyValidationError, PlanCompilationError) as exc:
        raise HTTPException(
            422,
            detail={
                "message": "Topology compilation failed.",
                "diagnostics": [
                    item.model_dump(by_alias=True) for item in exc.diagnostics
                ],
            },
        ) from exc


def _compiled_response(
    req: DeployRequest, compiled: CompiledPlan, settings_doc: dict | None = None
) -> dict:
    from app.core.execution_manifest import build_execution_groups

    return {
        "topologyVersion": req.topology.version,
        "operations": [op.model_dump(by_alias=True) for op in compiled.operations],
        "criticalPath": compiled.critical_path,
        "estimatedDurationSeconds": compiled.estimated_duration_seconds,
        "criticalPathDurationSeconds": compiled.critical_path_duration_seconds,
        "groups": build_execution_groups(
            req.topology, compiled.operations, settings_doc
        ),
        "resources": {
            "nodes": len(req.topology.nodes),
            "relationships": len(req.topology.edges),
            "dnsRecords": [
                record.model_dump(by_alias=True) for record in compiled.dns_records
            ],
        },
    }


def validate_plan(
    ops: list[PlanOp],
    user: AuthedUser,
    *,
    target_configured: bool,
    guest_network_configured: bool,
    project_id: str | None = None,
    clone_base: str | None = None,
    clone_bases: Mapping[str, str] | None = None,
) -> None:
    """Raise 422 on a malformed plan: duplicate ids, unknown/self deps, cycles,
    a ``createVm`` missing its ``vmName``/``template`` params, or invalid
    authored-ISO content — and 403 when a role without ``ISO_AUTHOR`` submits
    authored content at all.

    Every ``createVm`` is a real clone (any client ``simulate`` param is
    ignored — real-vs-stub is never client authority), so each one needs a
    configured shared ESXi target up front. The guest IP range is needed
    everywhere except an uploaded-ISO op (``isoId``), which is the complete disc
    — the server injects nothing and claims no pool address. Inline ``files``
    still need it: they layer over the rendered set, which bakes the address.
    Also rewrites each ``createVm``'s ``vmName`` in place via
    ``enforce_guest_vm_name`` — a guest must not be able to name a real VM
    outside its own identity's namespace; the derived guest name is
    ``guest-<user>-<project>-<machine>``, so a guest clone needs ``project_id``.
    Finally rejects two ``createVm`` ops that resolve to the same derived name.

    Called explicitly from the route (not a pydantic validator) so the checks
    stay easy to read and the errors are unambiguous 422s.
    """
    clone_base = clone_base or settings.clone_base
    template_clone_bases = {
        template: LINUX_PRODUCT_BASE for template in LINUX_PRODUCT_TEMPLATES
    }
    template_clone_bases.update(clone_bases or {})
    ids = [op.id for op in ops]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, detail="Plan contains duplicate op ids.")

    # A guest's real clone names are derived as guest-<user>-<project>-<machine>;
    # the project segment comes from this plan's project context, so a clone
    # without one can't be named. Operators keep free-form names and don't need it.
    if (
        user.role is Role.GUEST
        and not (project_id and project_id.strip())
        and any(op.kind is PlanOpKind.create_vm for op in ops)
    ):
        raise HTTPException(
            422,
            detail="A guest deploy that clones a VM needs a project context (projectId).",
        )

    id_set = set(ids)
    linux_product_nodes = {
        op.target
        for op in ops
        if op.kind is PlanOpKind.create_vm
        and op.params.get("template") in LINUX_PRODUCT_TEMPLATES
    }
    plan_files_bytes = 0
    seen_iso_ids: set[str] = set()
    for op in ops:
        if op.id in op.depends_on:
            raise HTTPException(422, detail=f"Op '{op.id}' depends on itself.")
        unknown = [dep for dep in op.depends_on if dep not in id_set]
        if unknown:
            raise HTTPException(
                422, detail=f"Op '{op.id}' depends on unknown op(s): {unknown}."
            )
        if op.kind is not PlanOpKind.create_vm:
            if op.files or op.params.get("isoId"):
                raise HTTPException(
                    422,
                    detail=f"Op '{op.id}': ISO content is only valid on createVm ops.",
                )
            if op.kind is PlanOpKind.domain_join and op.target in linux_product_nodes:
                raise HTTPException(
                    422,
                    detail=(
                        f"Op '{op.id}' (domainJoin) targets a Linux product server; "
                        "domain integration is not implemented yet."
                    ),
                )
            # Cross-node ops name their second node so the backend can resolve
            # its real identity. ``secondary``/``target`` are canvas
            # node ids (not op ids), resolved from the registry at run time — a
            # node created earlier this plan or surviving from a prior deploy —
            # so they aren't validated against the op-id set here. domainLeave
            # targets a membership the node already holds, so it needs none.
            if op.secondary is not None and op.secondary == op.target:
                raise HTTPException(
                    422, detail=f"Op '{op.id}' has itself as its secondary node."
                )
            if (
                op.kind
                in (
                    PlanOpKind.domain_join,
                    PlanOpKind.ca_connect,
                    PlanOpKind.web_server_cert,
                )
                and not op.secondary
            ):
                raise HTTPException(
                    422,
                    detail=(
                        f"Op '{op.id}' ({op.kind.value}) needs a 'secondary' node "
                        "(the DC / parent CA / issuing CA it wires to)."
                    ),
                )
            continue

        if not op.params.get("vmName"):
            raise HTTPException(
                422, detail=f"Op '{op.id}' (createVm) is missing the 'vmName' param."
            )
        if op.params.get("template") not in TEMPLATE_IDS:
            raise HTTPException(
                422,
                detail=f"Op '{op.id}' (createVm) has a missing or unknown 'template' param.",
            )
        # Per-template config (CA algorithm/key length, …) rides flat in params;
        # this is the authoritative validator + the unknown-key injection gate.
        try:
            validate_template_config(op.params["template"], op.params)
        except ValueError as exc:
            raise HTTPException(422, detail=f"Op '{op.id}' (createVm): {exc}") from exc

        iso_id = op.params.get("isoId")
        authored = bool(op.files) or bool(iso_id)
        if authored:
            # The hard authored-ISO gate: authored content runs arbitrary scripts
            # as SYSTEM — DEPLOY alone (guest-eligible) must never be enough to
            # submit any of it, inline files included even though those now
            # layer over the rendered set rather than replacing it.
            if Capability.ISO_AUTHOR not in ROLE_CAPABILITIES[user.role]:
                raise HTTPException(
                    403,
                    detail=(
                        f"Role '{user.role.value}' does not have capability "
                        f"'{Capability.ISO_AUTHOR.value}'."
                    ),
                )
            if op.files and iso_id:
                raise HTTPException(
                    422,
                    detail=(
                        f"Op '{op.id}' (createVm) carries both inline files and an "
                        "uploaded ISO — pick one."
                    ),
                )
            if iso_id and iso_id in seen_iso_ids:
                raise HTTPException(
                    422,
                    detail=(
                        f"Op '{op.id}' (createVm) reuses an uploaded ISO already "
                        "consumed by another op in this plan."
                    ),
                )
            if iso_id:
                seen_iso_ids.add(iso_id)

            op_bytes = 0
            names: set[str] = set()
            for file in op.files:
                if not _ISO_FILE_NAME.match(file.name):
                    raise HTTPException(
                        422,
                        detail=(
                            f"Op '{op.id}': invalid script filename '{file.name}' "
                            "(letters/digits/._- and a .ps1/.sh extension)."
                        ),
                    )
                if file.name in names:
                    raise HTTPException(
                        422,
                        detail=f"Op '{op.id}': duplicate script filename '{file.name}'.",
                    )
                names.add(file.name)
                op_bytes += len(file.content.encode("utf-8"))
            if op_bytes > ISO_OP_MAX_BYTES:
                raise HTTPException(
                    422,
                    detail=(
                        f"Op '{op.id}': authored files exceed "
                        f"{ISO_OP_MAX_BYTES // 1024} KiB total."
                    ),
                )
            plan_files_bytes += op_bytes
            if plan_files_bytes > ISO_PLAN_MAX_BYTES:
                raise HTTPException(
                    422,
                    detail=(
                        "Authored files across the plan exceed "
                        f"{ISO_PLAN_MAX_BYTES // 1024} KiB total."
                    ),
                )

        # The worker opens its own ESXi connection against the shared
        # target from the settings document (see app.tasks) — without a
        # configured target a real clone can't run at all, so reject it
        # here rather than letting the worker fail the op later.
        if not target_configured:
            raise HTTPException(
                422,
                detail=(
                    f"Op '{op.id}' (createVm) needs a shared ESXi target, but none is configured"
                ),
            )
        # Same fail-early logic for the IP the clone's firstboot ISO bakes in.
        # Only an uploaded ISO skips the pool: it is attached verbatim, whereas
        # inline files still ride the rendered (address-baking) firstboot set.
        if not iso_id and not guest_network_configured:
            raise HTTPException(
                422,
                detail=(
                    f"Op '{op.id}' (createVm) needs a guest IP range, but none is configured"
                ),
            )
        op.params["vmName"] = enforce_guest_vm_name(
            op.params["vmName"], user, project_id
        )
        # Guard the golden image: a clone whose resolved name equals the base
        # copies ``<base>/<base>.vmdk`` onto itself (same src and dst), which
        # ESXi rejects as "file already exists" — but only after clobbering the
        # directory. Reject it up front. Guests can't reach this (their names are
        # namespaced), so this catches free-form operator names.
        selected_base = template_clone_bases.get(op.params["template"], clone_base)
        if op.params["vmName"] == selected_base:
            raise HTTPException(
                422,
                detail=(
                    f"Op '{op.id}' (createVm) would name a VM '{selected_base}', "
                    "the base image it clones from — rename the node."
                ),
            )

    # Reject two createVm ops that resolve to the same real VM name (e.g. two
    # nodes both labelled "dc01" in one project) before enqueuing — the user
    # renames a node rather than the worker failing the second clone on VmExists.
    derived: dict[str, str] = {}
    for op in ops:
        if op.kind is not PlanOpKind.create_vm:
            continue
        vm_name = op.params["vmName"]
        if vm_name in derived:
            raise HTTPException(
                422,
                detail=(
                    f"Ops '{derived[vm_name]}' and '{op.id}' resolve to the same VM "
                    f"name '{vm_name}' — rename a node."
                ),
            )
        derived[vm_name] = op.id

    # Kahn's algorithm: a plan with a dependency cycle can never fully drain.
    indegree = {op.id: 0 for op in ops}
    dependents: dict[str, list[str]] = {op.id: [] for op in ops}
    for op in ops:
        for dep in op.depends_on:
            dependents[dep].append(op.id)
            indegree[op.id] += 1

    ready = [op_id for op_id, deg in indegree.items() if deg == 0]
    visited = 0
    while ready:
        op_id = ready.pop()
        visited += 1
        for nxt in dependents[op_id]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)

    if visited != len(ops):
        raise HTTPException(422, detail="Plan contains a dependency cycle.")


@router.post(
    "/compile",
    dependencies=[Depends(require_capability(Capability.DEPLOY))],
)
async def compile_deploy(
    req: DeployRequest,
    user: AuthedUser = Depends(get_current_user),
) -> dict:
    """Dry-run topology compilation without checking or changing infrastructure."""

    compiled = _compile_or_422(req)
    # Reuse payload/security checks, but deliberately treat environmental
    # prerequisites as available: target/image/network preflight belongs to
    # execution, while this endpoint is a pure review of topology and intent.
    validate_plan(
        compiled.operations,
        user,
        target_configured=True,
        guest_network_configured=True,
        project_id=req.project_id,
    )
    try:
        settings_doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    except RuntimeError:
        # Pure unit callers do not enter the FastAPI lifespan/Mongo setup;
        # infrastructure_profiles_from_doc(None) supplies configured defaults.
        settings_doc = None
    return _compiled_response(req, compiled, settings_doc)


async def _refuse_joined_lab_deploy(project_id: str | None, user: AuthedUser) -> None:
    """403 a deploy aimed at a lab the caller merely joined.

    A join code grants a finished environment to look at and open desktops in,
    not a canvas to build on. Nothing unsafe would happen without this — a
    guest's clone names are rebuilt from their own identity, so the VMs would
    be theirs — but they would be filed under somebody else's lab, show up in
    that lab's environment group, and eat pool addresses the lab's owner never
    asked for. Owning the project is unaffected: ``joined_lab_project_ids``
    excludes labs the caller owns, so holding a code for your own lab changes
    nothing.
    """
    if not project_id:
        return
    if project_id in await joined_lab_project_ids(user.username):
        raise HTTPException(
            403,
            detail=(
                "This lab was deployed for you to explore. Create your own "
                "project to build and deploy a topology."
            ),
        )


@router.post(
    "",
    status_code=202,
    dependencies=[Depends(require_capability(Capability.DEPLOY))],
)
async def deploy(
    req: DeployRequest,
    user: AuthedUser = Depends(get_current_user),
) -> dict:
    """Enqueue a deploy plan as a Celery job; stream progress over ws /api/ws/jobs/{job_id}.

    The actual work runs in a separate worker process which opens its own ESXi
    connection against the shared target from the settings document (see
    ``app.tasks.start_plan_task``) — this route validates the plan and, for
    plans containing real clones, that a target is configured at all.
    """
    from app.tasks import (
        start_plan_task,
    )  # local import: avoids loading Celery for every route

    await _refuse_joined_lab_deploy(req.project_id, user)
    compiled = _compile_or_422(req)
    # From this point forward, every check and the queued worker payload sees
    # only backend-derived dependencies/order. Client dependsOn never survives.
    req.ops = compiled.operations
    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    image_config = golden_image_config_from_doc(doc)
    deployment_profiles = deployment_profiles_from_doc(doc)
    target = _target_from_doc(doc)
    validate_plan(
        req.ops,
        user,
        target_configured=target is not None,
        guest_network_configured=guest_network_from_doc(doc) is not None,
        project_id=req.project_id,
        clone_base=image_config.base,
        clone_bases={
            template: deployment_profiles[role_for_template(template)].base
            for template in LINUX_PRODUCT_TEMPLATES
        },
    )

    # Reject a derived name that already belongs to a *different* live VM before
    # enqueuing (a clean 409 rather than a late per-op VmExists). An existing
    # entry whose ``appName`` is one of this plan's own createVm nodes is that
    # node's prior/failed attempt being retried, not a collision — exclude it.
    create_ops = [op for op in req.ops if op.kind is PlanOpKind.create_vm]
    if create_ops:
        environment = await run_in_threadpool(
            partial(
                preflight_control_plane,
                infrastructure_profiles_from_doc(doc),
                mongo_ready=True,
                require_agent=any(
                    op.params["template"] not in LINUX_PRODUCT_TEMPLATES
                    for op in create_ops
                ),
            )
        )
        if not environment.ready:
            raise HTTPException(
                409,
                detail={
                    "message": "Control-plane preflight failed.",
                    "preflight": environment.model_dump(by_alias=True),
                },
            )
        plan_node_ids = {op.target for op in create_ops}
        names = [op.params["vmName"] for op in create_ops]
        cursor = vm_registry_col().find(
            {"vmName": {"$in": names}, "status": {"$ne": "deleted"}},
            projection={"vmName": 1, "appName": 1},
        )
        async for entry in cursor:
            if entry.get("appName") not in plan_node_ids:
                raise HTTPException(
                    409,
                    detail=(
                        f"A VM named '{entry['vmName']}' already exists — "
                        "rename the node before deploying."
                    ),
                )

    # Uploaded ISOs must exist before the plan is enqueued — the worker can
    # only turn a missing GridFS file into a late op error, so fail the whole
    # request here while the client can still fix it.
    for op in req.ops:
        iso_id = op.params.get("isoId")
        if not iso_id:
            continue
        try:
            oid = ObjectId(iso_id)
        except InvalidId:
            raise HTTPException(
                422, detail=f"Op '{op.id}': 'isoId' is not a valid ISO reference."
            )
        if await get_db()["isos.files"].find_one({"_id": oid}, {"_id": 1}) is None:
            raise HTTPException(
                422,
                detail=(
                    f"Op '{op.id}': uploaded ISO '{iso_id}' not found — it may have "
                    "been consumed or expired; re-upload and retry."
                ),
            )

    # Last read-only gate before enqueue: prove each selected Windows/Linux image,
    # aggregate datastore capacity, and every derived inventory name against
    # the live ESXi host. The snapshot rides with the job and is checked again
    # by the worker before its first datastore write.
    preflight = None
    if create_ops:
        assert target is not None  # validate_plan rejected an unconfigured target
        conn = await connect_to_target(target)
        preflight = await run_in_threadpool(
            partial(
                preflight_infrastructure,
                conn,
                deployment_profiles,
                [
                    PlannedMachine(
                        role=role_for_template(
                            op.params["template"], op.params.get("caType")
                        ),
                        name=op.params["vmName"],
                    )
                    for op in create_ops
                ],
            )
        )
        if not preflight.ready:
            raise HTTPException(
                409,
                detail={
                    "message": "Infrastructure preflight failed.",
                    "preflight": preflight.model_dump(by_alias=True),
                },
            )

    job_id = uuid.uuid4().hex
    await plan_runs_col().update_one(
        {"jobId": job_id},
        {
            "$setOnInsert": {
                "jobId": job_id,
                "owner": user.username,
                "ownerRole": user.role.value,
                # Recorded so this run can attribute the VMs it created —
                # ``jobId`` is the only link a registry row keeps back here.
                "projectId": req.project_id,
                "topology": redact_evidence(req.topology.model_dump(by_alias=True)),
                "operations": redact_evidence(
                    [op.model_dump(by_alias=True) for op in req.ops]
                ),
                "preflight": (
                    preflight.model_dump(by_alias=True) if preflight else None
                ),
                "createdAt": int(
                    datetime.datetime.now(datetime.UTC).timestamp() * 1000
                ),
                "updatedAt": int(
                    datetime.datetime.now(datetime.UTC).timestamp() * 1000
                ),
                "ttlAt": datetime.datetime.now(datetime.UTC)
                + datetime.timedelta(days=7),
            }
        },
        upsert=True,
    )
    transport.publish(job_id, QueuedMsg(), status=JobStatus.queued)
    # Owner role rides along so a minted agent's provisioning command is later
    # dispatched under the deploying user's role (both roles hold VM_PROVISION).
    start_plan_task.delay(
        job_id,
        req.model_dump(by_alias=True),
        user.role.value,
        preflight.model_dump(by_alias=True) if preflight else None,
        user.username,
    )
    # The preflight rides back as a receipt: it was computed anyway, and the
    # client renders what was verified during the otherwise-opaque wait.
    return {
        "job_id": job_id,
        "preflight": preflight.model_dump(by_alias=True) if preflight else None,
    }


@router.get(
    "/{job_id}",
    dependencies=[Depends(require_capability(Capability.DEPLOY))],
)
async def get_deploy_run(
    job_id: str,
    user: AuthedUser = Depends(get_current_user),
) -> dict:
    """The presentation facts a reconnecting client can't reconstruct itself.

    The ``/api/ws/jobs/{job_id}`` stream carries live *op state*, but three things
    the Staged panel renders alongside it only ever existed in the tab that
    clicked Deploy: when the run started (for the elapsed clock), what preflight
    verified (the receipt), and the compiled execution manifest (the labels and
    expandable step trees). All three are already persisted on the run document,
    so a reload rehydrates from here rather than from client memory.

    ``groups`` is recompiled from the stored topology/operations exactly the way
    ``run_plan_operation_task`` recompiles them, so the step ids line up with the
    ones the worker publishes.
    """

    run = await plan_runs_col().find_one({"jobId": job_id})
    if run is None:
        raise HTTPException(404, detail=f"Deployment job '{job_id}' was not found.")
    if user.role is not Role.OPERATOR and run.get("owner") != user.username:
        raise HTTPException(403, detail="This deployment belongs to another user.")

    groups: list[dict] = []
    topology_doc = run.get("topology")
    operations = run.get("operations")
    if topology_doc and operations:
        from app.core.execution_manifest import build_execution_groups

        try:
            settings_doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
        except RuntimeError:
            settings_doc = None
        try:
            topology = TopologyDocument(**topology_doc)
            compiled = compile_plan(
                topology, [PlanOp(**op) for op in operations]
            ).operations
            groups = build_execution_groups(topology, compiled, settings_doc)
        except Exception:  # noqa: BLE001 — a manifest is a nicety, the run is not
            # An older run document may predate a schema change, and a manifest
            # that can't be rebuilt must not cost the client its timer and
            # receipt too. The panel already degrades to uncompiled op labels.
            logger.exception("unable to rebuild the execution manifest for %s", job_id)

    return {
        "jobId": job_id,
        "createdAt": run.get("createdAt"),
        "preflight": run.get("preflight"),
        "groups": groups,
    }


@router.get(
    "/{job_id}/evidence",
    dependencies=[Depends(require_capability(Capability.DEPLOY))],
)
async def download_evidence(
    job_id: str,
    user: AuthedUser = Depends(get_current_user),
) -> StreamingResponse:
    """Download the redacted topology, execution facts, and public PKI artifacts."""

    run = await plan_runs_col().find_one({"jobId": job_id})
    if run is None:
        raise HTTPException(404, detail=f"Evidence for job '{job_id}' was not found.")
    if user.role is not Role.OPERATOR and run.get("owner") != user.username:
        raise HTTPException(403, detail="This deployment belongs to another user.")
    payload, digest = build_evidence_bundle(run)
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="pki-evidence-{job_id}.zip"',
            "X-Evidence-SHA256": digest,
        },
    )


@router.post(
    "/{job_id}/cancel",
    status_code=202,
    dependencies=[Depends(require_capability(Capability.DEPLOY))],
)
async def cancel_deployment(
    job_id: str,
    body: CancelRequest,
    user: AuthedUser = Depends(get_current_user),
) -> dict:
    """Cooperatively stop after the active step or active operation."""

    run = await plan_runs_col().find_one({"jobId": job_id}, {"owner": 1})
    if run is None:
        raise HTTPException(404, detail=f"Deployment job '{job_id}' was not found.")
    if user.role is not Role.OPERATOR and run.get("owner") != user.username:
        raise HTTPException(403, detail="This deployment belongs to another user.")
    transport.request_cancel(job_id, body.mode)
    return {"job_id": job_id, "mode": body.mode, "status": "stop-requested"}


@router.post(
    "/{job_id}/reconcile",
    status_code=202,
    dependencies=[Depends(require_capability(Capability.DEPLOY))],
)
async def reconcile_deployment(
    job_id: str,
    user: AuthedUser = Depends(get_current_user),
) -> dict:
    """Reapply the persisted desired state to existing live lab machines."""

    from app.tasks import reconcile_plan_task

    source = await plan_runs_col().find_one({"jobId": job_id})
    if source is None:
        raise HTTPException(404, detail=f"Deployment job '{job_id}' was not found.")
    if user.role is not Role.OPERATOR and source.get("owner") != user.username:
        raise HTTPException(403, detail="This deployment belongs to another user.")

    reconcile_job_id = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.UTC)
    await plan_runs_col().insert_one(
        {
            "jobId": reconcile_job_id,
            "sourceJobId": job_id,
            "runKind": "reconcile",
            "owner": user.username,
            "ownerRole": user.role.value,
            "topology": source.get("topology") or {},
            "operations": source.get("operations") or [],
            "preflight": source.get("preflight"),
            "artifacts": source.get("artifacts") or {},
            "createdAt": int(now.timestamp() * 1000),
            "updatedAt": int(now.timestamp() * 1000),
            "ttlAt": now + datetime.timedelta(days=7),
        }
    )
    transport.publish(reconcile_job_id, QueuedMsg(), status=JobStatus.queued)
    reconcile_plan_task.delay(reconcile_job_id, job_id, user.role.value, user.username)
    return {"job_id": reconcile_job_id, "source_job_id": job_id}


@router.post(
    "/{job_id}/teardown",
    status_code=202,
    dependencies=[Depends(require_capability(Capability.VM_DELETE))],
)
async def teardown_deployment(
    job_id: str,
    user: AuthedUser = Depends(get_current_user),
) -> dict:
    """Remove topology-owned services, memberships, DNS, leases, and VMs."""

    from app.tasks import teardown_plan_task

    source = await plan_runs_col().find_one({"jobId": job_id})
    if source is None:
        raise HTTPException(404, detail=f"Deployment job '{job_id}' was not found.")
    if user.role is not Role.OPERATOR and source.get("owner") != user.username:
        raise HTTPException(403, detail="This deployment belongs to another user.")

    teardown_job_id = uuid.uuid4().hex
    now = datetime.datetime.now(datetime.UTC)
    await plan_runs_col().insert_one(
        {
            "jobId": teardown_job_id,
            "sourceJobId": job_id,
            "runKind": "teardown",
            "owner": user.username,
            "ownerRole": user.role.value,
            "topology": source.get("topology") or {},
            "operations": [],
            "createdAt": int(now.timestamp() * 1000),
            "updatedAt": int(now.timestamp() * 1000),
            "ttlAt": now + datetime.timedelta(days=7),
        }
    )
    transport.publish(teardown_job_id, QueuedMsg(), status=JobStatus.queued)
    teardown_plan_task.delay(teardown_job_id, job_id, user.role.value, user.username)
    return {"job_id": teardown_job_id, "source_job_id": job_id}
