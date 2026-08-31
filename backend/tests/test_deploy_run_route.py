"""``GET /api/deploy/{job_id}`` — the facts a reconnecting client can't rebuild.

A reload loses three things the Staged panel renders next to live op state: when
the run started (the elapsed clock), what preflight verified (the receipt), and
the compiled manifest (labels + expandable step trees). All three are already on
the run document, so this route is what makes a resumed deploy look like the one
that was running before the tab reloaded.
"""

import asyncio
import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.authz import AuthedUser, Role  # noqa: E402
from app.routers import deploy as deploy_router  # noqa: E402
from app.routers.deploy import get_deploy_run  # noqa: E402

JOB_ID = "b6a1f0c3d4e54f0a9b2c3d4e5f607182"

TOPOLOGY = {
    "version": 1,
    "nodes": [
        {
            "id": "dc",
            "name": "DC01",
            "role": "domainController",
            "config": {
                "domainName": "encon.pki",
                "netbiosName": "ENCON",
                # Exactly what redact_evidence leaves behind on the stored doc.
                "domainAdminPassword": "***",
            },
        },
        {"id": "root", "name": "CA01", "role": "rootCa"},
        {"id": "issuing", "name": "CA02", "role": "issuingCa"},
        {"id": "web", "name": "SRV1", "role": "webServer"},
    ],
    "edges": [
        {"id": "parent", "kind": "caParent", "source": "root", "target": "issuing"},
        {
            "id": "issuing-domain",
            "kind": "domainMembership",
            "source": "issuing",
            "target": "dc",
        },
        {
            "id": "web-domain",
            "kind": "domainMembership",
            "source": "web",
            "target": "dc",
        },
        {
            "id": "publication",
            "kind": "caPublication",
            "source": "issuing",
            "target": "web",
        },
    ],
}

OPERATIONS = [
    {
        "id": "create-dc",
        "kind": "createVm",
        "target": "dc",
        "params": {
            "vmName": "DC01",
            "template": "domainController",
            "domainAdminPassword": "***",
        },
        "dependsOn": [],
    },
    {
        "id": "create-root",
        "kind": "createVm",
        "target": "root",
        "params": {
            "vmName": "CA01",
            "template": "certificateAuthority",
            "caType": "Root",
        },
        "dependsOn": [],
    },
    {
        "id": "create-issuing",
        "kind": "createVm",
        "target": "issuing",
        "params": {
            "vmName": "CA02",
            "template": "certificateAuthority",
            "caType": "Issuing",
        },
        "dependsOn": [],
    },
    {
        "id": "create-web",
        "kind": "createVm",
        "target": "web",
        "params": {"vmName": "SRV1", "template": "webServer"},
        "dependsOn": [],
    },
    {
        "id": "join-issuing",
        "kind": "domainJoin",
        "target": "issuing",
        "secondary": "dc",
        "dependsOn": [],
    },
    {
        "id": "join-web",
        "kind": "domainJoin",
        "target": "web",
        "secondary": "dc",
        "dependsOn": [],
    },
    {
        "id": "connect",
        "kind": "caConnect",
        "target": "issuing",
        "secondary": "root",
        "dependsOn": [],
    },
    {
        "id": "publish",
        "kind": "webServerCert",
        "target": "issuing",
        "secondary": "web",
        "dependsOn": [],
    },
]

PREFLIGHT = {
    "ready": True,
    "checkedAt": 1_785_000_000_000,
    "checks": [{"key": "baseImage", "ok": True, "detail": "ws-2025-base present"}],
}


class _FakePlanRuns:
    def __init__(self, doc: dict | None) -> None:
        self.doc = doc

    async def find_one(self, query: dict, projection: dict | None = None):
        if self.doc is None or self.doc.get("jobId") != query.get("jobId"):
            return None
        return dict(self.doc)


def _run_doc(**overrides) -> dict:
    doc = {
        "jobId": JOB_ID,
        "owner": "gina",
        "ownerRole": "guest",
        "createdAt": 1_785_781_000_000,
        "preflight": PREFLIGHT,
        "topology": TOPOLOGY,
        "operations": OPERATIONS,
    }
    doc.update(overrides)
    return doc


def _user(username: str, role: Role = Role.GUEST) -> AuthedUser:
    return AuthedUser(username=username, role=role, auth="local")


@pytest.fixture
def plan_runs(monkeypatch):
    def _install(doc: dict | None) -> _FakePlanRuns:
        col = _FakePlanRuns(doc)
        monkeypatch.setattr(deploy_router, "plan_runs_col", lambda: col)
        return col

    return _install


def test_the_owner_gets_the_start_time_preflight_and_manifest(plan_runs) -> None:
    # An operator, because the receipt is an operator's diagnostic — a guest
    # owner gets the same run without it (see below).
    plan_runs(_run_doc())

    response = asyncio.run(get_deploy_run(JOB_ID, _user("gina", Role.OPERATOR)))

    assert response["jobId"] == JOB_ID
    assert response["createdAt"] == 1_785_781_000_000
    assert response["preflight"] == PREFLIGHT
    # Recompiled, so the synthesized provision ops carry rows of their own —
    # that's what re-materializes the read-only provision rows after a reload.
    assert [group["id"] for group in response["groups"]] == [
        "create-dc",
        "create-root",
        "create-issuing",
        "create-web",
        "create-dc::provision",
        "create-root::provision",
        "create-issuing::provision",
        "create-web::provision",
        "join-issuing",
        "join-web",
        "connect",
        "publish",
    ]
    join = next(group for group in response["groups"] if group["id"] == "join-web")
    assert join["label"] == "Join SRV1 to the domain"
    # The row list the panel expands, verify probes included — `domain.verify` is
    # the reboot step's probe, not a step of its own.
    assert [step["command"] for step in join["steps"]] == [
        "dns.set_client",
        "domain.join",
        "system.reboot",
        "domain.verify",
        "dns.apply_resources",
        "dns.verify",
        "iis.setup_certenroll",
    ]


def test_redaction_leaves_the_manifest_intact(plan_runs) -> None:
    """The stored topology is redacted before it reaches Mongo, and the manifest
    is rebuilt from that copy — so the masking must not reach the domain facts the
    sequence builders branch on."""
    plan_runs(_run_doc())

    response = asyncio.run(get_deploy_run(JOB_ID, _user("gina")))

    dc_provision = next(
        group for group in response["groups"] if group["id"] == "create-dc::provision"
    )
    assert dc_provision["label"] == "Provision DC01 — AD DS forest"
    assert any(
        step.get("command") == "dc.install_forest" for step in dc_provision["steps"]
    )


def test_another_guests_run_is_refused(plan_runs) -> None:
    plan_runs(_run_doc())

    with pytest.raises(HTTPException) as caught:
        asyncio.run(get_deploy_run(JOB_ID, _user("hal")))

    assert caught.value.status_code == 403


def test_an_unknown_job_is_a_404(plan_runs) -> None:
    plan_runs(None)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(get_deploy_run(JOB_ID, _user("gina")))

    assert caught.value.status_code == 404


def test_an_unrebuildable_manifest_still_returns_the_timer_and_receipt(
    plan_runs,
) -> None:
    """A run document from before a schema change can't be recompiled. The elapsed
    clock and the receipt are independent of that, and losing them too is the
    regression this route exists to fix."""
    plan_runs(_run_doc(topology={"version": 1, "nodes": [], "edges": []}))

    response = asyncio.run(get_deploy_run(JOB_ID, _user("gina", Role.OPERATOR)))

    assert response["groups"] == []
    assert response["createdAt"] == 1_785_781_000_000
    assert response["preflight"] == PREFLIGHT


def test_a_guest_owner_is_rehydrated_without_the_receipt(plan_runs) -> None:
    """The receipt names a datastore, a VMX path and an image revision on every
    row, passing rows included. A guest reloading mid-deploy still needs the
    clock and the manifest — losing those is the regression this route exists to
    fix — so only the receipt is withheld, and the panel renders nothing for a
    null one rather than an empty checklist."""

    plan_runs(_run_doc())

    response = asyncio.run(get_deploy_run(JOB_ID, _user("gina")))

    assert response["preflight"] is None
    assert response["createdAt"] == 1_785_781_000_000
    assert response["groups"] != []
