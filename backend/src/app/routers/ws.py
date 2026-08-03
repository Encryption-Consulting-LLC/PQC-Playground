"""WebSocket routes for streaming background-job progress.

Generic: any job published via ``app.core.jobs.transport`` can be watched here, not
just clones. Mounted under ``/api`` → ``ws /api/ws/jobs/{job_id}``.

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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.authz import Role, resolve_user_token
from app.core.db import now_ms, plan_runs_col
from app.core.jobs import transport
from app.core.jobs.models import TERMINAL_TYPES
from app.core.jobs.replay import replay_plan_run

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
