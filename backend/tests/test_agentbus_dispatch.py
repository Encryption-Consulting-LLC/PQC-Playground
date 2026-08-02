"""dispatch_and_wait frame handling — progress relay + terminal resolution.

Drives the wait loop with a scripted pubsub (no real Valkey): a progress frame
must reach the ``on_progress`` callback (and never abort the dispatch, even if
the callback raises), and the following ``done`` frame still resolves the call.
"""

import json

import pytest

from app.core import agentbus


class _FakePubSub:
    def __init__(self, frames):
        self._frames = list(frames)

    def subscribe(self, _channel):
        pass

    def get_message(self, timeout=1.0):
        if not self._frames:
            return None
        frame = self._frames.pop(0)
        if frame is None:
            return None
        return {"type": "message", "data": json.dumps(frame)}

    def close(self):
        pass


class _FakeRedis:
    """Snapshot always absent, liveness key always present."""

    def __init__(self, frames):
        self._frames = frames
        self.published = []

    def get(self, key):
        if key.startswith("agent-conn:"):
            return b"1"
        return None

    def publish(self, channel, payload):
        self.published.append((channel, payload))

    def pubsub(self, **_kwargs):
        return _FakePubSub(self._frames)


def _dispatch(frames, on_progress=None):
    return agentbus.dispatch_and_wait(
        "vm-1",
        "dc.install_forest",
        {},
        job_id="job-1",
        role="operator",
        timeout_s=30,
        on_progress=on_progress,
        client=_FakeRedis(frames),
    )


def test_progress_frames_reach_the_callback_and_done_still_resolves():
    seen = []
    result = _dispatch(
        [
            {"type": "progress", "phase": "Installing AD DS", "percent": 40.0},
            {"type": "progress", "phase": "Promoting", "percent": 80.0},
            {"type": "done", "result": {"ok": True}},
        ],
        on_progress=lambda phase, pct: seen.append((phase, pct)),
    )
    assert result == {"ok": True}
    assert seen == [("Installing AD DS", 40.0), ("Promoting", 80.0)]


def test_a_raising_progress_callback_never_kills_the_dispatch():
    def boom(_phase, _pct):
        raise RuntimeError("ui callback bug")

    result = _dispatch(
        [
            {"type": "progress", "phase": "x", "percent": 1.0},
            {"type": "done", "result": {"ok": 1}},
        ],
        on_progress=boom,
    )
    assert result == {"ok": 1}


def test_progress_frames_without_a_callback_are_still_skipped():
    result = _dispatch(
        [
            {"type": "progress", "phase": "x", "percent": 1.0},
            {"type": "done", "result": {}},
        ]
    )
    assert result == {}


def test_a_wait_that_runs_out_raises_the_non_retryable_timeout():
    """A timeout is its own exception, not a plain DispatchError: the command is
    probably still running on the guest, so the sequence engine must not
    redispatch it (it reads the `timed_out` marker)."""
    with pytest.raises(agentbus.DispatchTimeout, match="timed out after 0s"):
        agentbus.dispatch_and_wait(
            "vm-1",
            "iis.setup_certenroll",
            {},
            job_id="job-timeout",
            role="operator",
            timeout_s=0,
            client=_FakeRedis([]),
        )
    assert agentbus.DispatchTimeout.timed_out is True
    assert issubclass(agentbus.DispatchTimeout, agentbus.DispatchError)


def test_dispatch_waits_through_the_agents_maximum_reconnect_backoff():
    """A buffered terminal frame may arrive after the agent's 30s retry."""

    class _ReconnectRedis(_FakeRedis):
        def __init__(self):
            super().__init__([None] * 30 + [{"type": "done", "result": {"ok": True}}])
            # Existing snapshot check + initial dispatch gate are live. The
            # lease then disappears for 30 polls and returns on reconnect.
            self.live = iter([True, True] + [False] * 30 + [True])

        def get(self, key):
            if key.startswith("agent-conn:"):
                return b"1" if next(self.live, True) else None
            return None

    result = agentbus.dispatch_and_wait(
        "vm-1",
        "ca.install",
        {},
        job_id="job-reconnect",
        role="operator",
        timeout_s=60,
        client=_ReconnectRedis(),
    )

    assert result == {"ok": True}
