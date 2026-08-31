"""What a guest is told, and what an operator still is.

Two properties matter more than any individual sentence.

The first is that the narrowing is **fail-closed**: the marker table only ever
upgrades a recognised failure to a more specific sentence, so a message reworded
upstream — or a vmkit release changing its own text — degrades to the vague
status default rather than falling through raw. Every test that pins a specific
sentence is therefore pinning a nicety; the test that pins an *unrecognised*
detail is pinning the guarantee.

The second is that operators are untouched. They are the ones who diagnose the
platform, and a shared table is exactly the kind of thing that quietly starts
narrowing for everybody.
"""

import os

import pytest
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from starlette.testclient import TestClient

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core import authz  # noqa: E402
from app.core.authz import (  # noqa: E402
    SESSION_REJECTED_HEADERS,
    AuthedUser,
    Role,
    get_current_user,
)
from app.core.errors import register_exception_handlers  # noqa: E402
from app.core.guest_errors import (  # noqa: E402
    guest_http_detail,
    guest_job_payload,
    guest_sentence,
)

# --- the table ---------------------------------------------------------------


def test_the_datastore_limit_is_not_quoted_at_a_guest() -> None:
    """The message that started this. A guest clicking Deploy was shown the
    org's own capacity arithmetic against an admin-set threshold."""

    raw = (
        "Infrastructure preflight no longer passes: Reserved 68719476736 bytes; "
        "projected usage 92.31% (limit 85.00%)."
    )

    assert guest_sentence(500, raw) == "There is not enough space to deploy this lab."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Guest IP pool exhausted — tear down unused VMs or widen the range "
            "in settings.",
            "This environment has no free addresses for a new lab.",
        ),
        (
            "No shared ESXi target configured — an operator must set it via "
            "PUT /api/settings.",
            "The lab environment is temporarily unavailable.",
        ),
        (
            "agent 'dc01' did not phone home within 900s of boot",
            "This machine did not finish starting up.",
        ),
        (
            "health gate 'certificate-health' failed: the issued probe did not "
            "receive a verified OCSP response",
            "The lab did not pass its final health check.",
        ),
        (
            "step 'ca.install' requires unavailable artifact 'rootCaCert'",
            "This machine could not be set up.",
        ),
        (
            "deployment cancellation requested before step 'ca.install'",
            "The deployment was cancelled.",
        ),
    ],
)
def test_the_advice_a_guest_cannot_take_is_removed(raw: str, expected: str) -> None:
    """Each of these named something only an admin can reach — a settings range,
    an ESXi target, a step id, a probe command."""

    assert guest_sentence(500, raw) == expected


def test_an_unrecognised_detail_falls_back_rather_than_through() -> None:
    """The guarantee. A marker can only ever make a message better; nothing has
    to match for the raw text to be withheld."""

    raw = "pyVmomi.VmomiSupport.vim.fault.NoPermission: datastore1 [moref-42]"

    assert guest_sentence(500, raw) == "Something went wrong while deploying this lab."
    assert guest_sentence(503, raw) == "The lab environment is temporarily unavailable."
    assert guest_sentence(404, raw) == "That item was not found."


def test_a_message_already_written_for_a_guest_survives() -> None:
    """The join-code and console refusals are the existing house style, and the
    credential one carries the single action that works — a redeploy really is
    the only fix, and it must not read as a transient failure."""

    assert (
        guest_sentence(410, "That join code is no longer active.")
        == "That join code is no longer active."
    )
    assert "redeploy" in guest_sentence(
        409,
        "No stored credentials for this VM. Remote desktop needs a password set "
        "at first boot, so redeploy this node to enable it.",
    )


def test_topology_diagnostics_are_the_documented_pass_through() -> None:
    """They name the guest's own nodes and are the canvas's only guidance on an
    invalid drawing. Vaguing them would make the canvas unusable."""

    detail = {
        "message": "Topology compilation failed.",
        "diagnostics": [{"message": "Issuing CA has no root CA parent."}],
    }

    narrowed = guest_http_detail(422, detail)

    assert narrowed["diagnostics"] == detail["diagnostics"]
    assert narrowed["message"] == "This topology cannot be deployed as it is drawn."


def test_the_preflight_object_is_dropped_from_a_409() -> None:
    """Its rows carry byte counts, VMX paths and image revisions even when they
    pass, so the whole object goes rather than each row being reworded."""

    detail = {
        "message": "Infrastructure preflight failed.",
        "preflight": {
            "checks": [
                {"ok": False, "detail": "Reserved 687194767 bytes; usage 92.31%."}
            ]
        },
    }

    narrowed = guest_http_detail(409, detail)

    assert narrowed == "This environment is not ready to deploy a lab right now."


# --- the job stream ----------------------------------------------------------


def test_the_terminal_frame_keeps_its_ops_under_result() -> None:
    """``DoneMsg`` nests ops one level deeper than ``PlanStateMsg``, and it is
    the frame a failed deploy ends on."""

    payload = {
        "type": "done",
        "result": {
            "ops": {
                "web": {
                    "status": "error",
                    "detail": "agent command 'iis.setup' failed: Access is denied",
                    "trace": "Traceback (most recent call last):",
                }
            }
        },
    }

    op = guest_job_payload(payload)["result"]["ops"]["web"]

    assert op["detail"] == "This machine could not be set up."
    assert "trace" not in op


def test_nested_step_details_are_narrowed_too() -> None:
    payload = {
        "type": "plan-state",
        "ops": {
            "web": {
                "status": "error",
                "detail": "provisioning failed: step 'iis.setup' did not report",
                "steps": {
                    "iis.setup": {
                        "status": "error",
                        "detail": "agent command 'iis.setup' timed out after 600s",
                    }
                },
            }
        },
    }

    op = guest_job_payload(payload)["ops"]["web"]

    assert op["detail"] == "This machine could not be set up."
    assert op["steps"]["iis.setup"]["detail"] == "This machine could not be set up."


def test_the_health_report_is_not_a_message_and_survives() -> None:
    """``result.health`` and ``result.certificateJourney`` are guest-facing
    features the canvas renders, and their nested ``detail`` keys are structured
    diagnostics rather than prose. This is why the rewriter walks named fields
    instead of every key called "detail" — such a walker would quietly corrupt
    the health report and take the badge and the journey lens with it.
    """

    health = {"checks": {"ocspResponder": {"detail": {"errorCodes": [0]}}}}
    payload = {
        "type": "plan-state",
        "ops": {"lab": {"status": "done", "result": {"health": health}}},
    }

    assert guest_job_payload(payload)["ops"]["lab"]["result"]["health"] == health


def test_an_ad_hoc_command_result_is_the_guests_own_output() -> None:
    """A guest holds VM_EXEC_ARBITRARY, so the executor relay's ``DoneMsg``
    carries output they asked for. Only ``result.ops`` is a plan."""

    payload = {"type": "done", "result": {"stdout": "Directory: C:\\\\Windows"}}

    assert guest_job_payload(payload) == payload


def test_a_heartbeat_passes_through_untouched() -> None:
    assert guest_job_payload({"type": "ping"}) == {"type": "ping"}


# --- the HTTP handler --------------------------------------------------------


class _Body(BaseModel):
    name: str


def _app() -> FastAPI:
    """A real app with the real handlers, so the delegation to FastAPI's own
    handler — and everything it carries — is exercised rather than mocked."""

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def _boom(user: AuthedUser = Depends(get_current_user)) -> dict:
        raise HTTPException(
            409,
            detail="Reserved 687194767 bytes; projected usage 92.31% (limit 85.00%).",
        )

    @app.get("/expired")
    async def _expired(user: AuthedUser = Depends(get_current_user)) -> dict:
        raise HTTPException(
            401,
            detail="Invalid or expired session token.",
            headers=dict(SESSION_REJECTED_HEADERS),
        )

    @app.post("/body")
    async def _body(
        payload: _Body, user: AuthedUser = Depends(get_current_user)
    ) -> dict:
        return {"ok": payload.name}

    return app


def _client(monkeypatch, role: Role) -> TestClient:
    """Authenticate by monkeypatching ``resolve_user_token``, deliberately not
    by overriding ``get_current_user``.

    ``dependency_overrides`` replaces the dependency wholesale, which skips the
    ``request.state`` write the handlers read — a test written that way passes
    whether or not any of this works.
    """

    async def _resolve(_token):
        return AuthedUser(username="gwen", role=role, auth="local")

    monkeypatch.setattr(authz, "resolve_user_token", _resolve)
    return TestClient(_app())


def test_a_guest_gets_the_sentence_and_an_operator_the_truth(monkeypatch) -> None:
    guest = _client(monkeypatch, Role.GUEST).get(
        "/boom", headers={"x-session-token": "t"}
    )
    operator = _client(monkeypatch, Role.OPERATOR).get(
        "/boom", headers={"x-session-token": "t"}
    )

    assert guest.status_code == operator.status_code == 409
    assert guest.json()["detail"] == "There is not enough space to deploy this lab."
    assert (
        operator.json()["detail"]
        == "Reserved 687194767 bytes; projected usage 92.31% (limit 85.00%)."
    )


def test_the_session_marker_survives_the_guest_branch(monkeypatch) -> None:
    """``WWW-Authenticate: Session`` is what the frontend clears its stored
    session on. A handler that builds its own response instead of delegating
    drops it, and an expired guest is then stranded on a wall of 401s with no
    login form."""

    response = _client(monkeypatch, Role.GUEST).get(
        "/expired", headers={"x-session-token": "t"}
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Session"
    assert response.json()["detail"] == "Your session is no longer valid."


def test_a_pydantic_error_does_not_name_internal_field_paths(monkeypatch) -> None:
    guest = _client(monkeypatch, Role.GUEST).post(
        "/body", json={}, headers={"x-session-token": "t"}
    )
    operator = _client(monkeypatch, Role.OPERATOR).post(
        "/body", json={}, headers={"x-session-token": "t"}
    )

    assert guest.status_code == operator.status_code == 422
    assert guest.json()["detail"] == "This topology cannot be deployed as it is drawn."
    assert operator.json()["detail"][0]["loc"] == ["body", "name"]


def test_an_unauthenticated_path_is_left_alone(monkeypatch) -> None:
    """No account, nothing to narrow for — and these paths raise no internals.
    This is also the SPA catch-all's 404 and every static asset request, which
    carry no session token at all."""

    response = _client(monkeypatch, Role.GUEST).get("/nope")

    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"
