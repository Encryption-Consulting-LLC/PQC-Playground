"""What a client can actually observe when the job socket turns it away.

Closing a WebSocket that was never accepted is answered by the ASGI server with
a plain HTTP 403, and the application close code is discarded. The frontend
distinguishes "this job is gone" (4404 — stop retrying, say so) from "the
connection blipped" (retry), so losing the code turned a finished deploy into a
silent transport failure. Every rejection here must therefore arrive as a real
close frame carrying its code, not as a denied handshake.
"""

import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from app.core.authz import AuthedUser, Role
from app.routers import ws


class _FakePubSub:
    """Replays a scripted sequence of ``get_message`` replies.

    A ``None`` entry is the idle timeout — what the real client returns when the
    channel said nothing for ``timeout`` seconds, and what makes the route
    heartbeat.
    """

    def __init__(self, script=()) -> None:
        self._script = list(script)

    async def subscribe(self, *_channels) -> None: ...

    async def unsubscribe(self, *_channels) -> None: ...

    async def aclose(self) -> None: ...

    async def get_message(self, ignore_subscribe_messages=False, timeout=None):
        return self._script.pop(0) if self._script else None


class _FakeRedis:
    def __init__(self, script=()) -> None:
        self._script = script

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self._script)

    async def aclose(self) -> None: ...


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(ws.transport, "make_async_client", lambda: _FakeRedis())
    app = FastAPI()
    app.include_router(ws.router)
    return TestClient(app)


def _published(message: dict) -> dict:
    """One pubsub delivery carrying *message*."""
    return {"type": "message", "data": json.dumps(message)}


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


_EVICTED_RUN = {
    "jobId": "job9",
    "owner": "gwen",
    "scheduler": {
        "ops": {
            "clone-dc": {"status": "done"},
            "iis-web": {
                "status": "error",
                "detail": "step 'iis.setup' timed out after 600s",
                "trace": "Traceback (most recent call last):\n  ...",
            },
        },
        "updatedAt": 1,
    },
}


def test_an_evicted_snapshot_replays_the_persisted_outcome(client, monkeypatch) -> None:
    monkeypatch.setattr(ws, "resolve_user_token", _as("gwen", Role.OPERATOR))
    monkeypatch.setattr(ws.transport, "read_snapshot", _snapshot(None))
    monkeypatch.setattr(ws, "plan_runs_col", _plan_run(_EVICTED_RUN))

    with client.websocket_connect("/ws/jobs/job9?token=t") as socket:
        message = socket.receive_json()

    assert message["type"] == "done"
    op = message["result"]["ops"]["iis-web"]
    assert op["detail"] == "step 'iis.setup' timed out after 600s"
    assert op["trace"].startswith("Traceback")


def test_a_guest_is_not_replayed_the_raw_failure(client, monkeypatch) -> None:
    """The terminal frame carries its ops at ``result.ops``, not ``ops``.

    That is the frame a failed deploy actually ends on, so narrowing only the
    top-level key would have left every real failure reaching the guest
    verbatim — step ids, probe timeouts and a Python traceback — while looking
    like the feature worked on the in-flight frames.
    """

    monkeypatch.setattr(ws, "resolve_user_token", _as("gwen", Role.GUEST))
    monkeypatch.setattr(ws.transport, "read_snapshot", _snapshot(None))
    monkeypatch.setattr(ws, "plan_runs_col", _plan_run(_EVICTED_RUN))

    with client.websocket_connect("/ws/jobs/job9?token=t") as socket:
        message = socket.receive_json()

    assert message["type"] == "done"
    op = message["result"]["ops"]["iis-web"]
    assert op["detail"] == "This machine could not be set up."
    assert "trace" not in op
    # The op it did not fail on is untouched apart from carrying no detail.
    assert message["result"]["ops"]["clone-dc"]["status"] == "done"


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


def _streaming_client(monkeypatch, script) -> TestClient:
    """A client whose job channel replays *script*, reaching the streaming loop."""
    monkeypatch.setattr(ws.transport, "make_async_client", lambda: _FakeRedis(script))
    monkeypatch.setattr(ws, "resolve_user_token", _as("guest", Role.GUEST))
    monkeypatch.setattr(ws.transport, "read_snapshot", _snapshot({"last": None}))
    app = FastAPI()
    app.include_router(ws.router)
    return TestClient(app)


def test_a_quiet_channel_still_sends_heartbeats(monkeypatch) -> None:
    """The regression: a deploy plan is silent for whole role installs.

    This stream only ever spoke when the transport published, so a proxy reaped
    it as idle and the browser — which reacts to nothing but `close` — kept a
    half-open socket. The terminal frame went into a connection nobody was
    reading and the canvas stayed locked on a deploy that had finished.
    """
    done = {"type": "done", "result": {}}
    client = _streaming_client(monkeypatch, [None, None, _published(done)])

    with client.websocket_connect("/ws/jobs/job9?token=t") as socket:
        assert socket.receive_json() == {"type": "ping"}
        assert socket.receive_json() == {"type": "ping"}
        assert socket.receive_json() == done


def test_heartbeats_do_not_displace_published_messages(monkeypatch) -> None:
    """A heartbeat is filler between real frames, never instead of one."""
    state = {"type": "plan-state", "ops": {"clone-dc": {"status": "running"}}}
    done = {"type": "done", "result": {"ops": {}}}
    client = _streaming_client(monkeypatch, [_published(state), None, _published(done)])

    with client.websocket_connect("/ws/jobs/job9?token=t") as socket:
        assert socket.receive_json() == state
        assert socket.receive_json() == {"type": "ping"}
        assert socket.receive_json() == done
