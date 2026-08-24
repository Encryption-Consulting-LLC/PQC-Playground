"""The live remote-desktop session map: concurrency cap and teardown revocation.

In-process, and deliberately so — the same reason ``core.agents._connected`` is.
These are WebSocket objects held by whichever process accepted them, so nothing
outside that process can close one. The API runs a single uvicorn worker (see
``deploy/prod-deploy.sh``), which is what makes an in-process map sufficient.

Two jobs:

* **A cap.** Framebuffer traffic shares the API's event loop with every guest
  agent socket, so a lab's worth of forgotten open desktops could starve agent
  dispatch. ``CONSOLE_MAX_SESSIONS`` is the backstop.
* **Revocation.** When a VM is destroyed its session has to die with it, and with
  a code the client can tell apart from a network blip — otherwise the overlay
  sits there retrying against a VM that no longer exists. Reuses the agent
  socket's ``4410``: "this VM is going away" should not mean two different things
  on two different socket kinds.
"""

from collections import defaultdict

from fastapi import WebSocket

from app.core.agents import TEARDOWN_CLOSE_CODE
from app.core.settings import settings

#: vmName → the sockets currently relaying a session to it. A set because an
#: operator with two tabs open on the same VM is legitimate.
_live: dict[str, set[WebSocket]] = defaultdict(set)


def active_count() -> int:
    return sum(len(sockets) for sockets in _live.values())


def at_capacity() -> bool:
    return active_count() >= settings.console_max_sessions


def register(vm_name: str, websocket: WebSocket) -> None:
    _live[vm_name].add(websocket)


def unregister(vm_name: str, websocket: WebSocket) -> None:
    """Idempotent — the relay's ``finally`` runs whether or not the socket was
    ever registered, and a revoked socket has already been dropped from the map."""
    sockets = _live.get(vm_name)
    if sockets is None:
        return
    sockets.discard(websocket)
    if not sockets:
        _live.pop(vm_name, None)


async def revoke_live_console_sockets(vm_names: list[str]) -> list[str]:
    """Close every live session to the named VMs; returns the names that had one.

    Called before a teardown is enqueued, alongside
    ``agents.revoke_live_agent_sockets``. Best effort: a VM with no open session
    contributes nothing, and a socket that has already gone away is not an error.
    """
    revoked: list[str] = []
    for vm_name in vm_names:
        sockets = _live.pop(vm_name, set())
        if not sockets:
            continue
        revoked.append(vm_name)
        for websocket in sockets:
            try:
                await websocket.close(code=TEARDOWN_CLOSE_CODE)
            except Exception:  # noqa: BLE001 — the socket may already be gone
                pass
    return revoked
