"""Slice-10 domainJoin expansion: the sequence shape + param resolution
(pure — no redis/Mongo)."""

import json
import os

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.sequences.definitions import op_sequence  # noqa: E402
from app.core.sequences.model import DnsRecordContext, NodeContext, RunContext  # noqa: E402


def _ctx(primary_template="standalone", with_dns=False):
    member = NodeContext(
        node_id="ca02",
        vm_name="guest-abc12-ca02",
        hostname="guest-abc12-ca02",
        agent_vm_id="vm-member",
        ip="192.168.1.92",
        template_id=primary_template,
        template_config={"certEnrollPath": "C:\\CertEnroll"},
    )
    dc = NodeContext(
        node_id="dc01",
        vm_name="guest-abc12-dc01",
        hostname="guest-abc12-dc01",
        agent_vm_id="vm-dc",
        ip="192.168.1.90",
        template_id="domainController",
        template_config={
            "domainName": "encon.pki",
            "netbiosName": "ENCON",
            "domainAdminPassword": "Str0ng-Lab-Pass!",
        },
    )
    return RunContext(
        nodes={"primary": member, "secondary": dc, "dc": dc},
        domain_name="encon.pki",
        netbios="ENCON",
        dns_records=(
            DnsRecordContext(
                id="dns:a:dc01:ca02",
                kind="A",
                server="dc01",
                subject="ca02",
                zone="encon.pki",
            ),
            DnsRecordContext(
                id="dns:ptr:dc01:ca02",
                kind="PTR",
                server="dc01",
                subject="ca02",
                zone="1.168.192.in-addr.arpa",
            ),
        )
        if with_dns
        else (),
    )


def test_domain_join_sequence_shape():
    steps = op_sequence("domainJoin", _ctx())
    assert [s.command for s in steps] == [
        "dns.set_client",
        "domain.join",
        "system.reboot",
    ]
    reboot = steps[2]
    assert reboot.expects_disconnect is True
    assert reboot.verify is not None and reboot.verify.command == "domain.verify"


def test_dns_points_at_the_dc_ip():
    steps = op_sequence("domainJoin", _ctx())
    dns = steps[0]
    ctx = _ctx()
    assert dns.resolve_params(ctx)["servers"] == "192.168.1.90"


def test_join_applies_and_verifies_planned_a_and_ptr_records():
    ctx = _ctx(with_dns=True)
    steps = op_sequence("domainJoin", ctx)
    assert [step.command for step in steps][-2:] == [
        "dns.apply_resources",
        "dns.verify",
    ]
    records = json.loads(steps[-2].resolve_params(ctx)["records"])
    assert records == [
        {
            "id": "dns:a:dc01:ca02",
            "kind": "A",
            "name": "guest-abc12-ca02",
            "value": "192.168.1.92",
            "zone": "encon.pki",
        },
        {
            "id": "dns:ptr:dc01:ca02",
            "kind": "PTR",
            "name": "192.168.1.92",
            "value": "guest-abc12-ca02.encon.pki.",
            "zone": "1.168.192.in-addr.arpa",
        },
    ]


def test_join_params_use_domain_netbios_admin_and_secret_password():
    # This credential only exists *in the guest* because the DC's firstboot
    # 50-password.ps1 reset the local Administrator before Install-ADDSForest
    # promoted it into the domain — the other half of the contract, pinned in
    # test_firstboot_password.py.
    ctx = _ctx()
    join = op_sequence("domainJoin", ctx)[1]
    params = join.resolve_params(ctx)
    assert params["domainName"] == "encon.pki"
    assert params["username"] == "ENCON\\Administrator"
    assert params["password"] == "Str0ng-Lab-Pass!"
    # The password param is flagged for redaction in progress/error frames.
    assert "password" in join.secret_keys


def test_verify_predicate_gates_on_part_of_domain():
    reboot = op_sequence("domainJoin", _ctx())[2]
    assert reboot.verify_predicate({"part_of_domain": True}) is True
    assert reboot.verify_predicate({"part_of_domain": False}) is False


def test_web_server_target_gets_the_certenroll_share_half():
    steps = op_sequence("domainJoin", _ctx(primary_template="webServer"))
    assert steps[-1].command == "iis.setup_certenroll"
    params = steps[-1].resolve_params(_ctx(primary_template="webServer"))
    assert params["scope"] == "share"
    assert params["netbiosName"] == "ENCON"


def test_non_web_target_has_no_iis_step():
    steps = op_sequence("domainJoin", _ctx(primary_template="certificateAuthority"))
    assert all(s.command != "iis.setup_certenroll" for s in steps)
