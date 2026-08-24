"""Operator-authored scripts layer *over* the rendered firstboot set.

The Config ISO panel used to be all-or-nothing: any inline file meant "this disc
is complete", which suppressed the pool IP, the phone-home agent and the DC's
``50-password.ps1`` — so hand-editing one script produced a VM with no address
that never provisioned. Now an authored name replaces the render of the same
name and anything else is appended, while the agent and the credential
bootstrap are injected either way. The narrowed "complete disc" rule (uploaded
ISOs only) lives in ``routers/deploy.py`` and ``tasks._run_clone_op``.
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
    """Capture the script list handed to isokit — that list *is* manifest order."""

    names: list[str] = []

    monkeypatch.setattr(
        "app.core.firstboot.configgen.render_hostname",
        lambda _platform, _hostname: "rendered-hostname",
    )
    monkeypatch.setattr(
        "app.core.firstboot.configgen.render_network",
        lambda _platform, _network: "rendered-network",
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


def test_authored_hostname_replaces_the_rendered_one(packed, tmp_path):
    """The operator's text is what boots — and it is not packed twice."""

    _build(
        tmp_path,
        agent=_bundle(tmp_path),
        authored_scripts=[("10-hostname.ps1", "Rename-Computer dc-lab")],
    )

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "40-install-executor.ps1",
    ]
    assert (tmp_path / "10-hostname.ps1").read_text(
        encoding="utf-8"
    ) == "Rename-Computer dc-lab"
    # The render still happened for everything the operator did not supply.
    assert (tmp_path / "20-network.ps1").read_text(
        encoding="utf-8"
    ) == "rendered-network"


def test_authored_files_keep_the_agent_and_the_password_script(packed, tmp_path):
    """The regression this change exists for: inline files must not disable the
    agent (no phone-home → no provisioning) or the DC credential bootstrap."""

    _build(
        tmp_path,
        agent=_bundle(tmp_path),
        admin_password=PASSWORD,
        authored_scripts=[("10-hostname.ps1", "Rename-Computer dc-lab")],
    )

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "40-install-executor.ps1",
        "50-password.ps1",
    ]
    assert PASSWORD in (tmp_path / "50-password.ps1").read_text(encoding="utf-8")


def test_extra_authored_files_land_before_the_agent_install(packed, tmp_path):
    """Anything that isn't an override is appended in name order, so the disc's
    execution order still matches what the numeric prefixes advertise."""

    _build(
        tmp_path,
        agent=_bundle(tmp_path),
        admin_password=PASSWORD,
        authored_scripts=[
            ("32-second.ps1", "second"),
            ("30-first.ps1", "first"),
        ],
    )

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "30-first.ps1",
        "32-second.ps1",
        "40-install-executor.ps1",
        "50-password.ps1",
    ]
    assert packed == sorted(packed)


def test_authored_agent_install_is_dropped_not_duplicated(packed, tmp_path):
    """The panel shows the agent as a non-removable row; an authored file of the
    same name must not shadow it or pack two entries under one disc filename."""

    _build(
        tmp_path,
        agent=_bundle(tmp_path),
        authored_scripts=[("40-install-executor.ps1", "whoami")],
    )

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "40-install-executor.ps1",
    ]
    # Dropped before the write, so no shadow copy is left in the build dir for
    # the real asset's disc entry to be confused with.
    assert not (tmp_path / "40-install-executor.ps1").exists()


def test_authored_password_script_keeps_its_post_agent_position(packed, tmp_path):
    _build(
        tmp_path,
        agent=_bundle(tmp_path),
        authored_scripts=[("50-password.ps1", "Set-LocalUser -Name Administrator")],
    )

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "40-install-executor.ps1",
        "50-password.ps1",
    ]


def test_no_authored_scripts_is_the_untouched_default_path(packed, tmp_path):
    _build(tmp_path, agent=_bundle(tmp_path), admin_password=PASSWORD)

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "40-install-executor.ps1",
        "50-password.ps1",
    ]


def test_authored_rdp_script_is_dropped_not_duplicated(packed, tmp_path):
    """Remote desktop is not a PACK-mode opt-out. An authored file of the same
    name is dropped like the agent installer's, so no operator can silently
    leave a whole lab unreachable from the console."""

    _build(
        tmp_path,
        agent=_bundle(tmp_path),
        authored_scripts=[("30-enable-rdp.ps1", "exit 0")],
    )

    assert packed == [
        "10-hostname.ps1",
        "20-network.ps1",
        "30-enable-rdp.ps1",
        "40-install-executor.ps1",
    ]
    # Dropped before the write, so the real asset's disc entry cannot be
    # confused with a shadow copy in the build directory.
    assert not (tmp_path / "30-enable-rdp.ps1").exists()


def test_linux_template_never_gets_the_rdp_script(packed, tmp_path):
    """``Enable-NetFirewallRule`` is Windows-only, and the Linux product
    templates have no remote desktop at all."""

    _build(tmp_path, template="certsecure")

    assert packed == ["10-hostname.sh", "20-network.sh"]
