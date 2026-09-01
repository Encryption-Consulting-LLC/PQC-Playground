"""Versioned semantic topology models and validation for deploy plans.

The canvas graph is a presentation document.  This module is the backend's
small, stable contract for the infrastructure it represents: role-bearing
nodes and capability relationships.  Executable operations are compiled from
this final document elsewhere; validation therefore does not depend on the
order in which the user happened to stage those operations.
"""

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TopologyRole(str, Enum):
    domain_controller = "domainController"
    root_ca = "rootCa"
    issuing_ca = "issuingCa"
    web_server = "webServer"
    client = "client"
    standalone = "standalone"
    #: A Linux product appliance (CertSecure Manager, CBOM Secure, CodeSign
    #: Secure). One role for all three, because the topology's questions —
    #: what may this connect to, what does it depend on — have the same answer
    #: for each; *which* product it is stays the createVm op's ``template``.
    product = "product"


class TopologyEdgeKind(str, Enum):
    domain_membership = "domainMembership"
    ca_parent = "caParent"
    ca_publication = "caPublication"
    #: A product appliance wired to a domain controller: "register my names in
    #: your zone and have your domain trust my certificate". Deliberately not a
    #: ``domain_membership``: nothing joins a domain here, no computer account
    #: is created, and the product stays a Linux box outside the forest.
    product_integration = "productIntegration"


class TopologyPortKind(str, Enum):
    ca_parent = "caParent"
    ca_publication = "caPublication"
    domain_boundary = "domainBoundary"
    web_host = "webHost"
    probe_certificate = "probeCertificate"
    product_integration = "productIntegration"


class TopologyResourceState(str, Enum):
    planned = "planned"
    realized = "realized"


_PORTS_BY_EDGE_KIND = {
    TopologyEdgeKind.domain_membership: (TopologyPortKind.domain_boundary,),
    TopologyEdgeKind.ca_parent: (TopologyPortKind.ca_parent,),
    TopologyEdgeKind.ca_publication: (
        TopologyPortKind.ca_publication,
        TopologyPortKind.web_host,
        TopologyPortKind.probe_certificate,
    ),
    TopologyEdgeKind.product_integration: (TopologyPortKind.product_integration,),
}


class DnsRecordKind(str, Enum):
    a = "A"
    ptr = "PTR"
    cname = "CNAME"


class TopologyNode(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    role: TopologyRole
    state: TopologyResourceState = TopologyResourceState.planned
    config: dict[str, str] = Field(default_factory=dict)


class TopologyEdge(BaseModel):
    id: str = Field(min_length=1, max_length=500)
    kind: TopologyEdgeKind
    source: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)
    state: TopologyResourceState = TopologyResourceState.planned
    ports: list[TopologyPortKind] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def infer_legacy_ports(self) -> "TopologyEdge":
        """Upgrade topology-v1 edges saved before capability ports existed."""

        if not self.ports:
            self.ports = list(_PORTS_BY_EDGE_KIND[self.kind])
        return self


class DnsRecordResource(BaseModel):
    """A symbolic DNS record whose runtime value comes from ``subject``.

    A/PTR resources resolve the subject node's allocated address and real
    guest hostname after cloning. CNAME resources use ``name`` as the alias
    and the subject node's FQDN as the target. ``server`` is the authoritative
    domain-controller node that owns the zone.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(min_length=1, max_length=500)
    kind: DnsRecordKind
    server: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    zone: str = Field(min_length=1, max_length=253)
    name: str | None = Field(default=None, min_length=1, max_length=63)


class TopologyDocument(BaseModel):
    """Backend-owned topology contract; version changes are explicit."""

    model_config = ConfigDict(populate_by_name=True)

    version: Literal[1] = 1
    nodes: list[TopologyNode] = Field(min_length=1, max_length=200)
    edges: list[TopologyEdge] = Field(default_factory=list, max_length=500)
    dns_records: list[DnsRecordResource] = Field(
        default_factory=list, max_length=500, alias="dnsRecords"
    )


class TopologyDiagnostic(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    code: str
    message: str
    node_ids: list[str] = Field(default_factory=list, alias="nodeIds")
    edge_ids: list[str] = Field(default_factory=list, alias="edgeIds")


class TopologyValidationError(ValueError):
    """Raised with every semantic error so a preview can render them at once."""

    def __init__(self, diagnostics: list[TopologyDiagnostic]):
        self.diagnostics = diagnostics
        super().__init__("; ".join(item.message for item in diagnostics))


class CompilableOp(Protocol):
    """The small PlanOp surface used here, avoiding a core -> router import."""

    id: str
    kind: Any
    target: str
    secondary: str | None
    params: dict[str, str]
    depends_on: list[str]

    def model_copy(self, *, deep: bool = False) -> Any: ...


@dataclass(frozen=True)
class CompiledPlan:
    operations: list[Any]
    dns_records: list[DnsRecordResource]
    critical_path: list[str]
    estimated_duration_seconds: int
    critical_path_duration_seconds: int


class PlanCompilationError(ValueError):
    def __init__(self, diagnostics: list[TopologyDiagnostic]):
        self.diagnostics = diagnostics
        super().__init__("; ".join(item.message for item in diagnostics))


def _valid_reverse_zone(value: str) -> bool:
    normalized = value.lower().rstrip(".")
    suffix = ".in-addr.arpa"
    if not normalized.endswith(suffix):
        return False
    labels = normalized[: -len(suffix)].split(".")
    return 1 <= len(labels) <= 3 and all(
        label.isdigit() and 0 <= int(label) <= 255 for label in labels
    )


#: Suffix of backend-synthesized provision op ids: ``{createVmOpId}::provision``.
#: A pure function of the client op id — scheduler state in
#: ``plan_runs.scheduler.ops`` and Celery ``task_id=f"{job_id}:{op_id}"`` key
#: on it, so it must be identical across every recompile.
PROVISION_SUFFIX = "::provision"

_DURATION_SECONDS = {
    "createVm": 600,
    "domainLeave": 600,
    "domainJoin": 1200,
    "caConnect": 2700,
    "webServerCert": 1800,
}

#: The post-clone role install a synthesized ``provision`` op runs; role-less
#: templates only wait for the agent and boot settle.
_PROVISION_DURATION_SECONDS = {
    TopologyRole.domain_controller: 1800,
    TopologyRole.root_ca: 1200,
    TopologyRole.issuing_ca: 300,
    TopologyRole.web_server: 300,
    TopologyRole.client: 300,
    TopologyRole.standalone: 300,
    # The CertSecure installer's own measured total is 595s on a cold image;
    # the rest of the sequence (hosts, certificate, trust push, DNS) is small
    # next to it. Every expensive phase is idempotent, so a re-run is far
    # cheaper than this — the estimate is sized for the case that matters.
    TopologyRole.product: 900,
}

_KIND_RANK = {
    "createVm": 0,
    "provision": 1,
    "domainLeave": 2,
    "domainJoin": 3,
    "caConnect": 4,
    "webServerCert": 5,
}

_ROLE_RANK = {
    TopologyRole.domain_controller: 0,
    TopologyRole.root_ca: 1,
    TopologyRole.issuing_ca: 2,
    TopologyRole.web_server: 3,
    TopologyRole.client: 4,
    TopologyRole.standalone: 5,
    # Last, and not only for tidiness: a product's provision op depends on every
    # Windows node in its domain being provisioned, so it can never be scheduled
    # before them and displaying it earlier would misdescribe the plan.
    TopologyRole.product: 6,
}


def _cycle_path(adjacency: dict[str, list[str]]) -> list[str] | None:
    """Return one closed DFS cycle path, including the repeated first node."""

    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node_id: str) -> list[str] | None:
        visiting.add(node_id)
        stack.append(node_id)
        for child in adjacency.get(node_id, []):
            if child in visiting:
                start = stack.index(child)
                return [*stack[start:], child]
            if child not in visited:
                found = visit(child)
                if found:
                    return found
        stack.pop()
        visiting.remove(node_id)
        visited.add(node_id)
        return None

    for node_id in adjacency:
        if node_id not in visited:
            found = visit(node_id)
            if found:
                return found
    return None


def validate_topology(topology: TopologyDocument) -> None:
    """Validate graph integrity and the two-tier PKI service relationships.

    The result is deliberately aggregate: a compiler preview should show all
    missing relationships in one pass instead of forcing a fix/retry loop.
    """

    diagnostics: list[TopologyDiagnostic] = []
    nodes: dict[str, TopologyNode] = {}
    for node in topology.nodes:
        if node.id in nodes:
            diagnostics.append(
                TopologyDiagnostic(
                    code="duplicate-node",
                    message=f"Topology contains duplicate node id '{node.id}'.",
                    node_ids=[node.id],
                )
            )
        else:
            nodes[node.id] = node

    # AD forest DNS names and AD CS common names are case-insensitive
    # identities. Reject collisions in the semantic document as well as in
    # the form so imports and older saved projects cannot bypass the guard.
    authored_names: dict[tuple[str, str], TopologyNode] = {}
    for node in nodes.values():
        if node.role is TopologyRole.domain_controller:
            field, label, code = "domainName", "domain name", "duplicate-domain-name"
        elif node.role in (TopologyRole.root_ca, TopologyRole.issuing_ca):
            field, label, code = "commonName", "CA common name", "duplicate-ca-name"
        else:
            continue
        value = node.config.get(field, "").strip().rstrip(".")
        if not value:
            continue
        key = (field, value.casefold())
        previous = authored_names.get(key)
        if previous:
            diagnostics.append(
                TopologyDiagnostic(
                    code=code,
                    message=(
                        f"{node.name} and {previous.name} use the same {label} "
                        f"'{value}'."
                    ),
                    node_ids=[previous.id, node.id],
                )
            )
        else:
            authored_names[key] = node

    edge_ids: set[str] = set()
    edge_keys: set[tuple[TopologyEdgeKind, str, str]] = set()
    valid_edges: list[TopologyEdge] = []
    for edge in topology.edges:
        if edge.id in edge_ids:
            diagnostics.append(
                TopologyDiagnostic(
                    code="duplicate-edge",
                    message=f"Topology contains duplicate edge id '{edge.id}'.",
                    edge_ids=[edge.id],
                )
            )
        edge_ids.add(edge.id)
        key = (edge.kind, edge.source, edge.target)
        if key in edge_keys:
            diagnostics.append(
                TopologyDiagnostic(
                    code="duplicate-relationship",
                    message=(
                        f"Relationship '{edge.kind.value}' from '{edge.source}' to "
                        f"'{edge.target}' is duplicated."
                    ),
                    node_ids=[edge.source, edge.target],
                    edge_ids=[edge.id],
                )
            )
        edge_keys.add(key)
        expected_ports = set(_PORTS_BY_EDGE_KIND[edge.kind])
        actual_ports = set(edge.ports)
        if actual_ports != expected_ports:
            diagnostics.append(
                TopologyDiagnostic(
                    code="connection-port-mismatch",
                    message=(
                        f"Relationship '{edge.id}' must expose "
                        f"{', '.join(port.value for port in _PORTS_BY_EDGE_KIND[edge.kind])}."
                    ),
                    node_ids=[edge.source, edge.target],
                    edge_ids=[edge.id],
                )
            )
        missing = [
            node_id for node_id in (edge.source, edge.target) if node_id not in nodes
        ]
        if missing:
            diagnostics.append(
                TopologyDiagnostic(
                    code="unknown-node",
                    message=f"Relationship '{edge.id}' references unknown node(s): {missing}.",
                    node_ids=missing,
                    edge_ids=[edge.id],
                )
            )
            continue
        if edge.source == edge.target:
            diagnostics.append(
                TopologyDiagnostic(
                    code="self-relationship",
                    message=f"Node '{nodes[edge.source].name}' cannot relate to itself.",
                    node_ids=[edge.source],
                    edge_ids=[edge.id],
                )
            )
            continue
        valid_edges.append(edge)

    memberships: dict[str, list[TopologyEdge]] = defaultdict(list)
    parents: dict[str, list[TopologyEdge]] = defaultdict(list)
    publications: dict[str, list[TopologyEdge]] = defaultdict(list)
    integrations: dict[str, list[TopologyEdge]] = defaultdict(list)
    ca_adjacency: dict[str, list[str]] = defaultdict(list)

    for edge in valid_edges:
        source = nodes[edge.source]
        target = nodes[edge.target]
        if edge.kind is TopologyEdgeKind.domain_membership:
            memberships[edge.source].append(edge)
            if target.role is not TopologyRole.domain_controller:
                diagnostics.append(
                    TopologyDiagnostic(
                        code="invalid-domain-target",
                        message=(
                            f"{source.name} joins {target.name}, but a domain membership "
                            "must target a domain controller."
                        ),
                        node_ids=[source.id, target.id],
                        edge_ids=[edge.id],
                    )
                )
            if source.role in (TopologyRole.domain_controller, TopologyRole.root_ca):
                diagnostics.append(
                    TopologyDiagnostic(
                        code="invalid-domain-member",
                        message=f"{source.name} ({source.role.value}) must not be domain joined.",
                        node_ids=[source.id],
                        edge_ids=[edge.id],
                    )
                )
        elif edge.kind is TopologyEdgeKind.ca_parent:
            parents[edge.target].append(edge)
            ca_adjacency[edge.source].append(edge.target)
            if target.role is not TopologyRole.issuing_ca:
                diagnostics.append(
                    TopologyDiagnostic(
                        code="invalid-ca-child",
                        message=f"{target.name} is not an issuing CA and cannot have a CA parent.",
                        node_ids=[target.id],
                        edge_ids=[edge.id],
                    )
                )
            if source.role not in (TopologyRole.root_ca, TopologyRole.issuing_ca):
                diagnostics.append(
                    TopologyDiagnostic(
                        code="invalid-ca-parent",
                        message=f"{source.name} is not a CA and cannot issue a CA certificate.",
                        node_ids=[source.id],
                        edge_ids=[edge.id],
                    )
                )
        elif edge.kind is TopologyEdgeKind.product_integration:
            integrations[edge.source].append(edge)
            if source.role is not TopologyRole.product:
                diagnostics.append(
                    TopologyDiagnostic(
                        code="invalid-product-integration-source",
                        message=(
                            f"{source.name} is not a product appliance and cannot "
                            "integrate with a domain."
                        ),
                        node_ids=[source.id],
                        edge_ids=[edge.id],
                    )
                )
            if target.role is not TopologyRole.domain_controller:
                diagnostics.append(
                    TopologyDiagnostic(
                        code="invalid-product-integration-target",
                        message=(
                            f"{source.name} integrates with {target.name}, but a "
                            "product integration must target a domain controller."
                        ),
                        node_ids=[source.id, target.id],
                        edge_ids=[edge.id],
                    )
                )
        elif edge.kind is TopologyEdgeKind.ca_publication:
            publications[edge.source].append(edge)
            if (
                source.role is not TopologyRole.issuing_ca
                or target.role is not TopologyRole.web_server
            ):
                diagnostics.append(
                    TopologyDiagnostic(
                        code="invalid-ca-publication",
                        message=(
                            "CA publication must connect an issuing CA to a web server; "
                            f"got {source.name} -> {target.name}."
                        ),
                        node_ids=[source.id, target.id],
                        edge_ids=[edge.id],
                    )
                )

    for node_id, edges in integrations.items():
        if len(edges) > 1:
            diagnostics.append(
                TopologyDiagnostic(
                    code="multiple-product-integrations",
                    message=(
                        f"{nodes[node_id].name} integrates with more than one "
                        "domain controller."
                    ),
                    node_ids=[node_id],
                    edge_ids=[edge.id for edge in edges],
                )
            )

    for node_id, edges in memberships.items():
        if len(edges) > 1:
            diagnostics.append(
                TopologyDiagnostic(
                    code="multiple-domains",
                    message=f"{nodes[node_id].name} belongs to more than one AD domain.",
                    node_ids=[node_id, *[edge.target for edge in edges]],
                    edge_ids=[edge.id for edge in edges],
                )
            )
    for node_id, edges in parents.items():
        if len(edges) > 1:
            diagnostics.append(
                TopologyDiagnostic(
                    code="multiple-ca-parents",
                    message=f"{nodes[node_id].name} has more than one CA parent.",
                    node_ids=[node_id, *[edge.source for edge in edges]],
                    edge_ids=[edge.id for edge in edges],
                )
            )

    cycle = _cycle_path(ca_adjacency)
    if cycle:
        diagnostics.append(
            TopologyDiagnostic(
                code="ca-cycle",
                message="CA hierarchy cycle: "
                + " -> ".join(nodes[node_id].name for node_id in cycle),
                node_ids=cycle,
            )
        )

    for issuing in (
        node for node in nodes.values() if node.role is TopologyRole.issuing_ca
    ):
        issuing_parents = parents.get(issuing.id, [])
        if not issuing_parents:
            diagnostics.append(
                TopologyDiagnostic(
                    code="missing-ca-parent",
                    message=f"{issuing.name} has no root CA parent.",
                    node_ids=[issuing.id],
                )
            )
        elif nodes[issuing_parents[0].source].role is not TopologyRole.root_ca:
            parent = nodes[issuing_parents[0].source]
            diagnostics.append(
                TopologyDiagnostic(
                    code="non-root-ca-parent",
                    message=f"{issuing.name}'s parent {parent.name} is not an offline root CA.",
                    node_ids=[issuing.id, parent.id],
                    edge_ids=[issuing_parents[0].id],
                )
            )

        issuing_memberships = memberships.get(issuing.id, [])
        if not issuing_memberships:
            diagnostics.append(
                TopologyDiagnostic(
                    code="issuing-ca-outside-domain",
                    message=(
                        f"{issuing.name} is an issuing CA and must be joined "
                        "to an AD domain."
                    ),
                    node_ids=[issuing.id],
                )
            )

        issuing_publications = publications.get(issuing.id, [])
        if not issuing_publications:
            diagnostics.append(
                TopologyDiagnostic(
                    code="missing-publication-host",
                    message=f"{issuing.name} publishes HTTP CDP/AIA, but no web host is connected.",
                    node_ids=[issuing.id],
                )
            )

        for publication in issuing_publications:
            web = nodes[publication.target]
            web_memberships = memberships.get(web.id, [])
            if not web_memberships:
                diagnostics.append(
                    TopologyDiagnostic(
                        code="publication-host-outside-domain",
                        message=(
                            f"{web.name} hosts CDP/AIA and OCSP for {issuing.name}, but is not "
                            "inside an AD domain."
                        ),
                        node_ids=[issuing.id, web.id],
                        edge_ids=[publication.id],
                    )
                )
            elif (
                issuing_memberships
                and web_memberships[0].target != issuing_memberships[0].target
            ):
                diagnostics.append(
                    TopologyDiagnostic(
                        code="publication-domain-mismatch",
                        message=f"{issuing.name} and publication host {web.name} are in different domains.",
                        node_ids=[issuing.id, web.id],
                        edge_ids=[
                            publication.id,
                            issuing_memberships[0].id,
                            web_memberships[0].id,
                        ],
                    )
                )
            if web.config.get("enableOcsp") == "Disabled":
                diagnostics.append(
                    TopologyDiagnostic(
                        code="ocsp-disabled",
                        message=f"{web.name} is the publication host, but Online Responder is disabled.",
                        node_ids=[web.id],
                        edge_ids=[publication.id],
                    )
                )

    for client in (node for node in nodes.values() if node.role is TopologyRole.client):
        if not memberships.get(client.id):
            diagnostics.append(
                TopologyDiagnostic(
                    code="client-outside-domain",
                    message=f"{client.name} cannot enroll because it is not inside an AD domain.",
                    node_ids=[client.id],
                )
            )

    publication_by_web = {
        edge.target: edge
        for edge in valid_edges
        if edge.kind is TopologyEdgeKind.ca_publication
    }
    for web in (
        node for node in nodes.values() if node.role is TopologyRole.web_server
    ):
        if (
            web.config.get("enableOcsp", "Enabled") != "Disabled"
            and web.id not in publication_by_web
        ):
            diagnostics.append(
                TopologyDiagnostic(
                    code="ocsp-template-grant-missing",
                    message=(
                        f"{web.name} has OCSP enabled, but no issuing CA grants "
                        "its enrollment templates."
                    ),
                    node_ids=[web.id],
                )
            )

    dns_ids: set[str] = set()
    dns_keys: dict[tuple[DnsRecordKind, str, str, str], DnsRecordResource] = {}
    valid_dns: list[DnsRecordResource] = []
    for record in topology.dns_records:
        if record.id in dns_ids:
            diagnostics.append(
                TopologyDiagnostic(
                    code="duplicate-dns-resource",
                    message=f"DNS resource id '{record.id}' is duplicated.",
                    node_ids=[record.server, record.subject],
                )
            )
        dns_ids.add(record.id)
        missing = [
            node_id
            for node_id in (record.server, record.subject)
            if node_id not in nodes
        ]
        if missing:
            diagnostics.append(
                TopologyDiagnostic(
                    code="dns-unknown-node",
                    message=f"DNS resource '{record.id}' references unknown node(s): {missing}.",
                    node_ids=missing,
                )
            )
            continue
        if nodes[record.server].role is not TopologyRole.domain_controller:
            diagnostics.append(
                TopologyDiagnostic(
                    code="dns-server-not-authoritative",
                    message=f"DNS resource '{record.id}' is not owned by a domain controller.",
                    node_ids=[record.server],
                )
            )
        if record.kind is DnsRecordKind.cname and not record.name:
            diagnostics.append(
                TopologyDiagnostic(
                    code="dns-cname-missing-name",
                    message=f"CNAME resource '{record.id}' needs an alias name.",
                    node_ids=[record.subject],
                )
            )
        if record.kind is not DnsRecordKind.cname and record.name is not None:
            diagnostics.append(
                TopologyDiagnostic(
                    code="dns-host-name-override",
                    message=f"{record.kind.value} resource '{record.id}' derives its name from its subject.",
                    node_ids=[record.subject],
                )
            )
        if record.kind is DnsRecordKind.ptr and not _valid_reverse_zone(record.zone):
            diagnostics.append(
                TopologyDiagnostic(
                    code="dns-invalid-reverse-zone",
                    message=f"PTR resource '{record.id}' has invalid reverse zone '{record.zone}'.",
                    node_ids=[record.server, record.subject],
                )
            )

        key_name = (record.name or record.subject).casefold()
        key = (record.kind, record.server, record.zone.casefold().rstrip("."), key_name)
        previous = dns_keys.get(key)
        if previous is not None:
            diagnostics.append(
                TopologyDiagnostic(
                    code="dns-record-conflict",
                    message=f"DNS resources '{previous.id}' and '{record.id}' claim the same record.",
                    node_ids=[record.server, previous.subject, record.subject],
                )
            )
        else:
            dns_keys[key] = record
        valid_dns.append(record)

    a_targets = {
        (record.server, record.zone.casefold().rstrip("."), record.subject)
        for record in valid_dns
        if record.kind is DnsRecordKind.a
    }
    for record in valid_dns:
        if record.kind is not DnsRecordKind.cname:
            continue
        target_key = (record.server, record.zone.casefold().rstrip("."), record.subject)
        if target_key not in a_targets:
            diagnostics.append(
                TopologyDiagnostic(
                    code="dns-cname-target-missing-a",
                    message=(
                        f"PKI CNAME '{record.name}.{record.zone}' is planned, but its target "
                        f"{nodes[record.subject].name} has no authoritative A record."
                    ),
                    node_ids=[record.server, record.subject],
                )
            )

    if diagnostics:
        raise TopologyValidationError(diagnostics)


def _kind_value(op: CompilableOp) -> str:
    return str(getattr(op.kind, "value", op.kind))


def _operation_cycle(
    dependencies: dict[str, set[str]], labels: dict[str, str]
) -> TopologyDiagnostic | None:
    """Return a concrete dependency-cycle diagnostic, if the DAG cannot drain."""

    adjacency: dict[str, list[str]] = defaultdict(list)
    for op_id, required in dependencies.items():
        for dependency in required:
            adjacency[dependency].append(op_id)
        adjacency.setdefault(op_id, [])
    cycle = _cycle_path(adjacency)
    if not cycle:
        return None
    return TopologyDiagnostic(
        code="operation-cycle",
        message="Operation dependency cycle: "
        + " -> ".join(labels[item] for item in cycle),
        node_ids=cycle,
    )


def _semantic_key(op: CompilableOp, nodes: dict[str, TopologyNode]) -> tuple:
    kind = _kind_value(op)
    node = nodes[op.target]
    role_rank = _ROLE_RANK[node.role]
    # Joins are most useful in role order too: issuing CA, publication host,
    # then optional enrollment clients.
    return (_KIND_RANK[kind], role_rank, node.name.casefold(), op.target, op.id)


def _canonical_order(
    operations: Sequence[CompilableOp],
    dependencies: dict[str, set[str]],
    nodes: dict[str, TopologyNode],
) -> list[str]:
    """Stable Kahn order with an explicit semantic tie-breaker."""

    by_id = {op.id: op for op in operations}
    dependents: dict[str, list[str]] = defaultdict(list)
    indegree = {op.id: len(dependencies[op.id]) for op in operations}
    for op_id, required in dependencies.items():
        for dependency in required:
            dependents[dependency].append(op_id)

    ready = sorted(
        (op_id for op_id, degree in indegree.items() if degree == 0),
        key=lambda op_id: _semantic_key(by_id[op_id], nodes),
    )
    ordered: list[str] = []
    while ready:
        op_id = ready.pop(0)
        ordered.append(op_id)
        for dependent in dependents[op_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=lambda item: _semantic_key(by_id[item], nodes))

    if len(ordered) != len(operations):
        labels = {
            op.id: f"{_kind_value(op)}({nodes[op.target].name})" for op in operations
        }
        diagnostic = _operation_cycle(dependencies, labels)
        raise PlanCompilationError(
            [
                diagnostic
                or TopologyDiagnostic(
                    code="operation-cycle",
                    message="Operation dependency graph contains a cycle.",
                )
            ]
        )
    return ordered


def _duration(op: CompilableOp, nodes: dict[str, TopologyNode]) -> int:
    if _kind_value(op) == "provision":
        return _PROVISION_DURATION_SECONDS[nodes[op.target].role]
    return _DURATION_SECONDS[_kind_value(op)]


def _critical_path(
    ordered: Sequence[str],
    by_id: dict[str, CompilableOp],
    dependencies: dict[str, set[str]],
    nodes: dict[str, TopologyNode],
) -> tuple[list[str], int]:
    totals: dict[str, int] = {}
    previous: dict[str, str | None] = {}
    order_index = {op_id: index for index, op_id in enumerate(ordered)}
    for op_id in ordered:
        required = dependencies[op_id]
        predecessor = (
            max(required, key=lambda item: (totals[item], -order_index[item]))
            if required
            else None
        )
        totals[op_id] = (totals[predecessor] if predecessor else 0) + _duration(
            by_id[op_id], nodes
        )
        previous[op_id] = predecessor
    end = max(ordered, key=lambda item: totals[item])
    path: list[str] = []
    current: str | None = end
    while current is not None:
        path.append(current)
        current = previous[current]
    path.reverse()
    return path, totals[end]


def _synthesize_provision(create_op: CompilableOp) -> Any:
    """The backend-only companion op that runs a fresh clone's post-clone
    provisioning (agent phone-home, boot settle, role install) as its own
    schedulable unit. Params stay empty on purpose — the runtime resolves
    everything from the sibling ``createVm`` op, which is already
    vmName-namespaced by the time the worker sees it."""

    op = create_op.model_copy(deep=True)
    op.id = f"{create_op.id}{PROVISION_SUFFIX}"
    # PlanOpKind is a str-enum; coerce through the sibling's own type so the
    # synthesized kind round-trips model_dump/redelivery identically.
    try:
        op.kind = type(create_op.kind)("provision")
    except ValueError:
        op.kind = "provision"
    op.secondary = None
    op.params = {}
    op.depends_on = []
    if hasattr(op, "files"):
        op.files = []
    return op


@dataclass(frozen=True)
class ProductIntegration:
    """What a ``productIntegration`` edge resolves to for one product node.

    Pure, and derived from the topology alone, because both callers must agree
    exactly: the execution manifest builds the deploy preview from it and the
    Celery worker builds the real sequence from it. A step list that differed
    between the two would renumber the panel's rows mid-run.
    """

    #: The domain controller the product registers its names with.
    dc_id: str
    #: Every Windows node that must trust the product's certificate — the DC
    #: itself plus its domain members. A member is included whether or not it
    #: is a CA or web server: the point is that a browser on any lab machine
    #: opens the product without a certificate warning.
    trust_node_ids: tuple[str, ...]


def product_integration(
    topology: TopologyDocument, node_id: str
) -> ProductIntegration | None:
    """Resolve ``node_id``'s product integration, or ``None`` when it has none.

    A product with no integration edge is a perfectly valid drawing — it is a
    Linux appliance nobody wired to a domain — and it simply gets no DNS
    records and no trust push.
    """
    dc_id = next(
        (
            edge.target
            for edge in topology.edges
            if edge.kind is TopologyEdgeKind.product_integration
            and edge.source == node_id
        ),
        None,
    )
    if dc_id is None:
        return None
    windows_roles = {
        TopologyRole.domain_controller,
        TopologyRole.root_ca,
        TopologyRole.issuing_ca,
        TopologyRole.web_server,
        TopologyRole.client,
        TopologyRole.standalone,
    }
    by_id = {node.id: node for node in topology.nodes}
    members = [
        edge.source
        for edge in topology.edges
        if edge.kind is TopologyEdgeKind.domain_membership and edge.target == dc_id
    ]
    trust = [
        candidate
        for candidate in [dc_id, *sorted(members)]
        if candidate in by_id and by_id[candidate].role in windows_roles
    ]
    return ProductIntegration(dc_id=dc_id, trust_node_ids=tuple(trust))


def compile_plan(
    topology: TopologyDocument, operations: Sequence[CompilableOp]
) -> CompiledPlan:
    """Compile final topology plus desired operations into an authoritative DAG.

    Caller-supplied ``dependsOn`` is never consulted.  IDs and operation
    payloads are copied unchanged, then only dependencies and list order are
    replaced.  This keeps resume/metrics identities stable across previews,
    persisted-project reloads, and retries where completed creates are absent.

    Every ``createVm`` gets a backend-synthesized ``provision`` companion op
    (clone and role install run as independent schedulable units on separate
    worker queues). Incoming ``provision`` ops are stripped first and
    re-synthesized, so recompiling a compiled plan is a fixed point, a client
    can never spoof one, and stale params can never ride along.
    """

    operations = [op for op in operations if _kind_value(op) != "provision"]
    validate_topology(topology)
    nodes = {node.id: node for node in topology.nodes}
    diagnostics: list[TopologyDiagnostic] = []
    seen_ids: set[str] = set()
    semantic_ops: dict[tuple[str, str, str | None], CompilableOp] = {}

    def error(code: str, message: str, *node_ids: str) -> None:
        diagnostics.append(
            TopologyDiagnostic(code=code, message=message, node_ids=list(node_ids))
        )

    edge_keys = {(edge.kind, edge.source, edge.target) for edge in topology.edges}
    resource_states: dict[tuple[str, str, str | None], TopologyResourceState] = {
        ("createVm", node.id, None): node.state for node in topology.nodes
    }
    resource_labels: dict[tuple[str, str, str | None], str] = {
        ("createVm", node.id, None): f"node {node.name}" for node in topology.nodes
    }
    for edge in topology.edges:
        if edge.kind is TopologyEdgeKind.domain_membership:
            key = ("domainJoin", edge.source, edge.target)
        elif edge.kind is TopologyEdgeKind.ca_parent:
            key = ("caConnect", edge.target, edge.source)
        elif edge.kind is TopologyEdgeKind.product_integration:
            # No operation of its own, on purpose: the DNS registration and the
            # trust push this relationship stands for run *inside* the product's
            # own provision op, where the installer that generated the
            # certificate and chose the names already is. Registering a resource
            # state here would demand an operation nothing ever stages and fail
            # every plan with `missing-operation`; what the edge does contribute
            # is the dependency below.
            continue
        else:
            key = ("webServerCert", edge.source, edge.target)
        resource_states[key] = edge.state
        resource_labels[key] = f"relationship {edge.id}"

    for op in operations:
        kind = _kind_value(op)
        if op.id in seen_ids:
            error(
                "duplicate-operation-id",
                f"Plan contains duplicate operation id '{op.id}'.",
            )
        seen_ids.add(op.id)
        if op.id.endswith(PROVISION_SUFFIX):
            error(
                "reserved-operation-id",
                f"Operation id '{op.id}' uses the reserved '{PROVISION_SUFFIX}' suffix.",
            )
        if kind not in _KIND_RANK:
            error(
                "unknown-operation-kind",
                f"Operation '{op.id}' has unknown kind '{kind}'.",
            )
            continue
        if op.target not in nodes:
            error(
                "operation-unknown-target",
                f"Operation '{op.id}' targets unknown node '{op.target}'.",
                op.target,
            )
            continue
        if op.secondary and op.secondary not in nodes:
            error(
                "operation-unknown-secondary",
                f"Operation '{op.id}' references unknown node '{op.secondary}'.",
                op.secondary,
            )
            continue
        key = (kind, op.target, op.secondary)
        if key in semantic_ops:
            error(
                "duplicate-operation",
                f"Operations '{semantic_ops[key].id}' and '{op.id}' describe the same action.",
                op.target,
            )
        else:
            semantic_ops[key] = op

        resource_state = resource_states.get(key)
        if resource_state is TopologyResourceState.realized:
            error(
                "operation-resource-realized",
                f"Operation '{op.id}' would repeat already realized {resource_labels[key]}.",
                op.target,
                *([op.secondary] if op.secondary else []),
            )

        if kind == "createVm":
            template = op.params.get("template")
            expected_templates = {
                TopologyRole.domain_controller: ("domainController", None),
                TopologyRole.root_ca: ("certificateAuthority", "Root"),
                TopologyRole.issuing_ca: ("certificateAuthority", "Issuing"),
                TopologyRole.web_server: ("webServer", None),
                TopologyRole.client: ("client", None),
                # A pre-`product`-role canvas emitted every Linux product as
                # `standalone`; still accepted so a topology saved before that
                # role existed compiles unchanged.
                TopologyRole.standalone: (
                    {"standalone", "certsecure", "cbom", "codesign"},
                    None,
                ),
                TopologyRole.product: ({"certsecure", "cbom", "codesign"}, None),
            }[nodes[op.target].role]
            allowed = expected_templates[0]
            template_matches = (
                template in allowed if isinstance(allowed, set) else template == allowed
            )
            if not template_matches or (
                expected_templates[1]
                and op.params.get("caType") != expected_templates[1]
            ):
                error(
                    "operation-role-mismatch",
                    f"createVm for {nodes[op.target].name} does not match role {nodes[op.target].role.value}.",
                    op.target,
                )
        elif kind == "domainJoin":
            if (
                not op.secondary
                or (
                    TopologyEdgeKind.domain_membership,
                    op.target,
                    op.secondary,
                )
                not in edge_keys
            ):
                error(
                    "operation-relationship-mismatch",
                    f"domainJoin for {nodes[op.target].name} is not present in the final topology.",
                    op.target,
                    *([op.secondary] if op.secondary else []),
                )
        elif kind == "caConnect":
            if (
                not op.secondary
                or (
                    TopologyEdgeKind.ca_parent,
                    op.secondary,
                    op.target,
                )
                not in edge_keys
            ):
                error(
                    "operation-relationship-mismatch",
                    f"caConnect for {nodes[op.target].name} is not present in the final topology.",
                    op.target,
                    *([op.secondary] if op.secondary else []),
                )
        elif kind == "webServerCert":
            if (
                not op.secondary
                or (
                    TopologyEdgeKind.ca_publication,
                    op.target,
                    op.secondary,
                )
                not in edge_keys
            ):
                error(
                    "operation-relationship-mismatch",
                    f"webServerCert for {nodes[op.target].name} is not present in the final topology.",
                    op.target,
                    *([op.secondary] if op.secondary else []),
                )

    for key, state in resource_states.items():
        if state is TopologyResourceState.planned and key not in semantic_ops:
            kind, target, secondary = key
            error(
                "missing-operation",
                f"Planned {resource_labels[key]} requires a '{kind}' operation.",
                target,
                *([secondary] if secondary else []),
            )

    if diagnostics:
        raise PlanCompilationError(diagnostics)

    cloned = [op.model_copy(deep=True) for op in operations]
    # One provision companion per createVm — even agent-less (authored-ISO)
    # clones, which keeps compilation pure; the runtime no-ops when the
    # registry shows no agent. Realized nodes have no createVm here, so they
    # get no provision either.
    provision_ops = [
        _synthesize_provision(op) for op in cloned if _kind_value(op) == "createVm"
    ]
    cloned.extend(provision_ops)
    by_id = {op.id: op for op in cloned}
    creates = {op.target: op.id for op in cloned if _kind_value(op) == "createVm"}
    provisions = {op.target: op.id for op in provision_ops}
    leaves = {op.target: op.id for op in cloned if _kind_value(op) == "domainLeave"}
    joins = {
        (op.target, op.secondary): op.id
        for op in cloned
        if _kind_value(op) == "domainJoin"
    }
    ca_connects = {
        (op.target, op.secondary): op.id
        for op in cloned
        if _kind_value(op) == "caConnect"
    }
    dependencies: dict[str, set[str]] = {op.id: set() for op in cloned}

    membership_by_member = {
        edge.source: edge.target
        for edge in topology.edges
        if edge.kind is TopologyEdgeKind.domain_membership
    }
    parent_by_ca = {
        edge.target: edge.source
        for edge in topology.edges
        if edge.kind is TopologyEdgeKind.ca_parent
    }
    publications_by_ca: dict[str, list[str]] = defaultdict(list)
    for edge in topology.edges:
        if edge.kind is TopologyEdgeKind.ca_publication:
            publications_by_ca[edge.source].append(edge.target)
    integration_dc_by_product = {
        edge.source: edge.target
        for edge in topology.edges
        if edge.kind is TopologyEdgeKind.product_integration
    }
    members_by_dc: dict[str, list[str]] = defaultdict(list)
    for member_id, dc_id in membership_by_member.items():
        members_by_dc[dc_id].append(member_id)

    def require(op_id: str, candidate: str | None) -> None:
        if candidate is not None and candidate != op_id:
            dependencies[op_id].add(candidate)

    def provisioned(node_id: str | None) -> str | None:
        """'VM ready and its role installed' — the node's provision op when it
        is cloned this plan, else its bare createVm (realized nodes: neither)."""
        if node_id is None:
            return None
        return provisions.get(node_id) or creates.get(node_id)

    for op in cloned:
        kind = _kind_value(op)
        if kind == "createVm":
            continue
        if kind == "provision":
            require(op.id, creates.get(op.target))
            dc_id = integration_dc_by_product.get(op.target)
            if dc_id is not None:
                # The product's sequence writes A records at this DC and pushes
                # its certificate into the machine root store of every Windows
                # node in the domain — so all of them must already be standing,
                # with a live agent, before it runs. That makes an integrated
                # product a leaf of its domain's subgraph, which is also where
                # it belongs: it is the last thing in the lab to be stood up and
                # the first thing anyone opens.
                require(op.id, provisioned(dc_id))
                for member_id in members_by_dc[dc_id]:
                    require(op.id, provisioned(member_id))
            continue
        require(op.id, provisioned(op.target))
        if op.secondary:
            require(op.id, provisioned(op.secondary))
        if kind == "domainJoin":
            require(op.id, leaves.get(op.target))
            if nodes[op.target].role is TopologyRole.client:
                dc_id = op.secondary
                for issuing_id, issuing_dc in membership_by_member.items():
                    if (
                        issuing_dc == dc_id
                        and nodes[issuing_id].role is TopologyRole.issuing_ca
                    ):
                        root_id = parent_by_ca.get(issuing_id)
                        require(op.id, ca_connects.get((issuing_id, root_id)))
        elif kind == "caConnect":
            issuing_id = op.target
            dc_id = membership_by_member[issuing_id]
            require(op.id, provisioned(dc_id))
            require(op.id, joins.get((issuing_id, dc_id)))
            for web_id in publications_by_ca[issuing_id]:
                web_dc_id = membership_by_member[web_id]
                require(op.id, provisioned(web_id))
                require(op.id, provisioned(web_dc_id))
                require(op.id, joins.get((web_id, web_dc_id)))
        elif kind == "webServerCert":
            issuing_id = op.target
            web_id = op.secondary
            root_id = parent_by_ca[issuing_id]
            issuing_dc_id = membership_by_member[issuing_id]
            web_dc_id = membership_by_member[web_id]
            require(op.id, provisioned(root_id))
            require(op.id, provisioned(issuing_dc_id))
            require(op.id, provisioned(web_dc_id))
            require(op.id, joins.get((issuing_id, issuing_dc_id)))
            require(op.id, joins.get((web_id, web_dc_id)))
            require(op.id, ca_connects.get((issuing_id, root_id)))

    ordered_ids = _canonical_order(cloned, dependencies, nodes)
    ordered = [by_id[op_id] for op_id in ordered_ids]
    for op in ordered:
        op.depends_on = sorted(
            dependencies[op.id], key=lambda item: ordered_ids.index(item)
        )
    critical_path, critical_duration = _critical_path(
        ordered_ids, by_id, dependencies, nodes
    )
    return CompiledPlan(
        operations=ordered,
        dns_records=list(topology.dns_records),
        critical_path=critical_path,
        estimated_duration_seconds=sum(_duration(op, nodes) for op in ordered),
        critical_path_duration_seconds=critical_duration,
    )
