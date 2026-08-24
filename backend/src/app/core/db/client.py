"""Mongo client lifecycle and collection accessors.

The async client is created inside the FastAPI lifespan (``app.main``) so it
binds to uvicorn's event loop — module-level construction would race the loop.
Startup is fail-fast: if Mongo is unreachable the ping below raises and the
process refuses to boot, consistent with the guest-mode env validation in
``core/settings.py``. The 5s server-selection timeout also bounds *runtime*
outages to a fast 503 (via the PyMongoError handler in ``core/errors.py``)
instead of pymongo's 30s default.

Routers import the collection accessors, which raise if startup never ran.
"""

from pathlib import Path

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from app.core.agent_binary import sha256_file
from app.core.db.models import SettingsDoc, now_ms, to_mongo
from app.core.settings import settings

_client: AsyncMongoClient | None = None
_db: AsyncDatabase | None = None

#: Fixed primary key of the settings singleton document.
SETTINGS_DOC_ID = "global"


async def init_db() -> None:
    """Connect, fail-fast ping, ensure indexes, seed the settings singleton."""
    global _client, _db
    _client = AsyncMongoClient(settings.mongo_url, serverSelectionTimeoutMS=5000)
    _db = _client[settings.mongo_db]
    await _client.admin.command("ping")
    await _ensure_indexes()
    await _seed_settings_doc()
    await _seed_example_account()
    await _seed_ip_pool()


async def close_db() -> None:
    global _client, _db
    if _client is not None:
        await _client.close()
    _client = _db = None


def get_db() -> AsyncDatabase:
    if _db is None:
        raise RuntimeError(
            "Mongo not initialized — init_db() runs in the app lifespan."
        )
    return _db


def projects_col() -> AsyncCollection:
    return get_db()["projects"]


def project_shares_col() -> AsyncCollection:
    return get_db()["project_shares"]


def lab_invites_col() -> AsyncCollection:
    return get_db()["lab_invites"]


def vm_registry_col() -> AsyncCollection:
    return get_db()["vm_registry"]


def settings_col() -> AsyncCollection:
    return get_db()["settings"]


def users_col() -> AsyncCollection:
    return get_db()["users"]


def ip_pool_col() -> AsyncCollection:
    return get_db()["ip_pool"]


def plan_runs_col() -> AsyncCollection:
    return get_db()["plan_runs"]


def step_metrics_col() -> AsyncCollection:
    return get_db()["step_metrics"]


async def _ensure_indexes() -> None:
    """Idempotent — create_indexes no-ops on identical existing indexes."""
    await projects_col().create_indexes(
        [
            # List endpoint sorts by recency.
            IndexModel([("updatedAt", DESCENDING)]),
            # What the list endpoint actually queries: every read is scoped to
            # one owner and sorted by recency.
            IndexModel([("owner", ASCENDING), ("updatedAt", DESCENDING)]),
        ]
    )
    await project_shares_col().create_indexes(
        [
            # A guest can quickly find/update links they created. Share ids
            # themselves are the collection's unique ``_id`` values.
            IndexModel([("owner", ASCENDING), ("updatedAt", DESCENDING)]),
            # Accepted collaborators may republish a newer snapshot.
            IndexModel([("collaborators", ASCENDING)]),
        ]
    )
    await lab_invites_col().create_indexes(
        [
            # A joiner's own labs, and the membership check the remote-desktop
            # route makes.
            IndexModel([("members", ASCENDING), ("revoked", ASCENDING)]),
            # One *live* code per lab, structurally: a second mint for the same
            # project collides instead of quietly splitting one cohort across
            # two codes. Revoked invites drop out of the index and are kept for
            # the record.
            IndexModel(
                [("projectId", ASCENDING)],
                unique=True,
                partialFilterExpression={"revoked": False},
            ),
        ]
    )
    await vm_registry_col().create_indexes(
        [
            # One entry per real ESXi VM; the upsert key.
            IndexModel([("vmName", ASCENDING)], unique=True),
            # Canvas → registry lookups.
            IndexModel([("projectId", ASCENDING), ("nodeId", ASCENDING)]),
            # Environment grouping for the admin teardown console.
            IndexModel([("owner", ASCENDING), ("projectCode", ASCENDING)]),
            # moid is unique when known, absent allowed.
            IndexModel([("moid", ASCENDING)], unique=True, sparse=True),
        ]
    )
    # An earlier design used a sparse unique email index, but documents store an explicit
    # ``email: null`` (pydantic dumps the field), which sparse indexes DO index —
    # two email-less users would collide. Replace it with a partial index that
    # only indexes real string values.
    existing = await users_col().index_information()
    if "email_1" in existing and "partialFilterExpression" not in existing["email_1"]:
        await users_col().drop_index("email_1")
    await users_col().create_indexes(
        [
            IndexModel([("username", ASCENDING)], unique=True),
            IndexModel(
                [("email", ASCENDING)],
                unique=True,
                partialFilterExpression={"email": {"$type": "string"}},
            ),
        ]
    )
    await ip_pool_col().create_indexes(
        [
            # Allocation query path: lowest free address first.
            IndexModel([("status", ASCENDING), ("ord", ASCENDING)]),
            # One address per VM; partial so the many `vmName: null` free
            # documents don't collide (same pattern as the users email index).
            IndexModel(
                [("vmName", ASCENDING)],
                unique=True,
                partialFilterExpression={"vmName": {"$type": "string"}},
            ),
        ]
    )
    # plan_runs: per-plan cross-VM context + per-step resume cursor +
    # artifact relay. TTL-expired ~7d after the last write so finished runs
    # self-evict. `expireAfterSeconds` is on a Date field, so `updatedAt` here
    # is a real datetime, distinct from the `now_ms()` epoch-millis used
    # elsewhere on the doc.
    await plan_runs_col().create_indexes(
        [
            IndexModel([("jobId", ASCENDING)], unique=True),
            IndexModel([("ttlAt", ASCENDING)], expireAfterSeconds=0),
        ]
    )
    # step_metrics: per-command duration samples written by the sequence
    # worker; the median loader reads the newest N per command.
    await step_metrics_col().create_indexes(
        [
            IndexModel([("command", ASCENDING), ("at", DESCENDING)]),
        ]
    )
    # settings: singleton via fixed _id — no extra indexes.


async def _seed_settings_doc() -> None:
    """Insert the settings singleton from env if absent; operator edits made
    through the settings routes survive restarts ($setOnInsert only).

    The env ESXi password (if provided) is encrypted at seed time — it exists
    in Mongo only as ciphertext. Deferred import: ``core.secrets`` needs
    ``SETTINGS_ENC_KEY``, and keeping it out of module scope keeps this module
    importable in tooling contexts without the full secret env."""
    from app.core.secrets import encrypt_secret

    seed = SettingsDoc(
        esxi_host=settings.esxi_host,
        esxi_user=settings.esxi_user,
        esxi_password_enc=(
            encrypt_secret(settings.esxi_password) if settings.esxi_password else None
        ),
        esxi_port=settings.esxi_port,
        clone_base=settings.clone_base,
        clone_datastore=settings.clone_datastore,
        clone_guest_os=settings.clone_guest_os,
        clone_network=settings.clone_network,
        clone_max_usage_pct=settings.clone_max_usage_pct,
        guest_ip_start=settings.guest_ip_start,
        guest_ip_end=settings.guest_ip_end,
        guest_prefix=settings.guest_prefix,
        guest_gateway=settings.guest_gateway,
        guest_dns1=settings.guest_dns1,
        guest_dns2=settings.guest_dns2,
        guest_dns_suffix=settings.guest_dns_suffix,
        updated_at=now_ms(),
    )
    await settings_col().update_one(
        {"_id": SETTINGS_DOC_ID},
        {"$setOnInsert": to_mongo(seed)},
        upsert=True,
    )
    # Backfill for documents created before the password field existed (or
    # wiped by an operator): the env seed fills an *absent* password but never
    # overwrites an operator-set one.
    if settings.esxi_password:
        await settings_col().update_one(
            {"_id": SETTINGS_DOC_ID, "esxiPasswordEnc": None},
            {"$set": {"esxiPasswordEnc": encrypt_secret(settings.esxi_password)}},
        )
    # Backfill golden-image fields added in schema v4 without overwriting an
    # operator's later choices.
    await settings_col().update_one(
        {"_id": SETTINGS_DOC_ID},
        {"$set": {"schemaVersion": 5}},
    )
    for field, value in (
        ("cloneBase", settings.clone_base),
        ("cloneDatastore", settings.clone_datastore),
        ("cloneGuestOs", settings.clone_guest_os),
        ("cloneNetwork", settings.clone_network),
        ("cloneMaxUsagePct", settings.clone_max_usage_pct),
    ):
        await settings_col().update_one(
            {"_id": SETTINGS_DOC_ID, field: {"$exists": False}},
            {"$set": {field: value}},
        )
    # Same backfill idea for the guest subnet (fields new in schema v3): the
    # env seed fills a doc that has never had a range, never overwrites one.
    if settings.guest_ip_start:
        await settings_col().update_one(
            {"_id": SETTINGS_DOC_ID, "guestIpStart": None},
            {
                "$set": {
                    "guestIpStart": settings.guest_ip_start,
                    "guestIpEnd": settings.guest_ip_end,
                    "guestPrefix": settings.guest_prefix,
                    "guestGateway": settings.guest_gateway,
                    "guestDns1": settings.guest_dns1,
                    "guestDns2": settings.guest_dns2,
                    "guestDnsSuffix": settings.guest_dns_suffix,
                }
            },
        )
    await _sync_saved_agent_hash()


def _profiles_with_agent_hash(
    raw_profiles: list[dict],
    digest: str,
    *,
    materialize_missing: bool = False,
) -> tuple[list[dict], bool]:
    """Return profiles with existing qualifications pointed at ``digest``."""

    from app.core.infrastructure import assumed_tested_qualification

    validated_at = now_ms()
    changed = False
    profiles: list[dict] = []
    for raw in raw_profiles:
        profile = dict(raw)
        qualification = profile.get("qualification")
        if isinstance(qualification, dict):
            next_qualification = dict(qualification)
            if next_qualification.get("agentSha256") != digest:
                next_qualification["agentSha256"] = digest
                profile["qualification"] = next_qualification
                changed = True
        elif materialize_missing:
            profile["qualification"] = assumed_tested_qualification(
                profile["role"], digest, validated_at
            ).model_dump(by_alias=True)
            changed = True
        profiles.append(profile)
    return profiles, changed


async def _sync_saved_agent_hash() -> None:
    """Keep saved canary qualifications aligned with the bundled agent file.

    Development rebuilds replace ``backend/agent/pki-executor.exe``. On the
    next backend startup this backfills the deploy-time digest into every
    existing role qualification, avoiding manual per-role edits in Settings.
    It does not fabricate missing qualifications because image revision,
    command, ML-DSA, and OCSP canaries still have to come from the image test.
    """

    if not settings.executor_agent_path:
        return
    path = Path(settings.executor_agent_path)
    if not path.is_file():
        return
    try:
        digest = sha256_file(path).lower()
    except OSError:
        return

    from app.core.infrastructure import infrastructure_profiles_from_doc

    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID}) or {}
    raw_profiles = [
        profile.model_dump(by_alias=True)
        for profile in infrastructure_profiles_from_doc(doc).values()
    ]
    profiles, changed = _profiles_with_agent_hash(
        raw_profiles,
        digest,
        materialize_missing=True,
    )
    if not changed:
        return
    await settings_col().update_one(
        {"_id": SETTINGS_DOC_ID},
        {
            "$set": {
                "infrastructureProfiles": profiles,
                "updatedAt": now_ms(),
            }
        },
    )


async def _seed_example_account() -> None:
    """Seed the example guest account (username/password) if it doesn't exist.

    Gives a fresh deploy a working login out of the box. Idempotent and
    non-destructive: only inserts when the username is absent, so it never
    overwrites the account or a password an operator later changed. Skipped if
    the password is configured empty. Deferred import: ``core.identity`` pulls
    in pwdlib, kept out of module scope to match ``_seed_settings_doc``.
    """
    import logging

    from pymongo.errors import DuplicateKeyError

    from app.core.db.models import UserDoc
    from app.core.identity import hash_password

    username = settings.example_guest_username
    password = settings.example_guest_password
    if not username or not password:
        return
    if await users_col().find_one({"username": username}) is not None:
        return
    doc = UserDoc(
        id=username,
        username=username,
        password_hash=hash_password(password),
        role="guest",
        auth="local",
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    try:
        await users_col().insert_one(to_mongo(doc))
    except DuplicateKeyError:
        return  # lost a concurrent-boot race; the account already exists
    logging.getLogger(__name__).info(
        "Seeded example guest account '%s' (change or remove for production).",
        username,
    )


async def _seed_ip_pool() -> None:
    """Reconcile the IP pool with the stored guest range at startup. Deferred
    import: ``core.ippool`` imports this module's accessors."""
    from app.core.ippool import guest_network_from_doc, sync_pool_async

    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    await sync_pool_async(guest_network_from_doc(doc))
