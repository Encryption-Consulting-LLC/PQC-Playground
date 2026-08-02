"""Guest IP allocation pool.

One document per *assignable* address in the operator-configured guest range
(``guest*`` fields on the settings singleton) — subnet network and broadcast
addresses are filtered out by ``is_assignable`` and never seeded. Pre-seeding
makes allocation a single atomic ``find_one_and_update`` — no next-free
computation, no race between concurrent worker tasks, and exhaustion is simply
"no document matched".

Document shape (collection ``ip_pool``)::

    _id: "10.0.20.51"        # the address — natural unique key
    ord: 167777331           # int(IPv4Address) — numeric sort key
    status: "free" | "allocated"
    vmName / jobId / allocatedAt: allocation record (None while free)
    updatedAt: epoch ms

Async functions run in the API process (seeding at startup and on settings
edits, the inspect route); ``*_sync`` functions run in the Celery worker over
the ``core/db/sync.worker_db`` database.
"""

from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any

from pymongo import ASCENDING, ReturnDocument, UpdateOne

from app.core.db.client import SETTINGS_DOC_ID, ip_pool_col, settings_col
from app.core.db.models import now_ms
from app.core.settings import settings


def _max_pool_size() -> int:
    """Sanity cap so a typo'd range can't seed a runaway collection. Read at
    call time (not import) so ``GUEST_POOL_MAX_SIZE`` env edits take effect and
    tests can override it."""
    return settings.guest_pool_max_size


class IpPoolExhaustedError(Exception):
    """Every address in the guest pool is allocated."""


@dataclass(frozen=True)
class GuestNetwork:
    """The validated guest subnet config a deploy needs. ``dns1`` is required
    because configgen's static ``NetworkConfig`` requires a primary DNS."""

    ip_start: str
    ip_end: str
    prefix: int
    gateway: str
    dns1: str
    dns2: str | None = None
    dns_suffix: str | None = None


def guest_network_from_doc(doc: dict | None) -> GuestNetwork | None:
    """Settings document → GuestNetwork; None until start/end/gateway/dns1 are
    all set (mirrors ``core/esxi._target_from_doc``)."""
    if not doc:
        return None
    start, end = doc.get("guestIpStart"), doc.get("guestIpEnd")
    gateway, dns1 = doc.get("guestGateway"), doc.get("guestDns1")
    if not (start and end and gateway and dns1):
        return None
    return GuestNetwork(
        ip_start=start,
        ip_end=end,
        prefix=doc.get("guestPrefix") or 24,
        gateway=gateway,
        dns1=dns1,
        dns2=doc.get("guestDns2") or None,
        dns_suffix=doc.get("guestDnsSuffix") or None,
    )


def is_assignable(ip: int, prefix: int) -> bool:
    """False for an address that is the network or broadcast address of its
    ``/prefix`` subnet — a guest can never hold one of those.

    The check is per-subnet, not per-octet: on a /24 that's the familiar ``.0``
    and ``.255``, but a range spanning a /23 keeps the ``.255``/``.0`` pair in
    the middle, and a /25 also drops ``.127``/``.128``. Prefixes 31 and 32 have
    no reserved addresses (RFC 3021 point-to-point / single host), so every
    address stays assignable there.
    """
    if prefix >= 31:
        return True
    host_mask = (1 << (32 - prefix)) - 1
    host_bits = ip & host_mask
    return host_bits != 0 and host_bits != host_mask


def range_ips(start: str, end: str, prefix: int) -> list[str]:
    """Expand an inclusive IPv4 range into its assignable addresses, skipping
    the network/broadcast address of every ``/prefix`` subnet the range touches.

    Raises ValueError on a malformed, oversized, or entirely-reserved range
    (callers surface it as a 422 on operator input). The size cap is applied to
    the raw span, before filtering — it guards against a typo'd range seeding a
    runaway collection, so it must not be softened by the exclusions.
    """
    if not 1 <= prefix <= 32:
        raise ValueError(f"Guest prefix /{prefix} is not between 1 and 32.")
    lo, hi = IPv4Address(start), IPv4Address(end)
    if hi < lo:
        raise ValueError(f"Guest IP range end {end} is below start {start}.")
    size = int(hi) - int(lo) + 1
    max_size = _max_pool_size()
    if size > max_size:
        raise ValueError(f"Guest IP range spans {size} addresses (max {max_size}).")
    ips = [
        str(IPv4Address(n))
        for n in range(int(lo), int(hi) + 1)
        if is_assignable(n, prefix)
    ]
    if not ips:
        raise ValueError(
            f"Guest IP range {start}-{end} contains no assignable addresses on a "
            f"/{prefix} — every address in it is a network or broadcast address."
        )
    return ips


def validate_network(net: GuestNetwork) -> None:
    """Raise ValueError on a malformed range, gateway, or DNS address —
    operator input is rejected here (422) rather than failing a deploy later."""
    range_ips(net.ip_start, net.ip_end, net.prefix)
    for label, value in (
        ("gateway", net.gateway),
        ("dns1", net.dns1),
        ("dns2", net.dns2),
    ):
        if not value:
            continue
        try:
            IPv4Address(value)
        except ValueError:
            raise ValueError(
                f"Guest {label} '{value}' is not a valid IPv4 address."
            ) from None


# --------------------------------------------------------------------------- #
# API process (async)                                                         #
# --------------------------------------------------------------------------- #
async def sync_pool_async(net: GuestNetwork | None) -> None:
    """Reconcile the pool with the configured range: seed missing assignable
    addresses as free, drop free addresses that fell out of it. Allocated
    documents are never touched — an out-of-range allocation lives until
    released, which also covers an address seeded before it was reclassified as
    network/broadcast: it stays with its VM and simply isn't re-seeded once freed.

    ``net=None`` (range unconfigured/cleared) drops every free document, so a
    cleared range immediately stops handing out addresses.
    """
    col = ip_pool_col()
    if net is None:
        await col.delete_many({"status": "free"})
        return

    ips = range_ips(net.ip_start, net.ip_end, net.prefix)
    await col.bulk_write(
        [
            UpdateOne(
                {"_id": ip},
                {
                    "$setOnInsert": {
                        "ord": int(IPv4Address(ip)),
                        "status": "free",
                        "vmName": None,
                        "jobId": None,
                        "allocatedAt": None,
                        "updatedAt": now_ms(),
                    }
                },
                upsert=True,
            )
            for ip in ips
        ],
        ordered=False,
    )
    await col.delete_many({"_id": {"$nin": ips}, "status": "free"})


async def load_guest_network() -> GuestNetwork | None:
    """Read the guest subnet config from the settings document (API process)."""
    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    return guest_network_from_doc(doc)


async def release_ip_async(vm_name: str) -> None:
    """API-process mirror of ``release_ip_sync``.

    Used where a registry row is discarded without a worker in the loop —
    purging a bookkeeping entry for a VM that is already gone. The row is the
    last thing linking the address to anything, so releasing has to happen
    before it is deleted or the address is leaked with no way to find it again.
    """
    await ip_pool_col().update_many(
        {"vmName": vm_name},
        {
            "$set": {
                "status": "free",
                "vmName": None,
                "jobId": None,
                "allocatedAt": None,
                "updatedAt": now_ms(),
            }
        },
    )


async def list_pool_async() -> dict[str, Any]:
    """Inspect shape for ``GET /api/ip-pool``."""
    entries = [
        {
            "ip": doc["_id"],
            "status": doc["status"],
            "vmName": doc.get("vmName"),
            "allocatedAt": doc.get("allocatedAt"),
        }
        async for doc in ip_pool_col().find().sort("ord", ASCENDING)
    ]
    return {
        "entries": entries,
        "free": sum(1 for e in entries if e["status"] == "free"),
        "allocated": sum(1 for e in entries if e["status"] == "allocated"),
    }


# --------------------------------------------------------------------------- #
# Celery worker (sync, over core/db/sync.worker_db)                           #
# --------------------------------------------------------------------------- #
def load_guest_network_sync(db) -> GuestNetwork | None:
    """Worker variant of ``load_guest_network`` over an open sync database."""
    return guest_network_from_doc(db["settings"].find_one({"_id": SETTINGS_DOC_ID}))


def allocate_ip_sync(db, vm_name: str, job_id: str) -> str:
    """Atomically claim the lowest free address for ``vm_name``.

    Idempotent per VM name: a redelivered task (``task_acks_late``) finds its
    earlier allocation instead of claiming a second address. Raises
    ``IpPoolExhaustedError`` when nothing is free.
    """
    col = db["ip_pool"]
    existing = col.find_one({"vmName": vm_name, "status": "allocated"})
    if existing is not None:
        return existing["_id"]

    doc = col.find_one_and_update(
        {"status": "free"},
        {
            "$set": {
                "status": "allocated",
                "vmName": vm_name,
                "jobId": job_id,
                "allocatedAt": now_ms(),
                "updatedAt": now_ms(),
            }
        },
        sort=[("ord", ASCENDING)],
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise IpPoolExhaustedError(
            "Guest IP pool exhausted — tear down unused VMs or widen the range in settings."
        )
    return doc["_id"]


def release_ip_sync(db, vm_name: str) -> None:
    """Return ``vm_name``'s address (if any) to the pool. Safe to call when
    nothing is allocated."""
    db["ip_pool"].update_many(
        {"vmName": vm_name},
        {
            "$set": {
                "status": "free",
                "vmName": None,
                "jobId": None,
                "allocatedAt": None,
                "updatedAt": now_ms(),
            }
        },
    )
