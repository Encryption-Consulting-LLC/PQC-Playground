"""Slice-12 sequences: webServerCert (IIS/OCSP) + client enrollment tail on
domainJoin (pure)."""

import json
import os
from types import SimpleNamespace

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.sequences.definitions import (  # noqa: E402
    op_sequence,
    provision_steps,
    teardown_action_sequence,
)
from app.core.sequences.engine import _reboot_recovery_signature  # noqa: E402
from app.core.sequences.model import DnsRecordContext, NodeContext, RunContext  # noqa: E402
from app.core.sequences.model import Step  # noqa: E402
from app.tasks import _run_sequence_op  # noqa: E402


def _node(nid, vm, template, cfg=None, ip="192.168.1.1"):
    return NodeContext(
        node_id=nid,
        vm_name=vm,
        hostname=vm,
        agent_vm_id=f"v-{nid}",
        ip=ip,
        template_id=template,
        template_config=cfg or {},
    )


def _web_ctx():
    dc = _node(
        "dc01",
        "guest-abc12-dc01",
        "domainController",
        {
            "domainName": "encon.pki",
            "netbiosName": "ENCON",
            "domainAdminPassword": "Str0ng-Lab-Pass!",
        },
    )
    ca = _node(
        "ca02",
        "guest-abc12-ca02",
        "certificateAuthority",
        {"caType": "Issuing", "commonName": "EncryptionConsulting Issuing CA"},
    )
    root = _node(
        "ca01",
        "guest-abc12-ca01",
        "certificateAuthority",
        {"caType": "Root", "commonName": "EC-Root-CA"},
    )
    web = _node(
        "srv1",
        "guest-abc12-srv1",
        "webServer",
        {"certEnrollPath": "C:\\CertEnroll", "ocspRefreshMinutes": "15"},
    )
    return RunContext(
        nodes={
            "primary": web,
            "secondary": ca,
            "ca": ca,
            "root": root,
            "dc": dc,
            "web": web,
        },
        domain_name="encon.pki",
        netbios="ENCON",
        pki_host="pki.encon.pki",
        dns_records=(
            DnsRecordContext(
                id="dns:cname:dc01:pki",
                kind="CNAME",
                server="dc01",
                subject="srv1",
                zone="encon.pki",
                name="pki",
            ),
        ),
        artifacts={
            "root_cert_filename": "guest-abc12-ca01_EC-Root-CA.crt",
            "root_crl_filename": "EC-Root-CA.crl",
            "issuing_cert_filename": (
                "guest-abc12-ca02_EncryptionConsulting Issuing CA.crt"
            ),
            "issuing_crl_filename": "EncryptionConsulting Issuing CA.crl",
            "issuing_delta_crl_filename": "EncryptionConsulting Issuing CA+.crl",
        },
    )


def test_web_server_cert_sequence_shape():
    steps = op_sequence("webServerCert", _web_ctx())
    commands = [s.command for s in steps]
    assert commands == [
        "iis.setup_certenroll",
        "ocsp.install",
        "cert.enroll",
        "ocsp.configure_revocation",
        "dns.apply_resources",
        "dns.verify",
        "dns.verify",
        "cert.enroll",
        "cert.verify",
        "pki.verify",
        "ca.verify",
        "ca.verify",
        "ocsp.verify",
        "dns.verify",
        "dns.verify",
        "system.identity",
        "system.identity",
        "system.identity",
        "system.identity",
        "lab.verify",
    ]


def test_web_iis_step_is_the_web_half():
    ctx = _web_ctx()
    iis = op_sequence("webServerCert", ctx)[0]
    assert iis.resolve_params(ctx)["scope"] == "web"


#: A Windows role change is slow, and a stalled one fails ~17 minutes in — the
#: role-change steps that carried neither a retry schedule nor a reboot recovery
#: (`iis-web`, `iis-share`, `ca-install`, `install-issuing`) failed their whole
#: op on the first stall and cancelled every dependent.
_ROLE_CHANGE_COMMANDS = {
    "iis.setup_certenroll",
    "iis.remove_certenroll",
    "ocsp.install",
    "ocsp.remove",
    "ca.install",
    "ca.uninstall",
    "dc.install_forest",
    "dc.remove_forest",
}


def _all_role_change_steps():
    """Every role-change step the backend can dispatch — the op sequences, the
    teardown actions, *and* the createVm provision tails (where `ca-install`
    lives)."""
    ctx = _web_ctx()
    sequences = (
        [
            op_sequence(kind, ctx)
            for kind in ("webServerCert", "domainJoin", "caConnect", "domainLeave")
        ]
        + [
            teardown_action_sequence(kind, ctx)
            for kind in ("web.cleanup", "ca.cleanup", "forest.cleanup")
        ]
        + [
            provision_steps("domainController"),
            provision_steps("certificateAuthority", ca_type="Root"),
            provision_steps("certificateAuthority", ca_type="Issuing"),
            provision_steps("webServer"),
            provision_steps("client"),
            provision_steps("standalone"),
        ]
    )
    return [
        step
        for steps in sequences
        for step in steps
        if step.command in _ROLE_CHANGE_COMMANDS
    ]


def test_no_windows_role_change_is_left_on_the_default_timeout():
    """`iis.setup_certenroll` stands up IIS — the same class of work as an AD DS
    promotion or an ADCS install, all of which run 8-24 minutes on the
    deployment host. The 300s Step default cut it off mid-install."""
    checked = set()
    for step in _all_role_change_steps():
        checked.add(step.command)
        assert step.timeout_s >= 1800, f"{step.id} ({step.command})"
    assert {"iis.setup_certenroll", "ocsp.install", "ca.install"} <= checked
    assert {"ocsp.remove", "iis.remove_certenroll", "ca.uninstall"} <= checked


def test_a_feature_install_is_sized_for_its_observed_worst_case():
    """`iis-web` legitimately ran 23.5m and `ocsp-install` 20.5m on a host that
    varies 2.8x with load — a 30-minute cap left too little headroom, and a
    dispatch timeout is precisely the failure retries cannot rescue."""
    by_id = {step.id: step for step in _all_role_change_steps()}
    for step_id in ("iis-web", "iis-share", "ocsp-install"):
        assert by_id[step_id].timeout_s == 2700, step_id


def test_no_windows_role_change_is_left_without_a_recovery_path():
    """The companion invariant to the timeout one: a role change must be able to
    survive one failure, either by retrying or by rebooting first. A guest-side
    stall is retryable (only *dispatch* timeouts are not — the engine refuses
    those whatever the schedule says)."""
    steps = _all_role_change_steps()
    naked = [
        f"{step.id} ({step.command})"
        for step in steps
        if not step.retry_delays_s and not step.reboot_recovery_signatures
    ]
    assert naked == []
    checked = {step.command for step in steps}
    assert {"iis.setup_certenroll", "ocsp.install", "ca.install"} <= checked
    assert {"ocsp.remove", "iis.remove_certenroll", "ca.uninstall"} <= checked


def test_feature_installs_recover_from_a_server_manager_stall():
    by_id = {step.id: step for step in _all_role_change_steps()}
    for step_id in ("iis-web", "iis-share", "ca-install", "install-issuing"):
        step = by_id[step_id]
        assert step.retry_delays_s, step_id
        assert "getenumerationstate_timeout" in step.reboot_recovery_signatures


def test_the_prod_server_manager_stall_detail_matches_a_recovery_signature():
    """Verbatim from `plan_runs` job 5fd68414aff44884b9d77f2b6d319400, op
    4f595dfe-f883-4374-b8d9-f058f436230b (`iis-web`) — the failure this recovery
    exists for. Anchored here so a reworded signature can't silently stop
    matching it. Both signatures are exercised because PowerShell wraps its
    error block at console width and can split either token."""
    detail = (
        "agent command 'iis.setup_certenroll' failed: powershell execution "
        "failed: script exited with code 1: Install-WindowsFeature : The "
        "request for the server to discover installed features timed out.\r\n"
        "At C:\\WINDOWS\\SystemTemp\\pki-executor-fIVoIr.ps1:1 char:503\r\n"
        "+ ...  'share') { Install-WindowsFeature Web-Server "
        "-IncludeManagementTools ...\r\n"
        "    + CategoryInfo          : OperationTimeout: (@{Vhd=; "
        "Credent...Name=localhost}:PSObject) [Install-WindowsFeature], \r\n"
        "    DeploymentProviderException\r\n"
        "    + FullyQualifiedErrorId : GetEnumerationState_Timeout,"
        "Microsoft.Windows.ServerManager.Commands.AddWindowsFeatureCo \r\n"
        "   mmand\r\n"
    )
    iis_web = next(
        step
        for step in op_sequence("webServerCert", _web_ctx())
        if step.id == "iis-web"
    )

    assert (
        _reboot_recovery_signature(iis_web, RuntimeError(detail))
        == "getenumerationstate_timeout"
    )
    # The English message text is the fallback when the wrap lands on the id.
    truncated = detail.replace("GetEnumerationState_Timeout", "GetEnumerationState_")
    assert (
        _reboot_recovery_signature(iis_web, RuntimeError(truncated))
        == "discover installed features"
    )


def test_provision_tail_always_settles_the_inherited_reboot_first():
    """The base image ships an unconsumed CBS reboot mark, so every agent-backed
    guest reboots before any role work — including the two templates whose tail
    is otherwise empty but that install a Windows feature later in the plan (the
    issuing CA's `install-issuing`, the web host's `iis-web`)."""
    tails = {
        "domainController": provision_steps("domainController"),
        "rootCa": provision_steps("certificateAuthority", ca_type="Root"),
        "issuingCa": provision_steps("certificateAuthority", ca_type="Issuing"),
        "webServer": provision_steps("webServer"),
        "client": provision_steps("client"),
        "standalone": provision_steps("standalone"),
    }
    for label, steps in tails.items():
        assert steps, label
        assert steps[0].id == "settle-reboot", label
        assert steps[0].command == "system.reboot", label
        assert steps[0].expects_disconnect is True, label
    assert [step.id for step in tails["issuingCa"]] == ["settle-reboot"]
    assert [step.id for step in tails["webServer"]] == ["settle-reboot"]


def test_ocsp_config_points_at_the_issuing_ca():
    ctx = _web_ctx()
    cfg = next(s for s in op_sequence("webServerCert", ctx) if s.id == "ocsp-config")
    params = cfg.resolve_params(ctx)
    assert params["caConfig"] == (
        "guest-abc12-ca02.encon.pki\\EncryptionConsulting Issuing CA"
    )
    # Named after the CA it answers for, not a hardcoded default.
    assert params["name"] == "EncryptionConsulting Issuing CA"
    # The relayed copy `issuing-to-web` already wrote — the responder reads
    # the certificate off disk instead of fetching it with certutil.
    assert params["caCertPath"] == (
        "C:\\CertEnroll\\guest-abc12-ca02_EncryptionConsulting Issuing CA.crt"
    )
    assert params["refreshMinutes"] == "15"
    assert cfg.verify.command == "ocsp.verify"


def test_deferred_cname_targets_the_web_host_on_the_dc():
    ctx = _web_ctx()
    cname = next(
        s for s in op_sequence("webServerCert", ctx) if s.id == "dns-cname-apply"
    )
    assert cname.target == "dc"
    params = cname.resolve_params(ctx)
    assert json.loads(params["records"])[0] == {
        "id": "dns:cname:dc01:pki",
        "kind": "CNAME",
        "name": "pki",
        "value": "guest-abc12-srv1.encon.pki.",
        "zone": "encon.pki",
    }


def test_cname_and_http_are_verified_from_web_and_ca():
    ctx = _web_ctx()
    verify = [
        step
        for step in op_sequence("webServerCert", ctx)
        if step.id.startswith("dns-cname-verify-")
    ]
    assert [step.target for step in verify] == ["primary", "ca"]
    assert all(
        step.resolve_params(ctx)["httpUrl"] == "http://pki.encon.pki/CertEnroll/"
        for step in verify
    )


def test_web_sequence_enrolls_a_dedicated_health_probe():
    ctx = _web_ctx()
    enroll = next(
        step
        for step in op_sequence("webServerCert", ctx)
        if step.id == "enroll-health-probe"
    )

    assert enroll.target == "primary"
    assert enroll.resolve_params(ctx) == {
        "template": "Workstation",
        "exportPath": "C:\\Transfer\\lab-health-probe.cer",
        "refreshPolicy": "true",
    }


def test_final_health_gate_targets_all_four_machines():
    ctx = _web_ctx()
    steps = op_sequence("webServerCert", ctx)
    by_id = {step.id: step for step in steps}

    assert by_id["certificate-health"].target == "web"
    cert_params = by_id["certificate-health"].resolve_params(ctx)
    assert cert_params["expectedSignatureOid"] == "2.16.840.1.101.3.4.3.19"
    assert cert_params["rootPath"].endswith("guest-abc12-ca01_EC-Root-CA.crt")
    assert cert_params["issuingPath"].endswith(
        "guest-abc12-ca02_EncryptionConsulting Issuing CA.crt"
    )
    assert by_id["enterprise-pki-health"].target == "dc"
    assert by_id["root-ca-health"].target == "root"
    assert by_id["issuing-ca-health"].target == "ca"
    assert by_id["ocsp-health"].target == "web"
    assert by_id["lab-health"].aggregate is not None
    assert steps[-1].id == "lab-health"


def test_enterprise_health_checks_every_http_artifact():
    ctx = _web_ctx()
    step = next(
        item
        for item in op_sequence("webServerCert", ctx)
        if item.id == "enterprise-pki-health"
    )
    urls = json.loads(step.resolve_params(ctx)["httpUrls"])

    assert urls == [
        "http://pki.encon.pki/CertEnroll/guest-abc12-ca01_EC-Root-CA.crt",
        "http://pki.encon.pki/CertEnroll/EC-Root-CA.crl",
        (
            "http://pki.encon.pki/CertEnroll/"
            "guest-abc12-ca02_EncryptionConsulting%20Issuing%20CA.crt"
        ),
        "http://pki.encon.pki/CertEnroll/EncryptionConsulting%20Issuing%20CA.crl",
        "http://pki.encon.pki/CertEnroll/EncryptionConsulting%20Issuing%20CA+.crl",
    ]


def test_completed_publication_op_exposes_health_report(monkeypatch):
    from app.core.sequences import context, definitions, worker
    from app import tasks

    ctx = _web_ctx()
    health = {"healthy": True, "failures": [], "checks": {}}
    monkeypatch.setattr(context, "build_run_context", lambda *args: ctx)
    monkeypatch.setattr(
        definitions,
        "op_sequence",
        lambda *args: [Step(id="lab-health", command="lab.verify", target="web")],
    )
    monkeypatch.setattr(
        worker,
        "run_op_sequence",
        lambda *args, **kwargs: {"lab-health": health},
    )
    monkeypatch.setattr(tasks, "_step_median_seconds", lambda *args: {})
    op = SimpleNamespace(
        id="publish",
        kind=SimpleNamespace(value="webServerCert"),
    )
    state = {}

    assert _run_sequence_op({}, op, [], "job", "guest", state, lambda: None) is True
    assert state["publish"].result["steps"] == 1
    assert state["publish"].result["health"] == health
    assert state["publish"].result["certificateJourney"]["schemaVersion"] == 1


def test_failed_publication_op_exposes_health_report(monkeypatch):
    from app.core.sequences import context, definitions, worker
    from app.core.sequences.engine import HealthGateError
    from app import tasks

    ctx = _web_ctx()
    health = {"healthy": False, "failures": ["OCSP failed"], "checks": {}}
    results = {"lab-health": health}
    monkeypatch.setattr(context, "build_run_context", lambda *args: ctx)
    monkeypatch.setattr(
        definitions,
        "op_sequence",
        lambda *args: [Step(id="lab-health", command="lab.verify", target="web")],
    )
    monkeypatch.setattr(
        worker,
        "run_op_sequence",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            HealthGateError(
                "health gate 'lab.verify' failed: OCSP failed",
                step_id="lab-health",
                health=health,
                results=results,
            )
        ),
    )
    monkeypatch.setattr(tasks, "_step_median_seconds", lambda *args: {})
    op = SimpleNamespace(id="publish", kind=SimpleNamespace(value="webServerCert"))
    state = {}

    assert _run_sequence_op({}, op, [], "job", "guest", state, lambda: None) is False
    assert state["publish"].status == "error"
    assert state["publish"].result["health"] == health
    assert state["publish"].result["certificateJourney"]["healthy"] is False


def _client_ctx(with_ca=True):
    dc = _node(
        "dc01",
        "guest-abc12-dc01",
        "domainController",
        {
            "domainName": "encon.pki",
            "netbiosName": "ENCON",
            "domainAdminPassword": "Str0ng-Lab-Pass!",
        },
    )
    win11 = _node("win11", "guest-abc12-win11", "client")
    nodes = {"primary": win11, "secondary": dc, "dc": dc}
    if with_ca:
        nodes["ca"] = _node(
            "ca02", "guest-abc12-ca02", "certificateAuthority", {"caType": "Issuing"}
        )
    return RunContext(
        nodes=nodes,
        domain_name="encon.pki",
        netbios="ENCON",
    )


def test_client_join_appends_enroll_and_verify():
    steps = op_sequence("domainJoin", _client_ctx(with_ca=True))
    enroll = steps[-1]
    assert enroll.command == "cert.enroll"
    p = enroll.resolve_params(_client_ctx())
    assert p["template"] == "Workstation"
    assert p["exportPath"] == "C:\\win11.cer"
    assert enroll.verify.command == "cert.verify"
    assert enroll.verify_predicate({"chain_ok": True}) is True


def test_client_join_without_a_ca_skips_enrollment():
    steps = op_sequence("domainJoin", _client_ctx(with_ca=False))
    assert all(s.command != "cert.enroll" for s in steps)
