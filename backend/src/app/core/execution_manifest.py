"""Pure, secret-free execution manifests for deploy review and progress UI."""

from typing import Any

from app.core.infrastructure import (
    LINUX_PRODUCT_TEMPLATES,
    deployment_profiles_from_doc,
    role_for_template,
)
from app.core.sequences.context import dns_records_for_context
from app.core.sequences.definitions import (
    CA,
    DC,
    PRIMARY,
    ROOT,
    SECONDARY,
    WEB,
    op_sequence,
    provision_steps,
)
from app.core.sequences.model import DnsRecordContext, NodeContext, RunContext, Step
from app.core.topology import PROVISION_SUFFIX, TopologyDocument, TopologyRole


#: Role-aware detail for a synthesized provision group's label — what the op
#: actually installs after the clone's first boot settles.
_PROVISION_DETAIL = {
    TopologyRole.domain_controller: "AD DS forest",
    TopologyRole.root_ca: "Root CA setup",
}


_COMMAND_LABELS = {
    "dc.install_forest": "Install Active Directory forest",
    "system.reboot": "Reboot and reconnect",
    "dns.set_client": "Configure DNS client",
    "domain.join": "Join Active Directory domain",
    "domain.leave": "Leave Active Directory domain",
    "file.read": "Read relay artifact",
    "file.write": "Copy relay artifact",
    "ca.install": "Install Certificate Authority",
    "ca.install_cert": "Install issued CA certificate",
    "ca.sign_request": "Sign issuing CA request",
    "ca.configure_settings": "Configure CA policy",
    "ca.configure_cdp_aia": "Configure CDP and AIA",
    "ca.publish_crl": "Publish certificate and CRLs",
    "ca.publish_template": "Publish certificate templates",
    "cert.enroll": "Enroll certificate",
    "cert.verify": "Verify certificate chain and revocation",
    "iis.setup_certenroll": "Configure CertEnroll publication",
    "ocsp.install": "Install Online Responder",
    "ocsp.configure_revocation": "Configure OCSP revocation",
    "lab.verify": "Aggregate PKI health evidence",
}


def _label(command: str) -> str:
    return _COMMAND_LABELS.get(
        command, command.replace(".", " ").replace("_", " ").title()
    )


def _preview_context(topology: TopologyDocument, op) -> RunContext:
    by_id = {}
    role_aliases = {
        TopologyRole.domain_controller: DC,
        TopologyRole.root_ca: ROOT,
        TopologyRole.issuing_ca: CA,
        TopologyRole.web_server: WEB,
    }
    aliases = {}
    for node in topology.nodes:
        template = {
            TopologyRole.domain_controller: "domainController",
            TopologyRole.root_ca: "certificateAuthority",
            TopologyRole.issuing_ca: "certificateAuthority",
            TopologyRole.web_server: "webServer",
            TopologyRole.client: "client",
            TopologyRole.standalone: "standalone",
        }[node.role]
        context = NodeContext(
            node_id=node.id,
            vm_name=node.name,
            hostname=node.name.lower(),
            template_id=template,
            template_config=node.config,
        )
        by_id[node.id] = context
        if node.role in role_aliases:
            aliases[role_aliases[node.role]] = context

    kind = str(getattr(op.kind, "value", op.kind))
    primary_id = op.secondary if kind == "webServerCert" else op.target
    secondary_id = op.target if kind == "webServerCert" else op.secondary
    aliases[PRIMARY] = by_id[primary_id]
    if secondary_id:
        aliases[SECONDARY] = by_id[secondary_id]
    dc = aliases.get(DC)
    domain = dc.template_config.get("domainName") if dc else None
    dns_records = dns_records_for_context(topology)
    if not dns_records and dc and WEB in aliases:
        dns_records = (
            DnsRecordContext(
                id="preview-a",
                kind="A",
                server=dc.node_id,
                subject=aliases[WEB].node_id,
                zone=domain or "preview.local",
            ),
        )
    return RunContext(
        nodes=aliases,
        domain_name=domain,
        netbios=dc.template_config.get("netbiosName") if dc else None,
        pki_host=f"pki.{domain}" if domain else "pki.local",
        dns_records=dns_records,
        artifacts={
            "root_crt": "preview",
            "root_crl": "preview",
            "issuing_csr": "preview",
            "issuing_crt": "preview",
            "root_cert_filename": "root-ca.crt",
            "root_crl_filename": "root-ca.crl",
            "issuing_cert_filename": "issuing-ca.crt",
            "issuing_crl_filename": "issuing-ca.crl",
            "issuing_delta_crl_filename": "issuing-ca+.crl",
        },
    )


def _step_kind(step: Step, *, verify: bool = False) -> str:
    if verify:
        return "verify"
    if step.aggregate is not None:
        return "backend"
    if step.command == "system.reboot":
        return "wait"
    if step.command.startswith("file."):
        return "relay"
    return "agent"


def visible_step_ids(steps: list[Step]) -> list[str]:
    """The manifest row ids a step list expands to, in display order.

    A verify probe is not a :class:`Step` of its own but *is* a row of its own
    (``{step.id}.verify``), which is why ``len(steps)`` is not the number of rows
    the panel shows. The runtime counted its ``Step n/total`` over the step list
    while the panel numbered rows, so the two disagreed the moment a verify row
    went by: srv01's domain join reported ``Step 1/6`` above a 7-row list. This is
    the one ordering both sides read, so they can't drift again.
    """
    rows: list[str] = []
    for step in steps:
        rows.append(step.id)
        if step.verify is not None:
            rows.append(f"{step.id}.verify")
    return rows


def _manifest_steps(
    steps: list[Step], aliases: dict[str, NodeContext]
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for step in steps:
        by_id[step.id] = {
            "label": _label(step.command),
            "command": step.command,
            "kind": _step_kind(step),
            "targetNodeId": aliases[step.target].node_id,
        }
        if step.verify is not None:
            by_id[f"{step.id}.verify"] = {
                "label": _label(step.verify.command),
                "command": step.verify.command,
                "kind": _step_kind(step.verify, verify=True),
                "targetNodeId": aliases[step.verify.target].node_id,
            }
    result: list[dict[str, Any]] = []
    previous: str | None = None
    for row_id in visible_step_ids(steps):
        result.append(
            {
                "id": row_id,
                **by_id[row_id],
                "dependsOn": [previous] if previous else [],
            }
        )
        previous = row_id
    return result


def build_execution_groups(
    topology: TopologyDocument,
    operations: list,
    settings_doc: dict | None,
) -> list[dict[str, Any]]:
    """Expand compiled operations into stable, redacted UI groups."""

    topology_nodes = {node.id: node for node in topology.nodes}
    ops_by_id = {op.id: op for op in operations}
    profiles = deployment_profiles_from_doc(settings_doc)
    groups = []
    for op in operations:
        kind = str(getattr(op.kind, "value", op.kind))
        target = topology_nodes[op.target]
        secondary = topology_nodes.get(op.secondary) if op.secondary else None
        source_base = None
        if kind == "createVm":
            steps = [
                {
                    "id": "prepare",
                    "label": "Prepare guest IP and first-boot media",
                    "kind": "backend",
                    "dependsOn": [],
                },
                {
                    "id": "clone",
                    "label": "Clone and power on virtual machine",
                    "kind": "clone",
                    "dependsOn": ["prepare"],
                },
            ]
            for item in steps:
                item["targetNodeId"] = op.target
            profile_role = role_for_template(
                op.params["template"], op.params.get("caType")
            )
            source_base = profiles[profile_role].base
            label = "Clone VM"
        elif kind == "provision":
            # Synthesized ops carry empty params; the sibling createVm holds
            # the template/config, exactly as the runtime resolves them.
            sibling = ops_by_id.get(op.id.removesuffix(PROVISION_SUFFIX), op)
            template = sibling.params.get("template", "")
            context = _preview_context(topology, sibling)
            if template in LINUX_PRODUCT_TEMPLATES:
                product_label = {
                    "certsecure": "Set up CertSecure Manager (stub)",
                    "cbom": "Set up CBOM Secure (stub)",
                    "codesign": "Set up CodeSign Secure (stub)",
                }[template]
                steps = [
                    {
                        "id": "service-setup",
                        "label": product_label,
                        "kind": "backend",
                        "dependsOn": [],
                    }
                ]
            else:
                steps = [
                    {
                        "id": "agent-ready",
                        "label": "Wait for executor agent",
                        "kind": "wait",
                        "dependsOn": [],
                    },
                    {
                        "id": "boot-settle",
                        "label": "Wait for first boot to settle",
                        "kind": "wait",
                        "dependsOn": ["agent-ready"],
                    },
                ]
            for item in steps:
                item["targetNodeId"] = op.target
            # Only agent-backed guests get a step tail — a Linux product VM
            # never reaches ``provision_steps`` at run time (``_run_provision_op``
            # returns after its setup stub), so the preview must not show one.
            if template not in LINUX_PRODUCT_TEMPLATES:
                provision = provision_steps(
                    template,
                    ca_type=sibling.params.get("caType"),
                    node_id=op.target,
                    dns_records=context.dns_records,
                )
                tail = _manifest_steps(provision, context.nodes)
                if tail:
                    tail[0]["dependsOn"] = ["boot-settle"]
                steps.extend(tail)
            detail = (
                "Service setup stub"
                if template in LINUX_PRODUCT_TEMPLATES
                else _PROVISION_DETAIL.get(target.role, "Boot & settle")
            )
            label = f"Provision {target.name} — {detail}"
        else:
            context = _preview_context(topology, op)
            steps = _manifest_steps(op_sequence(kind, context), context.nodes)
            label = {
                "domainJoin": f"Join {target.name} to the domain",
                "domainLeave": f"Remove {target.name} from the domain",
                "caConnect": f"Issue {target.name} from {secondary.name if secondary else 'parent CA'}",
                "webServerCert": f"Configure PKI services on {secondary.name if secondary else target.name}",
            }.get(kind, kind)
        groups.append(
            {
                "id": op.id,
                "kind": kind,
                "label": label,
                "target": op.target,
                "secondary": op.secondary,
                "dependsOn": list(op.depends_on),
                "sourceBase": source_base,
                "steps": steps,
            }
        )
    return groups
