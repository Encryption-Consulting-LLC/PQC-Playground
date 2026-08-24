"""The ticket route's refusals, and what it does and does not hand the browser.

Two things are pinned here. First, that the response carries no credential: the
whole design rests on the password reaching guacd without passing through the
client, so a field added carelessly to the response model would quietly undo it.

Second, that every refusal is specific. "Connect did nothing" is the least useful
thing this feature could say, and the four reasons a session is impossible are
genuinely different actions for the user: wait (still cloning), nothing (no
address yet), redeploy (no stored credential — nothing can set a password inside
an already-running guest), or tell an admin (guacd is down). They are also easy
to collapse into one generic 409 by accident.
"""

import os

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.authz import AuthedUser, Role, get_current_user  # noqa: E402
from app.core.console.guacd import GuacdError  # noqa: E402
from app.core.secrets import encrypt_secret  # noqa: E402
from app.routers import console  # noqa: E402

PASSWORD = "gen-Erated-24charPass1"
GUEST_VM = "guest-alice-9e4edb-ca01"


def _row(**overrides):
    row = {
        "vmName": GUEST_VM,
        "status": "ready",
        "ip": "192.0.2.10",
        "agent": {"templateId": "certificateAuthority", "templateConfig": {}},
        "localAdminPasswordEnc": encrypt_secret(PASSWORD),
    }
    row.update(overrides)
    return row


@pytest.fixture
def app(monkeypatch):
    """The route with its three outbound calls stubbed: the registry read, the
    guacd probe, and the ticket write.

    The registry row is held in a mutable box so ``_client`` can set it per test
    without re-patching — patching the module global directly would leak the stub
    into every other test module in the session.
    """
    application = FastAPI()
    application.include_router(console.router)
    application.include_router(console.admin_router)
    box: dict = {"row": None}

    class _Col:
        async def find_one(self, _query):
            return box["row"]

    async def _probe(**_kwargs):
        return None

    async def _mint(**_kwargs):
        return "tkt-123"

    monkeypatch.setattr(console, "vm_registry_col", lambda: _Col())
    monkeypatch.setattr(console, "probe", _probe)
    monkeypatch.setattr(console.tickets, "mint", _mint)
    monkeypatch.setattr(console, "at_capacity", lambda: False)
    application.state.row_box = box
    return application


def _client(app, row, username="alice", role=Role.GUEST) -> TestClient:
    app.state.row_box["row"] = row
    # Capability gating resolves the user through the same dependency, so one
    # override covers both it and the route's own ``user`` parameter.
    app.dependency_overrides[get_current_user] = lambda: AuthedUser(
        username=username, role=role, auth="local"
    )
    return TestClient(app)


def test_a_ready_vm_mints_a_ticket_without_leaking_the_credential(app):
    client = _client(app, _row())

    response = client.post(f"/console/{GUEST_VM}")

    assert response.status_code == 200
    body = response.json()
    assert body["ticketId"] == "tkt-123"
    assert body["credentialLabel"] == "Administrator (local)"
    # The point of the ticket indirection: no password, no username, no address.
    assert PASSWORD not in response.text
    assert "192.0.2.10" not in response.text


def test_a_guest_cannot_mint_for_another_namespace(app):
    client = _client(app, _row(vmName="guest-bob-9e4edb-ca01"))

    response = client.post("/console/guest-bob-9e4edb-ca01")

    assert response.status_code == 403


def test_an_operator_may_mint_for_any_name(app):
    client = _client(app, _row(), username="opal", role=Role.OPERATOR)

    assert client.post("/console/operator-owned-ca01").status_code == 200


def test_a_missing_row_is_404(app):
    client = _client(app, None)

    assert client.post(f"/console/{GUEST_VM}").status_code == 404


def test_a_deleted_row_is_404(app):
    client = _client(app, _row(status="deleted"))

    assert client.post(f"/console/{GUEST_VM}").status_code == 404


def test_a_still_cloning_vm_says_to_wait(app):
    client = _client(app, _row(status="cloning"))
    response = client.post(f"/console/{GUEST_VM}")

    assert response.status_code == 409
    assert "still being created" in response.json()["detail"]


def test_a_vm_with_no_address_says_so(app):
    client = _client(app, _row(ip=None))
    response = client.post(f"/console/{GUEST_VM}")

    assert response.status_code == 409
    assert "no address" in response.json()["detail"]


def test_a_vm_with_no_stored_credential_asks_for_a_redeploy(app):
    """The pre-console fleet. A redeploy is genuinely the only fix, so the detail
    has to say that rather than reading as a transient failure."""

    client = _client(app, _row(localAdminPasswordEnc=None))
    response = client.post(f"/console/{GUEST_VM}")

    assert response.status_code == 409
    assert "redeploy" in response.json()["detail"].lower()


def test_an_unreachable_guacd_is_503_before_a_ticket_exists(app, monkeypatch):
    """Preflighted here rather than left to the socket: the same failure one step
    later renders as a blank overlay that never resolves."""

    async def _refuse(**_kwargs):
        raise GuacdError("down")

    async def _unexpected(**_kwargs):
        raise AssertionError("a ticket was minted for an unreachable guacd")

    monkeypatch.setattr(console, "probe", _refuse)
    monkeypatch.setattr(console.tickets, "mint", _unexpected)
    client = _client(app, _row())

    response = client.post(f"/console/{GUEST_VM}")

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]


def test_capacity_is_503_not_a_silent_queue(app, monkeypatch):
    monkeypatch.setattr(console, "at_capacity", lambda: True)
    client = _client(app, _row())

    response = client.post(f"/console/{GUEST_VM}")

    assert response.status_code == 503
    assert "in use" in response.json()["detail"]


def test_the_admin_door_crosses_the_ownership_boundary(app):
    """Why the capability exists: an admin diagnosing an abandoned lab before
    reclaiming it has no other way in, and holds no VM_CONSOLE."""

    client = _client(app, _row(), username="root", role=Role.ADMIN)

    assert client.post(f"/admin/console/{GUEST_VM}").status_code == 200
    # ...and the self-service door stays shut to them.
    assert client.post(f"/console/{GUEST_VM}").status_code == 403
