"""Plan clones must honor operator-configured per-role sizing."""

from app.core.golden_image import GoldenImageConfig
from app.core.infrastructure import (
    InfrastructureProfile,
    infrastructure_profiles_from_doc,
)
from app.tasks import _plan_clone_defaults


def _profile(cpus: int, memory_mb: int) -> InfrastructureProfile:
    return InfrastructureProfile(
        role="domainController",
        base="ws-2025-base",
        datastore="datastore1",
        expectedGuestOs="windows2022srvNext-64",
        network="VM Network",
        cpus=cpus,
        memoryMb=memory_mb,
        systemDiskGb=60,
        maxUsagePct=80,
    )


def test_clone_params_carry_the_profile_sizing() -> None:
    params = _plan_clone_defaults(_profile(cpus=8, memory_mb=8192))

    assert params["cpus"] == 8
    assert params["mem_mb"] == 8192


def test_clone_params_honor_a_custom_operator_sizing() -> None:
    params = _plan_clone_defaults(_profile(cpus=16, memory_mb=32768))

    assert params["cpus"] == 16
    assert params["mem_mb"] == 32768


def test_sizing_free_golden_image_falls_back_to_demo_defaults() -> None:
    params = _plan_clone_defaults(
        GoldenImageConfig(
            base="ws-2025-base",
            datastore="datastore1",
            expectedGuestOs="windows2022srvNext-64",
            network="VM Network",
            maxUsagePct=80,
        )
    )

    assert params["cpus"] == 8
    assert params["mem_mb"] == 8192


def test_default_role_profiles_size_every_role_at_8c_8g() -> None:
    profiles = infrastructure_profiles_from_doc({})

    assert set(profiles) == {"domainController", "rootCa", "issuingCa", "webServer"}
    assert all(profile.cpus == 8 for profile in profiles.values())
    assert all(profile.memory_mb == 8192 for profile in profiles.values())
