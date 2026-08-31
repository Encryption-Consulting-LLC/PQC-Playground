"""vmkit and pymongo typed errors → HTTP status codes.

``map_vmkit_error`` is the single source of truth for the status/detail a vmkit
exception maps to. It is reused in two places:

* ``register_exception_handlers(app)`` — wires it to FastAPI so HTTP routes that
  let a vmkit error propagate get the right status, as a ``{"detail": ...}`` body
  matching FastAPI/Pydantic's own error format (one error-parsing branch for callers).
* ``app.tasks`` — the clone Celery task runs off-request in a separate worker
  process, where there is no HTTP response to attach a handler to, so it maps the
  error itself into a terminal progress message published over the job transport.

The table is ordered most-specific first; lookup walks it by ``isinstance`` so the
base-class catch-all (``VmkitError`` → 500) fires only when nothing else matches.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    ExecutionTimeout,
    NetworkTimeout,
    PyMongoError,
    ServerSelectionTimeoutError,
)

from app.core.authz import Role
from app.core.guest_errors import guest_http_detail

from vmkit.errors import (
    AuthenticationError,
    ConnectionFailedError,
    InsufficientSpaceError,
    ValidationError,
    VmExistsError,
    VmkitError,
    VmNotFoundError,
)

# Most-specific first; VmkitError (base) is the catch-all and must stay last.
#
# ``AuthenticationError`` is 502, never 401: vmkit raises it from one place, a
# rejected *ESXi* login, so it says nothing about the caller's own session. 401
# is reserved for that — the frontend clears the session on one, and mapping a
# downstream credential failure onto it logged the operator out mid-deploy. See
# ``core.esxi.get_esxi``, which has argued the same in prose since before this.
_ERROR_STATUS: tuple[tuple[type[VmkitError], int], ...] = (
    (ValidationError, 422),
    (AuthenticationError, 502),
    (ConnectionFailedError, 502),
    (VmExistsError, 409),
    (VmNotFoundError, 404),
    (InsufficientSpaceError, 409),
    (VmkitError, 500),
)


def map_vmkit_error(exc: VmkitError) -> tuple[int, str]:
    """Return the ``(status_code, detail)`` for a vmkit exception."""
    for exc_type, status in _ERROR_STATUS:
        if isinstance(exc, exc_type):
            return status, str(exc)
    return 500, str(exc)


# Most-specific first; PyMongoError (base) is the catch-all and must stay last.
# ServerSelectionTimeoutError subclasses ConnectionFailure — keep it first.
# Details are fixed strings, not str(exc): pymongo messages can leak the
# connection string / host internals.
_MONGO_ERROR_STATUS: tuple[tuple[type[PyMongoError], int, str], ...] = (
    (DuplicateKeyError, 409, "Resource already exists."),
    (ServerSelectionTimeoutError, 503, "Database unavailable."),
    (ConnectionFailure, 503, "Database unavailable."),
    (NetworkTimeout, 504, "Database timed out."),
    (ExecutionTimeout, 504, "Database timed out."),
    (PyMongoError, 500, "Database error."),
)


def map_mongo_error(exc: PyMongoError) -> tuple[int, str]:
    """Return the ``(status_code, detail)`` for a pymongo exception."""
    for exc_type, status, detail in _MONGO_ERROR_STATUS:
        if isinstance(exc, exc_type):
            return status, detail
    return 500, "Database error."


def is_guest(request: Request) -> bool:
    """Whether the caller of *request* is a guest, for message narrowing only.

    ``get_current_user`` records the role it resolved on ``request.state``; it is
    a sub-dependency of ``require_capability``, so on every authenticated route
    it has run before the router body, before ``get_esxi``, and before body
    validation. Absent (an unauthenticated route, a static asset, the SPA
    catch-all) means "not a guest", which is the honest answer — there is no
    account to narrow for, and those paths raise no internals.

    The one gap is a *malformed* request body, which FastAPI parses before it
    solves dependencies: that 400/422 is raised with no role recorded. Its text
    is FastAPI's own and names nothing about this deployment, so it is left
    alone rather than paid for with a token-decoding middleware.

    Note for tests: overriding ``get_current_user`` through
    ``app.dependency_overrides`` replaces the dependency wholesale and so skips
    the ``request.state`` write. A test that authenticates that way exercises
    the un-narrowed path no matter what role it names — monkeypatch
    ``authz.resolve_user_token`` instead.
    """

    return getattr(request.state, "role", None) is Role.GUEST


def register_exception_handlers(app: FastAPI) -> None:
    """Attach a single vmkit-error→HTTP handler to *app*.

    Registered on the ``VmkitError`` base so FastAPI dispatches every subclass to
    it; the concrete status comes from ``map_vmkit_error``.

    Also the one place a guest's error text is narrowed (``core.guest_errors``),
    which is why the generic ``HTTPException`` handler is overridden at all —
    routers raise ~120 of those and none of them should have to know who is
    asking. Every branch delegates to FastAPI's own handler rather than building
    a response: that is what preserves ``exc.headers`` — the
    ``WWW-Authenticate: Session`` marker is what the frontend clears its session
    on, and losing it strands an expired guest on a wall of 401s with no login
    form — and the no-body rule for 204/304.
    """

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if is_guest(request):
            exc = HTTPException(
                status_code=exc.status_code,
                detail=guest_http_detail(exc.status_code, exc.detail),
                headers=getattr(exc, "headers", None),
            )
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        # The default body is a list of pydantic dicts naming internal field
        # paths (``body.ops.3.params.vmName``). A guest gets the one sentence.
        if is_guest(request):
            return JSONResponse(
                status_code=422, content={"detail": guest_http_detail(422, None)}
            )
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(VmkitError)
    async def _vmkit_error(request: Request, exc: VmkitError) -> JSONResponse:
        status, detail = map_vmkit_error(exc)
        if is_guest(request):
            detail = guest_http_detail(status, detail)
        return JSONResponse(status_code=status, content={"detail": detail})

    @app.exception_handler(PyMongoError)
    async def _mongo_error(request: Request, exc: PyMongoError) -> JSONResponse:
        status, detail = map_mongo_error(exc)
        if is_guest(request):
            detail = guest_http_detail(status, detail)
        return JSONResponse(status_code=status, content={"detail": detail})

    # Deferred import — core.secrets needs SETTINGS_ENC_KEY at call time and
    # this module is imported by tooling that may lack the full env.
    from app.core.secrets import SecretDecryptionError

    @app.exception_handler(SecretDecryptionError)
    async def _secret_error(
        request: Request, exc: SecretDecryptionError
    ) -> JSONResponse:
        detail = "Stored ESXi password cannot be decrypted (SETTINGS_ENC_KEY changed?)"
        if is_guest(request):
            detail = guest_http_detail(503, detail)
        return JSONResponse(status_code=503, content={"detail": detail})
