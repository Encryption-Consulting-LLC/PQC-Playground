"""The live session map: the concurrency cap, and revocation on teardown.

The map is in-process for the same reason ``core.agents._connected`` is — these
are WebSocket objects held by whichever process accepted them, so nothing outside
that process can close one. Two behaviours are worth pinning.

The cap protects the API's event loop: framebuffer traffic shares it with every
guest agent socket (a single uvicorn worker, on purpose), so forgotten open
desktops could starve agent dispatch.

Revocation is the one that is silent when broken. A destroyed VM whose session
stays open leaves the overlay retrying against a machine that no longer exists,
and it has to close with a code the client can distinguish from a network blip —
the same ``4410`` the agent socket uses, because "this VM is going away" must not
mean two different things on two different socket kinds.
"""

import asyncio
import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.agents import TEARDOWN_CLOSE_CODE  # noqa: E402
from app.core.console import sessions  # noqa: E402
from app.core.settings import settings  # noqa: E402


class _FakeSocket:
    def __init__(self, fail=False):
        self.closed_with: int | None = None
        self._fail = fail

    async def close(self, code: int) -> None:
        if self._fail:
            raise RuntimeError("already gone")
        self.closed_with = code


def _reset():
    sessions._live.clear()


def test_registering_and_unregistering_tracks_the_count():
    _reset()
    a, b = _FakeSocket(), _FakeSocket()

    sessions.register("ca01", a)
    sessions.register("ca01", b)  # two tabs on one VM is legitimate
    assert sessions.active_count() == 2

    sessions.unregister("ca01", a)
    sessions.unregister("ca01", b)
    assert sessions.active_count() == 0
    # The empty set is dropped, so the map does not grow forever.
    assert "ca01" not in sessions._live


def test_unregister_is_idempotent():
    """The relay's ``finally`` runs whether or not the socket was registered, and
    a revoked socket has already been dropped from the map."""

    _reset()
    socket = _FakeSocket()

    sessions.unregister("ca01", socket)  # never registered
    sessions.register("ca01", socket)
    sessions.unregister("ca01", socket)
    sessions.unregister("ca01", socket)  # again

    assert sessions.active_count() == 0


def test_at_capacity_tracks_the_configured_cap(monkeypatch):
    _reset()
    monkeypatch.setattr(settings, "console_max_sessions", 2)

    sessions.register("ca01", _FakeSocket())
    assert not sessions.at_capacity()

    sessions.register("dc01", _FakeSocket())
    assert sessions.at_capacity()


def test_revocation_closes_with_the_teardown_code():
    _reset()
    doomed, spared = _FakeSocket(), _FakeSocket()
    sessions.register("ca01", doomed)
    sessions.register("dc01", spared)

    revoked = asyncio.run(sessions.revoke_live_console_sockets(["ca01"]))

    assert revoked == ["ca01"]
    assert doomed.closed_with == TEARDOWN_CLOSE_CODE
    # Only the named VM: teardown of one lab must not drop another's session.
    assert spared.closed_with is None
    assert sessions.active_count() == 1


def test_revocation_reports_only_vms_that_had_a_session():
    """Best effort, like the agent equivalent: a VM with nothing open contributes
    nothing rather than being reported as revoked."""

    _reset()
    sessions.register("ca01", _FakeSocket())

    assert asyncio.run(
        sessions.revoke_live_console_sockets(["ca01", "dc01", "web01"])
    ) == ["ca01"]


def test_a_socket_that_is_already_gone_does_not_abort_the_sweep():
    """Teardown must not be blocked by a session that died a moment earlier."""

    _reset()
    broken, healthy = _FakeSocket(fail=True), _FakeSocket()
    sessions.register("ca01", broken)
    sessions.register("dc01", healthy)

    asyncio.run(sessions.revoke_live_console_sockets(["ca01", "dc01"]))

    assert healthy.closed_with == TEARDOWN_CLOSE_CODE
    assert sessions.active_count() == 0
