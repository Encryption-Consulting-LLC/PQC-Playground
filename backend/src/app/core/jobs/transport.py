"""Valkey-backed transport for job progress, shared by the API and worker processes.

Replaces the old in-process ``registry``/``pubsub``/``runner`` trio. A Celery worker
runs in a separate OS process and can't touch the API's in-process session dict or
asyncio pubsub, so job state now lives in Valkey instead:

* a pub/sub **channel** ``jobs:{id}`` carries every message as it happens, for
  whoever is connected right now;
* a **snapshot** key ``jobs:{id}:snapshot`` holds ``{"status", "last"}`` so a late or
  reconnecting WebSocket subscriber sees current state immediately, the same role
  ``Job.last`` played before. It carries a TTL so finished jobs evict themselves.

``publish`` (sync) is called from both the FastAPI process (enqueueing) and the
Celery worker (progress/terminal). ``read_snapshot``/``subscribe`` (async, via
``redis.asyncio``) are used by the WebSocket route.
"""

import json

import redis
import redis.asyncio as redis_asyncio

from app.core.jobs.models import JobStatus, Message
from app.core.settings import settings

#: How long a snapshot survives while the job is still active / after it ends.
ACTIVE_TTL_SECONDS = 3600
_TERMINAL_TTL_SECONDS = 600

#: One shared sync client; cheap, thread-safe connection pool under the hood.
_client = redis.Redis.from_url(settings.valkey_url, decode_responses=True)


def channel(job_id: str) -> str:
    return f"jobs:{job_id}"


def snapshot_key(job_id: str) -> str:
    return f"jobs:{job_id}:snapshot"


def cancel_key(job_id: str) -> str:
    return f"jobs:{job_id}:cancel"


def cancel_reason_key(job_id: str) -> str:
    return f"jobs:{job_id}:cancel:reason"


def request_cancel(job_id: str, mode: str, reason: str | None = None) -> None:
    """Request a cooperative stop at the next step or operation boundary.

    *reason*, when given (e.g. an admin cross-user stop), is stored alongside the
    flag so the worker can surface it as the cancelled ops' ``detail`` and the
    terminal ``ErrorMsg`` — that's how the affected user learns *why* it stopped.
    """

    if mode not in ("step", "operation"):
        raise ValueError("cancel mode must be 'step' or 'operation'")
    pipe = _client.pipeline()
    pipe.set(cancel_key(job_id), mode, ex=ACTIVE_TTL_SECONDS)
    if reason:
        pipe.set(cancel_reason_key(job_id), reason, ex=ACTIVE_TTL_SECONDS)
    pipe.execute()


def cancel_mode(job_id: str) -> str | None:
    return _client.get(cancel_key(job_id))


def cancel_reason(job_id: str) -> str | None:
    return _client.get(cancel_reason_key(job_id))


def publish(
    job_id: str, msg: Message, *, status: JobStatus, terminal: bool = False
) -> None:
    """Write the snapshot and fan the message out to live subscribers.

    Snapshot-then-publish (not the reverse) so a subscriber that reads the snapshot
    right after a PUBLISH never sees stale state.
    """
    payload = msg.model_dump_json()
    snapshot = json.dumps({"status": status.value, "last": json.loads(payload)})
    ttl = _TERMINAL_TTL_SECONDS if terminal else ACTIVE_TTL_SECONDS
    pipe = _client.pipeline()
    pipe.set(snapshot_key(job_id), snapshot, ex=ttl)
    pipe.publish(channel(job_id), payload)
    pipe.execute()


def make_publisher(job_id: str):
    """Return a ``Publish``-shaped callable (``Message -> None``) bound to *job_id*.

    Lets ``CloneProgressReducer`` (written against the old in-process ``Publish``
    type) keep publishing ``ProgressMsg`` samples without knowing about Valkey.
    """

    def _publish(msg: Message) -> None:
        publish(job_id, msg, status=JobStatus.running)

    return _publish


async def read_snapshot(
    redis_client: "redis_asyncio.Redis", job_id: str
) -> dict | None:
    """Return ``{"status", "last"}`` for *job_id*, or None if it doesn't exist (yet/anymore)."""
    raw = await redis_client.get(snapshot_key(job_id))
    if raw is None:
        return None
    return json.loads(raw)


def make_async_client() -> "redis_asyncio.Redis":
    return redis_asyncio.from_url(settings.valkey_url, decode_responses=True)
