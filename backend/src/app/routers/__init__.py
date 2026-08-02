"""Aggregates all routers under the /api prefix.

Import ``api_router`` and call ``app.include_router(api_router)`` in the app
factory. Adding a new feature area means: create ``routers/foo.py`` with a
``router = APIRouter(...)`` and include it here.
"""

from fastapi import APIRouter

from app.routers import (
    admin_deployments,
    admin_teardown,
    admin_users,
    auth,
    config,
    deploy,
    ip_pool,
    iso,
    meta,
    executor,
    project_shares,
    projects,
    settings,
    vm,
    vm_registry,
    ws,
)

api_router = APIRouter(prefix="/api")

for _router in (
    meta.router,
    config.router,
    auth.router,
    admin_users.router,
    admin_deployments.router,
    admin_teardown.router,
    vm.router,
    deploy.router,
    iso.router,
    project_shares.router,
    projects.router,
    vm_registry.router,
    ip_pool.router,
    settings.router,
    executor.router,
    ws.router,
):
    api_router.include_router(_router)
