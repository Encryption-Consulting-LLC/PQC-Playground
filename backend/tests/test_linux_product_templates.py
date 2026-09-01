"""Linux product components clone Ubuntu, run the agent, and install for real.

Every product template gets the musl build of the same executor agent the
Windows templates run, so its presence dot is honest and it is commandable. Only
the products in ``PAYLOAD_PRODUCT_TEMPLATES`` additionally carry an installation
tree on the firstboot disc and expand into a real provision sequence; the rest
provision to "agent connected, boot settled" and nothing more.
"""

import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.execution_manifest import build_execution_groups  # noqa: E402
from app.core.firstboot import (  # noqa: E402
    PRODUCT_PAYLOAD_DISC_NAME,
    TEMPLATE_IDS,
    AgentBundle,
    ProductPayload,
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


@pytest.fixture
def linux_net():
    return GuestNetwork(
        ip_start="192.0.2.10",
        ip_end="192.0.2.20",
        prefix=24,
        gateway="192.0.2.1",
        dns1="192.0.2.2",
    )


@pytest.fixture
def stub_iso_build(monkeypatch):
    """Record what a firstboot build renders and packs, without touching disc."""

    recorded = {"rendered": [], "scripts": [], "files": []}
    monkeypatch.setattr(
        "app.core.firstboot.configgen.render_hostname",
        lambda platform, hostname: (
            recorded["rendered"].append((platform, hostname)) or "hostname"
        ),
    )
    monkeypatch.setattr(
        "app.core.firstboot.configgen.render_network",
        lambda platform, _network: (
            recorded["rendered"].append((platform, "network")) or "network"
        ),
    )
    monkeypatch.setattr(
        "app.core.firstboot.isokit.build_script_iso",
        lambda scripts, _iso: recorded["scripts"].extend(p.name for p in scripts),
    )
    monkeypatch.setattr(
        "app.core.firstboot.isokit.build_config_iso",
        lambda _iso, scripts, files: (
            recorded["scripts"].extend(p.name for p in scripts),
            recorded["files"].extend(p.name for p in files),
        ),
    )
    return recorded


def test_linux_firstboot_uses_shell_scripts(stub_iso_build, linux_net, tmp_path):
    build_firstboot_iso(
        template="certsecure",
        vm_name="guest-alice-project-CertSecure01",
        ip="192.0.2.10",
        net=linux_net,
        dest_dir=tmp_path,
        # A Windows-only Set-LocalUser step must never reach a Linux product VM.
        admin_password="Str0ng-Lab-Pass!",
    )

    assert stub_iso_build["rendered"][0] == (
        "linux",
        "guest-alice-project-certsecure01",
    )
    assert all(platform == "linux" for platform, _v in stub_iso_build["rendered"])
    # No agent bundle passed, so this is still the v1 disc: identity + network.
    assert stub_iso_build["scripts"] == ["10-hostname.sh", "20-network.sh"]


def test_a_bundled_linux_agent_packs_the_shell_installer(
    stub_iso_build, linux_net, tmp_path
):
    """The whole point of the platform-keyed asset lookup.

    Bundling the *Windows* installer onto a Linux disc was previously refused
    outright; now it has to pick the right one instead, and picking the wrong
    one is silent — the runner executes a .ps1 on a box with no PowerShell and
    the only symptom is an agent that never phones home.
    """
    binary = tmp_path / "pki-executor"
    binary.write_bytes(b"\x7fELF")

    build_firstboot_iso(
        template="cbom",
        vm_name="guest-alice-project-CBOM01",
        ip="192.0.2.11",
        net=linux_net,
        dest_dir=tmp_path,
        agent=AgentBundle(binary_path=binary, config_toml="[identity]"),
    )

    assert stub_iso_build["scripts"] == [
        "10-hostname.sh",
        "20-network.sh",
        "40-install-executor.sh",
    ]
    assert stub_iso_build["files"] == ["pki-executor", "executor.toml"]


def test_a_product_payload_is_staged_with_its_digest(
    stub_iso_build, linux_net, tmp_path
):
    archive = tmp_path / "CertSecure Manager v3.3 - CLM Server.tar.gz"
    archive.write_bytes(b"tarball")
    binary = tmp_path / "pki-executor"
    binary.write_bytes(b"\x7fELF")

    build_firstboot_iso(
        template="certsecure",
        vm_name="guest-alice-project-CertSecure01",
        ip="192.0.2.10",
        net=linux_net,
        dest_dir=tmp_path,
        agent=AgentBundle(binary_path=binary, config_toml="[identity]"),
        payload=ProductPayload(archive_path=archive, sha256="a" * 64),
    )

    assert stub_iso_build["scripts"] == [
        "10-hostname.sh",
        "20-network.sh",
        "40-install-executor.sh",
        "45-stage-certsecure.sh",
    ]
    # Renamed on the disc: the vendor's own filename carries spaces and a
    # version, and the staging script's baked-in name must not depend on it.
    assert stub_iso_build["files"] == [
        "pki-executor",
        "executor.toml",
        PRODUCT_PAYLOAD_DISC_NAME,
    ]

    staged = (tmp_path / "45-stage-certsecure.sh").read_text(encoding="utf-8")
    assert "a" * 64 in staged
    assert PRODUCT_PAYLOAD_DISC_NAME in staged
    assert "__PAYLOAD_SHA256__" not in staged


def test_a_payload_without_an_agent_is_refused(linux_net, tmp_path):
    # Nothing on that disc would ever install the tree it carries.
    archive = tmp_path / "payload.tar.gz"
    archive.write_bytes(b"tarball")
    with pytest.raises(ValueError, match="nothing would install it"):
        build_firstboot_iso(
            template="certsecure",
            vm_name="cs01",
            ip="192.0.2.10",
            net=linux_net,
            dest_dir=tmp_path,
            payload=ProductPayload(archive_path=archive, sha256="a" * 64),
        )


def test_a_product_without_an_installer_only_waits_for_its_agent():
    """CBOM Secure and CodeSign Secure ship no installation payload yet.

    They still get an agent — that is what makes their presence dot honest and
    them commandable — so their provision op is the same two waits every
    role-less Windows template gets, and nothing more. This is the row the panel
    shows, so it is worth pinning: the previous behaviour was a 0.6-second
    "Service setup stub" that reported success on a machine where nothing had
    been installed.
    """
    topology = TopologyDocument(
        nodes=[TopologyNode(id="product", name="CBOM01", role="product")]
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
    assert [step["id"] for step in provision["steps"]] == [
        "agent-ready",
        "boot-settle",
    ]
    assert "stub" not in provision["label"]


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


def test_only_certsecure_ships_an_installation_payload_today():
    # CBOM Secure and CodeSign Secure are deliberately still agent-only. Pinned
    # so adding a payload for one is a decision someone made rather than a
    # side effect of touching the product list.
    from app.core.infrastructure import PAYLOAD_PRODUCT_TEMPLATES

    assert PAYLOAD_PRODUCT_TEMPLATES == {"certsecure"}
    assert PAYLOAD_PRODUCT_TEMPLATES < LINUX_PRODUCT_TEMPLATES


def test_ubuntu_vmx_and_inventory_guest_ids_match():
    assert guest_os_ids_match("ubuntu64Guest", "ubuntu-64")
