"""Agent presence lease refresh must survive transient Valkey failures."""

import asyncio
import contextlib

import pytest

from app.routers import executor


def test_keepalive_retries_after_a_transient_refresh_error(monkeypatch):
    async def exercise():
        calls = 0
        refreshed = asyncio.Event()

        async def refresh(_vm_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("temporary Valkey failure")
            refreshed.set()

        async def no_delay(_seconds):
            await asyncio.sleep(0)

        monkeypatch.setattr(executor.agentbus, "refresh_agent_live", refresh)
        task = asyncio.create_task(executor._keepalive("vm-1", sleep=no_delay))
        try:
            await asyncio.wait_for(refreshed.wait(), timeout=1)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert calls == 2

    asyncio.run(exercise())


def test_initial_lease_failure_does_not_leak_a_green_presence(monkeypatch):
    vm_id = "vm-initial-lease-failure"
    executor.agents._connected.pop(vm_id, None)

    class _WebSocket:
        headers = {
            "x-executor-vm-id": vm_id,
            "x-executor-token": "token",
        }
        query_params = {}

        async def accept(self):
            pass

    async def authenticated(_vm_id, _token):
        return True

    async def fail_mark(_vm_id):
        raise ConnectionError("Valkey unavailable")

    async def clear(_vm_id):
        pass

    monkeypatch.setattr(executor, "_authenticate", authenticated)
    monkeypatch.setattr(executor.agentbus, "mark_agent_live", fail_mark)
    monkeypatch.setattr(executor.agentbus, "clear_agent_live", clear)

    with pytest.raises(ConnectionError, match="Valkey unavailable"):
        asyncio.run(executor.connect(_WebSocket()))

    assert executor.agents.resolve_agent(vm_id) is None
