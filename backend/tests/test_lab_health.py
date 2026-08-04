"""Structured final lab-health aggregation is strict and diagnostic."""

from copy import deepcopy

from app.core.sequences.health import (
    ML_DSA_87_SIGNATURE_OID,
    aggregate_lab_health,
)
from app.core.sequences.model import NodeContext, RunContext, StepRuntime


def _node(role: str) -> NodeContext:
    return NodeContext(
        node_id=role,
        vm_name=f"guest-lab-{role}",
        hostname=f"guest-lab-{role}",
        agent_vm_id=f"agent-{role}",
    )


def _runtime() -> StepRuntime:
    nodes = {role: _node(role) for role in ("dc", "root", "ca", "web")}
    return StepRuntime(ctx=RunContext(nodes=nodes), node=nodes["web"])


def _identity(hostname: str) -> dict:
    return {
        "hostname": hostname,
        "operating_system": "Microsoft Windows Server 2025 Standard",
        "version": "10.0.26100",
        "server": True,
    }


def _healthy_results() -> dict[str, dict]:
    return {
        "certificate-health": {
            "chain": {"ok": True},
            "aia": {"ok": True, "verified_certificates": 2},
            "cdp": {"ok": True, "verified_base_crls": 2, "verified_delta_crls": 1},
            "ocsp": {"ok": True, "verified_responses": 1},
            "ml_dsa": {"ok": True, "expected_oid": ML_DSA_87_SIGNATURE_OID},
            "validity": {"ok": True},
            "revocation_freshness": {"ok": True},
            "certificates": [],
        },
        "enterprise-pki-health": {
            "containers": {
                "nt_auth": True,
                "aia": True,
                # Always false in the wild: the agent compares CA common names
                # against `CN=CDP`'s host-named children. The aggregate ignores it
                # and re-derives the answer from `observed`.
                "cdp": False,
                "certification_authorities": True,
                "enrollment_services": True,
            },
            "observed": {
                "aia": ["EC-Issuing-CA", "EC-Root-CA"],
                "cdp": ["guest-lab-root", "guest-lab-ca"],
                "certification_authorities": "EC-Root-CA",
                "enrollment_services": "EC-Issuing-CA",
            },
            "templates": {"ok": True},
            "http_artifacts": {"ok": True},
        },
        "root-ca-health": {"service": "Running", "ping_ok": True},
        "issuing-ca-health": {"service": "Running", "ping_ok": True},
        "ocsp-health": {"configured": True, "configurations": [{}]},
        "dns-health-web": {
            "all_verified": True,
            "ad_srv_ok": True,
            "http_ok": True,
        },
        "dns-health-issuing": {
            "all_verified": True,
            "ad_srv_ok": True,
            "http_ok": True,
        },
        "identity-dc": _identity("guest-lab-dc"),
        "identity-root": _identity("guest-lab-root"),
        "identity-issuing": _identity("guest-lab-ca"),
        "identity-web": _identity("guest-lab-web"),
    }


def test_healthy_gate_returns_structured_evidence() -> None:
    report = aggregate_lab_health(_runtime(), _healthy_results())

    assert report["healthy"] is True
    assert report["failures"] == []
    assert report["checks"]["certificate"]["ocsp"]["ok"] is True
    assert report["checks"]["runtimeIdentities"]["root"]["ok"] is True


def test_each_required_certificate_fact_is_a_hard_gate() -> None:
    paths = (
        ("chain", "ok"),
        ("aia", "ok"),
        ("cdp", "ok"),
        ("ocsp", "ok"),
        ("ml_dsa", "ok"),
        ("validity", "ok"),
        ("revocation_freshness", "ok"),
    )
    for section, field in paths:
        results = deepcopy(_healthy_results())
        results["certificate-health"][section][field] = False

        report = aggregate_lab_health(_runtime(), results)

        assert report["healthy"] is False, section
        assert report["failures"], section


def test_missing_enterprise_container_fails_with_diagnostic() -> None:
    results = _healthy_results()
    results["enterprise-pki-health"]["containers"]["nt_auth"] = False

    report = aggregate_lab_health(_runtime(), results)

    assert report["healthy"] is False
    assert "AD enterprise PKI containers" in "; ".join(report["failures"])


def test_cdp_container_is_read_from_the_host_names_it_publishes_under() -> None:
    """The agent's own `cdp` boolean is always false and must not be consumed."""
    report = aggregate_lab_health(_runtime(), _healthy_results())

    containers = report["checks"]["enterprisePki"]["containers"]
    assert containers["ok"] is True
    assert containers["detail"]["cdp"] is True


def test_a_single_published_cdp_host_arrives_as_a_bare_string() -> None:
    """PowerShell collapses a one-element array; both CAs may be one host."""
    results = _healthy_results()
    results["enterprise-pki-health"]["observed"]["cdp"] = "guest-lab-root"
    runtime = _runtime()
    runtime.ctx.nodes["ca"] = runtime.ctx.nodes["root"]

    report = aggregate_lab_health(runtime, results)

    assert report["checks"]["enterprisePki"]["containers"]["ok"] is True


def test_a_ca_that_published_no_crl_fails_the_container_check() -> None:
    results = _healthy_results()
    results["enterprise-pki-health"]["observed"]["cdp"] = ["guest-lab-root"]

    report = aggregate_lab_health(_runtime(), results)

    assert report["healthy"] is False
    assert "AD enterprise PKI containers" in "; ".join(report["failures"])


def test_cdp_is_unassertable_without_observed_names() -> None:
    """A pre-`observed` agent must not fail a lab over a check it cannot answer."""
    results = _healthy_results()
    del results["enterprise-pki-health"]["observed"]

    report = aggregate_lab_health(_runtime(), results)

    assert report["healthy"] is True
    assert "cdp" not in report["checks"]["enterprisePki"]["containers"]["detail"]


def test_wrong_runtime_identity_fails_the_gate() -> None:
    results = _healthy_results()
    results["identity-web"]["hostname"] = "unexpected-host"

    report = aggregate_lab_health(_runtime(), results)

    assert report["healthy"] is False
    assert "expected Windows Server host guest-lab-web" in "; ".join(report["failures"])
