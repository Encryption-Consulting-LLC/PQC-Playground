"""The Guacamole protocol: instruction codec, guacd handshake, and the relay.

This module is the reason the stack needs no ``guacamole-client`` webapp. guacd
speaks a small text protocol over TCP 4822 and the browser's
``guacamole-common-js`` speaks the *same* protocol over a WebSocket, so an
authenticated relay in the middle is all that stands between them. Everything
else Guacamole ships (a servlet container, a user database, a second session
model) exists to answer "who is this and what may they connect to" — questions
this app already answers.

Wire format
-----------
An instruction is comma-separated length-prefixed elements ending in ``;``::

    5.mouse,3.512,3.384,1.1;

The length is a count of *characters*, not bytes — ``len()`` on a ``str``, after
decoding UTF-8. Getting that wrong only shows up on non-ASCII input (a clipboard
paste, a hostname with an accent), which is why the codec is tested directly.

Handshake
---------
guacd's is fixed and order-dependent::

    →  select,rdp                 which protocol
    ←  args,VERSION_1_5_0,hostname,port,...
    →  size,<w>,<h>,<dpi>
    →  audio / video / image      supported mimetypes (empty = none)
    →  connect,<v1>,<v2>,...      positional, aligned to `args`
    ←  ready,$<connection-id>

The ``connect`` values are positional: element *i* answers the parameter named at
``args[i]``, with ``args[0]`` skipped because it is guacd's version token, not a
parameter. So the parameter map is resolved *against the arg list guacd sent*
rather than a list hardcoded here — that is what makes this work across guacd
versions that added parameters.

After ``ready`` neither side interprets anything: instructions are forwarded
verbatim in both directions.

Tunnel framing
--------------
``guacamole-common-js``'s ``WebSocketTunnel`` reserves the *empty* opcode for
tunnel-level messages, and expects the server's first frame to be one carrying
the tunnel UUID::

    0.,36.<uuid>;

It then sends periodic ``0.,4.ping,<timestamp>;`` keepalives. Those are the
tunnel's own bookkeeping — guacd has never heard of them and answers an unknown
opcode by closing the connection, so inbound internal instructions are handled
here and never forwarded.
"""

import asyncio
from collections.abc import Iterable, Sequence

#: Opcode reserved by guacamole-common-js for tunnel-level messages.
INTERNAL_OPCODE = ""

#: guacd's reply to ``select`` starts with its version, not a parameter name.
_ARGS_VERSION_ELEMENTS = 1

#: Read cap for a single instruction. Generous — a clipboard blob or a large
#: image update is legitimately big — but not unbounded: without a ceiling a peer
#: that sends a length prefix and then stalls pins memory until the socket dies.
MAX_INSTRUCTION_CHARS = 1 << 22


class GuacdError(Exception):
    """The handshake failed, or guacd closed mid-conversation."""


def encode(elements: Iterable[str]) -> str:
    """Encode one instruction. Lengths are character counts, not byte counts."""
    return ",".join(f"{len(str(e))}.{e}" for e in elements) + ";"


def _parse(buffer: str) -> tuple[list[str], int] | None:
    """Parse the instruction at the front of *buffer*.

    Returns its elements and the index just past its terminating ``;``, or
    ``None`` if *buffer* does not hold a complete instruction yet.

    Length-driven, and that is the whole subtlety: ``;`` and ``,`` are legal
    *inside* an element value, so the terminator can only be recognised at an
    element boundary. Scanning for the first ``;`` instead splits an instruction
    in half the moment a value contains one — which an operator-set
    ``domainAdminPassword`` or a clipboard paste readily does, and which then
    corrupts every following instruction on the stream rather than failing
    cleanly.
    """
    cursor = 0
    elements: list[str] = []
    while True:
        dot = buffer.find(".", cursor)
        if dot < 0:
            return None
        try:
            length = int(buffer[cursor:dot])
        except ValueError as exc:
            raise GuacdError(
                f"Malformed instruction (bad length prefix): {buffer[cursor:dot]!r}"
            ) from exc
        if length < 0:
            raise GuacdError("Malformed instruction (negative length).")
        end = dot + 1 + length
        # The separator itself has to be present before the element can be
        # trusted as complete, or a value ending at the buffer boundary looks
        # terminated when the next read may extend it.
        if end >= len(buffer):
            return None
        elements.append(buffer[dot + 1 : end])
        separator = buffer[end]
        if separator == ";":
            return elements, end + 1
        if separator != ",":
            raise GuacdError(
                f"Malformed instruction (expected ',' or ';', got {separator!r})."
            )
        cursor = end + 1


def decode(instruction: str) -> list[str]:
    """Decode one complete instruction — terminating ``;`` included — into its
    elements.

    The terminator is required rather than optional. Callers used to strip it
    themselves, and ``rstrip(";")`` silently eats a trailing ``;`` that belongs to
    the last element's *value*, so the contract is "hand me exactly what came off
    the wire".
    """
    parsed = _parse(instruction)
    if parsed is None:
        raise GuacdError(f"Truncated instruction: {instruction!r}")
    elements, end = parsed
    if end != len(instruction):
        raise GuacdError(f"Trailing bytes after instruction: {instruction!r}")
    return elements


class InstructionReader:
    """Splits a byte stream into whole instructions.

    guacd frames on ``;``, TCP does not, and a WebSocket text frame is not framed
    on instruction boundaries either — one frame can carry several instructions
    or half of one. Both directions of the relay therefore need this buffering,
    and it is a class rather than a generator so the leftover partial survives
    between reads.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        """Add *chunk*; return every complete instruction it completed, each
        still carrying its trailing ``;`` so it can be forwarded unchanged."""
        self._buffer += chunk
        out: list[str] = []
        while True:
            parsed = _parse(self._buffer)
            if parsed is None:
                # Incomplete. Bound the wait: without a ceiling, a peer that
                # sends a length prefix and then stalls pins memory until the
                # socket dies.
                if len(self._buffer) > MAX_INSTRUCTION_CHARS:
                    raise GuacdError("Instruction exceeded the maximum length.")
                return out
            _, end = parsed
            out.append(self._buffer[:end])
            self._buffer = self._buffer[end:]


class GuacdConnection:
    """An open, handshaken guacd connection.

    ``asyncio`` streams throughout: this shares an event loop with every guest
    agent socket (the API runs a single uvicorn worker on purpose, because the
    agent connection map is in-process), so one blocking read here stalls agent
    dispatch for every lab on the box.
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._instructions = InstructionReader()
        self._pending: list[str] = []
        self.connection_id: str | None = None

    async def send_raw(self, instruction: str) -> None:
        """Forward an already-encoded instruction (the browser → guacd path)."""
        self._writer.write(instruction.encode("utf-8"))
        await self._writer.drain()

    async def send(self, elements: Sequence[str]) -> None:
        await self.send_raw(encode(elements))

    async def read_instruction(self) -> list[str]:
        """The next instruction's elements. Raises ``GuacdError`` on EOF."""
        while not self._pending:
            chunk = await self._reader.read(8192)
            if not chunk:
                raise GuacdError("guacd closed the connection.")
            self._pending.extend(
                self._instructions.feed(chunk.decode("utf-8", errors="replace"))
            )
        return decode(self._pending.pop(0))

    async def read_raw(self) -> str:
        """The next instruction verbatim, for the guacd → browser path."""
        while not self._pending:
            chunk = await self._reader.read(8192)
            if not chunk:
                raise GuacdError("guacd closed the connection.")
            self._pending.extend(
                self._instructions.feed(chunk.decode("utf-8", errors="replace"))
            )
        return self._pending.pop(0)

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except OSError, asyncio.CancelledError:
            # Already gone, or we are being torn down. Either way the socket is
            # closed, which is all this method promises.
            pass


async def connect(
    *,
    host: str,
    port: int,
    protocol: str,
    parameters: dict[str, str],
    width: int,
    height: int,
    dpi: int = 96,
    timeout: float = 10.0,
) -> GuacdConnection:
    """Open a guacd connection and complete the handshake.

    *parameters* is keyed by Guacamole parameter name. Only the ones guacd
    actually asked for are sent, and any it asked for that is absent is sent
    empty — which is how a parameter set stays valid across guacd versions
    instead of breaking the first time one adds an argument.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, TimeoutError) as exc:
        raise GuacdError(f"Cannot reach guacd at {host}:{port}.") from exc

    conn = GuacdConnection(reader, writer)
    try:
        await conn.send(["select", protocol])

        reply = await asyncio.wait_for(conn.read_instruction(), timeout=timeout)
        if not reply or reply[0] != "args":
            raise GuacdError(f"Expected 'args' from guacd, got {reply[:1]}.")
        arg_names = reply[1 + _ARGS_VERSION_ELEMENTS :]

        await conn.send(["size", str(width), str(height), str(dpi)])
        # Empty mimetype lists: audio, video and printing are out of scope, and
        # claiming support we do not relay would make guacd open streams the
        # browser then ignores.
        await conn.send(["audio"])
        await conn.send(["video"])
        await conn.send(["image"])

        await conn.send(["connect", *(parameters.get(name, "") for name in arg_names)])

        ready = await asyncio.wait_for(conn.read_instruction(), timeout=timeout)
        if not ready or ready[0] != "ready":
            # guacd reports a refused RDP login as an `error` instruction here,
            # so this is the branch a wrong password lands in.
            detail = ",".join(ready[1:]) if len(ready) > 1 else "no detail"
            raise GuacdError(f"guacd did not become ready ({detail}).")
        conn.connection_id = ready[1] if len(ready) > 1 else None
        return conn
    except GuacdError, TimeoutError:
        await conn.close()
        raise


async def probe(*, host: str, port: int, timeout: float = 3.0) -> None:
    """Reachability preflight: open a connection to guacd and drop it.

    Raises ``GuacdError`` if guacd is not there. Deliberately does not handshake
    — this answers "is the service up", not "will this VM accept a login", and a
    ``select``/``connect`` round trip would make an admin's Connect button wait on
    an RDP timeout to find out that guacd itself is fine.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, TimeoutError) as exc:
        raise GuacdError(f"Cannot reach guacd at {host}:{port}.") from exc
    del reader
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass


def rdp_parameters(
    *, hostname: str, username: str, password: str, width: int, height: int
) -> dict[str, str]:
    """Connection parameters for a lab Windows guest.

    ``ignore-cert`` is on because every freshly cloned Windows guest presents a
    self-signed RDP certificate that nothing in this stack has any way to sign;
    the connection to guacd is loopback and the session's own authentication is
    NLA, which firstboot leaves enabled. ``security=any`` lets guacd negotiate
    down for an image that has not finished configuring TLS yet rather than
    failing the connection outright.
    """
    return {
        "hostname": hostname,
        "port": "3389",
        "username": username,
        "password": password,
        "security": "any",
        "ignore-cert": "true",
        "resize-method": "display-update",
        # A lab desktop is looked at, not admired: skipping the wallpaper, themes
        # and font smoothing cuts the framebuffer traffic this relay carries
        # through the API's shared event loop.
        "enable-wallpaper": "false",
        "enable-theming": "false",
        "enable-font-smoothing": "false",
        "width": str(width),
        "height": str(height),
        "dpi": "96",
    }
