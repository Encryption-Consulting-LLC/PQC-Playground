"""The Guacamole wire protocol: codec, framing, and the guacd handshake.

This is the only genuinely novel code in the console path — everything else
follows a pattern already in the repo — and every one of its failure modes is
opaque from the outside. A codec that counts bytes instead of characters works
until someone pastes a non-ASCII string; a decoder that splits on commas works
until a password contains one; a handshake that sends ``connect`` values in a
fixed order works until guacd adds a parameter. All three surface as "the remote
desktop just shows a grey screen", so they are pinned here against a fake guacd
rather than discovered against a real one.
"""

import asyncio
import os

import pytest

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.console.guacd import (  # noqa: E402
    GuacdError,
    InstructionReader,
    connect,
    decode,
    encode,
    rdp_parameters,
)


# --- codec -------------------------------------------------------------------


def test_encode_matches_the_wire_format():
    assert encode(["select", "rdp"]) == "6.select,3.rdp;"


def test_encode_lengths_count_characters_not_bytes():
    """A UTF-8 byte count would desynchronize guacd's parser on the first
    accented character — a clipboard paste, or a hostname with a diacritic."""

    assert encode(["key", "café"]) == "3.key,4.café;"


def test_round_trip_survives_separator_characters_in_values():
    """Length-prefixed, not delimited: a generated password containing a comma,
    period or semicolon must not be re-split into extra elements."""

    values = ["connect", "a,b", "c.d", "e;f", ""]

    assert decode(encode(values)) == values


def test_decode_rejects_a_truncated_instruction():
    with pytest.raises(GuacdError):
        decode("9.short;")


def test_decode_rejects_a_missing_length_prefix():
    with pytest.raises(GuacdError):
        decode("nolength;")


def test_decode_requires_the_terminator():
    """A required terminator is what makes ``rstrip(';')`` unnecessary — and
    ``rstrip`` would eat a ``;`` belonging to the last element's value."""

    with pytest.raises(GuacdError):
        decode("3.nop")


# --- framing -----------------------------------------------------------------


def test_reader_splits_several_instructions_from_one_chunk():
    """TCP does not frame on ``;`` and neither does a WebSocket text frame, so
    one read can carry several instructions."""

    reader = InstructionReader()

    assert reader.feed("3.nop;4.sync,3.123;") == ["3.nop;", "4.sync,3.123;"]


def test_reader_holds_a_partial_instruction_until_it_completes():
    reader = InstructionReader()

    assert reader.feed("4.syn") == []
    assert reader.feed("c,3.123;") == ["4.sync,3.123;"]


def test_reader_forwards_instructions_verbatim():
    """The relay does not re-encode: an instruction that came in as bytes goes
    out as the same bytes, so a codec bug cannot corrupt pixel data."""

    reader = InstructionReader()
    raw = "3.img,1.1,4.blob;"

    assert reader.feed(raw) == [raw]


def test_reader_rejects_an_unbounded_instruction():
    """A peer that sends a length prefix and then stalls must not pin memory."""

    reader = InstructionReader()

    with pytest.raises(GuacdError):
        reader.feed("9999999999." + "x" * ((1 << 22) + 1))


def test_reader_does_not_split_on_a_semicolon_inside_a_value():
    """The framing bug this parser exists to avoid. ``;`` is legal inside a
    length-prefixed value — an operator-set domain admin password readily
    contains one — and splitting there corrupts every instruction after it
    instead of failing cleanly."""

    reader = InstructionReader()
    raw = encode(["connect", "pw;with;semicolons"])

    assert reader.feed(raw) == [raw]
    assert decode(raw) == ["connect", "pw;with;semicolons"]


def test_reader_holds_a_value_that_ends_exactly_at_the_buffer_boundary():
    """Without the separator in hand, a value ending at the boundary looks
    terminated when the next read may still extend it."""

    reader = InstructionReader()

    assert reader.feed("3.nop") == []
    assert reader.feed(";") == ["3.nop;"]


# --- handshake ---------------------------------------------------------------


class _FakeGuacd:
    """A guacd that answers the handshake and records what it was sent."""

    def __init__(self, arg_names, ready=True):
        self.arg_names = arg_names
        self.ready = ready
        self.received: list[str] = []
        self.host: str | None = None
        self.port: int | None = None

    async def open(self, host, port):
        self.host, self.port = host, port
        return _FakeReader(self), _FakeWriter(self)

    def reply(self):
        if len(self.received) == 1:  # answered `select`
            return encode(["args", "VERSION_1_5_0", *self.arg_names])
        if self.ready:
            return encode(["ready", "$abc123"])
        return encode(["error", "Authentication failure", "769"])


class _FakeReader:
    def __init__(self, guacd):
        self._guacd = guacd
        self._queue: list[bytes] = []

    def push(self, text):
        self._queue.append(text.encode("utf-8"))

    async def read(self, _n):
        while not self._queue:
            await asyncio.sleep(0)
        return self._queue.pop(0)


class _FakeWriter:
    def __init__(self, guacd):
        self._guacd = guacd
        self.reader: _FakeReader | None = None

    def write(self, data):
        self._guacd.received.append(data.decode("utf-8"))
        # guacd replies after `select` and after `connect`.
        text = self._guacd.received[-1]
        if text.startswith("6.select") or text.startswith("7.connect"):
            assert self.reader is not None
            self.reader.push(self._guacd.reply())

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


def _patched_open(guacd, monkeypatch):
    async def _open(host, port):
        reader, writer = await guacd.open(host, port)
        writer.reader = reader
        return reader, writer

    monkeypatch.setattr("app.core.console.guacd.asyncio.open_connection", _open)


def test_connect_sends_values_positionally_against_the_args_guacd_replied_with(
    monkeypatch,
):
    """The whole point of reading ``args`` instead of hardcoding an order: guacd
    versions differ in which parameters they accept and in what sequence."""

    guacd = _FakeGuacd(["hostname", "port", "username", "password"])
    _patched_open(guacd, monkeypatch)

    conn = asyncio.run(
        connect(
            host="127.0.0.1",
            port=4822,
            protocol="rdp",
            parameters={
                "hostname": "192.0.2.10",
                "port": "3389",
                "username": ".\\Administrator",
                "password": "pw,with.separators;",
            },
            width=1280,
            height=800,
        )
    )

    assert conn.connection_id == "$abc123"
    # The version token is answered by echoing it, not by omitting it — it holds
    # a positional slot like any other argument. Skipping it sends one value too
    # few, and guacd's punishment for that is uniquely misleading: it replies
    # `ready` and *then* drops the connection with "Client did not return the
    # expected number of arguments", so the handshake looks like it worked and
    # the session dies a moment later. Verified against guacd 1.5.0.
    assert decode(guacd.received[-1]) == [
        "connect",
        "VERSION_1_5_0",
        "192.0.2.10",
        "3389",
        ".\\Administrator",
        "pw,with.separators;",
    ]


def test_connect_sends_an_empty_value_for_an_arg_it_has_no_parameter_for(monkeypatch):
    """A guacd that asks for something unknown gets a blank, not a shifted list —
    a positional protocol punishes a skipped element by mis-assigning every
    later one, which is how a password ends up in the domain field."""

    guacd = _FakeGuacd(["hostname", "some-new-guacd-arg", "port"])
    _patched_open(guacd, monkeypatch)

    asyncio.run(
        connect(
            host="127.0.0.1",
            port=4822,
            protocol="rdp",
            parameters={"hostname": "192.0.2.10", "port": "3389"},
            width=1024,
            height=768,
        )
    )

    assert decode(guacd.received[-1]) == [
        "connect",
        "VERSION_1_5_0",
        "192.0.2.10",
        "",
        "3389",
    ]


def test_handshake_order_is_select_size_media_connect(monkeypatch):
    """guacd's handshake is order-dependent; it closes the connection on a
    surprise rather than explaining itself."""

    guacd = _FakeGuacd(["hostname"])
    _patched_open(guacd, monkeypatch)

    asyncio.run(
        connect(
            host="127.0.0.1",
            port=4822,
            protocol="rdp",
            parameters={"hostname": "192.0.2.10"},
            width=1024,
            height=768,
        )
    )

    opcodes = [decode(sent)[0] for sent in guacd.received]
    assert opcodes == ["select", "size", "audio", "video", "image", "connect"]


def test_an_error_in_place_of_ready_is_raised_not_ignored(monkeypatch):
    """guacd answers a handshake it cannot act on with ``error`` instead of
    ``ready``. Surfacing it is the difference between a stated reason and a blank
    overlay that never resolves.

    This is *not* the powered-off-guest case: guacd answers ``ready`` before it
    has reached the guest, so that one is caught by the browser's connect
    deadline instead."""

    guacd = _FakeGuacd(["hostname"], ready=False)
    _patched_open(guacd, monkeypatch)

    with pytest.raises(GuacdError, match="Authentication failure"):
        asyncio.run(
            connect(
                host="127.0.0.1",
                port=4822,
                protocol="rdp",
                parameters={"hostname": "192.0.2.10"},
                width=1024,
                height=768,
            )
        )


def test_an_unreachable_guacd_is_a_guacd_error(monkeypatch):
    async def _refuse(_host, _port):
        raise OSError("connection refused")

    monkeypatch.setattr("app.core.console.guacd.asyncio.open_connection", _refuse)

    with pytest.raises(GuacdError, match="Cannot reach guacd"):
        asyncio.run(
            connect(
                host="127.0.0.1",
                port=4822,
                protocol="rdp",
                parameters={},
                width=1024,
                height=768,
            )
        )


# --- parameters --------------------------------------------------------------


def test_rdp_parameters_carry_the_lab_defaults():
    params = rdp_parameters(
        hostname="192.0.2.10",
        username=".\\Administrator",
        password="secret",
        width=1280,
        height=800,
    )

    assert params["port"] == "3389"
    # Every freshly cloned guest presents a self-signed RDP certificate nothing
    # in this stack can sign; NLA (which firstboot leaves on) is the real gate.
    assert params["ignore-cert"] == "true"
    assert params["resize-method"] == "display-update"
    # Framebuffer traffic shares the API's event loop with every agent socket.
    assert params["enable-wallpaper"] == "false"


def test_connect_sends_exactly_one_value_per_arg_guacd_listed(monkeypatch):
    """The invariant behind both cases above, stated directly.

    guacd 1.5.0 lists 82 elements for RDP, so an off-by-one here is invisible by
    inspection — and it is the failure mode that produces a `ready` followed by a
    dead session rather than an honest error."""

    names = ["hostname", "port", *(f"arg{i}" for i in range(60))]
    guacd = _FakeGuacd(names)
    _patched_open(guacd, monkeypatch)

    asyncio.run(
        connect(
            host="127.0.0.1",
            port=4822,
            protocol="rdp",
            parameters={"hostname": "192.0.2.10", "port": "3389"},
            width=1024,
            height=768,
        )
    )

    sent = decode(guacd.received[-1])
    # opcode + version + every listed name.
    assert len(sent) - 1 == len(names) + 1
