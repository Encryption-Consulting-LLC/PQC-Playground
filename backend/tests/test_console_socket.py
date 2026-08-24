"""What a client can observe when the console socket turns it away, and who it
lets through.

Same discipline as ``test_job_socket_rejection``: a pre-accept ``close()`` is
answered with a plain HTTP 403 and the application close code is discarded, so
every rejection here has to arrive as a real close frame. It matters more for
this socket than for the job socket, because the codes mean different things to
the overlay — 4404 (the ticket is spent, mint a new one) must not be
indistinguishable from 4503 (guacd is down, retrying is pointless right now) or
from a blip.

The authorization is checked twice on purpose. The HTTP route already decided
this user may see this VM, but a ticket id is a bearer token, so holding one is
deliberately not sufficient: the socket re-resolves the user (which re-reads the
user document, so a disable or role change lands immediately) and re-runs the
guest ownership check, because a demotion between minting and connecting has to
take the session with it.
"""

import os

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.authz import AuthedUser, Role  # noqa: E402
from app.core.console.credentials import ConsoleCredentials  # noqa: E402
from app.core.console.guacd import GuacdError  # noqa: E402
from app.core.console.tickets import ConsoleTicket  # noqa: E402
from app.core import labs  # noqa: E402
from app.routers import ws  # noqa: E402

CREDS = ConsoleCredentials(
    username=".\\Administrator", password="secret", label="Administrator (local)"
)


class _NoInvites:
    """A ``lab_invites`` collection with nothing in it.

    The remote-desktop check falls through to lab membership when the caller's
    own namespace doesn't cover the VM (``core/labs.enforce_own_or_joined_vm``),
    so these tests have to answer that question — "no, nobody joined anything" —
    rather than leave it reaching for a Mongo that unit tests never start.
    """

    def find(self, _query):
        return self

    async def to_list(self, length=None):
        return []


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(labs, "lab_invites_col", lambda: _NoInvites())
    app = FastAPI()
    app.include_router(ws.router)
    return TestClient(app)


def _as(username: str, role: Role):
    async def _resolve(token: str | None):
        return AuthedUser(username=username, role=role, auth="local") if token else None

    return _resolve


def _ticket(owner="alice", vm_name="guest-alice-9e4edb-ca01"):
    async def _redeem(_ticket_id: str):
        return ConsoleTicket(
            vm_name=vm_name, owner=owner, host="192.0.2.10", credentials=CREDS
        )

    return _redeem


def _no_ticket():
    async def _redeem(_ticket_id: str):
        return None

    return _redeem


def close_code(client: TestClient, url: str) -> int:
    """The code the client receives — fails loudly if the handshake was denied.

    The rejection only becomes observable on a read: a denied *handshake* raises
    ``WebSocketDenialResponse`` instead, which is the bug this whole file exists
    to catch.
    """
    try:
        with client.websocket_connect(url) as socket:
            with pytest.raises(WebSocketDisconnect) as excinfo:
                socket.receive_text()
    except WebSocketDenialResponse as denial:  # pragma: no cover — the bug
        pytest.fail(
            f"upgrade denied with HTTP {denial.status_code}; the close code is lost"
        )
    return excinfo.value.code


def _closes_with(client, code: int, token: str = "t") -> None:
    assert close_code(client, f"/ws/console/tkt?token={token}") == code


def test_a_missing_token_closes_4401(client, monkeypatch):
    monkeypatch.setattr(ws, "resolve_user_token", _as("alice", Role.OPERATOR))

    assert close_code(client, "/ws/console/tkt") == 4401


def test_an_unknown_or_spent_ticket_closes_4404(client, monkeypatch):
    """Unknown, expired and already-redeemed are one answer on purpose — a replay
    and an expiry should look the same from outside."""

    monkeypatch.setattr(ws, "resolve_user_token", _as("alice", Role.OPERATOR))
    monkeypatch.setattr(ws.tickets, "redeem", _no_ticket())

    _closes_with(client, 4404)


def test_someone_elses_ticket_closes_4403(client, monkeypatch):
    """The id is unguessable, but it is still a bearer token: you must also be
    the person it was minted for."""

    monkeypatch.setattr(ws, "resolve_user_token", _as("bob", Role.OPERATOR))
    monkeypatch.setattr(ws.tickets, "redeem", _ticket(owner="alice"))

    _closes_with(client, 4403)


def test_a_guest_demoted_out_of_the_namespace_closes_4403(client, monkeypatch):
    """A ticket minted for an operator, redeemed after that account became a
    guest, must not still open a VM outside the guest namespace."""

    monkeypatch.setattr(ws, "resolve_user_token", _as("alice", Role.GUEST))
    monkeypatch.setattr(
        ws.tickets, "redeem", _ticket(owner="alice", vm_name="operator-owned-ca01")
    )

    _closes_with(client, 4403)


def test_a_guest_reaching_its_own_vm_is_not_refused_by_ownership(client, monkeypatch):
    """The negative case's control: the guest namespace check must not reject the
    caller's own VM. guacd is stubbed unreachable, so the session ends at 4503 —
    which is the proof ownership was passed."""

    monkeypatch.setattr(ws, "resolve_user_token", _as("alice", Role.GUEST))
    monkeypatch.setattr(ws.tickets, "redeem", _ticket(owner="alice"))

    async def _refuse(**_kwargs):
        raise GuacdError("down")

    monkeypatch.setattr(ws, "connect", _refuse)

    _closes_with(client, ws.WS_CLOSE_CONSOLE_UNAVAILABLE)


def test_an_unreachable_guacd_closes_4503_not_4404(client, monkeypatch):
    """A retryable service outage must not read as a spent ticket, or the overlay
    tells the user to reconnect forever against a daemon that is not running."""

    monkeypatch.setattr(ws, "resolve_user_token", _as("alice", Role.OPERATOR))
    monkeypatch.setattr(ws.tickets, "redeem", _ticket())

    async def _refuse(**_kwargs):
        raise GuacdError("cannot reach guacd")

    monkeypatch.setattr(ws, "connect", _refuse)

    _closes_with(client, ws.WS_CLOSE_CONSOLE_UNAVAILABLE)
    assert ws.WS_CLOSE_CONSOLE_UNAVAILABLE != 4404


def test_capacity_refuses_before_dialling_guacd(client, monkeypatch):
    """The cap exists to protect the API's event loop, so it has to be checked
    before a new guacd connection is opened, not after."""

    monkeypatch.setattr(ws, "resolve_user_token", _as("alice", Role.OPERATOR))
    monkeypatch.setattr(ws.tickets, "redeem", _ticket())
    monkeypatch.setattr(ws.sessions, "at_capacity", lambda: True)

    async def _unexpected(**_kwargs):
        raise AssertionError("guacd was dialled while at capacity")

    monkeypatch.setattr(ws, "connect", _unexpected)

    _closes_with(client, ws.WS_CLOSE_CONSOLE_UNAVAILABLE)
