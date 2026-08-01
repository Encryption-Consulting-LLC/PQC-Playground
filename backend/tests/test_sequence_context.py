"""Cross-node runtime aliases match each compiled operation's semantics."""

import os
import re
from types import SimpleNamespace

os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault(
    "SETTINGS_ENC_KEY",
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
)

from app.core.sequences import context as sequence_context  # noqa: E402
from app.core.sequences.model import NodeContext  # noqa: E402


def _node(node_id: str, template_id: str, config=None) -> NodeContext:
    return NodeContext(
        node_id=node_id,
        vm_name=f"guest-abc12-{node_id}",
        hostname=f"guest-abc12-{node_id}",
        agent_vm_id=f"v-{node_id}",
        ip="192.168.1.1",
        template_id=template_id,
        template_config=config or {},
    )


def _field(doc, path: str):
    value = doc
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(doc, key: str, condition) -> bool:
    value = _field(doc, key)
    if not isinstance(condition, dict):
        return value == condition
    if "$ne" in condition and value == condition["$ne"]:
        return False
    if "$in" in condition and value not in condition["$in"]:
        return False
    if "$regex" in condition and not re.search(condition["$regex"], value or ""):
        return False
    return True


class _FakeCollection:
    """Just enough Mongo for the registry lookups: dotted keys, $ne/$in/$regex,
    and natural (insertion) order — which is what makes an unscoped ``find_one``
    over several environments pick an arbitrary one."""

    def __init__(self, docs):
        self._docs = docs

    def find(self, query):
        return [
            d for d in self._docs if all(_matches(d, k, v) for k, v in query.items())
        ]

    def find_one(self, query):
        return next(iter(self.find(query)), None)


def _registry_doc(vm_name: str, node_id: str, template_id: str) -> dict:
    return {
        "vmName": vm_name,
        "appName": node_id,
        "status": "ready",
        "agent": {"vmId": f"v-{node_id}", "templateId": template_id},
    }


def test_namespace_prefix_keeps_the_project_segment():
    # `guest-<user>-` alone spans every project the guest ever deployed.
    assert (
        sequence_context._namespace_prefix("guest-guest-cc92f3-ca02")
        == "guest-guest-cc92f3-"
    )
    assert sequence_context._namespace_prefix("guest-alice-dc01") == "guest-alice-"
    assert sequence_context._namespace_prefix("lab-dc01") is None


def test_sibling_lookup_skips_an_earlier_projects_domain_controller(monkeypatch):
    db = {
        "vm_registry": _FakeCollection(
            [
                _registry_doc("guest-guest-039a8a-dc01", "dc-old", "domainController"),
                _registry_doc("guest-guest-cc92f3-dc01", "dc-new", "domainController"),
            ]
        )
    }
    monkeypatch.setattr(
        sequence_context,
        "_resolve_node",
        lambda db, node_id: _node(node_id, "domainController"),
    )

    found = sequence_context._find_domain_controller(db, "guest-guest-cc92f3-ca02")

    assert found.node_id == "dc-new"


def test_web_publication_context_targets_the_web_host(monkeypatch):
    ca = _node("ca02", "certificateAuthority", {"caType": "Issuing"})
    root = _node("ca01", "certificateAuthority", {"caType": "Root"})
    web = _node("srv1", "webServer")
    dc = _node(
        "dc01",
        "domainController",
        {"domainName": "encon.pki", "netbiosName": "ENCON"},
    )
    by_id = {"ca02": ca, "srv1": web}
    monkeypatch.setattr(
        sequence_context, "_resolve_node", lambda db, node_id: by_id[node_id]
    )
    monkeypatch.setattr(
        sequence_context, "_find_domain_controller", lambda db, name, scope=None: dc
    )
    monkeypatch.setattr(
        sequence_context,
        "_find_by_template",
        lambda db, name, template, scope=None: web,
    )
    monkeypatch.setattr(
        sequence_context, "_find_issuing_ca", lambda db, name, scope=None: ca
    )
    monkeypatch.setattr(
        sequence_context, "_find_root_ca", lambda db, name, scope=None: root
    )
    op = SimpleNamespace(
        kind=SimpleNamespace(value="webServerCert"),
        target="ca02",
        secondary="srv1",
    )

    resolved = sequence_context.build_run_context({}, op, [])

    assert resolved.node("primary").node_id == "srv1"
    assert resolved.node("secondary").node_id == "ca02"
    assert resolved.node("ca").node_id == "ca02"
    assert resolved.node("root").node_id == "ca01"
