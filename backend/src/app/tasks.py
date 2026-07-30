"""Celery tasks. Runs in the worker process — never inside the FastAPI app.

The clone task owns the blocking vmkit call and publishes progress over the Valkey
transport (``app.core.jobs.transport``). It reuses ``CloneProgressReducer`` and
``_clone_total_ops`` from the clone route unchanged, and ``map_vmkit_error`` for the
same error → status mapping the synchronous routes use.

The plan coordinator validates and preflights a deploy-plan DAG, then fans every
ready operation out as an independent Celery task across two queues: ``esxi``
for ops that open an ESXi connection (clones, destroys, plan preflight — capped
at ``Settings.clone_concurrency``) and ``provision`` for everything else
(``Settings.provision_concurrency`` — these ops mostly sleep on Valkey
pub/sub). Every ``createVm`` op is a real vmkit clone booted from a per-VM
firstboot ISO (``core.firstboot``) that bakes in an address claimed from the
guest IP pool (``core.ippool``) and ends at power-on; the agent wait, boot
settle, and role install run as the compiler-synthesized ``provision`` sibling
op, so a 30-minute forest install never holds an esxi slot. Op kinds without a
real command sequence are timed stubs. Each clone operation opens its own ESXi
connection against the shared org-wide target from the Mongo settings document
(``core.esxi.load_target_sync`` — the async client is API-process-bound), so
the worker needs Mongo + ``SETTINGS_ENC_KEY``, not ``ESXI_*`` env vars. Its
Mongo writes (IP allocation, vm_registry, scheduler state) go through one
short-lived sync client per task (``core.db.sync.worker_db``).
"""

import logging
import time
import traceback
import uuid
import datetime
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from pyVim.connect import Disconnect
from vmkit import clone_workflow, destroy_workflow, open_connection
from vmkit.errors import VmExistsError, VmkitError, VmNotFoundError
from vmkit.esxi import get_vm_by_name

from configgen import ExecutorAgentConfig, render_executor_config

from app.celery_app import celery_app
from app.core import agents
from app.core.db.models import now_ms
from app.core.db.sync import worker_db
from app.core.errors import map_vmkit_error
from app.core.esxi import load_target_sync
from app.core.firstboot import AgentBundle, build_authored_iso, build_firstboot_iso
from app.core.golden_image import (
    GoldenImageConfig,
    GoldenImagePreflight,
    golden_image_config_from_doc,
    preflight_golden_image,
)
from app.core.ippool import (
    IpPoolExhaustedError,
    allocate_ip_sync,
    load_guest_network_sync,
    release_ip_sync,
)
from app.core.infrastructure import (
    LINUX_PRODUCT_TEMPLATES,
    deployment_profiles_from_doc,
    role_for_template,
)
from app.core.infrastructure_preflight import (
    InfrastructurePreflight,
    PlannedMachine,
    preflight_infrastructure,
)
from app.core.settings import settings
from app.core.evidence import redact_evidence
from app.core.template_config import encrypt_config_secrets, extract_template_config
from app.core.jobs import transport
from app.core.jobs.models import (
    DoneMsg,
    ErrorMsg,
    JobStatus,
    OpRunState,
    StepRunState,
    PlanStateMsg,
    ProgressMsg,
    RunningMsg,
)


logger = logging.getLogger(__name__)


def _open_worker_connection():
    """Shared-target connection for one task; raises if no target is configured
    (the API routes pre-check this, so hitting it here means the target was
    unset between enqueue and execution)."""
    target = load_target_sync()
    if target is None:
        raise RuntimeError("No shared ESXi target configured (settings document).")
    return open_connection(target.host, target.user, target.password, target.port)


def _live_worker_connection(conn):
    """Return a live worker connection, reopening it if the ESXi session died.

    A plan holds ONE raw connection across every clone, but provisioning steps
    between clones (``dc.install_forest`` / ``ca.install``, each up to 1800s)
    can outlast ESXi's session idle-timeout — the next clone would then fail
    with ``vim.fault.NotAuthenticated``. The API side handles this in
    ``core.esxi.ConnectionManager``; the worker mirrors it with a cheap
    ``CurrentTime`` probe before each clone (clones are infrequent and far more
    expensive than the round trip, so no rate-limiting is needed here)."""
    if conn is not None:
        try:
            conn.si.CurrentTime()
            return conn
        except Exception:  # noqa: BLE001 — expired/dead session; drop and reopen
            try:
                Disconnect(conn.si)
            except Exception:  # noqa: BLE001 — the old session may already be dead
                pass
    return _open_worker_connection()


if TYPE_CHECKING:
    from vmkit import Connection

    from app.core.infrastructure import InfrastructureProfile
    from app.core.topology import TopologyDocument
    from app.routers.deploy import PlanOp


#: Server-side mirror of the frontend's STANDALONE_CLONE (constants/templates.ts /
#: the pre-staging topology.ts) — the backend does not accept arbitrary hardware
#: params from the client, only the per-VM name. Sizing is resolved server-side
#: from the operator's per-role InfrastructureProfile when one is passed; the
#: bare GoldenImageConfig fallback carries no sizing fields.
def _plan_clone_defaults(config: "GoldenImageConfig | InfrastructureProfile") -> dict:
    return {
        "base": config.base,
        "datastore": config.datastore,
        "guest_os": config.expected_guest_os,
        "max_usage_pct": config.max_usage_pct,
        "cpus": getattr(config, "cpus", None) or 8,
        "mem_mb": getattr(config, "memory_mb", None) or 8192,
    }


#: Three named phases per simulated op kind, ticked at a fixed cadence.
#: ``createVm`` is deliberately absent — it is always a real clone.
_SIMULATED_PHASES: dict[str, tuple[str, str, str]] = {
    "domainJoin": ("Joining domain", "Rebooting", "Verifying membership"),
    "domainLeave": ("Leaving domain", "Rebooting", "Verifying removal"),
    "caConnect": ("Generating CSR", "Signing certificate", "Installing CA certificate"),
    "webServerCert": ("Requesting certificate", "Binding to IIS", "Publishing CDP/AIA"),
}
_SIMULATED_PERCENTS = (33.0, 66.0, 100.0)
_SIMULATED_STEP_SECONDS = 0.6

_PRODUCT_SETUP_LABELS = {
    "certsecure": "Set up CertSecure Manager (stub)",
    "cbom": "Set up CBOM Secure (stub)",
    "codesign": "Set up CodeSign Secure (stub)",
}


@celery_app.task(name="clone_vm")
def clone_vm_task(job_id: str, params: dict) -> None:
    # Imported lazily to avoid a hard import-time dependency between the worker
    # entrypoint and the FastAPI router module.
    from app.routers.vm import CloneProgressReducer, CloneRequest, _clone_total_ops

    transport.publish(job_id, RunningMsg(), status=JobStatus.running)

    try:
        req = CloneRequest(**params)
        conn = _open_worker_connection()
        reducer = CloneProgressReducer(
            transport.make_publisher(job_id), _clone_total_ops(req)
        )
        result = clone_workflow(conn, progress=reducer, **params)
        transport.publish(
            job_id, DoneMsg(result=asdict(result)), status=JobStatus.done, terminal=True
        )
    except VmkitError as exc:
        status, detail = map_vmkit_error(exc)
        transport.publish(
            job_id,
            ErrorMsg(status=status, detail=detail),
            status=JobStatus.error,
            terminal=True,
        )
    except Exception as exc:  # noqa: BLE001 — surface anything as a terminal error
        transport.publish(
            job_id,
            ErrorMsg(status=500, detail=str(exc)),
            status=JobStatus.error,
            terminal=True,
        )


@celery_app.task(name="destroy_vm")
def destroy_vm_task(job_id: str, name: str) -> None:
    """Tear down a VM: power off + destroy, reclaim its guest IP, mark the
    registry entry deleted.

    A VM already absent from inventory (``VmNotFoundError``) still converges
    to success — the clone may have half-failed leaving only registry/IP
    state, and that must be cleanable through the same teardown call. Any
    other vmkit failure leaves the allocation in place (the VM still exists
    and may be using the address).
    """
    from app.routers.vm import CloneProgressReducer

    transport.publish(job_id, RunningMsg(), status=JobStatus.running)

    try:
        conn = _open_worker_connection()
        try:
            already_absent = False
            try:
                # Two ops: power off + destroy (the reducer only needs a total).
                reducer = CloneProgressReducer(transport.make_publisher(job_id), 2)
                destroy_workflow(conn, name=name, progress=reducer)
            except VmNotFoundError:
                already_absent = True
        finally:
            Disconnect(conn.si)

        with worker_db() as db:
            release_ip_sync(db, name)
            db["vm_registry"].update_one(
                {"vmName": name},
                {
                    "$set": {
                        "status": "deleted",
                        "powerState": None,
                        "ip": None,
                        # Revoke the agent identity: authenticate_persisted also
                        # excludes deleted VMs, but dropping the hash makes the
                        # revocation explicit and idempotent.
                        "agent": None,
                        "updatedAt": now_ms(),
                    }
                },
            )

        transport.publish(
            job_id,
            DoneMsg(result={"name": name, "alreadyAbsent": already_absent}),
            status=JobStatus.done,
            terminal=True,
        )
    except VmkitError as exc:
        status, detail = map_vmkit_error(exc)
        transport.publish(
            job_id,
            ErrorMsg(status=status, detail=detail),
            status=JobStatus.error,
            terminal=True,
        )
    except Exception as exc:  # noqa: BLE001 — surface anything as a terminal error
        transport.publish(
            job_id,
            ErrorMsg(status=500, detail=str(exc)),
            status=JobStatus.error,
            terminal=True,
        )


def _visible_steps(state: dict[str, OpRunState], op_id: str) -> dict[str, StepRunState]:
    current = state.get(op_id)
    return dict(current.steps) if current else {}


def _set_visible_step(
    state: dict[str, OpRunState],
    op_id: str,
    step_id: str,
    status: str,
    push,
    *,
    percent: float | None = None,
    phase: str | None = None,
    detail: str | None = None,
    result: dict | None = None,
) -> None:
    """Update one manifest child without discarding sibling step state."""

    steps = _visible_steps(state, op_id)
    steps[step_id] = StepRunState(
        status=status, percent=percent, phase=phase, detail=detail
    )
    current = state.get(op_id)
    state[op_id] = OpRunState(
        status="running",
        percent=current.percent if current else percent,
        phase=phase or (current.phase if current else None),
        result=result if result is not None else (current.result if current else None),
        steps=steps,
    )
    push()


def _fail_running_visible_step(
    state: dict[str, OpRunState], op_id: str, detail: str
) -> dict[str, StepRunState]:
    """Mark the active manifest child failed and return the preserved tree."""

    steps = _visible_steps(state, op_id)
    for step_id, step in steps.items():
        if step.status == "running":
            steps[step_id] = StepRunState(
                status="error", percent=step.percent, phase=step.phase, detail=detail
            )
            break
    return steps


def _op_progress_publisher(
    state: dict[str, OpRunState], op_id: str, push, *, step_id: str | None = None
):
    """``Publish``-shaped callable that folds ``ProgressMsg`` samples from
    ``CloneProgressReducer`` into this op's slot in the plan state, then pushes
    the whole snapshot."""

    def _publish(msg) -> None:
        if isinstance(msg, ProgressMsg):
            steps = _visible_steps(state, op_id)
            if step_id is not None:
                steps[step_id] = StepRunState(
                    status="running", percent=msg.percent, phase=msg.phase
                )
            current = state.get(op_id)
            state[op_id] = OpRunState(
                status="running",
                percent=msg.percent,
                phase=msg.phase,
                result=current.result if current else None,
                steps=steps,
            )
            push()

    return _publish


def _registry_upsert_sync(db, vm_name: str, **fields) -> None:
    """Worker-side mirror of ``routers/vm_registry.upsert_entry`` (same
    ``vmName`` key and ``$setOnInsert`` identity pinning, sync client)."""
    fields["updatedAt"] = now_ms()
    db["vm_registry"].update_one(
        {"vmName": vm_name},
        {
            "$set": fields,
            "$setOnInsert": {
                "_id": uuid.uuid4().hex,
                "createdAt": now_ms(),
                "schemaVersion": 1,
            },
        },
        upsert=True,
    )


#: Agents minted but never connected past this window (on a VM that errored or
#: was torn down) are swept — a backstop for a bricked firstboot or a wrong
#: backend URL leaving a dangling identity.
_STALE_AGENT_MS = 24 * 60 * 60 * 1000


def _sweep_stale_agents_sync(db) -> None:
    """Null out long-pending agent identities on failed/deleted VMs.

    Piggybacks on plan runs (like the ISO orphan sweep) rather than needing a
    scheduler. Only touches ``error``/``deleted`` registry entries, so a healthy
    VM whose agent is merely slow to phone home keeps its identity.
    """
    cutoff = now_ms() - _STALE_AGENT_MS
    db["vm_registry"].update_many(
        {
            "agent.provisionState": "pending",
            "agent.mintedAt": {"$lt": cutoff},
            "status": {"$in": ["error", "deleted"]},
        },
        {"$set": {"agent": None, "updatedAt": now_ms()}},
    )


def _cleanup_failed_clone(conn, db, vm_name: str, ip: str | None) -> None:
    """Roll back a failed createVm op without orphaning a live VM.

    The IP allocation and the just-minted agent identity are reclaimed only
    when the VM is provably absent from inventory. A clone that fails AFTER
    the VM was created (e.g. a vSphere fault while reading back the moid)
    leaves a booted VM holding both the address and the baked-in token —
    wiping them would strand the agent (403 forever, the hash is gone) and
    let a later clone claim an address still in use. When absence can't be
    proven (the inventory check itself fails), everything is kept; teardown
    is the reclaim path either way.
    """
    try:
        vm_absent = get_vm_by_name(conn.content, vm_name) is None
    except Exception:  # noqa: BLE001 — can't prove absence; keep identity + IP
        vm_absent = False
    if vm_absent:
        if ip is not None:
            release_ip_sync(db, vm_name)
        _registry_upsert_sync(db, vm_name, status="error", ip=None, agent=None)
    else:
        _registry_upsert_sync(db, vm_name, status="error")


def _set_provision_state(db, vm_name: str, provision_state: str) -> None:
    db["vm_registry"].update_one(
        {"vmName": vm_name},
        {"$set": {"agent.provisionState": provision_state, "updatedAt": now_ms()}},
    )


def _plan_domain_facts(
    ops: list["PlanOp"], topology: "TopologyDocument | None" = None
) -> tuple[str | None, str | None]:
    """The forest's (domainName, netbiosName) read from the plan's DC createVm
    op — a constant decided up front, so the root CA's DSConfigDN / pki URLs
    resolve even when the DC VM isn't up yet (they may clone in parallel)."""
    from app.routers.deploy import PlanOpKind

    for op in ops:
        if (
            op.kind is PlanOpKind.create_vm
            and op.params.get("template") == "domainController"
        ):
            return op.params.get("domainName"), op.params.get("netbiosName")
    if topology is not None:
        from app.core.topology import TopologyRole

        dc = next(
            (
                node
                for node in topology.nodes
                if node.role is TopologyRole.domain_controller
            ),
            None,
        )
        if dc is not None:
            return dc.config.get("domainName"), dc.config.get("netbiosName")
    return None, None


def _run_provision_op(
    conn_db,
    op: "PlanOp",
    ops: list["PlanOp"],
    job_id: str,
    owner_role: str,
    state: dict[str, OpRunState],
    push,
    topology: "TopologyDocument | None" = None,
) -> bool:
    """Run a cloned VM's post-clone provisioning as its own plan op.

    The op is backend-synthesized (id ``{createVmOpId}::provision``) and
    carries no params — everything (vmName, template, config) is resolved from
    the sibling ``createVm`` op, which the plan validator already namespaced
    and base-name-guarded. Waits for the baked-in agent to phone home, then
    walks the template's provision steps through the agentbus dispatch bridge
    (reboots + verify handled by the engine). Flips ``provisionState``
    applied/failed. A clone with no minted agent (authored ISO / bundling off)
    has nothing to provision and converges to done immediately. Never opens an
    ESXi connection — that keeps the esxi queue's concurrency cap meaningful.
    An empty sequence (nothing to install — e.g. a member server) still waits
    for phone-home — the op must never read ``done`` while the orchestrator
    has yet to connect, and later cross-node ops assume every clone in the
    plan has a live agent. A provisioning failure fails the op (dependents
    cancel) but leaves the booted VM registered and teardownable."""
    from app.core import agentbus
    from app.core.firstboot import hostname_for
    from app.core.sequences import (
        NodeContext,
        RunContext,
        SequenceCancelled,
        SequenceError,
    )
    from app.core.sequences.context import dns_records_for_context
    from app.core.sequences.definitions import provision_steps
    from app.core.sequences.worker import run_op_sequence
    from app.core.template_config import extract_template_config
    from app.core.topology import PROVISION_SUFFIX

    create_op = next(
        (item for item in ops if item.id == op.id.removesuffix(PROVISION_SUFFIX)),
        None,
    )
    if create_op is None:
        state[op.id] = OpRunState(
            status="error",
            detail="Provision op has no sibling createVm in the plan.",
        )
        push()
        return False

    vm_name = create_op.params["vmName"]
    template = create_op.params["template"]
    registry = conn_db["vm_registry"].find_one({"vmName": vm_name}) or {}
    vm_id = (registry.get("agent") or {}).get("vmId")
    ip = registry.get("ip")
    if template in LINUX_PRODUCT_TEMPLATES:
        _set_visible_step(
            state,
            op.id,
            "service-setup",
            "running",
            push,
            percent=0.0,
            phase=_PRODUCT_SETUP_LABELS[template],
        )
        # Intentional placeholder until product installation automation lands.
        # Keeping it as a real provision step makes the future implementation a
        # drop-in replacement without changing the plan/topology contract.
        time.sleep(_SIMULATED_STEP_SECONDS)
        _set_visible_step(
            state,
            op.id,
            "service-setup",
            "done",
            push,
            percent=100.0,
            phase="Service setup stub complete",
        )
        _set_provision_state(conn_db, vm_name, "applied")
        state[op.id] = OpRunState(
            status="done",
            percent=100.0,
            phase="Service setup stub complete",
            result={
                "vmName": vm_name,
                "setupStub": True,
                **({"ip": ip} if ip else {}),
            },
            steps=state[op.id].steps,
        )
        push()
        return True
    if vm_id is None:
        for step_id, phase in (
            ("agent-ready", "No orchestrator agent required"),
            ("boot-settle", "No orchestrator boot wait required"),
        ):
            _set_visible_step(
                state, op.id, step_id, "done", push, percent=100.0, phase=phase
            )
        state[op.id] = OpRunState(
            status="done",
            percent=100.0,
            phase="No orchestrator agent required",
            result={"vmName": vm_name, **({"ip": ip} if ip else {})},
            steps=state[op.id].steps,
        )
        push()
        return True

    dns_records = dns_records_for_context(topology)
    steps = provision_steps(
        template,
        ca_type=create_op.params.get("caType"),
        node_id=op.target,
        dns_records=dns_records,
    )

    _set_provision_state(conn_db, vm_name, "applying")
    # Carried on every running push so the frontend learns the agent identity
    # (and can show its presence dot) long before the op finishes.
    partial = {"agentVmId": vm_id}
    _set_visible_step(
        state,
        op.id,
        "agent-ready",
        "running",
        push,
        percent=0.0,
        phase="Waiting for agent",
        result=partial,
    )
    op_started = time.monotonic()
    try:
        agentbus.wait_for_agent(vm_id, timeout_s=settings.agent_phone_home_timeout_s)
        _set_visible_step(
            state,
            op.id,
            "agent-ready",
            "done",
            push,
            percent=100.0,
            phase="Agent connected",
            result=partial,
        )
        logger.info(
            "op %s: agent %s phoned home after %.1fs",
            op.id,
            vm_id,
            time.monotonic() - op_started,
        )

        # A fresh clone can phone home before its one firstboot reboot applies
        # the hostname. Probe boot_info until the VM is provably on the
        # post-reboot boot so we don't dispatch into an agent that reboot is
        # about to kill. Older images may expose a pending finalize task;
        # legacy agents fall back to the connection-stability dwell inside
        # wait_for_settled_boot.
        def _boot_phase(phase: str) -> None:
            _set_visible_step(
                state,
                op.id,
                "boot-settle",
                "running",
                push,
                phase=phase,
                result=partial,
            )

        _boot_phase("Waiting for boot to settle")
        settle_started = time.monotonic()
        agentbus.wait_for_settled_boot(
            vm_id,
            db=conn_db,
            timeout_s=settings.agent_phone_home_timeout_s,
            role=owner_role,
            job_key_prefix=f"{job_id}-{op.id}-bootprobe",
            on_phase=_boot_phase,
        )
        logger.info(
            "op %s: boot settled on %s after %.1fs",
            op.id,
            vm_id,
            time.monotonic() - settle_started,
        )
        _set_visible_step(
            state,
            op.id,
            "boot-settle",
            "done",
            push,
            percent=100.0,
            phase="Boot settled",
            result=partial,
        )
        if steps:
            state[op.id] = OpRunState(
                status="running",
                percent=0.0,
                phase="Provisioning",
                result=partial,
                steps=state[op.id].steps,
            )
            push()
            node = NodeContext(
                node_id=op.target,
                vm_name=vm_name,
                hostname=hostname_for(vm_name),
                agent_vm_id=vm_id,
                ip=ip,
                template_id=template,
                template_config=extract_template_config(template, create_op.params),
            )
            # Domain facts from the plan's DC op so the root CA's DSConfigDN /
            # pki URLs resolve even when the DC VM isn't up yet.
            domain_name, netbios = _plan_domain_facts(ops, topology)
            ctx = RunContext(
                nodes={"primary": node},
                domain_name=domain_name,
                netbios=netbios,
                pki_host=f"pki.{domain_name}" if domain_name else None,
                dns_records=dns_records,
            )
            callbacks = _sequence_progress(
                op.id,
                len(steps),
                state,
                push,
                result=partial,
                medians=_step_median_seconds(conn_db, steps),
            )
            on_step_complete, on_step_progress, on_step_tick = callbacks
            run_op_sequence(
                conn_db,
                steps,
                ctx,
                plan_job_id=job_id,
                op_id=op.id,
                role=owner_role,
                on_step_complete=on_step_complete,
                on_step_progress=on_step_progress,
                on_step_tick=on_step_tick,
                on_step_start=callbacks.start,
                on_verify_start=callbacks.verify_start,
                on_verify_done=callbacks.verify_done,
                should_stop=lambda: transport.cancel_mode(job_id) == "step",
            )
    except SequenceCancelled as exc:
        _set_provision_state(conn_db, vm_name, "failed")
        state[op.id] = OpRunState(
            status="cancelled", detail=str(exc), steps=state[op.id].steps
        )
        push()
        return False
    except (
        SequenceError,
        agentbus.AgentUnreachableError,
        agentbus.DispatchError,
        agentbus.ReconnectTimeoutError,
    ) as exc:
        _set_provision_state(conn_db, vm_name, "failed")
        detail = f"provisioning failed: {exc}"
        state[op.id] = OpRunState(
            status="error",
            detail=detail,
            steps=_fail_running_visible_step(state, op.id, detail),
        )
        push()
        return False
    except Exception as exc:  # noqa: BLE001 — surface as an op-level failure, not a plan crash
        _set_provision_state(conn_db, vm_name, "failed")
        detail = str(exc)
        state[op.id] = OpRunState(
            status="error",
            detail=detail,
            trace=traceback.format_exc(),
            steps=_fail_running_visible_step(state, op.id, detail),
        )
        push()
        return False

    logger.info(
        "op %s: provision of %s (%d steps) completed in %.1fs",
        op.id,
        vm_name,
        len(steps),
        time.monotonic() - op_started,
    )
    _set_provision_state(conn_db, vm_name, "applied")
    # vmName/ip/agentVmId ride the terminal result so the frontend row (and
    # its node) keeps identity facts even on a replayed terminal frame.
    state[op.id] = OpRunState(
        status="done",
        percent=100.0,
        phase="Done",
        result={
            "vmName": vm_name,
            "agentVmId": vm_id,
            **({"ip": ip} if ip else {}),
        },
        steps=state[op.id].steps,
    )
    push()
    return True


def _run_clone_op(
    conn: "Connection",
    db,
    op: "PlanOp",
    job_id: str,
    state: dict[str, OpRunState],
    push,
    owner_role: str = "guest",
    image_config: "GoldenImageConfig | InfrastructureProfile | None" = None,
) -> bool:
    """Execute a ``createVm`` op for real, from one of three ISO sources:

    - default: claim a guest IP, render+pack the per-VM firstboot ISO;
    - inline authored files: pack exactly what the operator wrote;
    - uploaded ISO: fetch the GridFS file and attach it verbatim.

    Authored/uploaded ops deliberately claim NO pool address and render nothing
    — the authored content is the complete disc, so their op result carries no
    ``ip``. Returns False on failure — a claimed IP is released so a failed op
    never strands an address."""
    from app.routers.iso import delete_uploaded_iso_sync, fetch_uploaded_iso_sync
    from app.routers.vm import CloneProgressReducer, CloneRequest, _clone_total_ops

    image_config = image_config or GoldenImageConfig(
        base=settings.clone_base,
        datastore=settings.clone_datastore,
        expectedGuestOs=settings.clone_guest_os,
        maxUsagePct=settings.clone_max_usage_pct,
    )
    vm_name = op.params["vmName"]
    template = op.params["template"]
    # Golden-image guard, enforced here and not only in deploy.validate_plan:
    # the worker trusts the plan payload's vmName verbatim (no re-validation,
    # no re-namespacing), so a plan that skipped the current route — a stale or
    # redelivered broker task, an old-code enqueue — could otherwise clone the
    # base image onto itself (`<base>/<base>.vmdk`, src == dst) and clobber the
    # golden template. Fail the op before any datastore write. This guard
    # stays on the only op kind that touches the datastore — synthesized
    # provision ops derive their vmName from this already-guarded sibling.
    if vm_name == image_config.base:
        state[op.id] = OpRunState(
            status="error",
            detail=(
                f"Refusing to clone a VM named '{image_config.base}' — it is the "
                "base image every clone copies from (stale/mis-routed plan?)."
            ),
        )
        push()
        return False
    iso_id = op.params.get("isoId")
    authored = bool(op.files) or bool(iso_id)
    bundling = (
        settings.orchestrator_bundling_enabled
        and not authored
        and template not in LINUX_PRODUCT_TEMPLATES
    )
    _set_visible_step(
        state,
        op.id,
        "prepare",
        "running",
        push,
        percent=0.0,
        phase="Preparing guest and first-boot media",
    )

    ip: str | None = None
    net = None
    vm_id: str | None = None  # set when an agent identity is minted
    if not authored:
        net = load_guest_network_sync(db)
        if net is None:
            # The route rejects plans without a configured range; hitting this
            # means it was cleared between enqueue and execution.
            detail = "Guest IP range is not configured."
            state[op.id] = OpRunState(
                status="error",
                detail=detail,
                steps=_fail_running_visible_step(state, op.id, detail),
            )
            push()
            return False
        # Fail cleanly BEFORE claiming an address if the agent binary is missing
        # on the worker host — an operator config error, not a per-VM one.
        if bundling and not Path(settings.orchestrator_agent_path).is_file():
            detail = (
                "Orchestrator agent binary not found on the worker host "
                "(ORCHESTRATOR_AGENT_PATH)."
            )
            state[op.id] = OpRunState(
                status="error",
                detail=detail,
                steps=_fail_running_visible_step(state, op.id, detail),
            )
            push()
            return False
        try:
            ip = allocate_ip_sync(db, vm_name, job_id)
        except IpPoolExhaustedError as exc:
            detail = str(exc)
            state[op.id] = OpRunState(
                status="error",
                detail=detail,
                steps=_fail_running_visible_step(state, op.id, detail),
            )
            push()
            return False

    _registry_upsert_sync(
        db, vm_name, appName=op.target, status="cloning", jobId=job_id, ip=ip
    )
    try:
        with TemporaryDirectory() as tmp:
            if iso_id:
                iso = fetch_uploaded_iso_sync(
                    db, iso_id, Path(tmp) / f"{vm_name}-config.iso"
                )
            elif op.files:
                iso = build_authored_iso(
                    [(f.name, f.content) for f in op.files],
                    vm_name=vm_name,
                    dest_dir=Path(tmp),
                )
            else:
                agent_bundle = None
                # Mint + bake an agent only when bundling is on AND the VM does
                # not already exist. A redelivery over a survivor (VmExists
                # below) must keep whatever token the running VM booted with —
                # so we never re-mint for it (we only hold the hash, not the
                # plaintext, and its throwaway ISO won't boot anyway).
                if bundling and get_vm_by_name(conn.content, vm_name) is None:
                    vm_id, token = agents.mint_identity()
                    # Persist the identity + the config the backend will dispatch
                    # after phone-home. Written before the ISO is built; the
                    # config never rides the ISO (backend-driven provisioning).
                    db["vm_registry"].update_one(
                        {"vmName": vm_name},
                        {
                            "$set": {
                                "agent": {
                                    "vmId": vm_id,
                                    "tokenHash": agents.hash_token(token),
                                    "role": owner_role,
                                    "templateId": op.params["template"],
                                    # Secrets (the DC's domainAdminPassword) are
                                    # AES-GCM encrypted before they touch Mongo;
                                    # the dispatch path decrypts them just in
                                    # time (core.template_config).
                                    "templateConfig": encrypt_config_secrets(
                                        op.params["template"],
                                        extract_template_config(
                                            op.params["template"], op.params
                                        ),
                                    ),
                                    "provisionState": "pending",
                                    "mintedAt": now_ms(),
                                }
                            }
                        },
                    )
                    agent_bundle = AgentBundle(
                        binary_path=Path(settings.orchestrator_agent_path),
                        config_toml=render_executor_config(
                            ExecutorAgentConfig(
                                vm_id=vm_id,
                                agent_token=token,
                                backend_url=settings.effective_agent_backend_url,
                                role=owner_role,
                            )
                        ),
                    )
                iso = build_firstboot_iso(
                    template=op.params["template"],
                    vm_name=vm_name,
                    ip=ip,
                    net=net,
                    dest_dir=Path(tmp),
                    agent=agent_bundle,
                )
            req = CloneRequest(
                name=vm_name,
                iso_path=str(iso),
                power_on=True,
                **_plan_clone_defaults(image_config),
            )
            _set_visible_step(
                state,
                op.id,
                "prepare",
                "done",
                push,
                percent=100.0,
                phase="Guest and first-boot media ready",
            )
            _set_visible_step(
                state,
                op.id,
                "clone",
                "running",
                push,
                percent=0.0,
                phase="Cloning virtual machine",
            )
            reducer = CloneProgressReducer(
                _op_progress_publisher(state, op.id, push, step_id="clone"),
                _clone_total_ops(req),
            )
            result = clone_workflow(conn, progress=reducer, **req.model_dump())

        _set_visible_step(
            state,
            op.id,
            "clone",
            "done",
            push,
            percent=100.0,
            phase="Virtual machine cloned and powered on",
        )

        vm = get_vm_by_name(conn.content, vm_name)
        _registry_upsert_sync(
            db,
            vm_name,
            status="ready",
            moid=vm._moId if vm is not None else None,
            powerState="poweredOn",
        )
        if iso_id:
            # Consumed — vmkit uploaded it to the datastore; the GridFS copy
            # has served its purpose (orphan sweep is the backstop).
            delete_uploaded_iso_sync(db, iso_id)

        # The createVm op ends at power-on. The agent wait, boot settle, and
        # the template's role install run as the synthesized sibling
        # ``{op.id}::provision`` op on the provision queue (_run_provision_op),
        # so this op releases its esxi slot the moment the VM is up — later
        # clones in the plan never queue behind a 30-minute forest install.
        state[op.id] = OpRunState(
            status="done",
            percent=100.0,
            phase="Done",
            # ip/vmName ride the op result so the frontend can label the node
            # and key teardown off the real inventory name. Authored clones
            # have no pool ip to report. agentVmId lets the Inspector
            # surface the auto-provisioned orchestrator identity.
            result={
                **asdict(result),
                "vmName": vm_name,
                **({"ip": ip} if ip else {}),
                **({"agentVmId": vm_id} if vm_id else {}),
            },
            steps=state[op.id].steps,
        )
        push()
        return True
    except VmExistsError as exc:
        # A VM by this name is already in inventory (redelivered task, or a
        # re-deploy over a survivor) — it may well be running with this very
        # address baked into its ISO, so the allocation is deliberately KEPT;
        # tearing the VM down is what releases it.
        status, detail = map_vmkit_error(exc)
        detail = f"{status}: {detail}"
        state[op.id] = OpRunState(
            status="error",
            detail=detail,
            steps=_fail_running_visible_step(state, op.id, detail),
        )
        push()
        return False
    except VmkitError as exc:
        status, detail = map_vmkit_error(exc)
        _cleanup_failed_clone(conn, db, vm_name, ip)
        detail = f"{status}: {detail}"
        state[op.id] = OpRunState(
            status="error",
            detail=detail,
            steps=_fail_running_visible_step(state, op.id, detail),
        )
        push()
        return False
    except Exception as exc:  # noqa: BLE001 — surface as an op-level failure, not a plan crash
        _cleanup_failed_clone(conn, db, vm_name, ip)
        detail = str(exc)
        state[op.id] = OpRunState(
            status="error",
            detail=detail,
            trace=traceback.format_exc(),
            steps=_fail_running_visible_step(state, op.id, detail),
        )
        push()
        return False


#: Op kinds whose real command sequence replaces the timed stub.
_REAL_SEQUENCE_KINDS = frozenset(
    {"domainJoin", "domainLeave", "caConnect", "webServerCert"}
)


#: Minimum gap between elapsed-heartbeat pushes for one step (the dispatch
#: poll ticks every ~1s; republishing that often is churn for no information).
_STEP_TICK_PUSH_GAP_S = 10.0
#: An estimated intra-step percent never claims more than this — the agent's
#: own frames (or step completion) take it the rest of the way.
_STEP_EST_PCT_CAP = 95.0


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _step_median_seconds(db, steps) -> dict[str, float]:
    """step_id → median duration (seconds) of past runs of the step's command
    — the priors behind the estimated intra-step percent. Steps whose command
    has never completed are absent (their heartbeat shows elapsed time only)."""
    from app.core.sequences.worker import load_step_medians

    medians_ms = load_step_medians(db, [s.command for s in steps])
    return {
        s.id: medians_ms[s.command] / 1000.0 for s in steps if s.command in medians_ms
    }


class _SequenceCallbacks:
    """Three legacy callbacks plus explicit step lifecycle callbacks."""

    def __init__(self, complete, progress, tick, start, verify_start, verify_done):
        self.complete = complete
        self.progress = progress
        self.tick = tick
        self.start = start
        self.verify_start = verify_start
        self.verify_done = verify_done

    def __iter__(self):
        return iter((self.complete, self.progress, self.tick))


def _sequence_progress(
    op_id: str,
    total: int,
    state: dict[str, OpRunState],
    push,
    result: dict | None = None,
    medians: dict[str, float] | None = None,
):
    """Progress callbacks for one op's step sequence: a completed-step counter
    (``Step n/total``), the agent's own intra-step relay
    (``Step n/total · step-id: phase``, percent scaled into the step's slice),
    and an elapsed-time heartbeat for steps whose command goes silent for
    minutes (``Install-ADDSForest`` reports 10% once, then nothing) — throttled
    to ~10s, with a duration-estimated percent when ``medians`` (step_id →
    seconds, from ``step_metrics``) knows this command. ``result`` (if given)
    rides along on every running push so the frontend keeps partial facts —
    e.g. the agent's vm_id — before the op finishes."""
    done_count = {"n": 0}
    agent_progress: dict[str, tuple[str | None, float | None]] = {}
    last_tick_push: dict[str, float] = {}
    medians = medians or {}

    def _steps() -> dict[str, StepRunState]:
        current = state.get(op_id)
        return dict(current.steps) if current else {}

    def _push_running(
        percent: float, phase: str, steps: dict[str, StepRunState] | None = None
    ) -> None:
        state[op_id] = OpRunState(
            status="running",
            percent=percent,
            phase=phase,
            result=result,
            steps=steps if steps is not None else _steps(),
        )
        push()

    def on_step_start(step_id: str) -> None:
        steps = _steps()
        steps[step_id] = StepRunState(status="running", percent=0.0)
        _push_running(round(100.0 * done_count["n"] / total, 1), step_id, steps)

    def on_step_complete(step_id: str) -> None:
        done_count["n"] += 1
        steps = _steps()
        steps[step_id] = StepRunState(status="done", percent=100.0)
        _push_running(
            round(100.0 * done_count["n"] / total, 1),
            f"Step {done_count['n']}/{total}",
            steps,
        )

    def on_step_progress(
        step_id: str, phase: str | None, percent: float | None
    ) -> None:
        agent_progress[step_id] = (phase, percent)
        n = done_count["n"]
        label = f"Step {n + 1}/{total} · {step_id}"
        if phase:
            label += f": {phase}"
        overall = round(100.0 * (n + (percent or 0.0) / 100.0) / total, 1)
        steps = _steps()
        steps[step_id] = StepRunState(status="running", percent=percent, phase=phase)
        _push_running(overall, label, steps)

    def on_step_tick(step_id: str, elapsed_s: float) -> None:
        if elapsed_s - last_tick_push.get(step_id, 0.0) < _STEP_TICK_PUSH_GAP_S:
            return
        last_tick_push[step_id] = elapsed_s
        phase, agent_pct = agent_progress.get(step_id, (None, None))
        n = done_count["n"]
        label = f"Step {n + 1}/{total} · {step_id}"
        if phase:
            label += f": {phase}"
        label += f" — {_fmt_duration(elapsed_s)}"
        pct = agent_pct or 0.0
        median_s = medians.get(step_id)
        if median_s:
            est = min(_STEP_EST_PCT_CAP, 100.0 * elapsed_s / median_s)
            pct = max(pct, est)
            label += f" (~{est:.0f}%, est. {_fmt_duration(median_s)})"
        overall = round(100.0 * (n + pct / 100.0) / total, 1)
        steps = _steps()
        steps[step_id] = StepRunState(status="running", percent=pct, phase=label)
        _push_running(overall, label, steps)

    def on_verify_start(step_id: str) -> None:
        steps = _steps()
        steps[step_id] = StepRunState(status="running", percent=0.0)
        _push_running(round(100.0 * done_count["n"] / total, 1), step_id, steps)

    def on_verify_done(step_id: str) -> None:
        steps = _steps()
        steps[step_id] = StepRunState(status="done", percent=100.0)
        _push_running(round(100.0 * done_count["n"] / total, 1), step_id, steps)

    return _SequenceCallbacks(
        on_step_complete,
        on_step_progress,
        on_step_tick,
        on_step_start,
        on_verify_start,
        on_verify_done,
    )


def _run_sequence_op(
    db,
    op: "PlanOp",
    ops: list["PlanOp"],
    job_id: str,
    owner_role: str,
    state: dict[str, OpRunState],
    push,
    topology: "TopologyDocument | None" = None,
) -> bool | None:
    """Run a non-createVm op as a real command sequence.

    Returns True/False on success/failure, or ``None`` when this op kind has no
    real sequence yet (the caller then falls back to the timed simulation) —
    which is also how an op whose expansion is empty for the current topology
    degrades. A resolution/sequence failure fails the op (dependents cancel).
    """
    from app.core.sequences import HealthGateError, SequenceCancelled, SequenceError
    from app.core.sequences.context import ContextError, build_run_context
    from app.core.sequences.definitions import op_sequence
    from app.core.sequences.worker import run_op_sequence

    if op.kind.value not in _REAL_SEQUENCE_KINDS:
        return None

    state[op.id] = OpRunState(status="running", percent=0.0, phase="Resolving")
    push()
    try:
        ctx = build_run_context(db, op, ops, topology)
        try:
            prior_run = (
                db["plan_runs"].find_one({"jobId": job_id}, {"artifacts": 1}) or {}
            )
        except KeyError, TypeError:
            prior_run = {}
        ctx.artifacts.update(prior_run.get("artifacts") or {})
        steps = op_sequence(op.kind.value, ctx)
    except ContextError as exc:
        state[op.id] = OpRunState(status="error", detail=str(exc))
        push()
        return False
    if not steps:
        return None  # nothing to do for this topology — let the caller simulate

    total = len(steps)
    callbacks = _sequence_progress(
        op.id, total, state, push, medians=_step_median_seconds(db, steps)
    )
    on_step_complete, on_step_progress, on_step_tick = callbacks

    op_started = time.monotonic()
    try:
        sequence_results = run_op_sequence(
            db,
            steps,
            ctx,
            plan_job_id=job_id,
            op_id=op.id,
            role=owner_role,
            on_step_complete=on_step_complete,
            on_step_progress=on_step_progress,
            on_step_tick=on_step_tick,
            on_step_start=callbacks.start,
            on_verify_start=callbacks.verify_start,
            on_verify_done=callbacks.verify_done,
            should_stop=lambda: transport.cancel_mode(job_id) == "step",
        )
    except SequenceCancelled as exc:
        state[op.id] = OpRunState(
            status="cancelled", detail=str(exc), steps=state[op.id].steps
        )
        push()
        return False
    except HealthGateError as exc:
        from app.core.sequences.journey import build_certificate_journey

        state[op.id] = OpRunState(
            status="error",
            detail=str(exc),
            result={
                "health": exc.health,
                "certificateJourney": build_certificate_journey(ctx, exc.results),
            },
            steps=state[op.id].steps,
        )
        push()
        return False
    except SequenceError as exc:
        state[op.id] = OpRunState(
            status="error", detail=str(exc), steps=state[op.id].steps
        )
        push()
        return False
    except Exception as exc:  # noqa: BLE001 — surface as an op-level failure
        state[op.id] = OpRunState(
            status="error", detail=str(exc), steps=state[op.id].steps
        )
        push()
        return False

    logger.info(
        "op %s: %s sequence (%d steps) completed in %.1fs",
        op.id,
        op.kind.value,
        total,
        time.monotonic() - op_started,
    )
    result = {"steps": total}
    if "lab-health" in sequence_results:
        result["health"] = sequence_results["lab-health"]
        from app.core.sequences.journey import build_certificate_journey

        result["certificateJourney"] = build_certificate_journey(ctx, sequence_results)
    state[op.id] = OpRunState(
        status="done",
        percent=100.0,
        phase="Done",
        result=result,
        steps=state[op.id].steps,
    )
    push()
    return True


def _simulate_op(op: "PlanOp", state: dict[str, OpRunState], push) -> bool:
    """Advance a stubbed op through its 3 named phases. Always succeeds in v1."""
    phases = _SIMULATED_PHASES[op.kind.value]
    for phase, percent in zip(phases, _SIMULATED_PERCENTS):
        time.sleep(_SIMULATED_STEP_SECONDS)
        state[op.id] = OpRunState(status="running", percent=percent, phase=phase)
        push()
    state[op.id] = OpRunState(
        status="done", percent=100.0, phase=phases[-1], result={"simulated": True}
    )
    push()
    return True


def _verify_worker_preflight(
    conn: "Connection",
    db,
    ops: list["PlanOp"],
    accepted_payload: dict | None,
) -> GoldenImageConfig:
    """Re-check the API snapshot before any clone can write to the datastore."""
    if accepted_payload is None:
        raise RuntimeError("Deploy job is missing its golden-image preflight snapshot.")
    accepted = GoldenImagePreflight(**accepted_payload)
    doc = db["settings"].find_one({"_id": "global"})
    config = golden_image_config_from_doc(doc)
    names = [op.params["vmName"] for op in ops if op.kind.value == "createVm"]
    current = preflight_golden_image(
        conn,
        config,
        requested_vm_names=names,
        clone_count=len(names),
    )
    if not current.ready:
        failed = "; ".join(check.detail for check in current.checks if not check.ok)
        raise RuntimeError(f"Golden-image preflight no longer passes: {failed}")
    if current.snapshot_id != accepted.snapshot_id:
        raise RuntimeError(
            "Golden-image prerequisites changed after preflight; retry the deploy."
        )
    return config


def _verify_worker_infrastructure_preflight(
    conn: "Connection", db, ops: list["PlanOp"], accepted_payload: dict | None
):
    """Re-check the complete role mapping and reservation before cloning."""

    if accepted_payload is None:
        raise RuntimeError(
            "Deploy job is missing its infrastructure preflight snapshot."
        )
    accepted = InfrastructurePreflight(**accepted_payload)
    doc = db["settings"].find_one({"_id": "global"})
    profiles = deployment_profiles_from_doc(doc)
    machines = [
        PlannedMachine(
            role=role_for_template(op.params["template"], op.params.get("caType")),
            name=op.params["vmName"],
        )
        for op in ops
        if op.kind.value == "createVm"
    ]
    current = preflight_infrastructure(conn, profiles, machines)
    if not current.ready:
        failed = "; ".join(check.detail for check in current.checks if not check.ok)
        raise RuntimeError(f"Infrastructure preflight no longer passes: {failed}")
    if current.snapshot_id != accepted.snapshot_id:
        raise RuntimeError(
            "Infrastructure prerequisites changed after preflight; retry the deploy."
        )
    return profiles


def _initialize_plan_run(
    db,
    job_id: str,
    request,
    owner_role: str,
    owner: str | None,
    preflight_snapshot: dict | None,
) -> None:
    """Persist a redacted recovery/evidence snapshot before the first step."""

    ttl_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
    payload = request.model_dump(by_alias=True)
    db["plan_runs"].update_one(
        {"jobId": job_id},
        {
            "$setOnInsert": {
                "jobId": job_id,
                "owner": owner,
                "ownerRole": owner_role,
                "topology": redact_evidence(payload.get("topology") or {}),
                "operations": redact_evidence(payload.get("ops") or []),
                "preflight": preflight_snapshot,
                "createdAt": now_ms(),
            },
            "$set": {"updatedAt": now_ms(), "ttlAt": ttl_at},
        },
        upsert=True,
    )


_PLAN_TERMINAL = frozenset({"done", "error", "cancelled"})


def ready_plan_operations(ops, statuses: dict[str, str]) -> tuple[list[str], list[str]]:
    """Pure scheduler decision: (ready ids, dependency-blocked ids)."""

    ready: list[str] = []
    blocked: list[str] = []
    for op in ops:
        if statuses.get(op.id) != "pending":
            continue
        dependency_states = [statuses.get(dep, "pending") for dep in op.depends_on]
        if any(status in {"error", "cancelled"} for status in dependency_states):
            blocked.append(op.id)
        elif all(status == "done" for status in dependency_states):
            ready.append(op.id)
    return ready, blocked


def _scheduler_states(db, job_id: str) -> dict[str, OpRunState]:
    doc = db["plan_runs"].find_one({"jobId": job_id}, {"scheduler.ops": 1}) or {}
    raw = (doc.get("scheduler") or {}).get("ops") or {}
    return {op_id: OpRunState(**value) for op_id, value in raw.items()}


def _publish_scheduler_states(db, job_id: str) -> dict[str, OpRunState]:
    states = _scheduler_states(db, job_id)
    transport.publish(job_id, PlanStateMsg(ops=states), status=JobStatus.running)
    return states


def _persist_scheduler_op(db, job_id: str, op_id: str, state: OpRunState) -> None:
    db["plan_runs"].update_one(
        {"jobId": job_id},
        {
            "$set": {
                f"scheduler.ops.{op_id}": state.model_dump(),
                "scheduler.updatedAt": now_ms(),
                "updatedAt": now_ms(),
            }
        },
    )


def _advance_plan(
    job_id: str,
    request,
    plan: dict,
    owner_role: str,
    preflight_snapshot: dict | None,
    owner: str | None,
    db,
) -> None:
    """Atomically claim newly-ready operations and publish terminal state."""

    states = _scheduler_states(db, job_id)
    statuses = {op_id: state.status for op_id, state in states.items()}
    if transport.cancel_mode(job_id):
        blocked = [op.id for op in request.ops if statuses.get(op.id) == "pending"]
        ready = []
    else:
        ready, blocked = ready_plan_operations(request.ops, statuses)

    while blocked:
        changed = False
        for op_id in blocked:
            if transport.cancel_mode(job_id):
                # A cross-user admin stop stores a reason so the affected user
                # sees *why* their op was cancelled; own-job cancels have none.
                detail = (
                    transport.cancel_reason(job_id)
                    or "Skipped: deployment cancellation was requested."
                )
            else:
                detail = "Skipped: a dependency failed or was cancelled."
            result = db["plan_runs"].update_one(
                {"jobId": job_id, f"scheduler.ops.{op_id}.status": "pending"},
                {
                    "$set": {
                        f"scheduler.ops.{op_id}": OpRunState(
                            status="cancelled", detail=detail
                        ).model_dump(),
                        "scheduler.updatedAt": now_ms(),
                    }
                },
            )
            if result.modified_count:
                statuses[op_id] = "cancelled"
                changed = True
        if transport.cancel_mode(job_id):
            break
        if not changed:
            statuses = {
                op_id: state.status
                for op_id, state in _scheduler_states(db, job_id).items()
            }
        ready, blocked = ready_plan_operations(request.ops, statuses)

    kinds = {op.id: str(getattr(op.kind, "value", op.kind)) for op in request.ops}
    enqueue_failed = False
    for op_id in ready:
        result = db["plan_runs"].update_one(
            {"jobId": job_id, f"scheduler.ops.{op_id}.status": "pending"},
            {
                "$set": {
                    f"scheduler.ops.{op_id}.status": "queued",
                    "scheduler.updatedAt": now_ms(),
                }
            },
        )
        if not result.modified_count:
            continue
        statuses[op_id] = "queued"
        try:
            run_plan_operation_task.apply_async(
                args=[job_id, op_id, plan, owner_role, preflight_snapshot, owner],
                task_id=f"{job_id}:{op_id}",
                # The one per-op routing decision task_routes can't express:
                # only createVm opens an ESXi connection, so only it competes
                # for the esxi queue's clone-concurrency slots.
                queue="esxi" if kinds.get(op_id) == "createVm" else "provision",
            )
        except Exception as exc:
            enqueue_failed = True
            _persist_scheduler_op(
                db,
                job_id,
                op_id,
                OpRunState(
                    status="error", detail=f"Unable to enqueue operation: {exc}"
                ),
            )

    states = _publish_scheduler_states(db, job_id)
    if enqueue_failed:
        _advance_plan(job_id, request, plan, owner_role, preflight_snapshot, owner, db)
        return
    if states and all(state.status in _PLAN_TERMINAL for state in states.values()):
        stop_reason = transport.cancel_reason(job_id)
        if stop_reason:
            # Stopped (not completed): drain to a terminal ErrorMsg so the
            # canvas toasts the reason and releases the deploy lock, instead of
            # a misleading "Deploy complete." from DoneMsg.
            transport.publish(
                job_id,
                ErrorMsg(status=409, detail=stop_reason),
                status=JobStatus.error,
                terminal=True,
            )
        else:
            transport.publish(
                job_id,
                DoneMsg(
                    result={
                        "ops": {
                            op_id: state.model_dump() for op_id, state in states.items()
                        }
                    }
                ),
                status=JobStatus.done,
                terminal=True,
            )


@celery_app.task(name="start_plan_v2")
def start_plan_task(
    job_id: str,
    plan: dict,
    owner_role: str = "guest",
    preflight_snapshot: dict | None = None,
    owner: str | None = None,
) -> None:
    """Compile, preflight, persist, and fan out every ready plan operation."""

    from app.routers.deploy import DeployRequest, PlanOpKind
    from app.core.topology import compile_plan

    conn = None
    request = None

    # Setup breadcrumbs: the window between `queued` and `running` is otherwise
    # silent while the worker connects and re-verifies — narrate it so the
    # client can show what the wait actually is. Still `queued`: no op has run.
    def _setup_phase(phase: str) -> None:
        transport.publish(
            job_id,
            ProgressMsg(percent=0.0, phase=phase, key="planSetup"),
            status=JobStatus.queued,
        )

    try:
        _setup_phase("Preparing plan…")
        request = DeployRequest(**plan)
        request.ops = compile_plan(request.topology, request.ops).operations
        plan = request.model_dump(by_alias=True)
        with worker_db() as db:
            _initialize_plan_run(
                db, job_id, request, owner_role, owner, preflight_snapshot
            )
            from app.routers.iso import gc_orphan_isos

            gc_orphan_isos(db)
            _sweep_stale_agents_sync(db)
            if any(op.kind is PlanOpKind.create_vm for op in request.ops):
                _setup_phase("Connecting to the ESXi host…")
                conn = _open_worker_connection()
                _setup_phase("Re-verifying infrastructure against the host…")
                _verify_worker_infrastructure_preflight(
                    conn, db, request.ops, preflight_snapshot
                )
            initial = {
                op.id: OpRunState(status="pending").model_dump() for op in request.ops
            }
            db["plan_runs"].update_one(
                {"jobId": job_id},
                {
                    "$set": {
                        "scheduler.version": 2,
                        "scheduler.ops": initial,
                        "scheduler.updatedAt": now_ms(),
                    }
                },
            )
            transport.publish(job_id, RunningMsg(), status=JobStatus.running)
            _advance_plan(
                job_id, request, plan, owner_role, preflight_snapshot, owner, db
            )
    except Exception as exc:  # noqa: BLE001
        transport.publish(
            job_id,
            ErrorMsg(status=500, detail=str(exc)),
            status=JobStatus.error,
            terminal=True,
        )
    finally:
        if conn is not None:
            Disconnect(conn.si)


@celery_app.task(name="run_plan_operation_v2")
def run_plan_operation_task(
    job_id: str,
    op_id: str,
    plan: dict,
    owner_role: str = "guest",
    preflight_snapshot: dict | None = None,
    owner: str | None = None,
) -> None:
    """Run one atomically claimed operation, then release its dependents."""

    from app.routers.deploy import DeployRequest, PlanOpKind
    from app.core.topology import compile_plan

    conn = None
    request = None
    try:
        request = DeployRequest(**plan)
        request.ops = compile_plan(request.topology, request.ops).operations
        op = next(item for item in request.ops if item.id == op_id)
        with worker_db() as db:
            claim = db["plan_runs"].update_one(
                {"jobId": job_id, f"scheduler.ops.{op_id}.status": "queued"},
                {
                    "$set": {
                        f"scheduler.ops.{op_id}.status": "running",
                        f"scheduler.ops.{op_id}.percent": 0.0,
                        f"scheduler.ops.{op_id}.phase": "Starting",
                        "scheduler.updatedAt": now_ms(),
                    }
                },
            )
            if not claim.modified_count:
                return
            states = _scheduler_states(db, job_id)
            if transport.cancel_mode(job_id):
                states[op_id] = OpRunState(
                    status="cancelled",
                    detail="Skipped: deployment cancellation was requested.",
                )
                _persist_scheduler_op(db, job_id, op_id, states[op_id])
                _advance_plan(
                    job_id, request, plan, owner_role, preflight_snapshot, owner, db
                )
                return

            def push() -> None:
                _persist_scheduler_op(db, job_id, op_id, states[op_id])
                _publish_scheduler_states(db, job_id)

            push()
            if op.kind is PlanOpKind.create_vm:
                conn = _open_worker_connection()
                profiles = deployment_profiles_from_doc(
                    db["settings"].find_one({"_id": "global"})
                )
                ok = _run_clone_op(
                    conn,
                    db,
                    op,
                    job_id,
                    states,
                    push,
                    owner_role,
                    profiles[
                        role_for_template(
                            op.params["template"], op.params.get("caType")
                        )
                    ],
                )
            elif op.kind is PlanOpKind.provision:
                # Deliberately no ESXi connection here — provision ops run on
                # the provision queue and must never consume esxi capacity.
                ok = _run_provision_op(
                    db,
                    op,
                    request.ops,
                    job_id,
                    owner_role,
                    states,
                    push,
                    request.topology,
                )
            else:
                result = _run_sequence_op(
                    db,
                    op,
                    request.ops,
                    job_id,
                    owner_role,
                    states,
                    push,
                    request.topology,
                )
                ok = _simulate_op(op, states, push) if result is None else result
            if not ok and states[op_id].status not in {"error", "cancelled"}:
                states[op_id] = OpRunState(status="error", detail="Operation failed.")
                push()
            _advance_plan(
                job_id, request, plan, owner_role, preflight_snapshot, owner, db
            )
    except Exception as exc:  # noqa: BLE001
        if request is None:
            transport.publish(
                job_id,
                ErrorMsg(status=500, detail=str(exc)),
                status=JobStatus.error,
                terminal=True,
            )
            return
        with worker_db() as db:
            _persist_scheduler_op(
                db, job_id, op_id, OpRunState(status="error", detail=str(exc))
            )
            _advance_plan(
                job_id, request, plan, owner_role, preflight_snapshot, owner, db
            )
    finally:
        if conn is not None:
            Disconnect(conn.si)


@celery_app.task(name="reconcile_plan")
def reconcile_plan_task(
    job_id: str,
    source_job_id: str,
    owner_role: str = "guest",
    owner: str | None = None,
) -> None:
    """Reapply convergent non-clone operations from a persisted plan snapshot."""

    from app.core.topology import TopologyDocument
    from app.routers.deploy import PlanOp

    transport.publish(job_id, RunningMsg(), status=JobStatus.running)
    try:
        with worker_db() as db:
            source = db["plan_runs"].find_one({"jobId": source_job_id})
            if source is None:
                raise RuntimeError(f"source deployment '{source_job_id}' was not found")
            topology = TopologyDocument(**(source.get("topology") or {}))
            ops = [
                PlanOp(**raw)
                for raw in source.get("operations") or []
                # provision excluded too: it only makes sense over a fresh
                # clone — replaying it here would fall through to the `None`
                # sequence fallback and mark every provision op stopped.
                if raw.get("kind") not in ("createVm", "provision", "domainLeave")
            ]
            state = {op.id: OpRunState(status="pending") for op in ops}

            def push() -> None:
                transport.publish(
                    job_id, PlanStateMsg(ops=dict(state)), status=JobStatus.running
                )

            push()
            stopped = False
            for op in ops:
                if transport.cancel_mode(job_id):
                    state[op.id] = OpRunState(
                        status="cancelled", detail="Reconcile cancellation requested."
                    )
                    stopped = True
                    push()
                    continue
                result = _run_sequence_op(
                    db, op, ops, job_id, owner_role, state, push, topology
                )
                if result is not True:
                    stopped = True
                    # Continue independent operations so the evidence bundle
                    # captures all drift, not only the first failed service.

            db["plan_runs"].update_one(
                {"jobId": job_id},
                {
                    "$set": {
                        "updatedAt": now_ms(),
                        "reconcileComplete": not stopped,
                        "owner": owner,
                    }
                },
            )
            transport.publish(
                job_id,
                DoneMsg(
                    result={
                        "sourceJobId": source_job_id,
                        "reconciled": not stopped,
                        "ops": {
                            key: value.model_dump() for key, value in state.items()
                        },
                    }
                ),
                status=JobStatus.done,
                terminal=True,
            )
    except Exception as exc:  # noqa: BLE001
        transport.publish(
            job_id,
            ErrorMsg(status=500, detail=str(exc)),
            status=JobStatus.error,
            terminal=True,
        )


@celery_app.task(name="teardown_plan")
def teardown_plan_task(
    job_id: str,
    source_job_id: str,
    owner_role: str = "guest",
    owner: str | None = None,
) -> None:
    """Execute a compiled teardown while continuing past cleanup warnings."""

    from app.core.sequences import SequenceCancelled
    from app.core.sequences.context import build_teardown_context
    from app.core.sequences.definitions import teardown_action_sequence
    from app.core.sequences.worker import run_op_sequence
    from app.core.teardown import compile_teardown
    from app.core.topology import TopologyDocument

    transport.publish(job_id, RunningMsg(), status=JobStatus.running)
    conn = None
    try:
        with worker_db() as db:
            source = db["plan_runs"].find_one({"jobId": source_job_id})
            if source is None:
                raise RuntimeError(f"source deployment '{source_job_id}' was not found")
            topology = TopologyDocument(**(source.get("topology") or {}))
            actions = compile_teardown(topology)
            state = {action.id: OpRunState(status="pending") for action in actions}
            warnings: list[str] = []

            def push() -> None:
                transport.publish(
                    job_id, PlanStateMsg(ops=dict(state)), status=JobStatus.running
                )

            push()
            for position, action in enumerate(actions):
                if transport.cancel_mode(job_id):
                    for pending in actions[position:]:
                        state[pending.id] = OpRunState(
                            status="cancelled",
                            detail="Teardown cancellation requested.",
                        )
                    push()
                    break

                state[action.id] = OpRunState(
                    status="running", percent=0.0, phase=action.kind
                )
                push()
                if action.kind == "vm.destroy":
                    registry = db["vm_registry"].find_one(
                        {"appName": action.node_id, "status": {"$ne": "deleted"}}
                    )
                    if registry is None:
                        state[action.id] = OpRunState(
                            status="done",
                            percent=100.0,
                            phase="Already absent",
                            result={"alreadyAbsent": True},
                        )
                        push()
                        continue
                    vm_name = registry["vmName"]
                    try:
                        conn = _live_worker_connection(conn)
                        try:
                            destroy_workflow(conn, name=vm_name)
                        except VmNotFoundError:
                            pass
                        release_ip_sync(db, vm_name)
                        _registry_upsert_sync(
                            db,
                            vm_name,
                            status="deleted",
                            powerState=None,
                            ip=None,
                            agent=None,
                        )
                    except Exception as exc:  # noqa: BLE001
                        state[action.id] = OpRunState(status="error", detail=str(exc))
                        warnings.append(f"{action.id}: {exc}")
                    else:
                        state[action.id] = OpRunState(
                            status="done",
                            percent=100.0,
                            phase="Destroyed",
                            result={"vmName": vm_name},
                        )
                    push()
                    continue

                try:
                    ctx = build_teardown_context(db, topology, action.node_id)
                    steps = teardown_action_sequence(action.kind, ctx)
                    if steps:
                        run_op_sequence(
                            db,
                            steps,
                            ctx,
                            plan_job_id=job_id,
                            op_id=action.id,
                            role=owner_role,
                            should_stop=lambda: transport.cancel_mode(job_id) == "step",
                        )
                except SequenceCancelled:
                    state[action.id] = OpRunState(
                        status="cancelled", detail="Teardown cancellation requested."
                    )
                except Exception as exc:  # noqa: BLE001
                    # Cleanup is best effort; VM destruction remains available
                    # when a broken guest agent cannot uninstall its role.
                    state[action.id] = OpRunState(status="error", detail=str(exc))
                    warnings.append(f"{action.id}: {exc}")
                else:
                    state[action.id] = OpRunState(
                        status="done", percent=100.0, phase="Removed"
                    )
                push()

            db["plan_runs"].update_one(
                {"jobId": job_id},
                {
                    "$set": {
                        "updatedAt": now_ms(),
                        "teardownWarnings": warnings,
                        "owner": owner,
                    }
                },
            )
            transport.publish(
                job_id,
                DoneMsg(
                    result={
                        "sourceJobId": source_job_id,
                        "removed": not any(
                            item.status == "error" and action.kind == "vm.destroy"
                            for action, item in (
                                (action, state[action.id]) for action in actions
                            )
                        ),
                        "warnings": warnings,
                        "ops": {
                            key: value.model_dump() for key, value in state.items()
                        },
                    }
                ),
                status=JobStatus.done,
                terminal=True,
            )
    except Exception as exc:  # noqa: BLE001
        transport.publish(
            job_id,
            ErrorMsg(status=500, detail=str(exc)),
            status=JobStatus.error,
            terminal=True,
        )
    finally:
        if conn is not None:
            Disconnect(conn.si)
