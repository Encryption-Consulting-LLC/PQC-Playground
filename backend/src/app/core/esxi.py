"""Shared ESXi target and the managed org-wide connection.

There is one org-wide ESXi target stored in the Mongo settings document
(password AES-GCM-encrypted, ``core/secrets``). Every authenticated user's VM
operations run against this single target.

Connection handling is a managed shared connection, not per-request: opening
a pyVmomi session is a full TLS handshake + login, so ``ConnectionManager``
keeps one live ``vmkit.Connection`` behind a lock and hands it out to every
request. It reopens when

  * the stored target changes (an admin ``PUT /settings`` edit takes effect on
    the next request — no restart, no explicit invalidation hook), or
  * a periodic liveness probe (``CurrentTime``, at most every 60s) finds the
    session dead (ESXi idle-timeout, host restart).

``get_esxi`` is the request dependency that replaced ``core/sessions``'s
``get_session``: same ``Connection`` return type, so routes swapped over
without signature changes. The blocking open/probe runs in Starlette's
threadpool, never on the event loop.

The Celery worker can't share this process's connection object; it opens its
own per task via ``load_target_sync`` (a short-lived sync PyMongo read) —
which is also what removed the old "real clones only work if the worker has
``ESXI_*`` env vars" wart.
"""

import logging
import threading
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException
from pymongo import MongoClient
from pyVim.connect import Disconnect
from pyVmomi import vim
from starlette.concurrency import run_in_threadpool
from vmkit import Connection, open_connection
from vmkit.errors import AuthenticationError, VmkitError

from app.core.authz import AuthedUser, get_current_user
from app.core.db import SETTINGS_DOC_ID, settings_col
from app.core.secrets import decrypt_secret
from app.core.settings import settings

_PROBE_INTERVAL_SECONDS = 60.0
_ROOT_VIEW_RETRY_DELAY_SECONDS = 1.0

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EsxiTarget:
    host: str
    user: str
    password: str
    port: int = 443


def _is_root_view_denial(exc: BaseException) -> bool:
    """Whether vmkit wrapped ESXi's transient root ``System.View`` denial.

    ``vmkit.connect`` translates every non-login failure raised by
    ``SmartConnect`` into ``ConnectionFailedError``. Python retains the
    original pyVmomi fault as its exception context, so inspect the chain
    instead of matching vmkit's rendered error text.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, vim.fault.NoPermission):
            object_id = getattr(getattr(current, "object", None), "_moId", None)
            return (
                current.privilegeId == "System.View" and object_id == "ha-folder-root"
            )
        current = current.__cause__ or current.__context__
    return False


def _target_from_doc(doc: dict | None) -> EsxiTarget | None:
    """Settings document → EsxiTarget; None while host/user/password are unset."""
    if not doc:
        return None
    host, user, enc = (
        doc.get("esxiHost"),
        doc.get("esxiUser"),
        doc.get("esxiPasswordEnc"),
    )
    if not (host and user and enc):
        return None
    return EsxiTarget(
        host=host,
        user=user,
        password=decrypt_secret(enc),
        port=doc.get("esxiPort") or 443,
    )


async def load_target() -> EsxiTarget | None:
    """Read the shared target from the settings document (API process)."""
    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    return _target_from_doc(doc)


def load_target_sync() -> EsxiTarget | None:
    """Worker-process variant: short-lived sync client, since the async client
    is API-process/event-loop-bound."""
    client: MongoClient = MongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000)
    try:
        doc = client[settings.mongo_db]["settings"].find_one({"_id": SETTINGS_DOC_ID})
    finally:
        client.close()
    return _target_from_doc(doc)


class ConnectionManager:
    """One lazily-opened shared ``vmkit.Connection``, reopened on target change
    or failed liveness probe. All methods are thread-safe; ``get`` blocks (call
    it from a threadpool in async contexts)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Connection | None = None
        self._target: EsxiTarget | None = None
        self._probed_at = 0.0

    def get(self, target: EsxiTarget) -> Connection:
        with self._lock:
            if self._conn is not None and target == self._target and self._alive():
                return self._conn
            if self._conn is not None:
                try:
                    Disconnect(self._conn.si)
                except Exception:  # noqa: BLE001 — the old session may already be dead
                    pass
            self._conn = None
            self._target = None
            try:
                conn = open_connection(
                    target.host, target.user, target.password, target.port
                )
            except Exception as exc:
                # ESXi can briefly authenticate a newly granted/updated user
                # before its root-folder authorization is visible to the SOAP
                # session. The next login succeeds once that propagation lands.
                # Retry only this precise fault: bad credentials, unreachable
                # hosts, and other inventory permission failures keep their
                # normal single-attempt behavior.
                if not _is_root_view_denial(exc):
                    raise
                log.warning(
                    "ESXi denied System.View on the root folder during login; "
                    "retrying the connection once"
                )
                time.sleep(_ROOT_VIEW_RETRY_DELAY_SECONDS)
                conn = open_connection(
                    target.host, target.user, target.password, target.port
                )
            self._conn = conn
            self._target = target
            self._probed_at = time.monotonic()
            return self._conn

    def _alive(self) -> bool:
        """Cheap staleness probe, rate-limited so hot paths skip the round trip."""
        now = time.monotonic()
        if now - self._probed_at < _PROBE_INTERVAL_SECONDS:
            return True
        try:
            self._conn.si.CurrentTime()  # type: ignore[union-attr]
            self._probed_at = now
            return True
        except Exception:  # noqa: BLE001 — any failure means "reopen"
            return False


manager = ConnectionManager()


async def connect_to_target(target: EsxiTarget) -> Connection:
    """Open (or reuse) the shared connection to *target*, as an HTTP error.

    ESXi connection failures are surfaced as 502 (bad *downstream* gateway),
    **including bad stored ESXi credentials** — an ESXi ``AuthenticationError``
    is deliberately never mapped to 401. 401 is reserved for the caller's own
    session being invalid; the frontend auto-logs-out on any 401, so letting an
    ESXi-credential failure reach the client as 401 would kick the signed-in
    admin out mid-edit while they're fixing the target. The detail names the
    real cause either way.

    Every route that reaches for a connection outside the ``get_esxi``
    dependency goes through this — the deploy preflight used to call
    ``manager.get`` bare, which is exactly how a rejected ESXi login became a
    session-ending 401 on a retried deploy.
    """
    try:
        return await run_in_threadpool(manager.get, target)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ESXi login failed for the configured target: {exc}",
        ) from exc
    except VmkitError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach the configured ESXi target: {exc}",
        ) from exc


async def get_esxi(_user: AuthedUser = Depends(get_current_user)) -> Connection:
    """FastAPI dependency: the shared org-target ``Connection``.

    Authenticated (identity resolution is per-request-cached, so pairing with
    ``require_capability`` costs nothing extra); 503 until an admin has
    configured the target, 502 on any connection failure (see
    :func:`connect_to_target`).
    """
    target = await load_target()
    if target is None:
        raise HTTPException(
            status_code=503,
            detail="No shared ESXi target configured",
        )
    return await connect_to_target(target)
