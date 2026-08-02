"""Worker↔agent dispatch bridge.

Plan execution runs in the Celery worker (blocking hour-scale vmkit clones
plus IP-pool idempotency belong there, not in the API process), but the
executor agent WebSockets live in the **FastAPI** process
(:mod:`app.core.agents`). The worker therefore can't send an agent a command
directly. This module is the relay across the two processes, over the same
Valkey used for job transport:

* **Worker side** (:func:`dispatch_and_wait`, sync redis): SUBSCRIBE the job's
  channel *before* publishing a dispatch request onto ``agent-dispatch``, then
  block for the agent's terminal frame (which the connect handler already
  relays onto ``jobs:{job_id}`` — no new return path). Redelivery-safe: a job
  whose snapshot is already terminal returns immediately.
* **API side** (:func:`run_dispatch_subscriber`, async lifespan task):
  SUBSCRIBE ``agent-dispatch``; whichever process holds the socket for that
  ``vm_id`` forwards the frame to the agent, the rest stay silent. The
  single-live-connection-per-vm_id takeover guarantees exactly one holder.

**Liveness / reboot-resume:** the connect handler calls :func:`mark_agent_live`
(TTL key ``agent-conn:{vm_id}`` + ``$set agent.lastConnectedAt`` on the
registry). :func:`dispatch_and_wait` refuses to dispatch to an agent whose
liveness key is absent; :func:`wait_for_reconnect` requires both a newer
registry timestamp and a present liveness key, so a reboot step resumes only
while the post-reboot connection is still dispatchable.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable

import redis

from app.core.jobs import transport
from app.core.settings import settings

logger = logging.getLogger(__name__)

#: Pub/sub channel the worker publishes dispatch requests onto; the API process
#: holding the target socket forwards them.
DISPATCH_CHANNEL = "agent-dispatch"


def agent_conn_key(vm_id: str) -> str:
    return f"agent-conn:{vm_id}"


#: Liveness TTL — refreshed by the connect handler's keepalive well inside it.
AGENT_CONN_TTL_SECONDS = 90

#: Consecutive ~1s dispatch polls with the liveness key absent before a
#: non-reboot command is declared dead. The bundled agent preserves outbound
#: progress across reconnects and backs off for as long as 30s, so the worker
#: must leave enough room for that final retry plus the WebSocket handshake.
#: This remains far shorter than a step's ``timeout_s`` (which would otherwise
#: hang the op when the agent process really died).
_DISPATCH_LIVENESS_GRACE_POLLS = 45


class AgentUnreachableError(Exception):
    """No live agent connection for the target vm_id (never phoned home, or its
    liveness key expired)."""


class DispatchError(Exception):
    """The dispatched command failed terminally, or the wait timed out."""


class DispatchTimeout(DispatchError):
    """The agent never relayed a terminal frame within the step's budget.

    Distinct from a plain error frame because the command is very likely *still
    running* on the guest — a slow ``Install-WindowsFeature`` does not stop when
    we give up waiting. Redispatching would race a second invocation against the
    first, so this failure must not be retried.
    """

    #: Duck-typed marker read by the (transport-agnostic) sequence engine.
    timed_out = True


class ReconnectTimeoutError(Exception):
    """The agent did not phone home again within the reboot-wait window."""


# --------------------------------------------------------------------------- #
# Worker side (sync)                                                          #
# --------------------------------------------------------------------------- #
def dispatch_and_wait(
    vm_id: str,
    command: str,
    params: dict,
    *,
    job_id: str,
    role: str,
    timeout_s: int,
    secret_keys: tuple[str, ...] = (),
    expect_disconnect: bool = False,
    on_progress: "Callable[[str | None, float | None], None] | None" = None,
    on_tick: "Callable[[float], None] | None" = None,
    client: "redis.Redis | None" = None,
) -> dict:
    """Dispatch one command to ``vm_id``'s agent and block for its terminal
    result. Returns the ``done`` frame's ``result`` dict; raises
    :class:`DispatchError` on an ``error`` frame or timeout, and
    :class:`AgentUnreachableError` if no agent is live.

    Redelivery-safe: if ``jobs:{job_id}`` already holds a terminal snapshot
    (the step ran before the worker was redelivered this task), its result is
    returned without re-dispatching — deterministic per-step job ids make this
    exact.

    ``expect_disconnect`` marks the reboot step, whose terminal frame is relayed
    before the socket legitimately drops; for every other command a liveness key
    that vanishes mid-wait means the agent died (e.g. an unexpected reboot), so
    we raise :class:`AgentUnreachableError` within seconds instead of hanging to
    ``timeout_s``.

    ``on_progress(phase, percent)`` is invoked for every non-terminal frame the
    agent relays for this job — the live sub-step feed the plan runner folds
    into the op state. ``on_tick(elapsed_s)`` is invoked on every ~1s poll that
    yields no frame — the elapsed-time heartbeat for long silent commands
    (``Install-ADDSForest`` reports once, then nothing for minutes). Both are
    purely observational: their exceptions are logged and swallowed so a UI
    callback can never kill a dispatch.
    """
    r = client or transport._client  # one shared sync pool

    existing = r.get(transport.snapshot_key(job_id))
    if existing is not None:
        snap = json.loads(existing)
        terminal = _terminal_result(snap)
        if terminal is not None:
            logger.info("dispatch %s already terminal (redelivery) — reusing", job_id)
            return terminal

    if r.get(agent_conn_key(vm_id)) is None:
        raise AgentUnreachableError(f"no live agent for vm_id '{vm_id}'")

    pubsub = r.pubsub(ignore_subscribe_messages=True)
    # Subscribe BEFORE publishing the dispatch request so we can't miss a fast
    # agent's terminal frame in the gap between publish and subscribe.
    pubsub.subscribe(transport.channel(job_id))
    try:
        request = json.dumps(
            {
                "vm_id": vm_id,
                "job_id": job_id,
                "command": command,
                "params": params,
                "role": role,
            }
        )
        r.publish(DISPATCH_CHANNEL, request)

        started = time.monotonic()
        deadline = started + timeout_s
        liveness_misses = 0
        while time.monotonic() < deadline:
            message = pubsub.get_message(timeout=1.0)
            if message is None:
                # Late-subscribe safety net: the terminal frame may have landed
                # in the snapshot before our subscribe took effect.
                snap_raw = r.get(transport.snapshot_key(job_id))
                if snap_raw is not None:
                    terminal = _terminal_result(json.loads(snap_raw))
                    if terminal is not None:
                        return terminal
                # Fail fast if the agent drops mid-command (unless a reboot is
                # expected) — otherwise a killed command hangs until timeout_s.
                if not expect_disconnect:
                    if r.get(agent_conn_key(vm_id)) is None:
                        liveness_misses += 1
                        if liveness_misses >= _DISPATCH_LIVENESS_GRACE_POLLS:
                            raise AgentUnreachableError(
                                f"agent '{vm_id}' disconnected while running "
                                f"'{command}'"
                            )
                    else:
                        liveness_misses = 0
                if on_tick is not None:
                    try:
                        on_tick(time.monotonic() - started)
                    except Exception:  # noqa: BLE001 — observational only
                        logger.exception("on_tick callback failed")
                continue
            liveness_misses = 0
            frame = json.loads(message["data"])
            outcome = _frame_outcome(frame)
            if outcome is None:
                if on_progress is not None:
                    try:
                        on_progress(frame.get("phase"), frame.get("percent"))
                    except Exception:  # noqa: BLE001 — observational only
                        logger.exception("on_progress callback failed")
                continue  # progress frame — keep waiting
            ok, payload = outcome
            if ok:
                return payload
            raise DispatchError(
                f"agent command '{command}' failed: {payload.get('detail', 'unknown error')}"
            )
        raise DispatchTimeout(f"agent command '{command}' timed out after {timeout_s}s")
    finally:
        pubsub.close()


def _frame_outcome(frame: dict) -> tuple[bool, dict] | None:
    """Classify one relayed job frame: ``(True, result)`` for done,
    ``(False, {detail})`` for error, ``None`` for a non-terminal frame."""
    kind = frame.get("type")
    if kind == "done":
        return True, frame.get("result") or {}
    if kind == "error":
        return False, {"detail": frame.get("detail") or "executor command failed"}
    return None


def _terminal_result(snapshot: dict) -> dict | None:
    """The result dict if ``snapshot['last']`` is a terminal frame, else None.
    An ``error`` snapshot raises so a redelivery re-fails identically."""
    last = snapshot.get("last") or {}
    outcome = _frame_outcome(last)
    if outcome is None:
        return None
    ok, payload = outcome
    if ok:
        return payload
    raise DispatchError(payload.get("detail", "executor command failed"))


def wait_for_agent(
    vm_id: str,
    timeout_s: int,
    *,
    client: "redis.Redis | None" = None,
    sleep=time.sleep,
    poll_interval_s: float = 3.0,
) -> None:
    """Block until ``vm_id``'s agent has phoned home (its liveness key exists),
    or raise :class:`AgentUnreachableError`. Called after a clone, before the
    createVm provision sequence runs — the freshly-booted VM needs time to
    install and start the agent."""
    r = client or transport._client
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if r.get(agent_conn_key(vm_id)) is not None:
            return
        sleep(poll_interval_s)
    raise AgentUnreachableError(
        f"agent '{vm_id}' did not phone home within {timeout_s}s of boot"
    )


def wait_for_reconnect(
    vm_id: str,
    since_ms: int,
    timeout_s: int,
    *,
    db,
    client: "redis.Redis | None" = None,
    sleep=time.sleep,
    now_ms=None,
    monotonic=time.monotonic,
    poll_interval_s: float = 3.0,
) -> None:
    """Block until a post-reboot agent connection is currently live.

    ``lastConnectedAt > since_ms`` proves that the agent connected after the
    reboot dispatch, while the Valkey liveness key proves that connection has
    not subsequently dropped.  Requiring both closes a race where a short-lived
    reconnect advanced Mongo, disconnected, and let the next sequence step fail
    immediately with ``no live agent``.

    Raises :class:`ReconnectTimeoutError` when no live post-reboot connection
    appears within the window. ``db`` is a worker sync database.
    """
    r = client or transport._client
    deadline = monotonic() + timeout_s
    while monotonic() < deadline:
        doc = db["vm_registry"].find_one(
            {"agent.vmId": vm_id}, {"agent.lastConnectedAt": 1}
        )
        last = ((doc or {}).get("agent") or {}).get("lastConnectedAt")
        if (
            isinstance(last, int)
            and last > since_ms
            and r.get(agent_conn_key(vm_id)) is not None
        ):
            return
        sleep(poll_interval_s)
    raise ReconnectTimeoutError(
        f"agent '{vm_id}' did not reconnect within {timeout_s}s of the reboot"
    )


def wait_for_stable_agent(
    vm_id: str,
    settle_s: int,
    timeout_s: int,
    *,
    db,
    client: "redis.Redis | None" = None,
    sleep=time.sleep,
    monotonic=time.monotonic,
    poll_interval_s: float = 3.0,
) -> None:
    """Block until ``vm_id``'s agent connection has stayed stable for a
    continuous ``settle_s`` — its liveness key present AND ``agent.lastConnectedAt``
    unchanged — or raise :class:`AgentUnreachableError` after ``timeout_s``.

    A freshly-cloned VM can phone home before its firstboot reboot applies the
    hostname; dispatching provisioning at that first phone-home would push a
    command into an agent the reboot is about to kill. This dwell rides through
    that reboot: the streak resets
    whenever the liveness key drops (reboot) or ``lastConnectedAt`` advances
    (reconnect), so only the final, stable boot accrues ``settle_s``."""
    r = client or transport._client
    deadline = monotonic() + timeout_s
    stable_lca: int | None = None
    stable_since: float | None = None
    while monotonic() < deadline:
        live = r.get(agent_conn_key(vm_id)) is not None
        doc = db["vm_registry"].find_one(
            {"agent.vmId": vm_id}, {"agent.lastConnectedAt": 1}
        )
        lca = ((doc or {}).get("agent") or {}).get("lastConnectedAt")
        now = monotonic()
        if not live or not isinstance(lca, int):
            stable_lca = None
            stable_since = None
        elif lca != stable_lca:
            # A (re)connect since we last looked — restart the dwell.
            stable_lca = lca
            stable_since = now
        elif stable_since is not None and now - stable_since >= settle_s:
            return
        sleep(poll_interval_s)
    raise AgentUnreachableError(
        f"agent '{vm_id}' connection did not stay stable for {settle_s}s "
        f"within {timeout_s}s"
    )


#: Gap between boot_info probes.
_BOOT_PROBE_INTERVAL_S = 20.0
#: Gap before the confirming second probe — must exceed both the current
#: SetupComplete shutdown delay and a legacy finalize's unregister→reboot
#: window (~15-20s), so a pre-reboot probe is always contradicted.
_BOOT_PROBE_CONFIRM_GAP_S = 45.0
#: Minimum uptime before a boot with no finalize task can count as settled.
_BOOT_UPTIME_FLOOR_S = 60
#: Per-dispatch timeout for one boot_info probe.
_BOOT_PROBE_TIMEOUT_S = 60
#: Liveness-key poll gap while the agent is offline mid-settle — the moment it
#: reconnects the next probe dispatches, instead of waiting out the probe
#: interval.
_BOOT_RECONNECT_POLL_S = 2.0
#: Tolerance when checking the confirm probe's uptime advanced consistently.
_BOOT_CONFIRM_SLACK_S = 10
#: Forced reboots before giving up on a finalize task that never unregisters.
_BOOT_FORCE_REBOOT_MAX = 2

#: The exact detail text an agent that predates ``system.boot_info`` returns —
#: pinned by an agent-side test; triggers the legacy-dwell fallback.
_BOOT_INFO_UNKNOWN = "unknown command 'system.boot_info'"


def wait_for_settled_boot(
    vm_id: str,
    *,
    db,
    timeout_s: int,
    role: str = "guest",
    job_key_prefix: str,
    on_phase: "Callable[[str], None] | None" = None,
    client: "redis.Redis | None" = None,
    sleep=time.sleep,
    monotonic=time.monotonic,
    dispatch=None,
) -> None:
    """Block until ``vm_id``'s VM is provably on its final, settled boot.

    Replaces the blind :func:`wait_for_stable_agent` dwell for provisioning:
    instead of inferring "boot settled" from connection stability (which any
    reconnect churn — e.g. a fresh clone's post-setup CPU storm — resets
    forever), this actively dispatches the read-tier ``system.boot_info``
    command and decides from facts:

    * finalize task still registered → a legacy image's intermediate boot;
      keep waiting. If it stays registered past
      ``settings.agent_boot_force_reboot_uptime_s`` (a missed ``-AtStartup``
      trigger) and isn't mid-run, dispatch ``system.reboot`` to recover it
      (capped at ``_BOOT_FORCE_REBOOT_MAX``).
    * finalize task absent + uptime past the floor → candidate settled boot,
      confirmed by a second probe ``_BOOT_PROBE_CONFIRM_GAP_S`` later on the
      *same* boot (uptime advanced consistently) — defeating both a current
      image's pre-reboot window and a legacy finalize's unregister→reboot
      window.
    * agent unreachable → mid-reboot; poll the liveness key and re-probe the
      moment it reconnects. A probe timeout keeps the settled-boot candidate —
      it says nothing about a reboot, and the same-boot uptime check catches a
      stale candidate on the next probe.
    * the agent doesn't know ``system.boot_info`` (legacy binary) → fall back
      to :func:`wait_for_stable_agent` for the remaining budget.

    Raises :class:`AgentUnreachableError` when ``timeout_s`` runs out, and
    :class:`DispatchError` when forced reboots can't clear the finalize task.
    ``on_phase(text)`` receives human-readable progress; observational only.
    """
    _dispatch = dispatch or dispatch_and_wait
    r = client or transport._client

    def _phase(text: str) -> None:
        if on_phase is not None:
            try:
                on_phase(text)
            except Exception:  # noqa: BLE001 — observational only
                logger.exception("on_phase callback failed")

    nonce = uuid.uuid4().hex[:8]
    deadline = monotonic() + timeout_s
    candidate: tuple[float, float] | None = None  # (uptime_s, monotonic_at)
    forced_reboots = 0
    attempt = 0
    while monotonic() < deadline:
        attempt += 1
        try:
            result = _dispatch(
                vm_id,
                "system.boot_info",
                {},
                job_id=f"{job_key_prefix}:bootinfo:{nonce}:{attempt}",
                role=role,
                timeout_s=_BOOT_PROBE_TIMEOUT_S,
                client=client,
            )
        except AgentUnreachableError:
            candidate = None
            _phase("Waiting for boot to settle — agent offline (rebooting)")
            # Poll the liveness key instead of blind-sleeping the probe
            # interval: the next probe dispatches (and the phase flips) within
            # ~2s of the agent reconnecting.
            while monotonic() < deadline:
                if r.get(agent_conn_key(vm_id)) is not None:
                    _phase("Agent back online — checking boot state")
                    break
                sleep(_BOOT_RECONNECT_POLL_S)
            continue
        except DispatchError as exc:
            if _BOOT_INFO_UNKNOWN in str(exc):
                _phase("Waiting for boot to settle (legacy agent)")
                remaining = max(1, int(deadline - monotonic()))
                return wait_for_stable_agent(
                    vm_id,
                    settle_s=settings.agent_boot_settle_s,
                    timeout_s=remaining,
                    db=db,
                    client=client,
                    sleep=sleep,
                    monotonic=monotonic,
                )
            # A probe timeout says nothing about a reboot — keep the candidate
            # (the same-boot uptime check already invalidates a stale one after
            # a real reboot) and keep the phase text moving so the UI never
            # freezes on the previous state.
            logger.warning("boot_info probe for %s failed: %s", vm_id, exc)
            _phase("Waiting for boot to settle — probe timed out, retrying")
            sleep(_BOOT_PROBE_INTERVAL_S)
            continue

        uptime = result.get("uptimeS")
        pending = result.get("finalizePending")
        running = result.get("finalizeRunning")
        if not isinstance(uptime, (int, float)) or not isinstance(pending, bool):
            candidate = None
            sleep(_BOOT_PROBE_INTERVAL_S)
            continue

        if pending:
            candidate = None
            _phase(f"Waiting for boot to settle — finalize pending (up {int(uptime)}s)")
            if (
                uptime >= settings.agent_boot_force_reboot_uptime_s
                and running is not True
            ):
                if forced_reboots >= _BOOT_FORCE_REBOOT_MAX:
                    raise DispatchError(
                        f"firstboot finalize on '{vm_id}' still pending after "
                        f"{forced_reboots} forced reboots"
                    )
                forced_reboots += 1
                _phase(
                    "Finalize still pending after "
                    f"{settings.agent_boot_force_reboot_uptime_s}s — "
                    "forcing a recovery reboot"
                )
                from app.core.db import now_ms

                since = now_ms()
                _dispatch(
                    vm_id,
                    "system.reboot",
                    {"delaySeconds": "5"},
                    job_id=f"{job_key_prefix}:bootkick:{nonce}:{attempt}",
                    role=role,
                    timeout_s=120,
                    expect_disconnect=True,
                    client=client,
                )
                wait_for_reconnect(
                    vm_id,
                    since,
                    max(1, int(deadline - monotonic())),
                    db=db,
                    sleep=sleep,
                )
            sleep(_BOOT_PROBE_INTERVAL_S)
            continue

        # Finalize task absent. This is normal for new single-reboot images,
        # including the short window after SetupComplete schedules that reboot.
        if uptime < _BOOT_UPTIME_FLOOR_S:
            candidate = None
            _phase(f"Waiting for boot to settle — booted {int(uptime)}s ago")
            sleep(_BOOT_PROBE_INTERVAL_S)
            continue

        if candidate is None:
            # First settled-looking probe. It could be the brief window before
            # a new image's scheduled reboot, or an old image's
            # unregister→reboot window. Confirm on the SAME boot after a gap
            # longer than either window.
            candidate = (float(uptime), monotonic())
            _phase(f"Boot looks settled (up {int(uptime)}s) — confirming")
            sleep(_BOOT_PROBE_CONFIRM_GAP_S)
            continue

        first_uptime, first_at = candidate
        elapsed = monotonic() - first_at
        if uptime >= first_uptime + elapsed - _BOOT_CONFIRM_SLACK_S:
            return  # same boot, uptime advanced consistently → settled
        # Uptime went backwards: a firstboot reboot happened between probes.
        candidate = None
        sleep(_BOOT_PROBE_INTERVAL_S)

    raise AgentUnreachableError(
        f"agent '{vm_id}' boot did not settle within {timeout_s}s"
    )


# --------------------------------------------------------------------------- #
# API side (async)                                                            #
# --------------------------------------------------------------------------- #
async def mark_agent_live(vm_id: str) -> None:
    """Record that ``vm_id``'s agent is connected: a short-TTL liveness key
    (refreshed by the connect keepalive) plus ``agent.lastConnectedAt`` on the
    registry doc (the worker's reboot-resume signal)."""
    from app.core.db import now_ms, vm_registry_col

    client = transport.make_async_client()
    try:
        await client.set(agent_conn_key(vm_id), "1", ex=AGENT_CONN_TTL_SECONDS)
    finally:
        await client.aclose()
    await vm_registry_col().update_one(
        {"agent.vmId": vm_id},
        {"$set": {"agent.lastConnectedAt": now_ms()}},
    )


async def refresh_agent_live(vm_id: str) -> None:
    """Re-arm the liveness TTL (called on the connect keepalive tick)."""
    client = transport.make_async_client()
    try:
        await client.set(agent_conn_key(vm_id), "1", ex=AGENT_CONN_TTL_SECONDS)
    finally:
        await client.aclose()


async def clear_agent_live(vm_id: str) -> None:
    """Drop the liveness key on a clean disconnect (best-effort; the TTL is the
    backstop for an unclean one)."""
    client = transport.make_async_client()
    try:
        await client.delete(agent_conn_key(vm_id))
    finally:
        await client.aclose()


async def run_dispatch_subscriber() -> None:
    """Lifespan task: forward ``agent-dispatch`` requests to whichever socket
    this process holds. Runs until cancelled at shutdown."""
    from app.core import agents

    client = transport.make_async_client()
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(DISPATCH_CHANNEL)
    logger.info("agent-dispatch subscriber started")
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                request = json.loads(message["data"])
            except ValueError, KeyError:
                logger.warning("malformed agent-dispatch frame")
                continue
            vm_id = request.get("vm_id")
            agent = agents.resolve_agent(vm_id) if vm_id else None
            if agent is None:
                continue  # another process holds this socket (or none does)
            try:
                await agent.send(
                    {
                        "job_id": request["job_id"],
                        "command": request["command"],
                        "params": request.get("params") or {},
                        "role": request.get("role") or "guest",
                    }
                )
            except Exception:  # noqa: BLE001 — a dead socket shouldn't kill the loop
                logger.warning("failed to forward dispatch to agent %s", vm_id)
    finally:
        await pubsub.aclose()
        await client.aclose()
