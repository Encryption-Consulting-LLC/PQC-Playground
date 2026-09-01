"""Role-specific infrastructure profiles for guided PKI deployments."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.settings import settings


PkiRole = Literal[
    "domainController",
    "rootCa",
    "issuingCa",
    "webServer",
    "certsecure",
    "cbom",
    "codesign",
]
PKI_ROLES: tuple[PkiRole, ...] = (
    "domainController",
    "rootCa",
    "issuingCa",
    "webServer",
)
PRODUCT_ROLES: tuple[PkiRole, ...] = ("certsecure", "cbom", "codesign")
LINUX_PRODUCT_TEMPLATES = frozenset(PRODUCT_ROLES)
#: Products whose installation tree ships on the firstboot ISO and whose
#: provision op runs a real sequence. Every product template gets the Linux
#: agent (so its presence dot is honest and it is commandable); only these carry
#: a payload and an install. A product absent here provisions to "agent
#: connected, boot settled" and nothing more, which is what CBOM Secure and
#: CodeSign Secure do until their own installers are wired.
PAYLOAD_PRODUCT_TEMPLATES = frozenset({"certsecure"})
LINUX_PRODUCT_BASE = "ub-24.04-base"
LINUX_PRODUCT_GUEST_OS = "ubuntu-64"
REQUIRED_AGENT_COMMANDS = frozenset(
    {
        "ca.publish_crl",
        "ca.uninstall",
        "dc.remove_forest",
        "dns.remove_resources",
        "dns.verify_absent",
        "domain.leave",
        "iis.remove_certenroll",
        "ocsp.remove",
    }
)
ASSUMED_TESTED_BASE_CHANGE_VERSION = "assumed-current"
ASSUMED_TESTED_RUNNER_VERSION = "assumed-tested"


class ImageQualification(BaseModel):
    """Observed canary facts tied to one immutable golden-image revision."""

    model_config = ConfigDict(populate_by_name=True)

    base_change_version: str = Field(alias="baseChangeVersion", min_length=1)
    windows_build: int = Field(alias="windowsBuild", ge=26100)
    runner_version: str = Field(alias="runnerVersion", min_length=1, max_length=80)
    agent_sha256: str = Field(alias="agentSha256", pattern=r"^[0-9a-fA-F]{64}$")
    validated_at: int = Field(alias="validatedAt", gt=0)
    ml_dsa_87_available: bool = Field(alias="mlDsa87Available")
    system_context_validated: bool = Field(alias="systemContextValidated")
    time_synchronized: bool = Field(default=False, alias="timeSynchronized")
    windows_updates_current: bool = Field(default=False, alias="windowsUpdatesCurrent")
    backend_callback_reachable: bool = Field(
        default=False, alias="backendCallbackReachable"
    )
    agent_commands: list[str] = Field(default_factory=list, alias="agentCommands")
    publication_manifest_version: int = Field(
        default=0, alias="publicationManifestVersion", ge=0
    )
    ocsp_reference_sha256: str | None = Field(
        default=None, alias="ocspReferenceSha256", pattern=r"^[0-9a-fA-F]{64}$"
    )


class InfrastructureProfile(BaseModel):
    """Clone, placement, and sizing policy for one PKI machine role."""

    model_config = ConfigDict(populate_by_name=True)

    role: PkiRole
    base: str = Field(min_length=1, max_length=80)
    datastore: str = Field(min_length=1, max_length=80)
    expected_guest_os: str = Field(alias="expectedGuestOs", min_length=1, max_length=80)
    network: str = Field(min_length=1, max_length=80)
    cpus: int = Field(ge=1, le=64)
    memory_mb: int = Field(alias="memoryMb", ge=1024, le=262144)
    system_disk_gb: int = Field(alias="systemDiskGb", ge=32, le=4096)
    max_usage_pct: float = Field(alias="maxUsagePct", gt=0, le=100)
    qualification: ImageQualification | None = None


_DEFAULT_SIZING: dict[PkiRole, tuple[int, int, int]] = {
    "domainController": (8, 8192, 60),
    "rootCa": (8, 8192, 60),
    "issuingCa": (8, 8192, 80),
    "webServer": (8, 8192, 80),
}

_PRODUCT_SIZING = (4, 8192, 40)


def default_infrastructure_profiles() -> list[InfrastructureProfile]:
    """Build defaults from the legacy singleton image settings."""

    profiles: list[InfrastructureProfile] = []
    for role in PKI_ROLES:
        cpus, memory_mb, disk_gb = _DEFAULT_SIZING[role]
        profiles.append(
            InfrastructureProfile(
                role=role,
                base=settings.clone_base,
                datastore=settings.clone_datastore,
                expectedGuestOs=settings.clone_guest_os,
                network=settings.clone_network,
                cpus=cpus,
                memoryMb=memory_mb,
                systemDiskGb=disk_gb,
                maxUsagePct=settings.clone_max_usage_pct,
            )
        )
    return profiles


def assumed_tested_qualification(
    role: PkiRole, digest: str, validated_at: int
) -> ImageQualification:
    """Build a dev qualification from an operator-accepted bundled agent."""

    return ImageQualification(
        baseChangeVersion=ASSUMED_TESTED_BASE_CHANGE_VERSION,
        windowsBuild=26100,
        runnerVersion=ASSUMED_TESTED_RUNNER_VERSION,
        agentSha256=digest,
        validatedAt=validated_at,
        mlDsa87Available=role in ("rootCa", "issuingCa"),
        systemContextValidated=True,
        timeSynchronized=True,
        windowsUpdatesCurrent=True,
        backendCallbackReachable=True,
        agentCommands=sorted(REQUIRED_AGENT_COMMANDS),
        publicationManifestVersion=1,
        ocspReferenceSha256=("0" * 64 if role == "webServer" else None),
    )


def infrastructure_profiles_from_doc(
    doc: dict | None,
) -> dict[PkiRole, InfrastructureProfile]:
    """Resolve the complete role map, backfilling legacy settings documents."""

    doc = doc or {}
    defaults = {profile.role: profile for profile in default_infrastructure_profiles()}
    legacy = {
        "base": doc.get("cloneBase") or settings.clone_base,
        "datastore": doc.get("cloneDatastore") or settings.clone_datastore,
        "expectedGuestOs": doc.get("cloneGuestOs") or settings.clone_guest_os,
        "network": doc.get("cloneNetwork") or settings.clone_network,
        "maxUsagePct": doc.get("cloneMaxUsagePct") or settings.clone_max_usage_pct,
    }
    for role, profile in list(defaults.items()):
        defaults[role] = profile.model_copy(
            update={
                "base": legacy["base"],
                "datastore": legacy["datastore"],
                "expected_guest_os": legacy["expectedGuestOs"],
                "network": legacy["network"],
                "max_usage_pct": legacy["maxUsagePct"],
            }
        )
    for raw in doc.get("infrastructureProfiles") or []:
        profile = InfrastructureProfile(**raw)
        defaults[profile.role] = profile
    return defaults


def deployment_profiles_from_doc(
    doc: dict | None,
) -> dict[PkiRole, InfrastructureProfile]:
    """Resolve guided Windows profiles plus fixed Ubuntu product profiles.

    Product services intentionally do not extend the operator's Windows image
    qualification form yet. They clone the shared ``ub-24.04-base`` image and
    inherit only placement policy (datastore, port group, usage ceiling) from
    the legacy/global clone settings until product-specific setup is built.
    """

    doc = doc or {}
    profiles = infrastructure_profiles_from_doc(doc)
    cpus, memory_mb, disk_gb = _PRODUCT_SIZING
    for role in PRODUCT_ROLES:
        profiles[role] = InfrastructureProfile(
            role=role,
            base=LINUX_PRODUCT_BASE,
            datastore=doc.get("cloneDatastore") or settings.clone_datastore,
            expectedGuestOs=LINUX_PRODUCT_GUEST_OS,
            network=doc.get("cloneNetwork") or settings.clone_network,
            cpus=cpus,
            memoryMb=memory_mb,
            systemDiskGb=disk_gb,
            maxUsagePct=(doc.get("cloneMaxUsagePct") or settings.clone_max_usage_pct),
        )
    return profiles


def role_for_template(template: str, ca_type: str | None = None) -> PkiRole:
    """Map a staged template configuration to its infrastructure role."""

    if template == "domainController":
        return "domainController"
    if template == "webServer":
        return "webServer"
    if template == "certificateAuthority":
        return "issuingCa" if ca_type == "Issuing" else "rootCa"
    if template in LINUX_PRODUCT_TEMPLATES:
        return template  # type: ignore[return-value]
    raise ValueError(f"template '{template}' has no guided PKI infrastructure profile")
