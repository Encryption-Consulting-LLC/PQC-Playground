"""FastAPI application factory.

Creates the app, registers error handlers, and mounts the /api router.
The module-level ``app`` binding is what uvicorn targets via "app.main:app"
(see ``cli.py``).

The lifespan owns the Mongo client: created here (not at import) so it binds
to uvicorn's event loop, and fail-fast — an unreachable Mongo aborts boot
rather than 503ing every persistence call later.

A missing shared ESXi target is non-fatal: the deploy boots degraded and VM
routes 503 until an operator sets one via PUT /api/settings.
"""

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.agentbus import run_dispatch_subscriber
from app.core.db import close_db, init_db
from app.core.errors import register_exception_handlers
from app.core.esxi import load_target
from app.core.secrets import SecretDecryptionError
from app.routers import api_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    try:
        target = await load_target()
    except SecretDecryptionError:
        # Boot degraded: an operator re-sets the password via PUT /api/settings.
        target = None
        logger.error(
            "Stored ESXi password cannot be decrypted — SETTINGS_ENC_KEY changed? "
            "Re-set the password via PUT /api/settings."
        )
    if target is None:
        logger.warning(
            "No shared ESXi target configured yet — VM routes will 503 until an "
            "operator sets one via PUT /api/settings."
        )
    # Forward worker→agent dispatch requests to whichever socket this
    # process holds (the plan runner's command bridge). Runs for the app's life.
    dispatch_task = asyncio.create_task(run_dispatch_subscriber())
    try:
        yield
    finally:
        dispatch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatch_task
        await close_db()


def _frontend_dist() -> Path:
    """Location of the built SPA. Overridable for split API/static deploys;
    defaults to the sibling ``frontend/dist`` in a full checkout."""
    override = os.environ.get("FRONTEND_DIST")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def _admin_dist() -> Path:
    """Location of the built admin SPA. Overridable for split deploys;
    defaults to the sibling ``admin/dist`` in a full checkout."""
    override = os.environ.get("ADMIN_DIST")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "admin" / "dist"


def _shell(index: Path) -> FileResponse:
    """A SPA's ``index.html``, explicitly uncacheable.

    The assets beside it are content-hashed and may be cached forever; the shell
    is the one file that must not be, because it is what names them. Without a
    ``Cache-Control`` a browser applies heuristic freshness from `Last-Modified`
    and can serve yesterday's shell — pointing at yesterday's bundle — without
    revalidating. A deploy then looks like it did not happen: the fix is on the
    box, the origin serves the new shell, and the tab keeps running the old one.
    """
    return FileResponse(index, headers={"Cache-Control": "no-store, must-revalidate"})


def _mount_admin(app: FastAPI) -> None:
    """Serve the built admin SPA at ``/admin``, same origin as the API.

    Registered *before* ``_mount_frontend``'s greedy ``/{path:path}`` catch-all
    so ``/admin`` isn't swallowed by the operator SPA's fallback first. Real
    files are served as-is; every other ``/admin/...`` path falls back to the
    admin build's ``index.html`` for client-side routing (it is a separate
    Vite app, built with ``base: "/admin/"``). No-op when the build is absent.
    """
    dist = _admin_dist()
    index = dist / "index.html"
    if not index.is_file():
        logger.info("No admin build at %s — /admin unavailable.", dist)
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/admin/assets", StaticFiles(directory=assets), name="admin-assets")

    @app.get("/admin", include_in_schema=False)
    async def _admin_root() -> FileResponse:
        return _shell(index)

    @app.get("/admin/{path:path}", include_in_schema=False)
    async def _admin_catch_all(path: str) -> FileResponse:
        candidate = dist / path
        if candidate.is_file() and candidate.resolve().is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return _shell(index)


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA from the same origin as the API.

    Registered last, so ``/api``, ``/docs``, ``/openapi.json``, and ``/admin``
    (added before this call) always win. Real files are served as-is; every
    other non-``/api`` path falls back to ``index.html`` for client-side
    routing. No-op when the build is absent (dev without ``pnpm build``, or
    tests).
    """
    dist = _frontend_dist()
    index = dist / "index.html"
    if not index.is_file():
        logger.info("No frontend build at %s — API-only.", dist)
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    async def _spa_root() -> FileResponse:
        return _shell(index)

    @app.get("/{path:path}", include_in_schema=False)
    async def _spa_catch_all(path: str) -> FileResponse:
        if path.startswith("api"):
            # Unmatched API paths are 404s, not SPA shell.
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = dist / path
        if candidate.is_file() and candidate.resolve().is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return _shell(index)


def create_app() -> FastAPI:
    app = FastAPI(
        title="pki-deploy-api",
        version="0.1.0",
        description="HTTP API for VM/PKI deployment.",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(api_router)
    _mount_admin(app)
    _mount_frontend(app)
    return app


app = create_app()
