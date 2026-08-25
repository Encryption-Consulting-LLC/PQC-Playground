"""Settings singleton routes — the shared org-wide ESXi target.

``core/esxi.py`` reads this document per request, so an operator edit here
takes effect on the next VM operation with no restart. The env ``ESXI_*``
vars are only a first-boot seed.

``esxiPassword`` is write-only: accepted in the PUT body, stored as the
AES-GCM blob from ``core/secrets.py``, and never returned — GET exposes just
``hasPassword``. That keeps the plaintext out of Mongo *and* out of every
API response/log. ``cloneAdminPassword`` — the golden image's own built-in
``Administrator`` password — gets exactly the same treatment behind
``hasCloneAdminPassword``.

(Absolute imports mean no clash with ``app.core.settings``.)
"""

from functools import partial

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from vmkit import Connection
from vmkit.errors import VmkitError

from app.core.authz import Capability, require_capability
from app.core.db import SETTINGS_DOC_ID, from_mongo, now_ms, settings_col
from app.core.esxi import load_target, manager
from app.core.golden_image import (
    golden_image_config_from_doc,
    preflight_golden_image,
    unreachable_golden_image,
)
from app.core.ippool import guest_network_from_doc, sync_pool_async, validate_network
from app.core.infrastructure import InfrastructureProfile
from app.core.infrastructure import infrastructure_profiles_from_doc
from app.core.infrastructure_preflight import (
    PlannedMachine,
    preflight_infrastructure,
    unreachable_infrastructure,
)
from app.core.environment_preflight import preflight_control_plane
from app.core.db import get_db
from app.core.secrets import encrypt_secret

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    """Partial update — only fields present in the request are $set."""

    model_config = ConfigDict(populate_by_name=True)

    esxi_host: str | None = Field(default=None, alias="esxiHost")
    esxi_user: str | None = Field(default=None, alias="esxiUser")
    esxi_password: str | None = Field(default=None, alias="esxiPassword")
    esxi_port: int | None = Field(default=None, alias="esxiPort")
    clone_base: str | None = Field(
        default=None, min_length=1, max_length=80, alias="cloneBase"
    )
    clone_datastore: str | None = Field(
        default=None, min_length=1, max_length=80, alias="cloneDatastore"
    )
    clone_guest_os: str | None = Field(
        default=None, min_length=1, max_length=80, alias="cloneGuestOs"
    )
    clone_network: str | None = Field(
        default=None, min_length=1, max_length=80, alias="cloneNetwork"
    )
    clone_max_usage_pct: float | None = Field(
        default=None, gt=0, le=100, alias="cloneMaxUsagePct"
    )
    # Deliberately *not* held to ``password_policy_errors``: this records a
    # password the image already has, it is not one the platform is choosing.
    # Rejecting it here would only stop the admin describing reality. Empty
    # string clears it.
    clone_admin_password: str | None = Field(
        default=None, max_length=128, alias="cloneAdminPassword"
    )
    infrastructure_profiles: list[InfrastructureProfile] | None = Field(
        default=None, min_length=4, max_length=4, alias="infrastructureProfiles"
    )
    guest_ip_start: str | None = Field(default=None, alias="guestIpStart")
    guest_ip_end: str | None = Field(default=None, alias="guestIpEnd")
    guest_prefix: int | None = Field(default=None, alias="guestPrefix")
    guest_gateway: str | None = Field(default=None, alias="guestGateway")
    guest_dns1: str | None = Field(default=None, alias="guestDns1")
    guest_dns2: str | None = Field(default=None, alias="guestDns2")
    guest_dns_suffix: str | None = Field(default=None, alias="guestDnsSuffix")
    feature_flags: dict[str, bool] | None = Field(default=None, alias="featureFlags")


class GoldenImageValidationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    requested_vm_names: list[str] = Field(
        default_factory=list, max_length=50, alias="requestedVmNames"
    )
    clone_count: int | None = Field(default=None, ge=0, le=50, alias="cloneCount")


class InfrastructureValidationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    machines: list[PlannedMachine] = Field(
        default_factory=lambda: [
            PlannedMachine(role="domainController", name="DC01"),
            PlannedMachine(role="rootCa", name="CA01"),
            PlannedMachine(role="issuingCa", name="CA02"),
            PlannedMachine(role="webServer", name="SRV1"),
        ],
        min_length=1,
        max_length=50,
    )


def _present(doc: dict) -> dict:
    """API shape: replace each ciphertext blob with a boolean "is it set" flag."""
    out = from_mongo(doc)
    out["hasPassword"] = out.pop("esxiPasswordEnc", None) is not None
    out["hasCloneAdminPassword"] = out.pop("cloneAdminPasswordEnc", None) is not None
    return out


@router.get("", dependencies=[Depends(require_capability(Capability.SETTINGS_READ))])
async def get_settings() -> dict:
    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    return _present(doc)


async def _acquire_esxi() -> tuple[Connection | None, str | None]:
    """Open (or reuse) the shared ESXi connection for a validate route.

    Returns ``(conn, None)`` on success or ``(None, detail)`` when the target is
    unconfigured, unreachable, or rejects the stored credentials. The validate
    routes report that failure as a ``ready: false`` preflight check rather than
    a 502 — the whole point of a preflight is to diagnose exactly this.
    """
    target = await load_target()
    if target is None:
        return None, "No shared ESXi target is configured."
    try:
        return await run_in_threadpool(manager.get, target), None
    except VmkitError as exc:
        return None, f"Could not connect to the configured ESXi target: {exc}"


@router.post(
    "/golden-image/validate",
    dependencies=[Depends(require_capability(Capability.SETTINGS_READ))],
)
async def validate_golden_image(
    body: GoldenImageValidationRequest,
) -> dict:
    """Validate the saved Windows golden image without mutating ESXi."""

    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    config = golden_image_config_from_doc(doc)
    conn, error = await _acquire_esxi()
    if conn is None:
        return unreachable_golden_image(
            config,
            error or "ESXi target unavailable.",
            requested_vm_names=body.requested_vm_names,
            clone_count=body.clone_count,
        ).model_dump(by_alias=True)
    result = await run_in_threadpool(
        partial(
            preflight_golden_image,
            conn,
            config,
            requested_vm_names=body.requested_vm_names,
            clone_count=body.clone_count,
        )
    )
    return result.model_dump(by_alias=True)


@router.post(
    "/infrastructure/validate",
    dependencies=[Depends(require_capability(Capability.SETTINGS_READ))],
)
async def validate_infrastructure(
    body: InfrastructureValidationRequest,
) -> dict:
    """Validate all selected role profiles as one capacity reservation."""

    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    conn, error = await _acquire_esxi()
    if conn is None:
        return unreachable_infrastructure(
            error or "ESXi target unavailable."
        ).model_dump(by_alias=True)
    result = await run_in_threadpool(
        partial(
            preflight_infrastructure,
            conn,
            infrastructure_profiles_from_doc(doc),
            body.machines,
        )
    )
    return result.model_dump(by_alias=True)


@router.post(
    "/environment/validate",
    dependencies=[Depends(require_capability(Capability.SETTINGS_READ))],
)
async def validate_environment() -> dict:
    """Validate API/worker reachability and immutable agent inputs."""

    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    try:
        mongo_ready = (await get_db().command("ping")).get("ok") == 1
    except Exception:  # noqa: BLE001
        mongo_ready = False
    result = await run_in_threadpool(
        partial(
            preflight_control_plane,
            infrastructure_profiles_from_doc(doc),
            mongo_ready=mongo_ready,
        )
    )
    return result.model_dump(by_alias=True)


@router.put("", dependencies=[Depends(require_capability(Capability.SETTINGS_WRITE))])
async def update_settings(body: SettingsUpdate) -> dict:
    fields = body.model_dump(by_alias=True, exclude_unset=True)
    profiles = fields.get("infrastructureProfiles")
    if profiles is not None and {profile["role"] for profile in profiles} != {
        "domainController",
        "rootCa",
        "issuingCa",
        "webServer",
    }:
        raise HTTPException(
            status_code=422,
            detail="Infrastructure profiles must contain each guided PKI role exactly once.",
        )
    if "esxiPassword" in fields:
        password = fields.pop("esxiPassword")
        fields["esxiPasswordEnc"] = encrypt_secret(password) if password else None
    if "cloneAdminPassword" in fields:
        password = fields.pop("cloneAdminPassword")
        fields["cloneAdminPasswordEnc"] = encrypt_secret(password) if password else None

    # Guest-range edits are validated against the *merged* document before
    # anything is written (a partial update can't be judged field-by-field),
    # then the IP pool is reseeded so the edit takes effect immediately.
    guest_edited = any(key.startswith("guest") for key in fields)
    merged_net = None
    if guest_edited:
        current = await settings_col().find_one({"_id": SETTINGS_DOC_ID}) or {}
        merged_net = guest_network_from_doc({**current, **fields})
        if merged_net is not None:
            try:
                validate_network(merged_net)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    fields["updatedAt"] = now_ms()
    await settings_col().update_one({"_id": SETTINGS_DOC_ID}, {"$set": fields})
    if guest_edited:
        await sync_pool_async(merged_net)
    doc = await settings_col().find_one({"_id": SETTINGS_DOC_ID})
    return _present(doc)
