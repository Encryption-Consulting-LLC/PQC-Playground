"""Single-use console tickets: the handoff from an authorized HTTP request to a
WebSocket that can actually carry the session.

Why a ticket at all, when the socket already authenticates the user? Because the
socket needs three things the browser must never hold: the guest's IP, the RDP
account, and its password. Browsers cannot set headers on a WebSocket upgrade
(which is why every socket in this app takes ``?token=``), so anything the client
carries ends up in a query string — in proxy logs, in history, in a referrer.

So the HTTP route does the authorization and the credential lookup, parks the
result in Valkey under an opaque id, and hands the browser only that id. The
socket redeems it. Two properties make that safe to hand out:

* **Single use.** Redemption is ``GETDEL``, so a replayed id gets nothing. A
  reconnect mints a fresh ticket, which re-runs the full authorization path
  rather than reusing a decision made minutes ago.
* **Short-lived.** ``CONSOLE_TICKET_TTL_S`` (default 60s) — the browser opens the
  socket immediately, so a longer window only makes a leaked id more useful.

The ticket still records ``owner``, and the socket re-checks it against the
authenticated user. Belt and braces: the id is unguessable, but an id is a
bearer token and this way holding one is not sufficient — you must also be the
person it was minted for, with a live, still-permitted session.

Valkey rather than process memory for the same reason job snapshots live there:
it is already a hard dependency, it gives expiry for free, and it does not pin
the feature to one API process.
"""

import json
import secrets
from dataclasses import asdict, dataclass

from app.core.console.credentials import ConsoleCredentials
from app.core.jobs.transport import make_async_client
from app.core.settings import settings

_KEY_PREFIX = "console:ticket:"


def _key(ticket_id: str) -> str:
    return f"{_KEY_PREFIX}{ticket_id}"


@dataclass(frozen=True)
class ConsoleTicket:
    """A redeemed session request. ``credentials`` is plaintext in Valkey for the
    ticket's lifetime — the same trade the firstboot ISO already makes for this
    password, and bounded to a minute rather than the VM's lifetime."""

    vm_name: str
    owner: str
    host: str
    credentials: ConsoleCredentials


async def mint(
    *, vm_name: str, owner: str, host: str, creds: ConsoleCredentials
) -> str:
    """Store a ticket and return its id. ``token_urlsafe(32)`` — an id that is
    guessable is the whole attack, and this is cheap."""
    ticket_id = secrets.token_urlsafe(32)
    payload = json.dumps(
        {
            "vmName": vm_name,
            "owner": owner,
            "host": host,
            "credentials": asdict(creds),
        }
    )
    client = make_async_client()
    try:
        await client.set(_key(ticket_id), payload, ex=settings.console_ticket_ttl_s)
    finally:
        await client.aclose()
    return ticket_id


async def redeem(ticket_id: str) -> ConsoleTicket | None:
    """Consume *ticket_id*, or ``None`` if it is unknown, expired, or already used.

    ``GETDEL`` is what makes those three indistinguishable to a caller, which is
    the point: a replay and an expiry should look the same from outside.
    """
    client = make_async_client()
    try:
        raw = await client.getdel(_key(ticket_id))
    finally:
        await client.aclose()
    if raw is None:
        return None
    data = json.loads(raw)
    return ConsoleTicket(
        vm_name=data["vmName"],
        owner=data["owner"],
        host=data["host"],
        credentials=ConsoleCredentials(**data["credentials"]),
    )
