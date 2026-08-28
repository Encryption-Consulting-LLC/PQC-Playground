"""Which ``Administrator`` password a clone puts on the disc, and stores.

Three rules, each silent when broken — the guest boots, the deploy goes green,
and the failure only shows up at a login prompt days later:

1. A **domain controller** keeps the operator's ``domainAdminPassword``. Anything
   else and every later ``domain.join`` fails with "the user name or password is
   incorrect", because ``Install-ADDSForest`` promotes that account into the
   forest keeping its password.
2. **Every other Windows guest** keeps the *golden image's* password. It is
   re-asserted, never invented: a per-VM value minted by the server is one the
   operator cannot type at the VM console, which is exactly the regression that
   made a working ``dc01`` sit beside member servers nobody could sign into.
3. The stored console credential never outlives the password it describes. The
   registry row is not deleted at teardown, so a redeploy of the same VM name
   that sets no password must **clear** the envelope rather than leave the
   previous VM's behind.
"""

import os

import pytest

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app import tasks  # noqa: E402
from app.core.golden_image import GoldenImageConfig  # noqa: E402
from app.core.ippool import GuestNetwork  # noqa: E402
from app.core.secrets import decrypt_secret  # noqa: E402
from app.routers.deploy import PlanOp  # noqa: E402

IMAGE_PASSWORD = "Gold3n-Image-Pass!"
OPERATOR_PASSWORD = "Op3rator-Set-Pass!"


class _Conn:
    content = object()
    si = object()


@pytest.fixture
def clone(monkeypatch):
    """Drive ``_run_clone_op`` to the ISO build and capture both halves of the
    decision: what ``build_firstboot_iso`` was asked to render, and what the
    ``cloning`` upsert stored."""

    def _run(template: str, image_password: str, params: dict | None = None):
        upserts: list[dict] = []
        iso_kwargs: dict = {}

        monkeypatch.setattr(
            tasks,
            "load_guest_network_sync",
            lambda db: GuestNetwork(
                ip_start="10.0.20.50",
                ip_end="10.0.20.60",
                prefix=24,
                gateway="10.0.20.1",
                dns1="10.0.20.1",
            ),
        )
        monkeypatch.setattr(
            tasks, "allocate_ip_sync", lambda db, name, job_id: "10.0.20.51"
        )
        monkeypatch.setattr(
            tasks,
            "load_image_admin_password_sync",
            lambda db: image_password,
        )
        monkeypatch.setattr(
            tasks,
            "_registry_upsert_sync",
            lambda db, name, **fields: upserts.append(fields),
        )
        monkeypatch.setattr(tasks.settings, "executor_agent_path", None)
        monkeypatch.setattr(tasks, "get_vm_by_name", lambda content, name: None)
        monkeypatch.setattr(tasks, "_cleanup_failed_clone", lambda *a, **kw: None)

        def _capture(**kwargs):
            iso_kwargs.update(kwargs)
            raise RuntimeError("stop once the disc's contents are decided")

        monkeypatch.setattr(tasks, "build_firstboot_iso", _capture)

        op = PlanOp(id="clone-1", kind="createVm", target="node-1")
        op.params = {
            "vmName": f"guest-alice-467893-{template[:4]}",
            "template": template,
            **(params or {}),
        }

        assert (
            tasks._run_clone_op(
                _Conn(),
                {},
                op,
                "job-1",
                {},
                lambda: None,
                "guest",
                GoldenImageConfig(
                    base="ws-2025-base",
                    datastore="datastore1",
                    expectedGuestOs="windows2022srvNext-64",
                    network="VM Network",
                    maxUsagePct=80.0,
                ),
            )
            is False
        )
        return iso_kwargs, upserts[0]

    return _run


def test_a_member_server_keeps_the_golden_image_password(clone):
    """Rule 2 — the disc re-asserts the image's own password, and that same value
    is what remote desktop will later sign in with."""

    iso_kwargs, fields = clone("webServer", IMAGE_PASSWORD)

    assert iso_kwargs["admin_password"] == IMAGE_PASSWORD
    assert decrypt_secret(fields["localAdminPasswordEnc"]) == IMAGE_PASSWORD


def test_two_guests_from_one_image_share_one_password(clone):
    """The password is a fact about the image, not about the VM. Per-VM values
    were the regression: the operator knows one password for the lab."""

    first, _ = clone("webServer", IMAGE_PASSWORD)
    second, _ = clone("certificateAuthority", IMAGE_PASSWORD)

    assert first["admin_password"] == second["admin_password"] == IMAGE_PASSWORD


def test_a_domain_controller_keeps_the_operator_password(clone):
    """Rule 1 — and it stores no local envelope: after promotion the local account
    is gone, so the console must resolve the domain credential instead."""

    iso_kwargs, fields = clone(
        "domainController",
        IMAGE_PASSWORD,
        {"domainAdminPassword": OPERATOR_PASSWORD, "domainName": "lab.local"},
    )

    assert iso_kwargs["admin_password"] == OPERATOR_PASSWORD
    assert fields["localAdminPasswordEnc"] is None


def test_an_unrecorded_image_password_resets_nothing(clone):
    """Rule 3 — a blank setting must leave the guest's imaged password alone *and*
    clear any envelope on a surviving row, rather than claim a credential the
    console would then fail on."""

    iso_kwargs, fields = clone("webServer", "")

    assert iso_kwargs["admin_password"] == ""
    assert fields["localAdminPasswordEnc"] is None
