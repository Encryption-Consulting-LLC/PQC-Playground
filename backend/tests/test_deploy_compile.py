"""Dry-run deploy compilation returns the exact authoritative worker plan."""

import asyncio
import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.authz import AuthedUser, Role
from app.routers.deploy import DeployRequest, _compile_or_422, compile_deploy


def _operator() -> AuthedUser:
    return AuthedUser(username="op", role=Role.OPERATOR, auth="local")


def _request() -> DeployRequest:
    topology = {
        "version": 1,
        "nodes": [
            {"id": "dc", "name": "DC01", "role": "domainController"},
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
    ops = [
        {
            "id": "publish",
            "kind": "webServerCert",
            "target": "issuing",
            "secondary": "web",
            "dependsOn": ["untrusted"],
        },
        {
            "id": "connect",
            "kind": "caConnect",
            "target": "issuing",
            "secondary": "root",
            "dependsOn": ["untrusted"],
        },
        {
            "id": "join-web",
            "kind": "domainJoin",
            "target": "web",
            "secondary": "dc",
            "dependsOn": ["untrusted"],
        },
        {
            "id": "join-issuing",
            "kind": "domainJoin",
            "target": "issuing",
            "secondary": "dc",
            "dependsOn": ["untrusted"],
        },
        {
            "id": "create-web",
            "kind": "createVm",
            "target": "web",
            "params": {"vmName": "SRV1", "template": "webServer"},
            "dependsOn": ["untrusted"],
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
            "dependsOn": ["untrusted"],
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
            "dependsOn": ["untrusted"],
        },
        {
            "id": "create-dc",
            "kind": "createVm",
            "target": "dc",
            "params": {
                "vmName": "DC01",
                "template": "domainController",
                "domainAdminPassword": "Str0ng-Lab-Pass!",
            },
            "dependsOn": ["untrusted"],
        },
    ]
    return DeployRequest(topology=topology, ops=ops)


def test_dry_run_returns_compiled_operations_and_estimates():
    response = asyncio.run(compile_deploy(_request(), _operator()))

    assert [op["id"] for op in response["operations"]] == [
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
    assert response["operations"][-1]["dependsOn"][-1] == "connect"
    assert response["criticalPath"] == [
        "create-dc",
        "create-dc::provision",
        "join-issuing",
        "connect",
        "publish",
    ]
    assert response["resources"] == {
        "nodes": 4,
        "relationships": 4,
        "dnsRecords": [],
    }
    clone = next(group for group in response["groups"] if group["kind"] == "createVm")
    assert clone["label"] == "Clone VM"
    assert clone["sourceBase"] == "ws-2025-base"
    assert [step["id"] for step in clone["steps"]] == ["prepare", "clone"]
    dc_provision = next(
        group for group in response["groups"] if group["id"] == "create-dc::provision"
    )
    assert dc_provision["kind"] == "provision"
    assert dc_provision["label"] == "Provision DC01 — AD DS forest"
    assert dc_provision["dependsOn"] == ["create-dc"]
    assert [step["id"] for step in dc_provision["steps"][:2]] == [
        "agent-ready",
        "boot-settle",
    ]
    # The role-install tail moved wholesale onto the provision group.
    assert any(
        step["command"] == "dc.install_forest"
        for step in dc_provision["steps"]
        if "command" in step
    )
    assert all(
        "params" not in step for group in response["groups"] for step in group["steps"]
    )


def test_dc_without_a_domain_admin_password_is_rejected():
    """The password is the lab's single credential — firstboot resets the DC's local
    Administrator to it and every later domain join signs in with it — so a DC op
    that omits it must 422 here, not fail deep inside the sequence."""

    request = _request()
    dc_op = next(op for op in request.ops if op.id == "create-dc")
    dc_op.params.pop("domainAdminPassword")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(compile_deploy(request, _operator()))

    assert caught.value.status_code == 422
    assert "domainAdminPassword" in str(caught.value.detail)


def test_compile_failure_returns_structured_diagnostics():
    request = _request()
    request.topology.edges = []

    with pytest.raises(HTTPException) as caught:
        _compile_or_422(request)

    assert caught.value.status_code == 422
    assert caught.value.detail["message"] == "Topology compilation failed."
    assert {item["code"] for item in caught.value.detail["diagnostics"]} >= {
        "missing-ca-parent",
        "issuing-ca-outside-domain",
        "missing-publication-host",
    }
