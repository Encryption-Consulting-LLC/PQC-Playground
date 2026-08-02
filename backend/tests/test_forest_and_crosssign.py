"""Slice-11 sequences: DC forest tail, root CA tail, and the caConnect
cross-sign handshake — shape + param resolution (pure)."""

import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.sequences.definitions import (  # noqa: E402
    _ds_config_dn,
    _forest_mode,
    op_sequence,
    provision_steps,
)
from app.core.sequences.model import (  # noqa: E402
    DnsRecordContext,
    NodeContext,
    RunContext,
)


def test_forest_mode_maps_levels():
    assert _forest_mode("Windows Server 2016") == "WinThreshold"
    assert _forest_mode("Windows Server 2022") == "WinThreshold"
    assert _forest_mode("Windows Server 2025") == "Win2025"


def test_ds_config_dn_from_domain():
    assert _ds_config_dn("encon.pki") == ("CN=Configuration,DC=encon,DC=pki")


def test_dc_provision_promotes_reboots_and_points_dns_at_self():
    steps = provision_steps("domainController")
    assert [s.command for s in steps] == [
        "dc.install_forest",
        "system.reboot",
        "dns.set_client",
    ]
    assert "safeModePassword" in steps[0].secret_keys
    # Promotion recovers from its own role install asking for a restart.
    assert "dcpromo.general.15" in steps[0].reboot_recovery_signatures
    assert steps[1].expects_disconnect is True
    assert steps[1].verify is None
    assert steps[2].verify.command == "dc.verify"


def test_dc_forest_params_map_config():
    node = NodeContext(
        node_id="dc",
        vm_name="guest-abc12-dc01",
        hostname="guest-abc12-dc01",
        agent_vm_id="v",
        ip="192.168.1.90",
        template_id="domainController",
        template_config={
            "domainName": "encon.pki",
            "netbiosName": "ENCON",
            "forestLevel": "Windows Server 2025",
            "domainAdminPassword": "Str0ng-Lab-Pass!",
        },
    )
    ctx = RunContext(nodes={"primary": node})
    p = provision_steps("domainController")[0].resolve_params(ctx)
    assert p["domainName"] == "encon.pki"
    assert p["forestMode"] == "Win2025"
    assert p["safeModePassword"] == "Str0ng-Lab-Pass!"
    # dns.set_client points at the DC's own IP.
    assert (
        provision_steps("domainController")[2].resolve_params(ctx)["servers"]
        == "192.168.1.90"
    )


def test_dc_applies_its_a_record_and_verifies_ad_srv_records():
    record = DnsRecordContext(
        id="dns:a:dc01:dc01",
        kind="A",
        server="dc01",
        subject="dc01",
        zone="encon.pki",
    )
    steps = provision_steps(
        "domainController",
        node_id="dc01",
        dns_records=(record,),
    )
    assert [step.command for step in steps][-2:] == [
        "dns.apply_resources",
        "dns.verify",
    ]
    node = NodeContext(
        node_id="dc01",
        vm_name="guest-abc12-dc01",
        hostname="guest-abc12-dc01",
        agent_vm_id="v",
        ip="192.168.1.90",
        template_id="domainController",
        template_config={"domainName": "encon.pki"},
    )
    ctx = RunContext(
        nodes={"primary": node},
        domain_name="encon.pki",
        dns_records=(record,),
    )
    verify = steps[-1].resolve_params(ctx)
    assert verify["requireAdSrv"] == "true"
    assert verify["domain"] == "encon.pki"


def _root_ctx():
    node = NodeContext(
        node_id="ca01",
        vm_name="guest-abc12-ca01",
        hostname="guest-abc12-ca01",
        agent_vm_id="v",
        template_id="certificateAuthority",
        template_config={"caType": "Root", "commonName": "EC-Root-CA"},
    )
    return RunContext(
        nodes={"primary": node},
        domain_name="encon.pki",
        pki_host="pki.encon.pki",
        artifacts={
            "root_cert_filename": "guest-abc12-ca01_EC-Root-CA.crt",
            "root_crl_filename": "EC-Root-CA.crl",
        },
    )


def test_root_ca_tail_full_sequence():
    steps = provision_steps("certificateAuthority", ca_type="Root")
    assert [s.command for s in steps] == [
        "ca.install",
        "ca.configure_settings",
        "ca.configure_cdp_aia",
        "ca.publish_crl",
        "file.read",
        "file.read",
    ]
    # The two reads publish the root cert + CRL into the relay.
    assert steps[4].produces == ("root_crt",)
    assert steps[5].produces == ("root_crl",)
    assert steps[3].result_artifacts == {
        "certificateFileName": "root_cert_filename",
        "baseCrlFileName": "root_crl_filename",
    }


def test_root_ca_settings_include_dsconfigdn_and_periods():
    ctx = _root_ctx()
    settings = provision_steps("certificateAuthority", ca_type="Root")[
        1
    ].resolve_params(ctx)
    assert settings["dsConfigDn"] == "CN=Configuration,DC=encon,DC=pki"
    assert settings["crlPeriodUnits"] == "52"
    assert settings["validityPeriodUnits"] == "10"
    assert settings["auditFilter"] == "127"


def test_root_ca_cdp_aia_use_pki_host_and_three_locations():
    ctx = _root_ctx()
    p = provision_steps("certificateAuthority", ca_type="Root")[2].resolve_params(ctx)
    assert p["aiaUrls"].count("\n") == 2  # 3 AIA locations
    assert p["cdpUrls"].count("\n") == 2  # 3 CDP locations
    assert "http://pki.encon.pki/CertEnroll/" in p["aiaUrls"]


def test_root_crt_read_path_uses_observed_publication_name():
    ctx = _root_ctx()
    read = provision_steps("certificateAuthority", ca_type="Root")[4]
    path = read.resolve_params(ctx)["path"]
    assert path.endswith("guest-abc12-ca01_EC-Root-CA.crt")


def test_root_ca_uses_configured_publication_directory_end_to_end():
    ctx = _root_ctx()
    ctx.nodes["primary"].template_config["certEnrollPath"] = "D:\\PKI\\Published"
    steps = provision_steps("certificateAuthority", ca_type="Root")

    publication = steps[2].resolve_params(ctx)
    assert "1:D:\\PKI\\Published\\%1_%3%4.crt" in publication["aiaUrls"]
    assert "1:D:\\PKI\\Published\\%3%8%9.crl" in publication["cdpUrls"]
    assert steps[3].resolve_params(ctx) == {"certEnrollPath": "D:\\PKI\\Published"}
    assert steps[4].resolve_params(ctx)["path"].startswith("D:\\PKI\\Published\\")
    assert steps[5].resolve_params(ctx)["path"].startswith("D:\\PKI\\Published\\")


def test_issuing_ca_has_no_createvm_tail():
    assert provision_steps("certificateAuthority", ca_type="Issuing") == []


def _full_lab_ctx():
    def node(nid, vm, template, cfg=None):
        return NodeContext(
            node_id=nid,
            vm_name=vm,
            hostname=vm,
            agent_vm_id=f"v-{nid}",
            ip="192.168.1.1",
            template_id=template,
            template_config=cfg or {},
        )

    dc = node(
        "dc01",
        "guest-abc12-dc01",
        "domainController",
        {
            "domainName": "encon.pki",
            "netbiosName": "ENCON",
            "domainAdminPassword": "Str0ng-Lab-Pass!",
        },
    )
    return RunContext(
        nodes={
            "primary": node(
                "ca02",
                "guest-abc12-ca02",
                "certificateAuthority",
                {"caType": "Issuing", "commonName": "EncryptionConsulting Issuing CA"},
            ),
            "secondary": node(
                "ca01", "guest-abc12-ca01", "certificateAuthority", {"caType": "Root"}
            ),
            "root": node(
                "ca01", "guest-abc12-ca01", "certificateAuthority", {"caType": "Root"}
            ),
            "dc": dc,
            "web": node("srv1", "guest-abc12-srv1", "webServer"),
        },
        domain_name="encon.pki",
        netbios="ENCON",
        pki_host="pki.encon.pki",
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


def test_caconnect_handshake_relays_root_csr_and_signed_cert():
    ctx = _full_lab_ctx()
    steps = op_sequence("caConnect", ctx)
    commands = [s.command for s in steps]
    # Root trusted on CA02, published to AD + web, issuing installed, CSR out /
    # signed cert back, config + templates + grant.
    assert commands.count("cert.addstore") == 2  # root cert + root CRL
    assert commands.count("cert.dspublish") == 2  # root cert + root CRL
    assert "ca.sign_request" in commands
    assert "ca.install_cert" in commands
    assert "ca.publish_template" in commands
    assert "template.grant_access" in commands
    # The CSR/signed-cert relay uses produce/consume artifacts.
    produced = {a for s in steps for a in s.produces}
    assert {"issuing_csr", "issuing_crt"} <= produced


def test_caconnect_trusts_the_root_crl_on_ca02_before_installing_the_cert():
    """`certutil -installcert` chain-validates, and the offline root's CDP isn't
    served yet — the CRL has to be in CA02's own CA store first."""

    ctx = _full_lab_ctx()
    steps = op_sequence("caConnect", ctx)
    order = [s.id for s in steps]
    by_id = {s.id: s for s in steps}

    relay = by_id["rootcrl-to-ca02"]
    assert relay.target == "primary"
    assert relay.consumes == ("root_crl",)
    assert relay.resolve_params(ctx)["path"] == "C:\\Transfer\\root-ca.crl"

    trust = by_id["addstore-rootcrl"]
    assert trust.target == "primary"
    assert trust.resolve_params(ctx) == {
        "store": "ca",
        "path": "C:\\Transfer\\root-ca.crl",
        "kind": "crl",
    }
    assert by_id["addstore-root"].resolve_params(ctx)["kind"] == "cert"

    assert (
        order.index("rootcrl-to-ca02")
        < order.index("addstore-rootcrl")
        < order.index("install-issuing-cert")
    )


def test_caconnect_installs_the_issued_cert_under_the_domain_admin():
    """Installing the cert publishes to AD, which LocalSystem can't do from a
    member server — the step carries the same credential the install did."""

    ctx = _full_lab_ctx()
    step = {s.id: s for s in op_sequence("caConnect", ctx)}["install-issuing-cert"]
    params = step.resolve_params(ctx)

    assert params["certPath"] == "C:\\Transfer\\IssuingCA.crt"
    assert params["username"] == "ENCON\\Administrator"
    assert params["password"] == ctx.nodes["dc"].template_config["domainAdminPassword"]
    assert step.secret_keys == ("password",)


def test_caconnect_recovers_missing_root_artifacts_from_configured_directory():
    ctx = _full_lab_ctx()
    ctx.artifacts.pop("root_cert_filename")
    ctx.artifacts.pop("root_crl_filename")
    ctx.nodes["root"].template_config.update(
        {
            "commonName": "EC-Root-CA",
            "certEnrollPath": "C:\\CaConfig",
        }
    )
    by_id = {step.id: step for step in op_sequence("caConnect", ctx)}

    crt = by_id["recover-root-crt"]
    crl = by_id["recover-root-crl"]
    assert crt.resolve_params(ctx)["path"] == (
        "C:\\CaConfig\\guest-abc12-ca01_EC-Root-CA.crt"
    )
    assert crl.resolve_params(ctx)["path"] == "C:\\CaConfig\\EC-Root-CA.crl"
    assert crt.produces == ("root_crt",)
    assert crl.produces == ("root_crl",)
    assert crt.resolve_result_artifact_defaults(ctx) == {
        "sourceFileName": "guest-abc12-ca01_EC-Root-CA.crt"
    }
    assert crl.resolve_result_artifact_defaults(ctx) == {
        "sourceFileName": "EC-Root-CA.crl"
    }


def test_caconnect_recovery_reads_skip_when_root_relay_is_available():
    ctx = _full_lab_ctx()
    ctx.artifacts.update({"root_crt": "Y2VydA==", "root_crl": "Y3Js"})
    by_id = {step.id: step for step in op_sequence("caConnect", ctx)}

    assert by_id["recover-root-crt"].skip_if_artifacts == (
        "root_crt",
        "root_cert_filename",
    )
    assert by_id["recover-root-crl"].skip_if_artifacts == (
        "root_crl",
        "root_crl_filename",
    )


def test_caconnect_preserves_http_publication_filenames():
    ctx = _full_lab_ctx()
    by_id = {step.id: step for step in op_sequence("caConnect", ctx)}

    assert (
        by_id["root-to-web"]
        .resolve_params(ctx)["path"]
        .endswith("guest-abc12-ca01_EC-Root-CA.crt")
    )
    assert (
        by_id["rootcrl-to-web"].resolve_params(ctx)["path"].endswith("EC-Root-CA.crl")
    )
    assert (
        by_id["issuing-to-web"]
        .resolve_params(ctx)["path"]
        .endswith("guest-abc12-ca02_EncryptionConsulting Issuing CA.crt")
    )


def test_caconnect_issuing_install_is_credentialed_and_secret():
    ctx = _full_lab_ctx()
    install = next(
        s for s in op_sequence("caConnect", ctx) if s.id == "install-issuing"
    )
    params = install.resolve_params(ctx)
    assert params["caType"] == "Issuing"
    assert params["username"] == "ENCON\\Administrator"
    assert params["password"] == "Str0ng-Lab-Pass!"
    assert "password" in install.secret_keys


def test_caconnect_grant_targets_the_web_computer_on_the_dc():
    ctx = _full_lab_ctx()
    grant = next(s for s in op_sequence("caConnect", ctx) if s.id == "grant-ocsp")
    assert grant.target == "dc"
    params = grant.resolve_params(ctx)
    assert params["template"] == "OCSPResponseSigning"
    assert params["computer"] == "guest-abc12-srv1"

    health_grant = next(
        s for s in op_sequence("caConnect", ctx) if s.id == "grant-health-probe"
    )
    health_params = health_grant.resolve_params(ctx)
    assert health_params["template"] == "Workstation"
    assert health_params["computer"] == "guest-abc12-srv1"


def test_caconnect_issuing_cdp_includes_unc_and_ocsp_when_web_present():
    ctx = _full_lab_ctx()
    cdp_aia = next(
        s for s in op_sequence("caConnect", ctx) if s.id == "issuing-cdp-aia"
    )
    p = cdp_aia.resolve_params(ctx)
    assert "/ocsp" in p["aiaUrls"]  # 32: OCSP AIA entry
    assert "\\\\guest-abc12-srv1.encon.pki\\CertEnroll\\" in p["cdpUrls"]
