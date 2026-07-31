"""Linux product components clone Ubuntu and expose setup placeholders."""

import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.execution_manifest import build_execution_groups  # noqa: E402
from app.core.firstboot import (  # noqa: E402
    TEMPLATE_IDS,
    build_firstboot_iso,
    platform_for_template,
)
from app.core.infrastructure import (  # noqa: E402
    LINUX_PRODUCT_BASE,
    LINUX_PRODUCT_TEMPLATES,
    deployment_profiles_from_doc,
    role_for_template,
)
from app.core.ippool import GuestNetwork  # noqa: E402
from app.core.topology import TopologyDocument, TopologyNode, compile_plan  # noqa: E402
from app.core.vmware_guest_os import guest_os_ids_match  # noqa: E402
from app.core.authz import AuthedUser, Role  # noqa: E402
from app.routers.deploy import PlanOp, validate_plan  # noqa: E402


@pytest.mark.parametrize("template", sorted(LINUX_PRODUCT_TEMPLATES))
def test_product_template_uses_fixed_ubuntu_profile(template):
    profiles = deployment_profiles_from_doc(
        {"cloneDatastore": "lab-ds", "cloneNetwork": "Lab Network"}
    )

    profile = profiles[role_for_template(template)]
    assert template in TEMPLATE_IDS
    assert platform_for_template(template) == "linux"
    assert profile.base == LINUX_PRODUCT_BASE
    assert profile.expected_guest_os == "ubuntu-64"
    assert profile.datastore == "lab-ds"
    assert profile.network == "Lab Network"


def test_linux_firstboot_uses_shell_scripts(monkeypatch, tmp_path):
    rendered: list[tuple[str, str]] = []
    packed: list[str] = []

    monkeypatch.setattr(
        "app.core.firstboot.configgen.render_hostname",
        lambda platform, hostname: rendered.append((platform, hostname)) or "hostname",
    )
    monkeypatch.setattr(
        "app.core.firstboot.configgen.render_network",
        lambda platform, _network: rendered.append((platform, "network")) or "network",
    )
    monkeypatch.setattr(
        "app.core.firstboot.isokit.build_script_iso",
        lambda scripts, _iso: packed.extend(path.name for path in scripts),
    )

    build_firstboot_iso(
        template="certsecure",
        vm_name="guest-alice-project-CertSecure01",
        ip="192.0.2.10",
        net=GuestNetwork(
            ip_start="192.0.2.10",
            ip_end="192.0.2.20",
            prefix=24,
            gateway="192.0.2.1",
            dns1="192.0.2.2",
        ),
        dest_dir=tmp_path,
        # A Windows-only Set-LocalUser step must never reach a Linux product VM.
        admin_password="Str0ng-Lab-Pass!",
    )

    assert rendered[0] == ("linux", "guest-alice-project-certsecure01")
    assert all(platform == "linux" for platform, _value in rendered)
    assert packed == ["10-hostname.sh", "20-network.sh"]


def test_product_plan_compiles_as_a_standalone_machine_with_setup_stub():
    topology = TopologyDocument(
        nodes=[TopologyNode(id="product", name="CBOM01", role="standalone")]
    )
    compiled = compile_plan(
        topology,
        [
            PlanOp(
                id="create-product",
                kind="createVm",
                target="product",
                params={"vmName": "CBOM01", "template": "cbom"},
            )
        ],
    )

    groups = build_execution_groups(topology, compiled.operations, {})
    clone = next(group for group in groups if group["kind"] == "createVm")
    provision = next(group for group in groups if group["kind"] == "provision")
    assert clone["sourceBase"] == LINUX_PRODUCT_BASE
    assert provision["steps"] == [
        {
            "id": "service-setup",
            "label": "Set up CBOM Secure (stub)",
            "kind": "backend",
            "dependsOn": [],
            "targetNodeId": "product",
        }
    ]


def test_product_base_name_is_protected():
    op = PlanOp(
        id="create-product",
        kind="createVm",
        target="product",
        params={"vmName": LINUX_PRODUCT_BASE, "template": "codesign"},
    )
    with pytest.raises(HTTPException) as exc:
        validate_plan(
            [op],
            AuthedUser(username="op", role=Role.OPERATOR, auth="local"),
            target_configured=True,
            guest_network_configured=True,
        )
    assert exc.value.status_code == 422
    assert LINUX_PRODUCT_BASE in str(exc.value.detail)


def test_product_domain_join_is_rejected_until_linux_integration_exists():
    ops = [
        PlanOp(
            id="create-product",
            kind="createVm",
            target="product",
            params={"vmName": "codesign01", "template": "codesign"},
        ),
        PlanOp(
            id="join-product",
            kind="domainJoin",
            target="product",
            secondary="dc",
        ),
    ]
    with pytest.raises(HTTPException) as exc:
        validate_plan(
            ops,
            AuthedUser(username="op", role=Role.OPERATOR, auth="local"),
            target_configured=True,
            guest_network_configured=True,
        )
    assert exc.value.status_code == 422
    assert "not implemented" in str(exc.value.detail)


def test_ubuntu_vmx_and_inventory_guest_ids_match():
    assert guest_os_ids_match("ubuntu64Guest", "ubuntu-64")
