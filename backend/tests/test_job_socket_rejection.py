"""What a client can actually observe when the job socket turns it away.

Closing a WebSocket that was never accepted is answered by the ASGI server with
a plain HTTP 403, and the application close code is discarded. The frontend
distinguishes "this job is gone" (4404 — stop retrying, say so) from "the
connection blipped" (retry), so losing the code turned a finished deploy into a
silent transport failure. Every rejection here must therefore arrive as a real
close frame carrying its code, not as a denied handshake.
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from app.core.authz import AuthedUser, Role
from app.routers import ws


class _FakePubSub:
    async def subscribe(self, *_channels) -> None: ...

    async def unsubscribe(self, *_channels) -> None: ...

    async def aclose(self) -> None: ...

    async def listen(self):  # pragma: no cover — terminal paths return first
        return
        yield


class _FakeRedis:
    def pubsub(self) -> _FakePubSub:
        return _FakePubSub()

    async def aclose(self) -> None: ...


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(ws.transport, "make_async_client", lambda: _FakeRedis())
    app = FastAPI()
    app.include_router(ws.router)
    return TestClient(app)


def _as(username: str, role: Role):
    async def _resolve(token: str | None):
        return AuthedUser(username=username, role=role, auth="local") if token else None

    return _resolve


def _snapshot(value):
    async def _read(_client, _job_id):
        return value

    return _read


def _plan_run(doc):
    class _Col:
        async def find_one(self, _query):
            return doc

    return lambda: _Col()


def close_code(client: TestClient, url: str) -> int:
    """The code the client receives — fails loudly if the handshake was denied."""
    try:
        with client.websocket_connect(url) as socket:
            with pytest.raises(WebSocketDisconnect) as excinfo:
                socket.receive_json()
    except WebSocketDenialResponse as denial:  # pragma: no cover — the bug
        pytest.fail(
            f"upgrade denied with HTTP {denial.status_code}; the close code is lost"
        )
    return excinfo.value.code


def test_a_bad_token_closes_with_4401_rather_than_denying_the_handshake(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(ws, "resolve_user_token", _as("guest", Role.GUEST))

    # No token at all → resolve returns None.
    assert close_code(client, "/ws/jobs/job9") == 4401


def test_an_unknown_job_closes_with_4404_rather_than_denying_the_handshake(
    client, monkeypatch
) -> None:
    monkeypatch.setattr(ws, "resolve_user_token", _as("guest", Role.GUEST))
    monkeypatch.setattr(ws.transport, "read_snapshot", _snapshot(None))
    monkeypatch.setattr(ws, "plan_runs_col", _plan_run(None))

    # The regression: this used to be answered with HTTP 403 and the 4404 never
    # reached the client, which read the denial as a retryable blip.
    assert close_code(client, "/ws/jobs/job9?token=t") == 4404


def test_an_evicted_snapshot_replays_the_persisted_outcome(client, monkeypatch) -> None:
    monkeypatch.setattr(ws, "resolve_user_token", _as("guest", Role.GUEST))
    monkeypatch.setattr(ws.transport, "read_snapshot", _snapshot(None))
    monkeypatch.setattr(
        ws,
        "plan_runs_col",
        _plan_run(
            {
                "jobId": "job9",
                "owner": "guest",
                "scheduler": {
                    "ops": {
                        "clone-dc": {"status": "done"},
                        "iis-web": {"status": "error", "detail": "feature timeout"},
                    },
                    "updatedAt": 1,
                },
            }
        ),
    )

    with client.websocket_connect("/ws/jobs/job9?token=t") as socket:
        message = socket.receive_json()

    assert message["type"] == "done"
    assert message["result"]["ops"]["iis-web"]["detail"] == "feature timeout"


def test_another_users_deployment_is_not_replayable(client, monkeypatch) -> None:
    monkeypatch.setattr(ws, "resolve_user_token", _as("guest", Role.GUEST))
    monkeypatch.setattr(ws.transport, "read_snapshot", _snapshot(None))
    monkeypatch.setattr(
        ws,
        "plan_runs_col",
        _plan_run(
            {
                "jobId": "job9",
                "owner": "someone-else",
                "scheduler": {"ops": {"clone-dc": {"status": "done"}}},
            }
        ),
    )

    assert close_code(client, "/ws/jobs/job9?token=t") == 4403


def test_an_operator_may_replay_any_deployment(client, monkeypatch) -> None:
    monkeypatch.setattr(ws, "resolve_user_token", _as("operator", Role.OPERATOR))
    monkeypatch.setattr(ws.transport, "read_snapshot", _snapshot(None))
    monkeypatch.setattr(
        ws,
        "plan_runs_col",
        _plan_run(
            {
                "jobId": "job9",
                "owner": "someone-else",
                "scheduler": {"ops": {"clone-dc": {"status": "done"}}},
            }
        ),
    )

    with client.websocket_connect("/ws/jobs/job9?token=t") as socket:
        assert socket.receive_json()["type"] == "done"
