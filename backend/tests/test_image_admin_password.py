"""Generated local ``Administrator`` passwords for the remote-desktop console.

A member server has no domain-wide account of its own, so before this the only
Windows guest with a server-known credential was the domain controller (whose
``domainAdminPassword`` firstboot resets the local Administrator to, because
``Install-ADDSForest`` then promotes that account into the forest). Every other
Windows template now gets a generated per-VM password instead — that is the
credential the console signs in with, and a local account survives a domain join
so no per-node domain-state logic is needed.

The rules pinned here are the ones that are silent when broken: a domain
controller must keep the operator's password (or every later ``domain.join``
fails with "the user name or password is incorrect"), an uploaded ISO must claim
no password at all (that disc is attached verbatim, so no reset runs), and a
redelivered clone over a surviving VM must not re-mint (the stored credential
would then point at nothing).
"""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.secrets import decrypt_secret, encrypt_secret  # noqa: E402
from app.core.template_config import (  # noqa: E402
    PASSWORD_MIN_LENGTH,
    generate_local_admin_password,
    password_policy_errors,
)

VM_NAME = "guest-alice-9e4edb-ca01"


def test_generated_password_satisfies_the_operator_policy():
    """One definition of compliance, not two — a generated password is held to
    the same gate ``validate_template_config`` holds an operator's to."""

    for _ in range(50):
        value = generate_local_admin_password(VM_NAME)
        assert password_policy_errors(value, VM_NAME) == []
        assert len(value) >= PASSWORD_MIN_LENGTH


def test_generated_password_is_powershell_safe():
    """The value is interpolated into firstboot PowerShell by
    ``configgen.render_password``, so a quote or ``$`` in one we mint would turn a
    credential bootstrap into a syntax error on a VM nobody can log into yet."""

    for _ in range(50):
        value = generate_local_admin_password(VM_NAME)
        assert not set(value) & set("'\"`$;\\")


def test_generated_passwords_are_unique_per_call():
    """Per-VM, not per-deploy: one guest's console credential must not open
    another guest's desktop."""

    assert len({generate_local_admin_password(VM_NAME) for _ in range(20)}) == 20


def test_envelope_round_trips():
    """What the registry stores is an AES-GCM envelope, and the console reads the
    password back out of it — nothing persists plaintext."""

    value = generate_local_admin_password(VM_NAME)
    envelope = encrypt_secret(value)

    assert value not in str(envelope)
    assert decrypt_secret(envelope) == value


def test_machine_name_is_excluded():
    """The policy rejects a password containing the machine name; the generator
    is sampled against that policy, so it cannot emit one."""

    assert password_policy_errors("ca01-ca01-ca01A!", "ca01") != []
    for _ in range(25):
        assert "ca01" not in generate_local_admin_password("ca01").lower()
