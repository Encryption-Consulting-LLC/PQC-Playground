"""Cross-node context resolution for plan-op sequences.

A non-createVm op (domainJoin / caConnect / webServerCert) touches more than
its own node: a member joins a *DC*, an issuing CA cross-signs with a *root*
and publishes through the *DC* and *web* host. This builds the
:class:`~app.core.sequences.model.RunContext` those sequences resolve their
params against — reading each node's live ``vm_registry`` doc (by ``appName``,
which is the canvas node id) for its real vmName, agent vm_id, IP and stored
(encrypted) template config, then deriving the shared domain facts
(``domainName`` / ``netbios`` / ``pki_host = pki.<domain>``).

Resolving from the registry, not the plan's own ``createVm`` ops, keeps it
correct on a **retry** plan — where the already-``done`` createVm ops have been
dropped from the op list but the VMs (and their registry docs) still exist. The
registry outlives a single plan, though, so every by-role lookup is confined to
the node ids the plan's own topology declares; without that a user's earlier,
long-torn-down environment is just as eligible as the one being deployed.

Every hostname a step emits into a URL / CNAME / UNC / ACL is the *other*
node's real guest-namespaced ``firstboot.hostname_for(vmName)`` — never a
display name — so the URLs baked into issued certs actually resolve.

Runs in the Celery worker over a sync Mongo client.
"""

import logging

from app.core.firstboot import hostname_for
from app.core.sequences.model import DnsRecordContext, NodeContext, RunContext
from app.core.template_config import decrypt_config_secrets
from app.core.vm_naming import namespace_prefix

logger = logging.getLogger(__name__)

#: Context alias keys the sequence definitions reference.
PRIMARY = "primary"
SECONDARY = "secondary"
DC = "dc"
ROOT = "root"
WEB = "web"
CA = "ca"  # the forest's issuing (enterprise) CA


class ContextError(Exception):
    """A node the op depends on isn't resolvable (missing/deleted registry doc,
    or no live agent) — a dangling reference that can't degrade gracefully."""


def _resolve_node(db, node_id: str) -> NodeContext:
    """Build a :class:`NodeContext` for ``node_id`` from its live registry doc
    (keyed on ``appName``). Raises :class:`ContextError` if it's absent."""
    doc = db["vm_registry"].find_one({"appName": node_id, "status": {"$ne": "deleted"}})
    if doc is None:
        raise ContextError(f"no live VM registered for node '{node_id}'")

    agent = doc.get("agent") or {}
    template = agent.get("templateId")
    # Decrypt the stored (encrypted) template config back to plaintext for
    # dispatch — this is where the DC's domainAdminPassword becomes usable.
    stored_config = agent.get("templateConfig") or {}
    config = decrypt_config_secrets(template, stored_config) if template else {}

    return NodeContext(
        node_id=node_id,
        vm_name=doc["vmName"],
        hostname=hostname_for(doc["vmName"]),
        agent_vm_id=agent.get("vmId"),
        ip=doc.get("ip"),
        template_id=template,
        template_config=config,
    )


def _namespace_prefix(vm_name: str) -> str | None:
    """The prefix a namespaced VM's siblings share — the same parse
    ``authz.enforce_guest_vm_name`` builds names with (``core/vm_naming``)."""
    return namespace_prefix(vm_name)


def _plan_node_ids(topology) -> set[str] | None:
    """The canvas node ids this plan's topology declares — the exact set a
    sibling lookup is allowed to resolve within. None when the caller supplied
    no topology, leaving only the VM-name namespace to scope by."""
    if topology is None:
        return None
    return {node.id for node in topology.nodes}


def _scoped_query(query: dict, sibling_vm_name: str, scope: "set[str] | None") -> dict:
    """Confine a sibling lookup to one environment, in place.

    ``scope`` — the plan's own topology node ids — is exact and preferred: a VM
    from a previous deploy is simply not in it. The VM-name namespace is the
    fallback for callers without a topology; it is coarser, because a name
    carries no more than the guest's identity and project code."""
    if scope is not None:
        query["appName"] = {"$in": sorted(scope)}
        return query
    prefix = _namespace_prefix(sibling_vm_name)
    if prefix is not None:
        query["vmName"] = {"$regex": f"^{prefix}"}
    return query


def _find_by_template(
    db, sibling_vm_name: str, template_id: str, scope: "set[str] | None" = None
) -> NodeContext | None:
    """The node of ``template_id`` sharing ``sibling_vm_name``'s environment, if
    one is registered — how ops locate the forest's DC / web host / root CA
    without the plan naming every node explicitly."""
    query = _scoped_query(
        {"agent.templateId": template_id, "status": {"$ne": "deleted"}},
        sibling_vm_name,
        scope,
    )
    doc = db["vm_registry"].find_one(query)
    return _resolve_node(db, doc["appName"]) if doc else None


def _find_domain_controller(
    db, sibling_vm_name: str, scope: "set[str] | None" = None
) -> NodeContext | None:
    return _find_by_template(db, sibling_vm_name, "domainController", scope)


def _find_ca_by_type(
    db, sibling_vm_name: str, ca_type: str, scope: "set[str] | None" = None
) -> NodeContext | None:
    """The certificateAuthority in ``sibling_vm_name``'s environment whose
    (decrypted) config is ``caType=<ca_type>``."""
    query = _scoped_query(
        {"agent.templateId": "certificateAuthority", "status": {"$ne": "deleted"}},
        sibling_vm_name,
        scope,
    )
    for doc in db["vm_registry"].find(query):
        node = _resolve_node(db, doc["appName"])
        if node.template_config.get("caType") == ca_type:
            return node
    return None


def _find_issuing_ca(
    db, sibling_vm_name: str, scope: "set[str] | None" = None
) -> NodeContext | None:
    """The enterprise *issuing* CA — the node clients enroll against and the web
    host's OCSP responder points at."""
    return _find_ca_by_type(db, sibling_vm_name, "Issuing", scope)


def _find_root_ca(
    db, sibling_vm_name: str, scope: "set[str] | None" = None
) -> NodeContext | None:
    """The standalone root CA of the operation's environment."""
    return _find_ca_by_type(db, sibling_vm_name, "Root", scope)


def dns_records_for_context(topology) -> tuple[DnsRecordContext, ...]:
    """Convert topology Pydantic resources to the sequence's pure dataclass."""

    if topology is None:
        return ()
    return tuple(
        DnsRecordContext(
            id=record.id,
            kind=str(getattr(record.kind, "value", record.kind)),
            server=record.server,
            subject=record.subject,
            zone=record.zone,
            name=record.name,
        )
        for record in topology.dns_records
    )


def build_run_context(db, op, all_ops, topology=None) -> RunContext:
    """Resolve the :class:`RunContext` for a non-createVm ``op``.

    Populates the alias keys the definitions use: ``primary`` (the op's target),
    ``secondary`` (its ``secondary`` node, when set), and ``dc`` (the domain
    controller — the ``secondary`` itself for a join, otherwise the DC found in
    the same environment). Domain facts come from the DC's own config.

    Every by-role lookup is confined to ``topology``'s own nodes, so a plan can
    only ever wire itself to the environment it describes.
    """
    kind = str(getattr(op.kind, "value", op.kind))
    # A webServerCert relationship is authored issuing-CA -> web-host so the
    # compiler can key CA dependencies naturally, while its command sequence
    # executes primarily on the web host. Normalize those aliases here; the
    # other op kinds already execute on their authored target.
    primary_id = op.secondary if kind == "webServerCert" else op.target
    secondary_id = op.target if kind == "webServerCert" else op.secondary
    if primary_id is None:
        raise ContextError(f"operation '{kind}' has no primary node")
    nodes: dict[str, NodeContext] = {PRIMARY: _resolve_node(db, primary_id)}
    scope = _plan_node_ids(topology)

    secondary_ctx = None
    if secondary_id:
        secondary_ctx = _resolve_node(db, secondary_id)
        nodes[SECONDARY] = secondary_ctx

    # The DC is the secondary when the op joins to it directly; otherwise the
    # forest's DC found by namespace.
    if secondary_ctx is not None and secondary_ctx.template_id == "domainController":
        dc_ctx = secondary_ctx
    else:
        dc_ctx = _find_domain_controller(db, nodes[PRIMARY].vm_name, scope)
    if dc_ctx is not None:
        nodes[DC] = dc_ctx

    # caConnect: the secondary is the parent (root) CA — also expose it as
    # `root`. The issuing CA additionally publishes through the forest's web
    # host, resolved by namespace.
    if (
        secondary_ctx is not None
        and secondary_ctx.template_config.get("caType") == "Root"
    ):
        nodes[ROOT] = secondary_ctx
    else:
        root_ctx = _find_root_ca(db, nodes[PRIMARY].vm_name, scope)
        if root_ctx is not None:
            nodes[ROOT] = root_ctx
    web_ctx = _find_by_template(db, nodes[PRIMARY].vm_name, "webServer", scope)
    if web_ctx is not None:
        nodes[WEB] = web_ctx

    # The issuing CA: the op's secondary when a web/client wires to it directly,
    # else found by namespace (for the DNS CNAME / client-enrollment gate).
    if (
        secondary_ctx is not None
        and secondary_ctx.template_config.get("caType") == "Issuing"
    ):
        nodes[CA] = secondary_ctx
    else:
        ca_ctx = _find_issuing_ca(db, nodes[PRIMARY].vm_name, scope)
        if ca_ctx is not None:
            nodes[CA] = ca_ctx

    domain_name = dc_ctx.template_config.get("domainName") if dc_ctx else None
    netbios = dc_ctx.template_config.get("netbiosName") if dc_ctx else None
    pki_host = f"pki.{domain_name}" if domain_name else None
    return RunContext(
        nodes=nodes,
        domain_name=domain_name,
        netbios=netbios,
        pki_host=pki_host,
        dns_records=dns_records_for_context(topology),
    )


def build_teardown_context(db, topology, primary_id: str) -> RunContext:
    """Resolve all surviving topology nodes for teardown action sequences."""

    from app.core.topology import TopologyRole

    resolved = {}
    for node in topology.nodes:
        try:
            resolved[node.id] = _resolve_node(db, node.id)
        except ContextError:
            continue
    if primary_id not in resolved:
        raise ContextError(f"no live VM registered for node '{primary_id}'")
    primary = resolved[primary_id]
    nodes = {PRIMARY: primary}
    role_aliases = {
        TopologyRole.domain_controller: DC,
        TopologyRole.root_ca: ROOT,
        TopologyRole.issuing_ca: CA,
        TopologyRole.web_server: WEB,
    }
    for topology_node in topology.nodes:
        if topology_node.id in resolved and topology_node.role in role_aliases:
            nodes[role_aliases[topology_node.role]] = resolved[topology_node.id]
    dc = nodes.get(DC)
    return RunContext(
        nodes=nodes,
        domain_name=dc.template_config.get("domainName") if dc else None,
        netbios=dc.template_config.get("netbiosName") if dc else None,
        pki_host=(
            f"pki.{dc.template_config.get('domainName')}"
            if dc and dc.template_config.get("domainName")
            else None
        ),
        dns_records=dns_records_for_context(topology),
    )
