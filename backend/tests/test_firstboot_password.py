"""The firstboot Administrator password script.

``Install-ADDSForest`` promotes the guest's *local* ``Administrator`` into the new
forest as the built-in **domain** Administrator, keeping its password — so
``50-password.ps1`` is what makes the operator's ``domainAdminPassword`` the
credential ``domain.join`` and the issuing-CA install later authenticate with
(the dispatch half of that contract is pinned in ``test_domain_join_sequence``).
Nothing else in the stack can set it: the agent has no password command, and the
reset has to land before promotion.

Every other Windows template gets the same script rendered the same way, from the
golden image's own password — this module pins the *rendering*, and which password
each role is handed is pinned in ``test_clone_admin_password``.
"""

import os

import pytest

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.firstboot import AgentBundle, build_firstboot_iso  # noqa: E402
from app.core.ippool import GuestNetwork  # noqa: E402

PASSWORD = "Str0ng-Lab-Pass!"

NET = GuestNetwork(
    ip_start="192.0.2.10",
    ip_end="192.0.2.20",
    prefix=24,
    gateway="192.0.2.1",
    dns1="192.0.2.2",
)


@pytest.fixture
def packed(monkeypatch):
    """Capture the script list handed to isokit on either ISO path.

    isokit packs in the order received, so this list *is* the manifest
    (execution) order — the numeric filenames only document it.
    """
    names: list[str] = []

    monkeypatch.setattr(
        "app.core.firstboot.configgen.render_hostname",
        lambda _platform, _hostname: "hostname",
    )
    monkeypatch.setattr(
        "app.core.firstboot.configgen.render_network",
        lambda _platform, _network: "network",
    )
    monkeypatch.setattr(
        "app.core.firstboot.isokit.build_script_iso",
        lambda scripts, _iso: names.extend(path.name for path in scripts),
    )
    monkeypatch.setattr(
        "app.core.firstboot.isokit.build_config_iso",
        lambda _iso, scripts, files=(): names.extend(path.name for path in scripts),
    )
    return names


def _bundle(tmp_path):
    binary = tmp_path / "agent-source.exe"
    binary.write_bytes(b"MZ")
    return AgentBundle(binary_path=binary, config_toml="[identity]\n")


def _build(tmp_path, **overrides):
    kwargs = {
        "template": "domainController",
        "vm_name": "guest-alice-9e4edb-dc01",
        "ip": "192.0.2.10",
        "net": NET,
        "dest_dir": tmp_path,
    }
    kwargs.update(overrides)
    return build_firstboot_iso(**kwargs)


def test_password_script_is_packed_after_the_agent_install(packed, tmp_path):
    _build(tmp_path, agent=_bundle(tmp_path), admin_password=PASSWORD)

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "40-install-executor.ps1",
        "50-password.ps1",
    ]
    # Pack order and numeric-name order must agree, or the disc's execution
    # order stops matching what the filenames advertise.
    assert packed == sorted(packed)


def test_password_script_resets_the_builtin_administrator(packed, tmp_path):
    _build(tmp_path, agent=_bundle(tmp_path), admin_password=PASSWORD)

    script = (tmp_path / "50-password.ps1").read_text(encoding="utf-8")
    assert "Set-LocalUser" in script
    assert "username = 'Administrator'" in script
    assert PASSWORD in script
    # The base image's firstboot runner owns the single reboot.
    assert "Restart-Computer" not in script
    assert not [
        line
        for line in script.splitlines()
        if "Write-Output" in line and PASSWORD in line
    ]


def test_a_non_domain_controller_packs_it_in_the_same_place(packed, tmp_path):
    """The password branch never consults ``template`` — only the *source* of the
    value differs by role, and that choice lives in ``tasks._run_clone_op``. A
    member server re-asserting the golden image's password gets a byte-identical
    script at the same manifest position as the DC's."""

    _build(
        tmp_path,
        template="webServer",
        vm_name="guest-alice-9e4edb-srv1",
        agent=_bundle(tmp_path),
        admin_password=PASSWORD,
    )

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "40-install-executor.ps1",
        "50-password.ps1",
    ]
    assert PASSWORD in (tmp_path / "50-password.ps1").read_text(encoding="utf-8")


def test_agentless_path_still_gets_the_password_script(packed, tmp_path):
    """Bundling disabled must not silently skip the credential bootstrap."""

    _build(tmp_path, admin_password=PASSWORD)

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "50-password.ps1",
    ]


@pytest.mark.parametrize("template", ["domainController", "webServer"])
def test_blank_password_renders_no_password_script(
    packed, tmp_path, monkeypatch, template
):
    monkeypatch.setattr(
        "app.core.firstboot.configgen.render_password",
        lambda *_args: pytest.fail("render_password called without a password"),
    )

    _build(tmp_path, template=template, agent=_bundle(tmp_path))

    assert "50-password.ps1" not in packed
    assert not (tmp_path / "50-password.ps1").exists()


def test_linux_template_never_gets_a_password_script(packed, tmp_path):
    """``Set-LocalUser`` is Windows-only; a stray password must not reach a product VM."""

    _build(tmp_path, template="certsecure", admin_password=PASSWORD)

    assert packed == ["10-hostname.sh", "20-network.sh"]
    assert not list(tmp_path.glob("50-password.*"))
