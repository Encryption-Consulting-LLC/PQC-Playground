"""The configured port group must reach the clone, not just the preflight.

vmkit renders a fresh VMX for every clone rather than copying the base VM's, and
pins ``ethernet0.networkName`` from its ``network`` argument. Before v0.5.1 that
argument did not exist and the template hardcoded ``"VM Network"``; the settings
document and the per-role profile were therefore read *only* by
``infrastructure_preflight``, which asserted the port group was correct while
every guest silently landed somewhere else.

That failure mode is invisible from the console -- the VM boots, gets its static
address, and simply never phones home -- so these tests pin the wiring rather
than the preflight, which was green throughout.
"""

from app.core.golden_image import GoldenImageConfig, golden_image_config_from_doc
from app.core.infrastructure import (
    InfrastructureProfile,
    deployment_profiles_from_doc,
)
from app.tasks import _plan_clone_defaults


def _profile(network: str) -> InfrastructureProfile:
    return InfrastructureProfile(
        role="domainController",
        base="ws-2025-base",
        datastore="datastore1",
        expectedGuestOs="windows2022srvNext-64",
        network=network,
        cpus=8,
        memoryMb=8192,
        systemDiskGb=60,
        maxUsagePct=80,
    )


def test_a_role_profiles_port_group_reaches_the_clone() -> None:
    params = _plan_clone_defaults(_profile("OKC1-VLAN-PORTGROUP"))

    assert params["network"] == "OKC1-VLAN-PORTGROUP"


def test_a_golden_image_configs_port_group_reaches_the_clone() -> None:
    params = _plan_clone_defaults(
        GoldenImageConfig(
            base="ws-2025-base",
            datastore="datastore1",
            expectedGuestOs="windows2022srvNext-64",
            network="OKC1-VLAN-PORTGROUP",
            maxUsagePct=80,
        )
    )

    assert params["network"] == "OKC1-VLAN-PORTGROUP"


def test_the_settings_documents_port_group_is_what_gets_cloned_onto() -> None:
    config = golden_image_config_from_doc({"cloneNetwork": "OKC1-VLAN-PORTGROUP"})

    assert config.network == "OKC1-VLAN-PORTGROUP"
    assert _plan_clone_defaults(config)["network"] == "OKC1-VLAN-PORTGROUP"


def test_a_legacy_document_without_a_port_group_falls_back_to_the_env_default() -> None:
    # Not a correctness claim about "VM Network" -- only that a settings
    # document predating the field resolves to the deployment's configured
    # default instead of failing validation on a required field.
    from app.core.settings import settings

    assert golden_image_config_from_doc({}).network == settings.clone_network


def test_every_product_role_clones_onto_the_configured_port_group() -> None:
    # The three Linux product roles take their placement from the global
    # setting regardless of any per-role profile, so they need the same
    # guarantee by a different route through deployment_profiles_from_doc.
    profiles = deployment_profiles_from_doc({"cloneNetwork": "OKC1-VLAN-PORTGROUP"})

    for role in ("certsecure", "cbom", "codesign"):
        assert profiles[role].network == "OKC1-VLAN-PORTGROUP"
        assert _plan_clone_defaults(profiles[role])["network"] == "OKC1-VLAN-PORTGROUP"


def test_a_per_role_profile_overrides_the_global_for_windows_roles() -> None:
    # The inverse of the product-role rule above, and the one that made the
    # migration look fixed when it was not: a document carrying explicit
    # infrastructureProfiles beats the global cloneNetwork for these four.
    doc = {
        "cloneNetwork": "OKC1-VLAN-PORTGROUP",
        "infrastructureProfiles": [
            _profile("VM Network").model_dump(by_alias=True) | {"role": role}
            for role in ("domainController", "rootCa", "issuingCa", "webServer")
        ],
    }
    profiles = deployment_profiles_from_doc(doc)

    for role in ("domainController", "rootCa", "issuingCa", "webServer"):
        assert profiles[role].network == "VM Network"
        assert _plan_clone_defaults(profiles[role])["network"] == "VM Network"
