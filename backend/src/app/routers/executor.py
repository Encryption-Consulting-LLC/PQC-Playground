"""Executor phone-home routes.

The Rust ``pki-executor`` agent connects outbound to
``ws /api/executor/connect`` once running, authenticating with a
vm_id/token pair. Two ways that pair exists:

* **Persisted:** the clone worker mints it and stores the hash on the
  VM's ``vm_registry`` document; the agent binary + its ``executor.toml``
  are baked onto the firstboot ISO, so a real deployed agent phones home with
  no human in the loop.
* **Pending (manual/dev):** ``POST /executor/register`` mints an in-process
  pair a human pastes into a local config.

Provisioning is **plan-driven**: the connect handler no longer
dispatches a per-template command on connect. It only marks the agent live
(``agentbus.mark_agent_live`` — the liveness key + ``lastConnectedAt`` the
worker's sequence engine waits on) and relays the agent's progress frames onto
the job transport. All command dispatch now flows from the Celery plan runner
through the ``agent-dispatch`` bridge (:mod:`app.core.agentbus`), which this
process forwards to whichever socket it holds (a lifespan subscriber in
``main.py``).

Auth is validated before ``accept()`` (4401 on failure); the agent sends
vm_id/token as request headers (kept out of access logs), with the browser-style
``?vm_id=&token=`` query still accepted for the manual path.

Dispatching a command (``POST /executor/{vm_id}/command``) is capability-gated
*and* ownership-gated: a guest can only command an agent bound to a VM in its own
namespace. The authenticated caller's role is forwarded in the frame; the agent
re-checks it locally as a second, structural gate (see ``phonehome.rs``).
"""

import asyncio
import contextlib
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core import agentbus, agents
from app.core.authz import (
    AuthedUser,
    Capability,
    Role,
    ROLE_CAPABILITIES,
    enforce_guest_vm_ownership,
    get_current_user,
    require_capability,
    resolve_user_token,
)
from app.core.db import vm_registry_col
from app.core.jobs import transport
from app.core.labs import enforce_own_or_joined_vm
from app.core.jobs.models import DoneMsg, ErrorMsg, JobStatus, ProgressMsg, QueuedMsg
from app.routers.ws import reject, send_json_or_disconnect

router = APIRouter(prefix="/executor", tags=["executor"])
logger = logging.getLogger(__name__)

# Mirrors pki-executor's `authz::Role::capabilities()` / command handlers
# (`commands/*.rs`) — there is no automated sync between the two languages,
# but both sides assert against byte-identical catalog fixtures
# (``tests/fixtures/command_catalog.json`` here and in pki-executor), so
# adding a command on one side without the other fails a test instead of
# surfacing as a 422 on dispatch.
_COMMAND_CAPABILITIES: dict[str, Capability] = {
    "hostname.rename": Capability.VM_UPDATE,
    "hostname.read": Capability.VM_READ,
    "ip.read": Capability.VM_READ,
    "ip.write": Capability.VM_UPDATE,
    "cert.verify": Capability.VM_READ,
    "ca.install": Capability.VM_PROVISION,
    "ca.uninstall": Capability.VM_PROVISION,
    "ca.configure_settings": Capability.VM_PROVISION,
    "ca.configure_cdp_aia": Capability.VM_PROVISION,
    "ca.publish_crl": Capability.VM_PROVISION,
    "ca.sign_request": Capability.VM_PROVISION,
    "ca.install_cert": Capability.VM_PROVISION,
    "ca.publish_template": Capability.VM_PROVISION,
    "ca.verify": Capability.VM_READ,
    "file.read": Capability.VM_PROVISION,
    "file.write": Capability.VM_PROVISION,
    "iis.setup_certenroll": Capability.VM_PROVISION,
    "iis.remove_certenroll": Capability.VM_PROVISION,
    "ocsp.install": Capability.VM_PROVISION,
    "ocsp.remove": Capability.VM_PROVISION,
    "ocsp.configure_revocation": Capability.VM_PROVISION,
    "ocsp.verify": Capability.VM_READ,
    "pki.verify": Capability.VM_READ,
    "cert.enroll": Capability.VM_PROVISION,
    "dc.install_forest": Capability.VM_PROVISION,
    "dc.remove_forest": Capability.VM_PROVISION,
    "dc.verify": Capability.VM_READ,
    "domain.join": Capability.VM_PROVISION,
    "domain.leave": Capability.VM_PROVISION,
    "domain.verify": Capability.VM_READ,
    "system.reboot": Capability.VM_PROVISION,
    "system.boot_info": Capability.VM_READ,
    "system.identity": Capability.VM_READ,
    "dns.set_client": Capability.VM_PROVISION,
    "dns.create_record": Capability.VM_PROVISION,
    "dns.apply_resources": Capability.VM_PROVISION,
    "dns.remove_resources": Capability.VM_PROVISION,
    "dns.verify": Capability.VM_READ,
    "dns.verify_absent": Capability.VM_READ,
    "cert.addstore": Capability.VM_PROVISION,
    "cert.dspublish": Capability.VM_PROVISION,
    "template.grant_access": Capability.VM_PROVISION,
    "powershell.exec_arbitrary": Capability.VM_EXEC_ARBITRARY,
    # Linux product provisioning. Registered on every platform in the agent (the
    # catalog is one contract), dispatched only to a Linux product guest.
    "apt.refresh": Capability.VM_PROVISION,
    "certsecure.write_hosts": Capability.VM_PROVISION,
    "certsecure.make_cert": Capability.VM_PROVISION,
    "certsecure.install": Capability.VM_PROVISION,
    "certsecure.harden": Capability.VM_PROVISION,
    "certsecure.verify": Capability.VM_READ,
}


class RegisterResponse(BaseModel):
    vm_id: str
    token: str


@router.post(
    "/register",
    dependencies=[Depends(require_capability(Capability.VM_CLONE))],
)
def register() -> RegisterResponse:
    """Mint an in-process vm_id/token pair for the manual/dev flow.

    A human copies both values into that agent's ``executor.toml``. Real
    deployed agents are provisioned automatically by the clone worker instead
    (persisted identity) — see the module docstring.
    """
    vm_id, token = agents.register_agent()
    return RegisterResponse(vm_id=vm_id, token=token)


class CommandRequest(BaseModel):
    command: str
    params: dict[str, str] = {}


@router.post("/{vm_id}/command", status_code=202)
async def dispatch_command(
    vm_id: str, req: CommandRequest, user: AuthedUser = Depends(get_current_user)
) -> dict:
    """Dispatch one command to a connected agent; stream progress over ws /api/ws/jobs/{job_id}."""
    required = _COMMAND_CAPABILITIES.get(req.command)
    if required is None:
        raise HTTPException(422, detail=f"Unknown executor command '{req.command}'.")

    role = user.role
    if required not in ROLE_CAPABILITIES[role]:
        raise HTTPException(
            403,
            detail=f"Role '{role.value}' does not have capability '{required.value}'.",
        )

    # Per-VM ownership: resolve vm_id -> the VM it's bound to, and refuse a guest
    # commanding an agent outside its namespace. A persisted agent always has a
    # registry doc; the manual/dev path has none — allowed for operators only.
    doc = await vm_registry_col().find_one({"agent.vmId": vm_id})
    if doc is not None and doc.get("status") == "deleted":
        raise HTTPException(404, detail=f"No executor agent for vm_id '{vm_id}'.")
    vm_name = doc.get("vmName") if doc else None
    if vm_name is not None:
        enforce_guest_vm_ownership(vm_name, user)
    elif role == Role.GUEST:
        raise HTTPException(404, detail=f"No executor agent for vm_id '{vm_id}'.")

    agent = agents.resolve_agent(vm_id)
    if agent is None:
        raise HTTPException(
            404, detail=f"No connected executor agent for vm_id '{vm_id}'."
        )

    job_id = uuid.uuid4().hex
    transport.publish(job_id, QueuedMsg(), status=JobStatus.queued)
    await agent.send(
        {
            "job_id": job_id,
            "command": req.command,
            "params": req.params,
            "role": role.value,
        }
    )
    return {"job_id": job_id}


async def _authenticate(vm_id: str | None, token: str | None) -> bool:
    if not vm_id or not token:
        return False
    if agents.authenticate_pending(vm_id, token):
        return True
    return await agents.authenticate_persisted(vm_id, token)


#: Liveness-key refresh cadence — comfortably inside ``AGENT_CONN_TTL_SECONDS``
#: so an idle-but-live agent's key never lapses between frames.
_KEEPALIVE_INTERVAL_S = 30
_KEEPALIVE_RETRY_S = 5


async def _keepalive(vm_id: str, *, sleep=asyncio.sleep) -> None:
    """Re-arm the agent's liveness TTL on a fixed cadence for as long as the
    socket lives (frames are sporadic, so the receive loop can't be relied on
    to refresh it). A transient Valkey failure must not kill this task: doing
    so leaves the in-process socket (and the UI's green presence dot) live while
    the worker-facing lease expires. Cancelled in the connect handler's
    ``finally``."""
    while True:
        await sleep(_KEEPALIVE_INTERVAL_S)
        while True:
            try:
                await agentbus.refresh_agent_live(vm_id)
                break
            except Exception:  # noqa: BLE001 - retry while the socket is live
                logger.exception(
                    "failed to refresh liveness lease for agent %s; retrying",
                    vm_id,
                )
                await sleep(_KEEPALIVE_RETRY_S)


@router.websocket("/connect")
async def connect(websocket: WebSocket) -> None:
    """Accept an executor agent's phone-home connection, mark it live, and
    relay its progress frames onto the job transport.

    vm_id/token come from the ``X-Executor-Vm-Id``/``X-Executor-Token``
    headers (the agent's path) or ``?vm_id=&token=`` (manual/dev). Auth is
    validated before any agent state is touched; a failure is rejected with 4401
    via ``ws.reject`` so the code survives. Provisioning is no longer
    kicked off here — the Celery plan runner drives every command through the
    ``agent-dispatch`` bridge.
    """
    vm_id = websocket.headers.get("x-executor-vm-id") or websocket.query_params.get(
        "vm_id"
    )
    token = websocket.headers.get("x-executor-token") or websocket.query_params.get(
        "token"
    )
    if not await _authenticate(vm_id, token):
        await reject(websocket, 4401)
        return

    await websocket.accept()
    # Single live connection per vm_id: close any prior socket (a copied ISO or a
    # half-dead reconnect) so it can't shadow the fresh one.
    previous = agents.pop_connection(vm_id)
    if previous is not None:
        try:
            await previous.websocket.close(code=4409)
        except Exception:  # noqa: BLE001 — the old socket may already be gone
            pass
    conn = agents.connect_agent(vm_id, websocket)
    logger.info("agent %s connected (phone-home)", vm_id)

    # Mark live (liveness key + lastConnectedAt) — the reboot-resume signal the
    # worker's sequence engine polls. A registry-less manual/dev agent still
    # gets its liveness key; the lastConnectedAt update simply matches nothing.
    keepalive: asyncio.Task | None = None
    try:
        # Keep setup inside the ownership guard. If the initial Valkey/Mongo
        # write fails, the socket handler exits and the map is cleaned instead
        # of leaking a permanently green presence entry with no live lease.
        await agentbus.mark_agent_live(vm_id)
        keepalive = asyncio.create_task(_keepalive(vm_id))
        while True:
            frame = await websocket.receive_json()
            job_id = frame.get("job_id")
            state = frame.get("state")
            if job_id and state:
                _relay_progress(job_id, state)
    except WebSocketDisconnect:
        pass
    finally:
        logger.info("agent %s disconnected (phone-home)", vm_id)
        if keepalive is not None:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
        # Only clear the shared liveness key if this socket is still the
        # registered one — a connection evicted by a takeover (4409) must not
        # delete the key its replacement just set.
        if agents.disconnect_if(vm_id, conn):
            await agentbus.clear_agent_live(vm_id)


def _relay_progress(job_id: str, state: dict) -> None:
    """Translate one executor `OpRunState` frame onto the existing job transport.

    `pending`/`cancelled` are never emitted by the executor's own
    `report.rs` helpers (only `running`/`done`/`error` are) — anything else
    is ignored rather than guessed at.
    """
    status = state.get("status")
    # Inbound half of the backend<-agent exchange. Terminal frames at info so the
    # command round-trip is visible in the default log; running frames (which can
    # be frequent) at debug.
    if status in ("done", "error"):
        logger.info(
            "<- agent frame job_id=%s status=%s detail=%s",
            job_id,
            status,
            state.get("detail"),
        )
    else:
        logger.debug(
            "<- agent frame job_id=%s status=%s phase=%s percent=%s",
            job_id,
            status,
            state.get("phase"),
            state.get("percent"),
        )
    if status == "running":
        transport.publish(
            job_id,
            ProgressMsg(
                percent=state.get("percent") or 0.0,
                phase=state.get("phase") or "",
                key=job_id,
            ),
            status=JobStatus.running,
        )
    elif status == "done":
        transport.publish(
            job_id,
            DoneMsg(result=state.get("result") or {}),
            status=JobStatus.done,
            terminal=True,
        )
    elif status == "error":
        transport.publish(
            job_id,
            ErrorMsg(
                status=500, detail=state.get("detail") or "executor command failed"
            ),
            status=JobStatus.error,
            terminal=True,
        )


async def _visible_agent_vm_ids(user: AuthedUser) -> list[str]:
    """vm_ids with a currently-connected agent, filtered for guests to the
    caller's own namespace plus any lab it has joined (operators see everything).

    Presence, and nothing else: the command route below keeps the plain
    own-namespace check, so seeing that a joined lab's machines are alive never
    becomes the ability to drive them. Without the lab clause a perfectly
    healthy pre-deployed lab renders every node with a red "executor offline"
    dot, which is a false alarm about somebody else's working environment.
    """
    vm_ids = agents.connected_vm_ids()
    if user.role != Role.GUEST:
        return vm_ids

    visible: list[str] = []
    for vm_id in vm_ids:
        doc = await vm_registry_col().find_one({"agent.vmId": vm_id}, {"vmName": 1})
        if doc is None:
            continue
        try:
            await enforce_own_or_joined_vm(doc["vmName"], user)
        except HTTPException:
            continue
        visible.append(vm_id)
    return visible


@router.get(
    "/agents",
    dependencies=[Depends(require_capability(Capability.VM_LIST))],
)
async def list_agents(user: AuthedUser = Depends(get_current_user)) -> dict:
    """List vm_ids with a currently-connected agent (guests see only their own)."""
    return {"vm_ids": await _visible_agent_vm_ids(user)}


#: Fallback re-check cadence for the presence watch — bounds how stale a
#: snapshot can go if a change ever slips past the in-process signal, and how
#: long a silently-dead browser socket lingers before a send attempt notices.
_PRESENCE_REFRESH_S = 30


@router.websocket("/agents/watch")
async def watch_agents(websocket: WebSocket, token: str | None = None) -> None:
    """Live agent-presence stream — the frontend's per-node online indicator.

    Auth mirrors ``ws /api/ws/jobs``: the session JWT rides as ``?token=``
    (browsers can't set headers on the upgrade) and is resolved by the same
    ``resolve_user_token``; 4401 on failure. On connect the client gets one
    ``{"type": "agents", "vm_ids": [...]}`` snapshot (guest-filtered like
    ``GET /agents``), then a fresh snapshot the moment any agent connects or
    disconnects (via ``agents.subscribe_presence`` — in-process, same
    single-API-process constraint as the connection map itself).
    """
    user = await resolve_user_token(token)
    if user is None or Capability.VM_LIST not in ROLE_CAPABILITIES[user.role]:
        await reject(websocket, 4401)
        return
    await websocket.accept()

    queue = agents.subscribe_presence()
    # The browser never sends frames; a pending receive is how we notice it
    # going away while we sit between presence changes.
    receiver = asyncio.create_task(websocket.receive_text())
    signal = asyncio.create_task(queue.get())
    try:
        last: list[str] | None = None
        while True:
            vm_ids = await _visible_agent_vm_ids(user)
            if vm_ids != last:
                await send_json_or_disconnect(
                    websocket, {"type": "agents", "vm_ids": vm_ids}
                )
                last = vm_ids
            done, _ = await asyncio.wait(
                {signal, receiver},
                timeout=_PRESENCE_REFRESH_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receiver in done:
                receiver.result()  # raises WebSocketDisconnect on close
                return  # an unexpected client frame — treat as end-of-watch
            if signal in done:
                signal.result()
                # Coalesce a burst of changes into one snapshot re-read.
                while not queue.empty():
                    queue.get_nowait()
                signal = asyncio.create_task(queue.get())
            # else: timeout — fall through and re-verify the snapshot
    except WebSocketDisconnect:
        pass
    finally:
        agents.unsubscribe_presence(queue)
        for task in (receiver, signal):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                await task
