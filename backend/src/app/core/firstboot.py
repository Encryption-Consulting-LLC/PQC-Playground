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

The **Linux product templates get an agent too**, from the same crate release
built for ``x86_64-unknown-linux-musl`` — ``_AGENT_BINARY_NAMES`` and
``_AGENT_INSTALL_SCRIPTS`` are keyed by guest platform for exactly that. They
additionally carry the product's ~489 MB installation tree as a disc payload
plus a ``45-`` step that verifies its digest and extracts it. That step
deliberately **stages without installing**: firstboot runs before the agent
exists, so a failure there is invisible until a phone-home that never comes,
whereas a copy and an extract are the two things that cannot meaningfully fail
on a disc that mounted. The 595-second vendor install is a sequence step with
progress, retries and a visible error.

``admin_password`` renders a ``Set-LocalUser`` step for the built-in local
``Administrator``, and every Windows template gets one. It cannot be an executor
step: ``Install-ADDSForest`` promotes the *local* Administrator into the new
forest as the built-in **domain** Administrator, keeping its password, so the
reset has to land *before* promotion — and the agent's command set has no way to
set a password at all.

The password's *source* differs by role, and the caller decides (see
``tasks.py``): a domain controller uses the operator's ``domainAdminPassword``,
because that is what every later ``domain.join`` and the issuing-CA install
authenticate with as ``<NETBIOS>\\Administrator``. Every other Windows template
re-asserts the golden image's *own* built-in Administrator password (the
admin-recorded ``cloneAdminPassword`` setting), so the guest answers to the
password it was imaged with rather than one the platform invented — the reset is
a no-op on an image that persisted it and self-healing on one that did not. That
same value is persisted AES-GCM on the VM's ``vm_registry`` row so remote desktop
has a credential to sign in with: a member server has no domain-wide account of
its own, and a local account survives a domain join.

Numbering fixes manifest (execution) order: ``10-`` hostname, ``20-`` network,
``30-`` Remote Desktop enablement (every Windows template), ``40-`` agent install
(bundled path only), ``45-`` product payload staging (product templates only),
``50-`` local Administrator password. isokit packs in the order received, so the
list handed to it *is* the manifest order — the numeric names only document it.

``30-enable-rdp.ps1`` is the other firstboot-only step, for the same reason the
agent installer is one: it has to work on a VM whose agent never came up, which
is precisely when someone needs to look inside. It is likewise not overridable
from PACK mode.
Scripts never reboot — the firstboot runner in the base image owns the single
reboot (established configgen convention).

The disc is not a secret store: it already ships the agent's bearer token in
``executor.toml`` and a Windows guest's local Administrator password as plaintext
PowerShell, and vmkit leaves it attached at
``[datastore] <vm>/<vm>-config.iso`` for the VM's lifetime. Generalizing the
password step from domain controllers to every Windows template adds one more
password to a disc that already had this property; it does not change the threat
model, but it does mean *every* lab VM's disc now carries one.

Operator-authored scripts (the Inspector's Config ISO panel, PACK mode) are
*layered over* this set rather than replacing it: ``authored_scripts`` lets a
file override the server-rendered script of the same name and appends anything
else, but the pool IP, the agent, and the DC's password reset are injected
either way. So an operator can hand-edit ``10-hostname.ps1`` without silently
producing a VM that never phones home. Only a pre-built **uploaded** ISO
bypasses this module entirely — that disc is attached verbatim.
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

_AGENT_DIR = Path(__file__).parent.parent / "assets" / "firstboot" / "_agent"

#: On-disc name for the embedded agent binary, per guest platform — what the
#: matching install script expects (see ``assets/firstboot/_agent``). Both are
#: the same crate release built for two targets, so the *contents* differ but
#: nothing else about the bundling does.
_AGENT_BINARY_NAMES = {"windows": "pki-executor.exe", "linux": "pki-executor"}
_AGENT_CONFIG_NAME = "executor.toml"
#: Static install step appended when an agent is bundled, per platform. Lives
#: under ``_agent`` (leading underscore → not a template dir, never
#: role-globbed).
_AGENT_INSTALL_SCRIPTS = {
    "windows": _AGENT_DIR / "40-install-executor.ps1",
    "linux": _AGENT_DIR / "40-install-executor.sh",
}
#: Static product-payload staging step, packed after the agent installer on a
#: product template. A template rendered with placeholders (the payload's name,
#: its digest and the install directory) rather than parameterised at run time,
#: because firstboot scripts take no arguments.
_PAYLOAD_STAGE_SCRIPT = _AGENT_DIR / "45-stage-certsecure.sh"

#: Where ``45-stage-certsecure.sh`` extracts the product tree. Fixed rather than
#: configurable: the agent's ``certsecure.*`` commands and its ``file.*`` relay
#: allowlist are both written against this exact path, so one side moving alone
#: breaks the other silently.
PRODUCT_INSTALL_DIR = "/opt/certsecure-manager"


@dataclass(frozen=True)
class ProductPayload:
    """A product's installation tree, shipped on the firstboot ISO.

    ``archive_path`` is the worker-host path to the ``.tar.gz``; ``sha256`` is
    baked into the staging script so the guest verifies what it extracts. The
    archive rides the disc as an isokit payload *file*, which is why this is
    ~489 MB of ISO per product VM — the acknowledged cost of ISO delivery, whose
    escape hatch is a golden image with the tree pre-staged (at which point the
    staging step becomes a no-op and nothing else changes).
    """

    archive_path: Path
    sha256: str


#: The product archive's name *on the disc*. isokit names each entry after its
#: path's filename, and the operator's downloaded archive is named whatever the
#: vendor called it — "CertSecure Manager v3.3 - CLM Server.tar.gz" as shipped.
#: Pinning the disc name keeps the staging script's baked-in filename
#: independent of that, and keeps it inside Joliet's 64-character limit.
PRODUCT_PAYLOAD_DISC_NAME = "product-payload.tar.gz"


def _payload_on_disc(archive_path: Path, dest_dir: Path) -> Path:
    """Present ``archive_path`` under :data:`PRODUCT_PAYLOAD_DISC_NAME`.

    A symlink, not a copy: the archive is ~489 MB and isokit streams it by
    path, so copying would double both the disk footprint and the build time of
    every product clone to rename a file. Falls back to a copy on a filesystem
    that refuses the link.
    """
    if archive_path.name == PRODUCT_PAYLOAD_DISC_NAME:
        return archive_path
    link = dest_dir / PRODUCT_PAYLOAD_DISC_NAME
    try:
        link.symlink_to(archive_path.resolve())
    except OSError:
        shutil.copy(archive_path, link)
    return link


#: Static Remote Desktop enablement step, packed for every Windows template.
#: Non-overridable for the same reason the agent installer is: a PACK-mode file
#: of the same name silently disabling remote desktop across a whole lab is not a
#: capability the Config ISO panel should hand out. Lives under ``_rdp``
#: (leading underscore → not a template dir, never role-globbed).
_RDP_ENABLE_SCRIPT = (
    Path(__file__).parent.parent / "assets" / "firstboot" / "_rdp" / "30-enable-rdp.ps1"
)
#: The Windows built-in local administrator. ``Install-ADDSForest`` carries *this*
#: account's password into the new forest as the built-in **domain** Administrator
#: password — the credential every later step signs in with as
#: ``<NETBIOS>\Administrator`` (``core.sequences.definitions.admin_username``).
#: Hardcoded on purpose: the inheritance is specific to the builtin, so pointing
#: this at another local account would silently produce an unjoinable domain.
LOCAL_ADMIN_ACCOUNT = "Administrator"

#: Disc filenames a PACK-mode author can never supply. The agent installer is
#: what makes the VM reachable at all, the RDP step is what makes it *visible*
#: at all, the payload staging step is what puts the product on disk — and
#: packing two entries under one disc filename is an isokit collision either
#: way. Both agent installers are listed regardless of platform: the check runs
#: over authored *names*, and a Windows author naming the Linux script (or the
#: reverse) should be refused the same way rather than have it land as an extra
#: script the runner then executes.
_NON_OVERRIDABLE_SCRIPT_NAMES = frozenset(
    {
        *(path.name for path in _AGENT_INSTALL_SCRIPTS.values()),
        _PAYLOAD_STAGE_SCRIPT.name,
        _RDP_ENABLE_SCRIPT.name,
    }
)


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
    authored_scripts: list[tuple[str, str]] | None = None,
    payload: ProductPayload | None = None,
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
    (see ``LOCAL_ADMIN_ACCOUNT``). Blank renders nothing, as does any Linux
    template. Callers pass one for every Windows template whose password is
    known — the DC's operator-set ``domainAdminPassword``, or the golden image's
    own Administrator password for the rest.

    Every Windows template also gets the static ``30-enable-rdp.ps1`` step, which
    is not overridable via ``authored_scripts``.

    ``payload`` ships a product's installation tree as an additional disc file
    plus a ``45-`` staging step that copies it out of the (transient) firstboot
    files directory, verifies its digest and extracts it. It requires an
    ``agent`` for the same reason the staging step does not install anything:
    the install is a sequence step, and a disc that carried the tree with no
    agent to act on it would produce a VM nobody can finish provisioning.

    ``authored_scripts`` is the operator's ``(name, content)`` set from the
    Config ISO panel, layered over the above: a name matching one of the
    rendered scripts *replaces* it (that render is then skipped, so the authored
    text is what boots), and any other name is appended after the role scripts
    in name order. The agent install step is deliberately not overridable —
    the panel presents it as a non-removable row, so an authored file of the
    same name is dropped rather than duplicated onto the disc.

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

    if payload is not None and agent is None:
        raise ValueError(
            "A product payload needs a bundled agent — nothing would install it."
        )

    # Authored files land on disc first so the renders below can check whether
    # they've already been supplied — same directory, so rendering over one
    # would clobber the operator's text.
    authored: dict[str, Path] = {}
    for name, content in authored_scripts or []:
        # Never overridable: the agent's install step is what makes the VM
        # reachable at all, the RDP step is what makes it *visible* at all, and
        # packing two entries under one disc filename is an isokit collision.
        # Skipped before the write so no shadow copy is left in dest_dir to
        # confuse anyone reading the build directory.
        if name in _NON_OVERRIDABLE_SCRIPT_NAMES:
            continue
        path = dest_dir / name
        path.write_text(content, encoding="utf-8")
        authored[name] = path

    hostname_script = authored.pop(f"10-hostname.{extension}", None)
    if hostname_script is None:
        hostname_script = dest_dir / f"10-hostname.{extension}"
        hostname_script.write_text(
            configgen.render_hostname(platform, hostname), encoding="utf-8"
        )

    network_script = authored.pop(f"20-network.{extension}", None)
    if network_script is None:
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

    # A domain controller's credential bootstrap. Kept out of ``scripts`` so it
    # packs *after* the agent install step: if this throws, the runner fails fast
    # and never reboots (so the agent service never starts either way), but the
    # binary and its service registration are already persisted — a power-cycle
    # then yields a connected, diagnosable VM. Running it earlier would abort
    # before the agent was even copied. Popped here rather than left to
    # ``extra_scripts`` so an authored override keeps that late position.
    password_scripts: list[Path] = []
    password_override = authored.pop("50-password.ps1", None)
    if password_override is not None:
        password_scripts.append(password_override)
    elif admin_password and platform == "windows":
        password_script = dest_dir / "50-password.ps1"
        password_script.write_text(
            configgen.render_password(platform, LOCAL_ADMIN_ACCOUNT, admin_password),
            encoding="utf-8",
        )
        password_scripts.append(password_script)

    # What's left is genuinely additional. Sorted so the manifest order the
    # numeric prefixes document is the order isokit receives.
    extra_scripts = [authored[name] for name in sorted(authored)]
    # Windows only: there is no RDP on the Linux product templates, and guacd
    # would need a different protocol (and credentials we never set) for them.
    rdp_scripts = [_RDP_ENABLE_SCRIPT] if platform == "windows" else []
    scripts = [
        hostname_script,
        network_script,
        *rdp_scripts,
        *role_scripts_for(template),
        *extra_scripts,
    ]

    iso_path = dest_dir / f"{vm_name}-config.iso"

    if agent is None:
        isokit.build_script_iso([*scripts, *password_scripts], iso_path)
        return iso_path

    # v2: embed the agent binary + config as payload files and append
    # the install step. build_config_iso names disc entries after each path's
    # filename, so the binary is copied to the fixed name the runner expects.
    config_path = dest_dir / _AGENT_CONFIG_NAME
    config_path.write_text(agent.config_toml, encoding="utf-8")
    binary_on_disc = dest_dir / _AGENT_BINARY_NAMES[platform]
    if agent.binary_path != binary_on_disc:
        shutil.copy(agent.binary_path, binary_on_disc)

    install_script = _AGENT_INSTALL_SCRIPTS[platform]
    agent_scripts = [install_script]
    files = [binary_on_disc, config_path]
    if payload is not None:
        # Rendered rather than copied: a firstboot script takes no arguments, so
        # the payload's disc name, its digest and the install directory have to
        # be baked in. Written into ``dest_dir`` alongside the other renders so
        # the disc carries exactly what this build resolved.
        stage_script = dest_dir / _PAYLOAD_STAGE_SCRIPT.name
        stage_script.write_text(
            _PAYLOAD_STAGE_SCRIPT.read_text(encoding="utf-8")
            .replace("__PAYLOAD_NAME__", PRODUCT_PAYLOAD_DISC_NAME)
            .replace("__PAYLOAD_SHA256__", payload.sha256)
            .replace("__INSTALL_DIR__", PRODUCT_INSTALL_DIR),
            encoding="utf-8",
        )
        agent_scripts.append(stage_script)
        files.append(_payload_on_disc(payload.archive_path, dest_dir))

    isokit.build_config_iso(
        iso_path,
        scripts=[*scripts, *agent_scripts, *password_scripts],
        files=files,
    )
    return iso_path
