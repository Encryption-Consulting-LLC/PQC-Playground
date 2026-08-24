"""Which account a remote-desktop session signs in as.

Sessions are credential-free from the user's side, so this resolution is the
whole authentication story — and both halves of it are silent when wrong. A
domain controller resolved as ``.\\Administrator`` would fail at the RDP login
prompt (promotion consumed the local account), and a member server resolved as
``<NETBIOS>\\Administrator`` would sign in as a domain admin the operator never
intended to hand out. ``None`` has to stay a first-class answer: the routes turn
it into "redeploy this node", which is honest, where opening a doomed connection
is not.
"""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.console.credentials import resolve_console_credentials  # noqa: E402
from app.core.secrets import encrypt_secret  # noqa: E402
from app.core.template_config import encrypt_config_secrets  # noqa: E402

DC_PASSWORD = "Str0ng-Lab-Pass!"
LOCAL_PASSWORD = "gen-Erated-24charPass1"


def _dc_row(**config_overrides):
    config = {
        "domainName": "encon.pki",
        "netbiosName": "ENCON",
        "domainAdminPassword": DC_PASSWORD,
    }
    config.update(config_overrides)
    return {
        "vmName": "guest-alice-9e4edb-dc01",
        "agent": {
            "templateId": "domainController",
            "templateConfig": encrypt_config_secrets("domainController", config),
        },
    }


def _member_row(template="certificateAuthority", password=LOCAL_PASSWORD):
    row = {
        "vmName": "guest-alice-9e4edb-ca01",
        "agent": {"templateId": template, "templateConfig": {}},
    }
    if password is not None:
        row["localAdminPasswordEnc"] = encrypt_secret(password)
    return row


def test_domain_controller_uses_the_promoted_domain_administrator():
    creds = resolve_console_credentials(_dc_row())

    assert creds is not None
    assert creds.username == "ENCON\\Administrator"
    assert creds.password == DC_PASSWORD


def test_domain_controller_without_a_netbios_name_falls_back_to_a_bare_name():
    """Same formatter the sequence engine uses, so a session and a domain join
    can never disagree about who they are."""

    creds = resolve_console_credentials(_dc_row(netbiosName=""))

    assert creds is not None
    assert creds.username == "Administrator"


def test_domain_controller_without_a_stored_password_has_no_session():
    row = _dc_row()
    row["agent"]["templateConfig"] = {"netbiosName": "ENCON"}

    assert resolve_console_credentials(row) is None


def test_member_server_uses_its_generated_local_administrator():
    creds = resolve_console_credentials(_member_row())

    assert creds is not None
    # ``.\`` is load-bearing: a domain-joined guest resolves a bare name against
    # the domain first, and this password belongs to the local SAM account.
    assert creds.username == ".\\Administrator"
    assert creds.password == LOCAL_PASSWORD


def test_member_server_without_a_stored_envelope_has_no_session():
    """The pre-console fleet. Nothing sets a password inside a running guest, so
    the only honest answer is a redeploy."""

    assert resolve_console_credentials(_member_row(password=None)) is None


def test_linux_template_has_no_session():
    """No RDP on the Linux product templates, and no credential was ever set."""

    row = _member_row(template="certsecure")

    assert resolve_console_credentials(row) is None


def test_a_row_with_no_agent_falls_back_to_the_local_envelope():
    """A clone that failed before minting an agent identity still has the
    password firstboot applied — that is exactly the VM someone wants to open."""

    creds = resolve_console_credentials(
        {
            "vmName": "guest-alice-9e4edb-ca01",
            "localAdminPasswordEnc": encrypt_secret(LOCAL_PASSWORD),
        }
    )

    assert creds is not None
    assert creds.password == LOCAL_PASSWORD


def test_credentials_never_carry_the_password_in_the_display_label():
    for row in (_dc_row(), _member_row()):
        creds = resolve_console_credentials(row)
        assert creds is not None
        assert creds.password not in creds.label
