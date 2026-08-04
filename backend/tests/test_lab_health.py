"""Structured final lab-health aggregation is strict and diagnostic."""

import json
from copy import deepcopy
from pathlib import Path

from app.core.sequences.health import (
    ML_DSA_87_SIGNATURE_OID,
    aggregate_lab_health,
)
from app.core.sequences.model import NodeContext, RunContext, StepRuntime

_FIXTURES = Path(__file__).parent / "fixtures"


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


def test_derived_revocation_freshness_is_not_reported_twice() -> None:
    """`cert.verify` derives freshness from chain+cdp+ocsp, so it echoes them."""
    results = deepcopy(_healthy_results())
    results["certificate-health"]["ocsp"] = {"ok": False, "verified_responses": 0}
    results["certificate-health"]["revocation_freshness"] = {"ok": False}

    report = aggregate_lab_health(_runtime(), results)

    assert report["warnings"] == [
        "the issued probe did not receive a verified OCSP response"
    ]
    freshness = report["checks"]["certificate"]["revocationFreshness"]
    assert freshness["ok"] is False
    assert freshness["advisory"] is True


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


def test_a_configured_responder_that_errors_is_reported_by_status() -> None:
    """`configured` proves the config exists, not that the responder can answer."""
    results = deepcopy(_healthy_results())
    results["ocsp-health"]["http_status"] = 500

    report = aggregate_lab_health(_runtime(), results)

    assert "Online Responder" in "; ".join(report["warnings"])
    assert "HTTP 500" in "; ".join(report["warnings"])
    assert report["checks"]["ocspResponder"]["ok"] is False
    assert report["checks"]["ocspResponder"]["detail"]["httpStatus"] == 500


def test_a_dead_responder_does_not_fail_a_lab_that_revokes_over_crl() -> None:
    """The prod shape: chain + CRLs verify, only the responder is broken."""
    results = deepcopy(_healthy_results())
    results["certificate-health"]["ocsp"] = {"ok": False, "verified_responses": 0}
    results["certificate-health"]["revocation_freshness"] = {"ok": False}
    results["ocsp-health"]["http_status"] = 500

    report = aggregate_lab_health(_runtime(), results)

    assert report["healthy"] is True
    assert report["failures"] == []
    assert len(report["warnings"]) == 2
    assert report["checks"]["certificate"]["ocsp"]["advisory"] is True
    assert report["checks"]["ocspResponder"]["advisory"] is True


def test_a_dead_responder_is_fatal_when_crl_revocation_is_also_broken() -> None:
    """With neither CRL nor OCSP there is no revocation left to fall back on."""
    results = deepcopy(_healthy_results())
    results["certificate-health"]["cdp"] = {"ok": False}
    results["certificate-health"]["ocsp"] = {"ok": False}
    results["ocsp-health"]["http_status"] = 500

    report = aggregate_lab_health(_runtime(), results)

    assert report["healthy"] is False
    joined = "; ".join(report["failures"])
    assert "verified OCSP response" in joined
    assert "Online Responder" in joined
    assert "advisory" not in report["checks"]["certificate"]["ocsp"]


def test_a_serving_responder_passes_the_gate() -> None:
    results = deepcopy(_healthy_results())
    results["ocsp-health"]["http_status"] = 200

    report = aggregate_lab_health(_runtime(), results)

    assert report["healthy"] is True


def test_an_agent_that_reports_no_endpoint_status_is_not_penalised() -> None:
    report = aggregate_lab_health(_runtime(), _healthy_results())

    assert report["checks"]["ocspResponder"]["ok"] is True
    assert report["checks"]["ocspResponder"]["detail"]["httpStatus"] is None


def test_wrong_runtime_identity_fails_the_gate() -> None:
    results = _healthy_results()
    results["identity-web"]["hostname"] = "unexpected-host"

    report = aggregate_lab_health(_runtime(), results)

    assert report["healthy"] is False
    assert "expected Windows Server host guest-lab-web" in "; ".join(report["failures"])


def test_the_recorded_prod_run_now_passes_with_one_ocsp_warning() -> None:
    """Replay of a real four-VM run that failed with three messages.

    Job ``e99d07f72c6c4db284af16b8240dae02`` reported "the issued probe did not
    receive a verified OCSP response; revocation evidence is stale or unavailable;
    one or more AD enterprise PKI containers are unhealthy". Only the first named
    a real fault: the container check compared CA common names against ``CN=CDP``'s
    host-named children, and freshness was derived from the same OCSP failure. The
    responder answered HTTP 500 with no signing certificate bound, while its CRLs
    verified 4 base and 4 delta -- so the lab revokes, and the gate should say so
    without stranding the deploy.
    """
    results = json.loads((_FIXTURES / "lab_health_ocsp_500.json").read_text())
    nodes = {
        alias: NodeContext(
            node_id=alias,
            vm_name=f"guest-guest-99646f-{host}",
            hostname=f"99646f-{host}",
            agent_vm_id=f"agent-{alias}",
        )
        for alias, host in (
            ("dc", "dc01"),
            ("root", "ca01"),
            ("ca", "ca02"),
            ("web", "srv1"),
        )
    }
    runtime = StepRuntime(
        ctx=RunContext(nodes=nodes, pki_host="pki.encon.pki"), node=nodes["web"]
    )

    report = aggregate_lab_health(runtime, results)

    assert report["healthy"] is True
    assert report["failures"] == []
    assert report["warnings"] == [
        "the issued probe did not receive a verified OCSP response",
        "the Online Responder at http://pki.encon.pki/ocsp returned HTTP 500",
    ]
    # The two misattributed messages are gone, and for the right reasons.
    assert report["checks"]["enterprisePki"]["containers"]["ok"] is True
    assert report["checks"]["certificate"]["revocationFreshness"]["advisory"] is True
