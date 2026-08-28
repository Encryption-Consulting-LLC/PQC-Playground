"""WebSocket routes: background-job progress, and remote-desktop sessions.

Job progress is generic — any job published via ``app.core.jobs.transport`` can be
watched here, not just clones. Mounted under ``/api`` → ``ws /api/ws/jobs/{job_id}``.

The console socket (``ws /api/ws/console/{ticket_id}``) shares this module for its
conventions rather than its mechanics: same ``?token=`` auth, same
``accept()``-before-``close()`` rejection discipline, same 4401/4403/4404 close
codes. What it carries is not job messages but the Guacamole protocol, relayed
byte-for-byte to guacd (see ``core.console``).

Auth: browsers can't set custom headers on the WS upgrade, so the session token
(a backend-minted JWT) comes as a ``?token=`` query param and is resolved by the
same ``resolve_user_token`` the HTTP dependency uses. Close codes: 4401
(bad/absent token), 4403 (someone else's deployment), 4404 (unknown job — no
snapshot in Valkey *and* nothing persisted to replay).

Every rejection ``accept()``s the upgrade *before* closing. Closing a WebSocket
that was never accepted makes the ASGI server reject the handshake with a plain
HTTP 403 and drop the close code entirely, so the client sees an opaque 1006 and
cannot tell "this job is gone" (don't retry) from "the connection blipped"
(do retry) — which is exactly how a finished-but-evicted deploy came back as a
silent transport failure instead of its real outcome.

Job state and live messages live in Valkey (see ``transport``), not in this
process, so this works the same whether the job is being run by a local worker or
one on another host, and the API can be scaled to multiple replicas without sticky
routing. Valkey snapshots expire, though, so a deploy plan whose snapshot is gone
falls back to its persisted ``plan_runs`` document (see ``jobs.replay``) — that's
what lets a tab reopened hours later still learn how the deploy ended.
"""

import json

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.core.authz import Role, resolve_user_token
from app.core.console import sessions, tickets
from app.core.console.guacd import GuacdError, connect, rdp_parameters
from app.core.console.relay import relay
from app.core.db import now_ms, plan_runs_col
from app.core.jobs import transport
from app.core.jobs.models import TERMINAL_TYPES
from app.core.jobs.replay import replay_plan_run
from app.core.labs import enforce_own_or_joined_vm
from app.core.settings import settings

router = APIRouter(prefix="/ws", tags=["ws"])


async def reject(websocket: WebSocket, code: int) -> None:
    """Close with *code* after accepting, so the client actually receives *code*.

    A bare pre-accept ``close()`` is answered by the server with HTTP 403 and the
    application close code is lost.
    """
    await websocket.accept()
    await websocket.close(code=code)


async def send_json_or_disconnect(websocket: WebSocket, payload: dict) -> None:
    """``send_json`` that treats a send racing the client's close as a disconnect.

    When the client closes, the close handshake completes before the app's
    pending send; uvicorn then raises a bare ``RuntimeError`` (not
    ``WebSocketDisconnect``) — normalize it so handlers' existing
    ``except WebSocketDisconnect`` cleanup applies.
    """
    try:
        await websocket.send_json(payload)
    except RuntimeError as exc:
        raise WebSocketDisconnect(code=1006) from exc


@router.websocket("/jobs/{job_id}")
async def job_progress(
    websocket: WebSocket, job_id: str, token: str | None = None
) -> None:
    user = await resolve_user_token(token)
    if user is None:
        await reject(websocket, 4401)
        return

    redis_client = transport.make_async_client()
    pubsub = redis_client.pubsub()
    try:
        # Subscribe before reading the snapshot so nothing published in between is
        # missed. The snapshot gives an instantly-current bar; if it's already
        # terminal the job finished before we connected and there's nothing to stream.
        await pubsub.subscribe(transport.channel(job_id))

        snapshot = await transport.read_snapshot(redis_client, job_id)
        replayed: list[dict] = []
        if snapshot is None:
            # No live snapshot: the job may still be a deploy plan whose state
            # outlives Valkey. Unlike the snapshot (which only the job's own id
            # can address), this reads a user-attributed document, so it carries
            # the same ownership rule as the HTTP deploy routes.
            run = await plan_runs_col().find_one({"jobId": job_id})
            if run is None:
                await reject(websocket, 4404)
                return
            if user.role is not Role.OPERATOR and run.get("owner") != user.username:
                await reject(websocket, 4403)
                return
            outcome = replay_plan_run(run, now_ms=now_ms())
            if outcome is None:
                await reject(websocket, 4404)
                return
            _, messages = outcome
            replayed = [msg.model_dump() for msg in messages]

        await websocket.accept()

        for msg in replayed:
            await send_json_or_disconnect(websocket, msg)
            if msg["type"] in TERMINAL_TYPES:
                return

        last = snapshot["last"] if snapshot is not None else None
        if last is not None:
            await send_json_or_disconnect(websocket, last)
            if last["type"] in TERMINAL_TYPES:
                return

        async for raw in pubsub.listen():
            if raw["type"] != "message":
                continue
            msg = json.loads(raw["data"])
            await send_json_or_disconnect(websocket, msg)
            if msg["type"] in TERMINAL_TYPES:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(transport.channel(job_id))
        await pubsub.aclose()
        await redis_client.aclose()


#: guacd is up but this VM will not take a session. Distinct from 4404 (the
#: ticket is gone — do not retry) because a retry here can legitimately succeed.
WS_CLOSE_CONSOLE_UNAVAILABLE = 4503

#: The first size handed to guacd. The client resizes to its real viewport
#: immediately (``resize-method=display-update``), so this is a first frame, not
#: the user's window.
_INITIAL_WIDTH = 1280
_INITIAL_HEIGHT = 800


@router.websocket("/console/{ticket_id}")
async def console_session(
    websocket: WebSocket, ticket_id: str, token: str | None = None
) -> None:
    """Relay a remote-desktop session for the VM a ticket was minted for.

    The ticket carries the guest's address and the RDP credentials, which is why
    it is redeemed here and never handed to the browser. Redemption is
    single-use, so the checks below run against a decision made seconds ago — and
    are then made again, because a ticket id is a bearer token and holding one
    must not be sufficient:

    * the caller must be a valid, still-enabled user (``resolve_user_token``
      re-reads the user document, so a disable or role change lands immediately);
    * the caller must be the user the ticket was minted for;
    * and a guest must still reach the VM — its own, or one in a lab it has
      joined (``enforce_own_or_joined_vm``, the same rule the minting route
      applied). A role demotion, or a join code revoked in between, has to take
      the session with it.

    Rejections go through ``reject`` for the reason documented at the top of this
    module: a pre-accept close is answered with a plain HTTP 403 and the
    application close code is discarded, leaving the client unable to tell "this
    ticket is spent" from "the connection blipped".
    """
    user = await resolve_user_token(token)
    if user is None:
        await reject(websocket, 4401)
        return

    ticket = await tickets.redeem(ticket_id)
    if ticket is None:
        # Unknown, expired, or already redeemed — indistinguishable on purpose.
        await reject(websocket, 4404)
        return
    if ticket.owner != user.username:
        await reject(websocket, 4403)
        return
    try:
        await enforce_own_or_joined_vm(ticket.vm_name, user)
    except HTTPException:
        await reject(websocket, 4403)
        return

    if sessions.at_capacity():
        await reject(websocket, WS_CLOSE_CONSOLE_UNAVAILABLE)
        return

    try:
        conn = await connect(
            host=settings.guacd_host,
            port=settings.guacd_port,
            protocol="rdp",
            parameters=rdp_parameters(
                hostname=ticket.host,
                username=ticket.credentials.username,
                domain=ticket.credentials.domain,
                password=ticket.credentials.password,
                width=_INITIAL_WIDTH,
                height=_INITIAL_HEIGHT,
            ),
            width=_INITIAL_WIDTH,
            height=_INITIAL_HEIGHT,
        )
    except GuacdError:
        await reject(websocket, WS_CLOSE_CONSOLE_UNAVAILABLE)
        return

    # ``guacamole-common-js`` opens its tunnel with the "guacamole" subprotocol and
    # will not talk to a server that does not select it.
    await websocket.accept(subprotocol="guacamole")
    sessions.register(ticket.vm_name, websocket)
    try:
        await relay(websocket, conn, keepalive_s=settings.console_keepalive_s)
    except WebSocketDisconnect:
        pass
    finally:
        sessions.unregister(ticket.vm_name, websocket)
        await conn.close()
