"""The byte pump between a browser's Guacamole tunnel and a guacd connection.

After guacd's handshake completes neither side interprets anything, so this is
almost pure forwarding. Three things stop it being *only* forwarding:

* **The tunnel UUID.** ``guacamole-common-js``'s ``WebSocketTunnel`` will not
  consider itself connected until the server sends an instruction on the
  reserved empty opcode carrying a tunnel id. It is the first frame, before any
  guacd output.
* **Tunnel-level instructions.** The client sends periodic ``ping`` keepalives on
  that same empty opcode. They are the tunnel's own bookkeeping; guacd answers an
  unknown opcode by closing the connection, so they are handled here and never
  forwarded.
* **Idle keepalive.** An unattended desktop produces no traffic, and a reverse
  proxy will reap a silent WebSocket (nginx's ``proxy_read_timeout`` defaults to
  60 seconds). A periodic ``nop`` — a no-op in the protocol, which the client
  discards — keeps the path warm so an idle session does not read as one that
  died on its own.

Three concurrent tasks rather than one loop with timeouts, because cancelling a
read mid-instruction to service a timer is how a relay loses framing. The first
task to finish ends the session; the rest are cancelled.
"""

import asyncio
import uuid

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app.core.console.guacd import (
    INTERNAL_OPCODE,
    GuacdConnection,
    GuacdError,
    InstructionReader,
    decode,
    encode,
)

#: A protocol no-op. The client parses and discards it; its only job is to put
#: bytes on an idle socket.
_NOP = encode(["nop"])


async def relay(
    websocket: WebSocket, conn: GuacdConnection, *, keepalive_s: int
) -> None:
    """Pump *websocket* ↔ *conn* until either side ends the session.

    Returns normally on an ordinary disconnect; that is the expected exit, not an
    error, so the caller closes the socket rather than reporting a failure.
    """
    await websocket.send_text(encode([INTERNAL_OPCODE, uuid.uuid4().hex]))

    last_sent = asyncio.get_running_loop().time()

    async def to_guacd() -> None:
        reader = InstructionReader()
        while True:
            chunk = await websocket.receive_text()
            for instruction in reader.feed(chunk):
                elements = decode(instruction)
                if elements and elements[0] == INTERNAL_OPCODE:
                    # Tunnel bookkeeping (``ping``). guacd would close the
                    # connection on an opcode it does not know.
                    continue
                await conn.send_raw(instruction)

    async def to_browser() -> None:
        nonlocal last_sent
        while True:
            instruction = await conn.read_raw()
            await websocket.send_text(instruction)
            last_sent = asyncio.get_running_loop().time()

    async def keepalive() -> None:
        nonlocal last_sent
        while True:
            await asyncio.sleep(keepalive_s)
            idle = asyncio.get_running_loop().time() - last_sent
            if idle >= keepalive_s:
                await websocket.send_text(_NOP)
                last_sent = asyncio.get_running_loop().time()

    tasks = [
        asyncio.create_task(to_guacd(), name="console-to-guacd"),
        asyncio.create_task(to_browser(), name="console-to-browser"),
        asyncio.create_task(keepalive(), name="console-keepalive"),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc is None or isinstance(exc, WebSocketDisconnect | GuacdError):
                # The three ordinary endings: the user closed the tab, the guest
                # dropped the RDP session, or guacd went away. None is a fault
                # worth propagating past the route.
                continue
            raise exc
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
