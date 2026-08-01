"""Per-VM firstboot ISO assembly.

Every real ``createVm`` boots from a config ISO built here on the Celery
worker: configgen renders the per-VM hostname and static-network scripts
(baking in the pool-allocated IP) and isokit packs them (with a
``firstboot.manifest``) into ``<vm>-config.iso``. vmkit then uploads that file
to ``[datastore] <vm>/<vm>-config.iso`` and attaches it during the clone — so
the ISO only needs to outlive the ``clone_workflow`` call.

Firstboot is deliberately minimal — identity + network (+ the phone-home agent
when bundled) — so a booted VM connects within minutes. Role/feature installs
(AD DS, AD CS, IIS) are *not* baked here: the agent's provision commands
(``dc.install_forest``, ``ca.install``, ``iis.setup``) run ``Install-Windows\
Feature`` themselves and are dispatched *after* phone-home, so the (slow) work
runs as a visible executor step instead of blocking the connection.
``role_scripts_for`` remains the hook for a genuinely firstboot-only script (one
that must run *before* the agent), but the shipping templates carry none.

The one credential exception is ``admin_password`` (a domain controller's
``domainAdminPassword``), which renders a ``Set-LocalUser`` step for the built-in
local ``Administrator``. It cannot be an executor step: ``Install-ADDSForest``
promotes the *local* Administrator into the new forest as the built-in **domain**
Administrator, keeping its password, so the reset has to land *before* promotion
— and the agent's command set has no way to set a password at all. That password
is what every later ``domain.join`` and the issuing-CA install authenticate with
as ``<NETBIOS>\\Administrator``.

Numbering fixes manifest (execution) order: ``10-`` hostname, ``20-`` network,
``40-`` agent install (bundled path only), ``50-`` local Administrator password
(domain controllers only). isokit packs in the order received, so the list handed
to it *is* the manifest order — the numeric names only document it.
Scripts never reboot — the firstboot runner in the base image owns the single
reboot (established configgen convention).

The disc is not a secret store: it already ships the agent's bearer token in
``executor.toml`` and now a domain controller's Administrator password as
plaintext PowerShell, and vmkit leaves it attached at
``[datastore] <vm>/<vm>-config.iso`` for the VM's lifetime.

``build_authored_iso`` packs an operator-authored script set verbatim via
isokit's v2 API — the server injects nothing (no hostname/network render, no
role scripts, no pool IP). ``build_firstboot_iso`` (the guest/default path)
uses ``build_script_iso`` and its v1 manifest.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import configgen
import isokit
from configgen import NetworkConfig

from app.core.ippool import GuestNetwork
from app.core.infrastructure import LINUX_PRODUCT_TEMPLATES

#: On-disc name for the embedded agent binary — what the install script and the
#: agent's Windows service expect (see ``assets/firstboot/_agent``).
_AGENT_BINARY_NAME = "pki-executor.exe"
_AGENT_CONFIG_NAME = "executor.toml"
#: Static install step appended when an agent is bundled. Lives under
#: ``_agent`` (leading underscore → not a template dir, never role-globbed).
_AGENT_INSTALL_SCRIPT = (
    Path(__file__).parent.parent
    / "assets"
    / "firstboot"
    / "_agent"
    / "40-install-executor.ps1"
)
#: The Windows built-in local administrator. ``Install-ADDSForest`` carries *this*
#: account's password into the new forest as the built-in **domain** Administrator
#: password — the credential every later step signs in with as
#: ``<NETBIOS>\Administrator`` (``core.sequences.definitions._admin_username``).
#: Hardcoded on purpose: the inheritance is specific to the builtin, so pointing
#: this at another local account would silently produce an unjoinable domain.
LOCAL_ADMIN_ACCOUNT = "Administrator"


@dataclass(frozen=True)
class AgentBundle:
    """The pki-executor payload to embed in a firstboot ISO.

    ``binary_path`` is the worker-host path to the agent exe;
    ``config_toml`` is the rendered ``executor.toml`` (identity + backend —
    no per-template config; that's dispatched after phone-home).
    """

    binary_path: Path
    config_toml: str


#: Server-side allowlist for the ``template`` param on createVm plan ops —
#: mirrors ``frontend/src/constants/templates.ts``. A template without role
#: scripts (standalone) still gets hostname + network.
TEMPLATE_IDS: frozenset[str] = frozenset(
    {
        "domainController",
        "certificateAuthority",
        "webServer",
        "client",
        "standalone",
        *LINUX_PRODUCT_TEMPLATES,
    }
)

_ASSETS = Path(__file__).parent.parent / "assets" / "firstboot"

_UNSAFE_HOSTNAME_CHARS = re.compile(r"[^A-Za-z0-9-]")


def platform_for_template(template: str) -> str:
    """Return the configgen platform for a server template."""

    return "linux" if template in LINUX_PRODUCT_TEMPLATES else "windows"


def hostname_for(vm_name: str) -> str:
    """Derive a valid Windows computer name from the (namespaced) VM name:
    safe charset, 15-char NetBIOS limit.

    Guest VM names are ``guest-<user>-[<project>-]<machine>``. The ``guest-``
    literal and the per-user segment don't help distinguish hosts on the shared
    guest subnet, so they're dropped and the OS hostname keeps the meaningful
    ``<project>-<machine>`` tail (e.g. ``guest-alice-467893-dc01`` → ``467893-
    dc01``) — readable *and* collision-safe across projects sharing the pool.
    The project code is fixed-width and kept intact; only a long machine name
    is clipped by the 15-char fit. Non-namespaced (operator) names are used
    as-is. Callers (firstboot render + the sequence engine's NodeContext) all
    route through this one function, so the OS hostname and the promoted DC name
    always agree.

    The tail split here deliberately does *not* reuse ``core/vm_naming``: that
    module answers "who owns this VM and which project is it in", where a
    six-alphanumeric second segment is a project code and anything else is part
    of the machine name. This one answers "what fits in 15 NetBIOS characters
    and stays unique on a shared subnet", where keeping an ambiguous segment is
    strictly safer than dropping it. Same string, different questions."""
    safe = _UNSAFE_HOSTNAME_CHARS.sub("-", vm_name).strip("-") or "vm"
    parts = safe.split("-")
    if parts[0] == "guest" and len(parts) > 2:
        safe = "-".join(parts[2:])  # drop the "guest" literal + <user> segment
    return safe[:15].strip("-") or "vm"


def linux_hostname_for(vm_name: str) -> str:
    """Derive a DNS-safe Linux hostname while retaining the project namespace."""

    safe = _UNSAFE_HOSTNAME_CHARS.sub("-", vm_name).strip("-").lower() or "vm"
    return safe[:63].strip("-") or "vm"


def role_scripts_for(template: str) -> list[Path]:
    """The template's fixed role scripts, in manifest order."""
    template_dir = _ASSETS / template
    if not template_dir.is_dir():
        return []
    extension = "*.sh" if platform_for_template(template) == "linux" else "*.ps1"
    return sorted(template_dir.glob(extension))


def build_firstboot_iso(
    *,
    template: str,
    vm_name: str,
    ip: str,
    net: GuestNetwork,
    dest_dir: Path,
    agent: AgentBundle | None = None,
    admin_password: str = "",
) -> Path:
    """Render + pack the per-VM config ISO into ``dest_dir``; returns its path.

    ``agent=None`` uses isokit v1 ``build_script_iso`` (hostname + network,
    plus any firstboot-only role scripts — none in the shipping templates).
    When an ``AgentBundle`` is given, the ISO switches to
    isokit's v2
    ``build_config_iso`` and additionally carries the agent binary + rendered
    ``executor.toml`` as payload files plus a static install step — so the
    booted VM installs and starts the phone-home agent.

    A non-empty ``admin_password`` on a Windows template additionally renders
    ``50-password.ps1``, resetting the guest's built-in local ``Administrator``
    (see ``LOCAL_ADMIN_ACCOUNT``). Blank — every template but a domain
    controller — renders nothing, as does any Linux template.

    Raises ``KeyError`` on an unknown template (routes validate against
    ``TEMPLATE_IDS`` first, so hitting it here is a programming error) and
    lets configgen/isokit ``ValueError``s propagate as op-level failures.
    """
    if template not in TEMPLATE_IDS:
        raise KeyError(f"Unknown template '{template}'.")

    platform = platform_for_template(template)
    extension = "sh" if platform == "linux" else "ps1"
    hostname = (
        linux_hostname_for(vm_name) if platform == "linux" else hostname_for(vm_name)
    )

    if platform == "linux" and agent is not None:
        raise ValueError(
            "The Windows executor agent cannot be bundled into a Linux template."
        )

    hostname_script = dest_dir / f"10-hostname.{extension}"
    hostname_script.write_text(
        configgen.render_hostname(platform, hostname), encoding="utf-8"
    )

    network_script = dest_dir / f"20-network.{extension}"
    network_script.write_text(
        configgen.render_network(
            platform,
            NetworkConfig(
                mode="static",
                ip=ip,
                prefix=net.prefix,
                gateway=net.gateway,
                dns1=net.dns1,
                dns2=net.dns2,
                dns_suffix=net.dns_suffix,
            ),
        ),
        encoding="utf-8",
    )

    scripts = [hostname_script, network_script, *role_scripts_for(template)]

    # A domain controller's credential bootstrap. Kept out of ``scripts`` so both
    # ISO paths can pack it *after* the agent install step: if this throws, the
    # runner fails fast and never reboots (so the agent service never starts
    # either way), but the binary and its service registration are already
    # persisted — a power-cycle then yields a connected, diagnosable VM. Running
    # it earlier would abort before the agent was even copied.
    password_scripts: list[Path] = []
    if admin_password and platform == "windows":
        password_script = dest_dir / "50-password.ps1"
        password_script.write_text(
            configgen.render_password(platform, LOCAL_ADMIN_ACCOUNT, admin_password),
            encoding="utf-8",
        )
        password_scripts.append(password_script)

    iso_path = dest_dir / f"{vm_name}-config.iso"

    if agent is None:
        isokit.build_script_iso([*scripts, *password_scripts], iso_path)
        return iso_path

    # v2: embed the agent binary + config as payload files and append
    # the install step. build_config_iso names disc entries after each path's
    # filename, so the binary is copied to the fixed name the runner expects.
    config_path = dest_dir / _AGENT_CONFIG_NAME
    config_path.write_text(agent.config_toml, encoding="utf-8")
    binary_on_disc = dest_dir / _AGENT_BINARY_NAME
    if agent.binary_path != binary_on_disc:
        shutil.copy(agent.binary_path, binary_on_disc)

    isokit.build_config_iso(
        iso_path,
        scripts=[*scripts, _AGENT_INSTALL_SCRIPT, *password_scripts],
        files=[binary_on_disc, config_path],
    )
    return iso_path


def build_authored_iso(
    files: list[tuple[str, str]],
    *,
    vm_name: str,
    dest_dir: Path,
) -> Path:
    """Pack operator-authored ``(name, content)`` scripts — exactly as received,
    in the order received (the frontend sends them name-sorted, matching the
    ``10-/20-/30-`` convention) — into ``dest_dir``; returns the ISO's path.

    The authored set is the complete disc: nothing is rendered or appended
    server-side. Names/sizes were validated in ``routers/deploy.py``; isokit
    ``ValueError``s propagate as op-level failures.
    """
    script_paths: list[Path] = []
    for name, content in files:
        path = dest_dir / name
        path.write_text(content, encoding="utf-8")
        script_paths.append(path)

    iso_path = dest_dir / f"{vm_name}-config.iso"
    isokit.build_config_iso(iso_path, scripts=script_paths)
    return iso_path
