"""Which Windows account a VM's remote-desktop session signs in as.

Sessions are credential-free from the user's point of view: they press Connect
and land on a desktop. The password never reaches the browser — it is read here,
put in a short-lived ticket, and handed to guacd. So this module answers "does
the server know a credential for this VM, and which one", and the answer comes
entirely from the VM's own registry row.

There are exactly two answers, and the split is not cosmetic:

* A **domain controller** signs in as ``<NETBIOS>\\Administrator`` with the
  operator's ``domainAdminPassword``. Firstboot reset the guest's *local*
  Administrator to it and ``Install-ADDSForest`` then promoted that account into
  the new forest keeping its password — so by the time anyone can connect, the
  local account is gone and the domain one has that password. This is the same
  credential ``core.sequences.definitions`` authenticates every ``domain.join``
  and the issuing-CA install with, which is why the username is formatted by
  that module's ``admin_username`` rather than re-derived here.
* **Everything else Windows** signs in as ``.\\Administrator`` with the generated
  per-VM password from its ``localAdminPasswordEnc`` envelope. A member server
  has no domain-wide account of its own, and a local account keeps working after
  a domain join — so this needs no per-node domain-state logic and stays correct
  whether the node has joined, left, or never joined a domain.

``None`` means "no session is possible", and the routes turn that into an
actionable 409 rather than opening a connection that will fail at the RDP login
prompt. It happens for a Linux product template (no RDP at all), and for any VM
cloned before the console existed — nothing retroactively sets a password inside
a running guest, so those need a redeploy.
"""

from dataclasses import dataclass

from app.core.firstboot import LOCAL_ADMIN_ACCOUNT, platform_for_template
from app.core.secrets import SecretDecryptionError, decrypt_secret
from app.core.sequences.definitions import admin_username
from app.core.template_config import (
    DOMAIN_ADMIN_PASSWORD_KEY,
    decrypt_config_secrets,
)

#: The one template whose credential is the operator's, not a generated one.
DOMAIN_CONTROLLER_TEMPLATE = "domainController"


@dataclass(frozen=True)
class ConsoleCredentials:
    """A resolved RDP login. ``password`` is plaintext and must not be serialized
    to any client — ``label`` is the display-safe half."""

    username: str
    password: str
    label: str


def resolve_console_credentials(row: dict) -> ConsoleCredentials | None:
    """The RDP login for the VM described by *row*, or ``None`` if none is known.

    *row* is a ``vm_registry`` document. Reads only that document (plus the
    encryption key), so this is unit-testable without Mongo.
    """
    agent = row.get("agent") or {}
    template = agent.get("templateId") or ""

    # A template we do not recognise is treated as Windows-with-no-credential
    # rather than guessed at: platform_for_template only knows the shipping set.
    if template and platform_for_template(template) != "windows":
        return None

    if template == DOMAIN_CONTROLLER_TEMPLATE:
        return _domain_controller_credentials(agent)
    return _local_credentials(row)


def _domain_controller_credentials(agent: dict) -> ConsoleCredentials | None:
    stored = agent.get("templateConfig") or {}
    try:
        config = decrypt_config_secrets(DOMAIN_CONTROLLER_TEMPLATE, stored)
    except SecretDecryptionError:
        # A rotated SETTINGS_ENC_KEY. Surfacing "no credentials" beats a 500:
        # nothing the operator does to this VM will recover it.
        return None
    password = config.get(DOMAIN_ADMIN_PASSWORD_KEY, "")
    if not password:
        return None
    netbios = config.get("netbiosName") or None
    return ConsoleCredentials(
        username=admin_username(netbios),
        password=password,
        label=admin_username(netbios),
    )


def _local_credentials(row: dict) -> ConsoleCredentials | None:
    envelope = row.get("localAdminPasswordEnc")
    if not envelope:
        return None
    try:
        password = decrypt_secret(dict(envelope))
    except SecretDecryptionError:
        return None
    # ``.\`` scopes the login to the local SAM explicitly. Without it a
    # domain-joined guest resolves a bare name against the domain first, and the
    # local Administrator this password belongs to is not a domain account.
    return ConsoleCredentials(
        username=f".\\{LOCAL_ADMIN_ACCOUNT}",
        password=password,
        label=f"{LOCAL_ADMIN_ACCOUNT} (local)",
    )
