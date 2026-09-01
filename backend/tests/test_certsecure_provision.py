"""CertSecure Manager provisions for real: install, trust, DNS, harden, verify.

The properties pinned here are the ones that are silent when wrong. A trust push
that carries nothing still reports every step green; a DNS registration aimed at
the product itself rather than at the domain controller fails only when someone
opens a browser; and a preview whose step list differs from the run's renumbers
the panel's rows mid-deploy.
"""

import os

import pytest

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
)

from app.core.execution_manifest import build_execution_groups  # noqa: E402
from app.core.sequences.definitions import provision_steps  # noqa: E402
from app.core.sequences.model import (  # noqa: E402
    DnsRecordContext,
    NodeContext,
    RunContext,
)
from app.core.topology import (  # noqa: E402
    PROVISION_SUFFIX,
    TopologyDocument,
    TopologyEdge,
    TopologyNode,
    compile_plan,
    product_integration,
)
from app.routers.deploy import PlanOp  # noqa: E402


DOMAIN = "encon.pki"
PRODUCT_CONFIG = {
    "frontendHost": f"certsecure.{DOMAIN}",
    "backendHost": f"certsecure-api.{DOMAIN}",
    "keycloakHost": f"kc.{DOMAIN}",
    "keycloakRealm": DOMAIN,
}


def _topology() -> TopologyDocument:
    """A DC, a web server joined to it, and a product integrated with it."""
    return TopologyDocument(
        nodes=[
            TopologyNode(
                id="dc",
                name="DC01",
                role="domainController",
                config={"domainName": DOMAIN, "netbiosName": "ENCON"},
            ),
            # OCSP off: the point here is the trust fan-out over the domain,
            # and an enabled responder drags in the issuing-CA validation rules
            # that say nothing about it.
            TopologyNode(
                id="web",
                name="SRV01",
                role="webServer",
                config={"enableOcsp": "Disabled"},
            ),
            TopologyNode(
                id="product", name="CS01", role="product", config=PRODUCT_CONFIG
            ),
        ],
        edges=[
            TopologyEdge(
                id="e-join", kind="domainMembership", source="web", target="dc"
            ),
            TopologyEdge(
                id="e-integrate",
                kind="productIntegration",
                source="product",
                target="dc",
            ),
        ],
    )


def _steps(topology: TopologyDocument):
    return provision_steps(
        "certsecure",
        node_id="product",
        integration=product_integration(topology, "product"),
    )


def _context(topology: TopologyDocument) -> RunContext:
    product = NodeContext(
        node_id="product",
        vm_name="guest-alice-abc123-cs01",
        hostname="abc123-cs01",
        agent_vm_id="vm-product",
        ip="10.0.181.5",
        template_id="certsecure",
        template_config=PRODUCT_CONFIG,
    )
    dc = NodeContext(
        node_id="dc",
        vm_name="guest-alice-abc123-dc01",
        hostname="abc123-dc01",
        agent_vm_id="vm-dc",
        ip="10.0.181.2",
        template_id="domainController",
        template_config={"domainName": DOMAIN, "netbiosName": "ENCON"},
    )
    web = NodeContext(
        node_id="web",
        vm_name="guest-alice-abc123-srv01",
        hostname="abc123-srv01",
        agent_vm_id="vm-web",
        ip="10.0.181.3",
        template_id="webServer",
    )
    return RunContext(
        nodes={"primary": product, "product": product, "dc": dc, "web": web},
        domain_name=DOMAIN,
        netbios="ENCON",
        dns_records=(
            DnsRecordContext(
                id="dns:a:dc:product:frontend",
                kind="A",
                server="dc",
                subject="product",
                zone=DOMAIN,
                name="certsecure",
            ),
            DnsRecordContext(
                id="dns:a:dc:product:keycloak",
                kind="A",
                server="dc",
                subject="product",
                zone=DOMAIN,
                name="kc",
            ),
        ),
    )


def test_the_install_directory_is_one_value_everywhere():
    """Three modules are written against this path and none can see the others.

    ``firstboot`` bakes it into the staging script that extracts the tree, the
    sequence resolves the certificate paths under it, and the agent's
    ``certsecure.*`` commands both ``cd`` into it and use it as their ``file.*``
    relay allowlist prefix. The agent's copy is a cross-repo mirror like the
    command catalog; these two are in one repo and can simply be pinned.
    """
    from app.core.firstboot import PRODUCT_INSTALL_DIR
    from app.core.sequences import definitions

    assert PRODUCT_INSTALL_DIR == "/opt/certsecure-manager"
    assert definitions._PRODUCT_DIR == PRODUCT_INSTALL_DIR
    assert definitions._PRODUCT_CERT_PATH.startswith(f"{PRODUCT_INSTALL_DIR}/")
    assert definitions._PRODUCT_KEY_PATH.startswith(f"{PRODUCT_INSTALL_DIR}/")


def test_the_sequence_installs_before_it_distributes_trust():
    # `cert.addstore` carries the DER `certsecure.make_cert` produced; running
    # a trust push before the certificate exists would relay an empty artifact
    # and still report every step green.
    ids = [step.id for step in _steps(_topology())]
    assert ids.index("product-cert") < ids.index("trust-write-dc")
    assert ids.index("product-cert") < ids.index("trust-write-web")


def test_every_windows_node_in_the_domain_is_given_the_certificate():
    # Not just the domain controller: the check that proves this worked is a
    # browser on any lab machine opening the dashboard without a warning.
    ids = {step.id for step in _steps(_topology())}
    assert {"trust-write-dc", "trust-add-dc"} <= ids
    assert {"trust-write-web", "trust-add-web"} <= ids


def test_the_relayed_certificate_is_the_one_the_product_generated():
    steps = {step.id: step for step in _steps(_topology())}
    (produced,) = steps["product-cert"].produces
    assert steps["trust-write-dc"].consumes == (produced,)


def test_dns_registration_runs_at_the_domain_controller():
    # The product is a Linux box with no DNS server on it; dispatching this at
    # `primary` would fail on a command the guest's own registry does have.
    (dns_step,) = [
        step for step in _steps(_topology()) if step.command == "dns.apply_resources"
    ]
    assert dns_step.target == "dc"


def test_the_product_publishes_its_configured_names_not_its_hostname():
    """A/PTR records normally take the subject's own guest hostname.

    The product publishes three service names that all point at one address, so
    each record carries its own label — and a resolver that ignored it would
    register `abc123-cs01` three times and none of the names anyone types.
    """
    import json

    topology = _topology()
    ctx = _context(topology)
    (dns_step,) = [
        step for step in _steps(topology) if step.command == "dns.apply_resources"
    ]
    records = json.loads(dns_step.resolve_params(ctx)["records"])
    assert sorted(record["name"] for record in records) == ["certsecure", "kc"]
    assert {record["value"] for record in records} == {"10.0.181.5"}


def test_the_installer_is_handed_the_operators_four_names():
    topology = _topology()
    ctx = _context(topology)
    (install,) = [
        step for step in _steps(topology) if step.command == "certsecure.install"
    ]
    params = install.resolve_params(ctx)
    assert params["frontendHost"] == PRODUCT_CONFIG["frontendHost"]
    assert params["backendHost"] == PRODUCT_CONFIG["backendHost"]
    assert params["keycloakHost"] == PRODUCT_CONFIG["keycloakHost"]
    assert params["keycloakRealm"] == PRODUCT_CONFIG["keycloakRealm"]


def test_the_certificate_covers_every_name_and_the_address():
    topology = _topology()
    ctx = _context(topology)
    (cert,) = [
        step for step in _steps(topology) if step.command == "certsecure.make_cert"
    ]
    params = cert.resolve_params(ctx)
    assert params["names"].split(",") == [
        PRODUCT_CONFIG["frontendHost"],
        PRODUCT_CONFIG["backendHost"],
        PRODUCT_CONFIG["keycloakHost"],
    ]
    assert params["ip"] == "10.0.181.5"


def test_hosts_entries_cover_the_product_and_the_lab():
    topology = _topology()
    ctx = _context(topology)
    (hosts,) = [
        step for step in _steps(topology) if step.command == "certsecure.write_hosts"
    ]
    lines = hosts.resolve_params(ctx)["entries"].splitlines()
    assert lines[0] == (
        "10.0.181.5 certsecure.encon.pki certsecure-api.encon.pki kc.encon.pki"
    )
    # The lab's own machines resolve during the install, before the product's
    # A records exist and before anything has verified DNS end to end.
    assert "10.0.181.2 abc123-dc01 abc123-dc01.encon.pki" in lines


def test_the_install_is_sized_against_a_cold_first_run():
    # 595s measured on a cold image, 287 of them building OpenSSL from source.
    # A dispatch timeout is the one failure a retry schedule cannot rescue.
    (install,) = [
        step for step in _steps(_topology()) if step.command == "certsecure.install"
    ]
    assert install.timeout_s >= 1800


def test_keycloak_is_advisory_and_the_dashboard_is_not():
    steps = {step.id: step for step in _steps(_topology())}
    # The dashboard answering is the deployment's definition of success.
    assert steps["product-install"].verify is not None
    assert steps["product-install"].verify_predicate({"frontendOk": True})
    assert not steps["product-install"].verify_predicate({"frontendOk": False})
    # Keycloak is not: a slow identity provider ships a warning, never a failed
    # deploy — and `settle` is the primitive whose window closing is a non-event.
    health = steps["product-health"]
    assert health.settle_predicate is not None
    assert health.verify_predicate is None


def test_the_product_waits_for_the_whole_domain_before_provisioning():
    """Its trust push needs live agents on every Windows node, not just the DC.

    Without this the product could be scheduled the moment its own clone
    finished and dispatch `cert.addstore` at a web server that has not booted.
    """
    topology = _topology()
    compiled = compile_plan(
        topology,
        [
            PlanOp(
                id="create-dc",
                kind="createVm",
                target="dc",
                params={
                    "vmName": "DC01",
                    "template": "domainController",
                    "domainName": DOMAIN,
                    "netbiosName": "ENCON",
                    "domainAdminPassword": "Lab-Passw0rd!23",
                },
            ),
            PlanOp(
                id="create-web",
                kind="createVm",
                target="web",
                params={
                    "vmName": "SRV01",
                    "template": "webServer",
                    "enableOcsp": "Disabled",
                },
            ),
            PlanOp(
                id="create-product",
                kind="createVm",
                target="product",
                params={"vmName": "CS01", "template": "certsecure", **PRODUCT_CONFIG},
            ),
            PlanOp(id="join-web", kind="domainJoin", target="web", secondary="dc"),
        ],
    )
    by_id = {op.id: op for op in compiled.operations}
    product_provision = by_id[f"create-product{PROVISION_SUFFIX}"]
    assert f"create-dc{PROVISION_SUFFIX}" in product_provision.depends_on
    assert f"create-web{PROVISION_SUFFIX}" in product_provision.depends_on


def test_the_preview_expands_to_exactly_what_the_run_will_do():
    """The panel numbers its rows from this list and the runtime reports against
    it; a preview that disagreed would renumber itself mid-deploy."""
    topology = _topology()
    compiled = compile_plan(
        topology,
        [
            PlanOp(
                id="create-product",
                kind="createVm",
                target="product",
                params={"vmName": "CS01", "template": "certsecure", **PRODUCT_CONFIG},
            ),
            PlanOp(
                id="create-dc",
                kind="createVm",
                target="dc",
                params={
                    "vmName": "DC01",
                    "template": "domainController",
                    "domainName": DOMAIN,
                    "netbiosName": "ENCON",
                    "domainAdminPassword": "Lab-Passw0rd!23",
                },
            ),
            PlanOp(
                id="create-web",
                kind="createVm",
                target="web",
                params={
                    "vmName": "SRV01",
                    "template": "webServer",
                    "enableOcsp": "Disabled",
                },
            ),
            PlanOp(id="join-web", kind="domainJoin", target="web", secondary="dc"),
        ],
    )
    groups = build_execution_groups(topology, compiled.operations, {})
    provision = next(
        group for group in groups if group["id"] == f"create-product{PROVISION_SUFFIX}"
    )
    rows = [step["id"] for step in provision["steps"]]
    assert rows[:2] == ["agent-ready", "boot-settle"]
    assert rows[2:] == [
        "apt-refresh",
        "product-hosts",
        "product-cert",
        "product-install",
        "product-install.verify",
        "trust-write-dc",
        "trust-add-dc",
        "trust-write-web",
        "trust-add-web",
        "product-dns",
        "product-harden",
        "product-health",
    ]
    # The trust push and the DNS registration run on other machines; a row that
    # named the product would point the panel's highlight at the wrong node.
    by_row = {step["id"]: step for step in provision["steps"]}
    assert by_row["trust-add-web"]["targetNodeId"] == "web"
    assert by_row["product-dns"]["targetNodeId"] == "dc"


@pytest.mark.parametrize(
    "field,value",
    [
        ("keycloakRealm", "encon pki"),
        ("frontendHost", "not a host"),
    ],
)
def test_bad_product_config_is_refused_at_the_gate(field, value):
    # 595 seconds into an install is the wrong place to learn that Keycloak will
    # not accept the realm name.
    from app.core.template_config import validate_template_config

    params = {
        "vmName": "CS01",
        "template": "certsecure",
        **PRODUCT_CONFIG,
        field: value,
    }
    with pytest.raises(ValueError):
        validate_template_config("certsecure", params)
