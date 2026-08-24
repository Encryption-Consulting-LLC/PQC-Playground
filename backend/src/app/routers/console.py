"""Remote desktop: authorize a session, hand back a ticket to open it with.

Two doors, mirroring the teardown console's split. ``POST /console/{vm_name}``
is the self-service one (``VM_CONSOLE``, operator + guest, ownership-scoped), and
``POST /admin/console/{vm_name}`` is the oversight one (``VM_CONSOLE_ADMIN``,
admin-only), which crosses the ownership boundary an admin otherwise has no way
past — the same reason ``admin_teardown`` exists. Admins hold no ``VM_CONSOLE``
and the canvas roles hold no ``VM_CONSOLE_ADMIN``, so the crossing surface is
exactly this one route.

Neither route opens anything. They answer "may this person see this desktop, and
does the server know a credential for it", then park the answer in a single-use
ticket (``core.console.tickets``) for ``ws /api/ws/console/{ticket_id}`` to
redeem. The split exists because the credentials must not travel through the
browser, and a WebSocket upgrade cannot carry them any other way.

The refusals are deliberately specific, because "Connect did nothing" is the
least useful thing this feature could say:

* **404** — no registry row (or a deleted one). For a guest this is also what a
  VM outside their namespace looks like, via ``enforce_guest_vm_ownership``.
* **409** — the VM exists but no session is possible, and the detail says which:
  still cloning, no address yet, a Linux template, or no stored credential (the
  pre-console fleet, which needs a redeploy — nothing can set a password inside
  an already-running guest).
* **503** — guacd is unreachable. The feature is down, the VM is fine.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.authz import (
    AuthedUser,
    Capability,
    get_current_user,
    require_capability,
)
from app.core.console import tickets
from app.core.console.credentials import resolve_console_credentials
from app.core.console.guacd import GuacdError, probe
from app.core.console.sessions import at_capacity
from app.core.db import vm_registry_col
from app.core.labs import enforce_own_or_joined_vm
from app.core.settings import settings

router = APIRouter(prefix="/console", tags=["console"])
#: The oversight door lives under ``/admin/*`` like every other admin-only route,
#: so the admin SPA's URL tree stays uniform.
admin_router = APIRouter(prefix="/admin/console", tags=["console"])

#: The size guacd is told to negotiate. The client resizes to the real viewport
#: immediately afterwards (``resize-method=display-update``), so this only has to
#: be a sane first frame, not the user's actual window.
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800


class ConsoleTicketResponse(BaseModel):
    """What the browser needs to open the session — and nothing more.

    Note what is absent: the username, the password, and the guest's address.
    ``credentialLabel`` is display text ("ENCON\\Administrator"), so the panel
    can say who the session will sign in as without the client ever holding the
    credential itself. camelCase on the wire, like every other route the two SPAs
    call.
    """

    model_config = ConfigDict(populate_by_name=True)

    ticket_id: str = Field(alias="ticketId")
    vm_name: str = Field(alias="vmName")
    credential_label: str = Field(alias="credentialLabel")
    expires_in_seconds: int = Field(alias="expiresInSeconds")


async def _mint_for(vm_name: str, user: AuthedUser) -> ConsoleTicketResponse:
    """Shared body of both routes. The *only* difference between them is whether
    ``enforce_guest_vm_ownership`` ran, so the checks below cannot drift apart."""
    row = await vm_registry_col().find_one({"vmName": vm_name})
    if row is None or row.get("status") == "deleted":
        raise HTTPException(404, detail=f"No VM named '{vm_name}'.")

    if row.get("status") == "cloning":
        raise HTTPException(
            409, detail="This VM is still being created — try again once it is ready."
        )
    host = row.get("ip")
    if not host:
        raise HTTPException(
            409, detail="This VM has no address yet, so there is nothing to connect to."
        )

    creds = resolve_console_credentials(row)
    if creds is None:
        raise HTTPException(
            409,
            detail=(
                "No stored credentials for this VM. Remote desktop needs a "
                "password set at first boot, so redeploy this node to enable it."
            ),
        )

    if at_capacity():
        raise HTTPException(
            503,
            detail=(
                f"All {settings.console_max_sessions} remote desktop sessions are "
                "in use. Close one and try again."
            ),
        )

    # Fail here rather than after the browser has opened a socket: a preflight
    # 503 renders as "the service is unavailable", where the same failure one
    # step later renders as a blank overlay that never resolves.
    try:
        await probe(host=settings.guacd_host, port=settings.guacd_port)
    except GuacdError as exc:
        raise HTTPException(
            503, detail="The remote desktop service is unavailable."
        ) from exc

    ticket_id = await tickets.mint(
        vm_name=vm_name, owner=user.username, host=host, creds=creds
    )
    return ConsoleTicketResponse(
        ticket_id=ticket_id,
        vm_name=vm_name,
        credential_label=creds.label,
        expires_in_seconds=settings.console_ticket_ttl_s,
    )


@router.post(
    "/{vm_name}",
    dependencies=[Depends(require_capability(Capability.VM_CONSOLE))],
)
async def mint_console_ticket(
    vm_name: str, user: AuthedUser = Depends(get_current_user)
) -> ConsoleTicketResponse:
    """Authorize a session on one's own VM, or on one in a lab one has joined."""
    await enforce_own_or_joined_vm(vm_name, user)
    return await _mint_for(vm_name, user)


@admin_router.post(
    "/{vm_name}",
    dependencies=[Depends(require_capability(Capability.VM_CONSOLE_ADMIN))],
)
async def mint_admin_console_ticket(
    vm_name: str, user: AuthedUser = Depends(get_current_user)
) -> ConsoleTicketResponse:
    """Authorize a session on anyone's VM, from the /admin console.

    No ownership check — that is the whole point of the capability. An admin
    diagnosing an abandoned lab before reclaiming it has no other way in, and
    holds no ``VM_CONSOLE`` to reach this by the other door.
    """
    return await _mint_for(vm_name, user)
