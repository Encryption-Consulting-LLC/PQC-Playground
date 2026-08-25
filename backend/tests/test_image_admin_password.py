"""Where a non-domain-controller Windows guest's ``Administrator`` password comes
from.

It comes from the **golden image**, and that is the whole point. A guest cloned
from the image answers to the password it was imaged with, so the credential an
operator types at the VM console, the one the remote-desktop session uses, and
the one recorded in settings are all the same string. A per-VM value minted by
the server would be a password no human could type — the regression this suite
exists to keep out.

The domain controller is the single exception and is covered by
``test_firstboot_password`` and ``test_console_credentials``: promotion turns its
local Administrator into the forest's domain Administrator, so its credential
must stay the operator's ``domainAdminPassword``.
"""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.golden_image import (  # noqa: E402
    image_admin_password_from_doc,
    load_image_admin_password_sync,
)
from app.core.secrets import decrypt_secret, encrypt_secret  # noqa: E402
from app.core.settings import settings  # noqa: E402

PASSWORD = "Str0ng-Lab-Pass!"


class _FakeDb:
    """Just enough of the worker's sync database for the one read."""

    def __init__(self, doc):
        self._doc = doc

    def __getitem__(self, name):
        assert name == "settings"
        return self

    def find_one(self, query):
        assert query == {"_id": "global"}
        return self._doc


def test_the_stored_password_round_trips():
    """Settings hold ciphertext, never the password — same envelope as the ESXi
    target's."""

    envelope = encrypt_secret(PASSWORD)

    assert PASSWORD not in str(envelope)
    assert decrypt_secret(envelope) == PASSWORD
    assert (
        image_admin_password_from_doc({"cloneAdminPasswordEnc": envelope}) == PASSWORD
    )


def test_the_settings_document_wins_over_the_env_seed(monkeypatch):
    """``CLONE_ADMIN_PASSWORD`` only seeds the document; an admin's later edit in
    the console is authoritative, exactly as it is for the ESXi target."""

    monkeypatch.setattr(settings, "clone_admin_password", "env-seed-value")

    doc = {"cloneAdminPasswordEnc": encrypt_secret(PASSWORD)}

    assert image_admin_password_from_doc(doc) == PASSWORD


def test_the_env_seed_covers_a_document_that_predates_the_field(monkeypatch):
    """An existing deployment's settings document has no ``cloneAdminPasswordEnc``
    until the seeder backfills it, and the clone task must not go blank in the
    meantime."""

    monkeypatch.setattr(settings, "clone_admin_password", PASSWORD)

    assert image_admin_password_from_doc({}) == PASSWORD
    assert image_admin_password_from_doc(None) == PASSWORD


def test_unrecorded_reads_as_blank(monkeypatch):
    """Blank is a real answer, not an error: firstboot then resets nothing (the
    guest keeps its image password either way) and no console credential is
    stored, which the console route turns into an actionable 409."""

    monkeypatch.setattr(settings, "clone_admin_password", "")

    assert image_admin_password_from_doc({}) == ""
    assert image_admin_password_from_doc({"cloneAdminPasswordEnc": None}) == ""


def test_an_undecryptable_envelope_reads_as_blank(monkeypatch):
    """A rotated ``SETTINGS_ENC_KEY`` costs the console credential; it must not
    fail every clone."""

    monkeypatch.setattr(settings, "clone_admin_password", "")

    corrupt = encrypt_secret(PASSWORD) | {"ciphertext": "not-base64-ciphertext"}

    assert image_admin_password_from_doc({"cloneAdminPasswordEnc": corrupt}) == ""


def test_the_worker_reads_the_same_singleton():
    """The clone task runs on a sync PyMongo client of its own, so this is the
    path that actually decides what lands on the disc."""

    doc = {"_id": "global", "cloneAdminPasswordEnc": encrypt_secret(PASSWORD)}

    assert load_image_admin_password_sync(_FakeDb(doc)) == PASSWORD
