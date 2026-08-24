"""Grouping the VM registry into environments, and flagging what looks dead.

The registry is a flat list of VMs, but a PKI lab is only ever built, used, and
reclaimed as a whole — a root CA, an issuing CA, a domain controller and a web
host that mean nothing apart. This module reassembles those groups from the
registry alone and decides which rows look abandoned, so the ``/admin`` console
can show the playground the way it is actually used.

Deliberately pure: plain dicts in, pydantic models out. No Mongo, no pyVmomi,
no FastAPI. Every input a decision depends on — the ESXi inventory, the account
list, the clock — is a parameter, which is what makes the classification rules
testable against hand-written rows instead of a live host.

**Group identity.** ``(owner, projectCode)``. Both come from a persisted field
when the row has one and from the VM name otherwise (``core/vm_naming``);
project *code* rather than project *id* because that is the only value both
sources can produce — a name carries six characters of a UUID and cannot be
turned back into it.

**Owner resolution never guesses.** A name yields a slug, not an account. A
reconciliation pass upgrades a slug to the real username only when exactly one
account slugs to it; two accounts colliding leaves the group showing the slug
and flagged ambiguous. Rows with no namespace at all — operator VMs, anything
predating the scheme — share one ``ungrouped`` bucket, which the console offers
per-VM teardown for and no whole-group action.

**Orphan signals accumulate.** A row can be absent from the host *and* have a
dead agent *and* be stuck cloning; each is a separate reason and all of them
are shown. ``inventory=None`` means the host could not be read, and the absence
signal is then simply not computed — never asserted false, which would render a
live VM as orphaned during an ESXi outage.
"""

from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.core.db.models import now_ms
from app.core.vm_naming import parse_guest_vm_name

#: Signal ids, mirrored by the console's badge map.
ABSENT_FROM_ESXI = "absentFromEsxi"
AGENT_DEAD = "agentDead"
STATUS_ERROR = "statusError"
STUCK_CLONING = "stuckCloning"

#: Group key for every row with no resolvable namespace.
UNGROUPED_KEY = "ungrouped"

_DEFAULT_AGENT_DEAD_AFTER_S = 86_400
_DEFAULT_STUCK_CLONING_AFTER_S = 3_600


class VmDetail(BaseModel):
    """One registry row, classified. camelCase on the wire like every
    ``/admin`` route."""

    model_config = ConfigDict(populate_by_name=True)

    vm_name: str = Field(alias="vmName")
    app_name: str | None = Field(default=None, alias="appName")
    status: str = "ready"
    ip: str | None = None
    #: True/False from a readable inventory, None when ESXi could not be read.
    present_in_esxi: bool | None = Field(default=None, alias="presentInEsxi")
    #: ``none`` when the VM never had an agent — a Linux template, an authored
    #: ISO, bundling switched off, or a row already torn down. Distinct from
    #: ``dead`` on purpose: a missing agent is normal, a silent one is not.
    agent_state: str = Field(default="none", alias="agentState")
    last_connected_at: int | None = Field(default=None, alias="lastConnectedAt")
    signals: list[str] = Field(default_factory=list)
    #: Whether deleting the registry row alone is provably safe (see
    #: ``is_purgeable``) — the console's interlock between "purge a bookkeeping
    #: entry" and "destroy a VM".
    purgeable: bool = False
    job_id: str | None = Field(default=None, alias="jobId")
    created_at: int | None = Field(default=None, alias="createdAt")
    updated_at: int | None = Field(default=None, alias="updatedAt")


class EnvironmentSummary(BaseModel):
    """One lab: every VM sharing an owner and a project code."""

    model_config = ConfigDict(populate_by_name=True)

    #: Opaque, stable per (owner, project) — what the console keys rows on.
    key: str
    owner: str | None = None
    #: False when the owner is a name-derived slug rather than a real account.
    owner_resolved: bool = Field(default=False, alias="ownerResolved")
    #: True when more than one account slugs to this namespace, so the row
    #: deliberately shows the slug instead of picking one.
    owner_ambiguous: bool = Field(default=False, alias="ownerAmbiguous")
    project_code: str | None = Field(default=None, alias="projectCode")
    #: The real project id, from the first row in the group that persisted one.
    #: None for a group assembled purely from VM names, which carry a code and
    #: can never yield the id it came from — so anything that has to look the
    #: project up (issuing a lab join code, for one) can only do it for a group
    #: that has this.
    project_id: str | None = Field(default=None, alias="projectId")
    #: Only ever set for a project persisted in Mongo. Guest project names live
    #: in the browser, so a guest environment falls back to its code.
    project_name: str | None = Field(default=None, alias="projectName")
    vms: list[VmDetail] = Field(default_factory=list)
    vm_count: int = Field(default=0, alias="vmCount")
    ip_count: int = Field(default=0, alias="ipCount")
    agents_live: int = Field(default=0, alias="agentsLive")
    agents_total: int = Field(default=0, alias="agentsTotal")
    flagged: int = 0
    created_at: int | None = Field(default=None, alias="createdAt")
    updated_at: int | None = Field(default=None, alias="updatedAt")


def _agent(doc: Mapping[str, Any]) -> dict | None:
    agent = doc.get("agent")
    return agent if isinstance(agent, dict) and agent else None


def _agent_last_seen(agent: Mapping[str, Any]) -> int | None:
    """When this agent last proved it was alive, or first could have.

    ``lastConnectedAt`` is the real signal; ``mintedAt`` is the fallback for an
    identity that never connected at all, so a VM that has been silent since
    the day it was cloned still ages out.
    """
    last = agent.get("lastConnectedAt")
    return last if isinstance(last, int) else agent.get("mintedAt")


def orphan_signals(
    doc: Mapping[str, Any],
    *,
    inventory: set[str] | None,
    now: int | None = None,
    agent_dead_after_s: int = _DEFAULT_AGENT_DEAD_AFTER_S,
    stuck_cloning_after_s: int = _DEFAULT_STUCK_CLONING_AFTER_S,
) -> list[str]:
    """Every reason this row looks abandoned, in severity order."""
    now = now if now is not None else now_ms()
    status = doc.get("status") or "ready"
    signals: list[str] = []

    # Absence is only meaningful against an inventory we actually read, and
    # only for a row that still claims to have a VM.
    if inventory is not None and status != "deleted" and doc["vmName"] not in inventory:
        signals.append(ABSENT_FROM_ESXI)

    agent = _agent(doc)
    if agent is not None and status != "deleted":
        last_seen = _agent_last_seen(agent)
        if isinstance(last_seen, int) and now - last_seen > agent_dead_after_s * 1000:
            signals.append(AGENT_DEAD)

    if status == "error":
        signals.append(STATUS_ERROR)

    # ``updatedAt``, not ``createdAt``: the registry upsert pins createdAt with
    # $setOnInsert, so a re-clone over an existing name would look instantly
    # stale if this measured from creation.
    if status == "cloning":
        updated = doc.get("updatedAt")
        if isinstance(updated, int) and now - updated > stuck_cloning_after_s * 1000:
            signals.append(STUCK_CLONING)

    return signals


def is_purgeable(doc: Mapping[str, Any], *, inventory: set[str] | None) -> bool:
    """Whether deleting this row outright destroys no bookkeeping that still
    describes a real VM.

    True only when the row is already marked ``deleted`` or the VM is provably
    absent from a readable inventory. An unreadable inventory proves nothing,
    so it answers False — the console's purge action stays disabled rather than
    letting an admin erase the last record of a live machine.
    """
    if (doc.get("status") or "ready") == "deleted":
        return True
    return inventory is not None and doc["vmName"] not in inventory


def classify_vm(
    doc: Mapping[str, Any],
    *,
    inventory: set[str] | None,
    now: int | None = None,
    agent_dead_after_s: int = _DEFAULT_AGENT_DEAD_AFTER_S,
    stuck_cloning_after_s: int = _DEFAULT_STUCK_CLONING_AFTER_S,
) -> VmDetail:
    """One registry row → the console's per-VM view of it."""
    now = now if now is not None else now_ms()
    signals = orphan_signals(
        doc,
        inventory=inventory,
        now=now,
        agent_dead_after_s=agent_dead_after_s,
        stuck_cloning_after_s=stuck_cloning_after_s,
    )
    agent = _agent(doc)
    if agent is None:
        agent_state = "none"
    else:
        agent_state = "dead" if AGENT_DEAD in signals else "live"

    status = doc.get("status") or "ready"
    return VmDetail(
        vm_name=doc["vmName"],
        app_name=doc.get("appName"),
        status=status,
        ip=doc.get("ip"),
        present_in_esxi=(None if inventory is None else doc["vmName"] in inventory),
        agent_state=agent_state,
        last_connected_at=(_agent_last_seen(agent) if agent else None),
        signals=signals,
        purgeable=is_purgeable(doc, inventory=inventory),
        job_id=doc.get("jobId"),
        created_at=doc.get("createdAt"),
        updated_at=doc.get("updatedAt"),
    )


def _identity(doc: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """``(owner_key, owner_or_slug, project_code)`` for one row.

    A persisted field always wins over the name: the name is a lossy encoding
    of the same facts, kept only for rows written before they were stored.
    """
    owner = doc.get("owner")
    code = doc.get("projectCode")
    parsed = parse_guest_vm_name(doc["vmName"])

    if isinstance(owner, str) and owner:
        return (
            f"user:{owner}",
            owner,
            (code or (parsed.project_code if parsed else None)),
        )
    if parsed is not None:
        return f"slug:{parsed.user}", parsed.user, (code or parsed.project_code)
    return UNGROUPED_KEY, None, None


def _slug_remap(
    keys: Iterable[str], slug_owners: Mapping[str, Iterable[str]]
) -> dict[str, str | None]:
    """``slug:<x>`` → the one account that slugs to ``x``, or None if several do.

    Absent from the result when nothing matches: an account may simply have
    been deleted since its VMs were cloned, and the slug remains the best
    available label for them.
    """
    resolved: dict[str, str | None] = {}
    for key in keys:
        if not key.startswith("slug:"):
            continue
        slug = key[len("slug:") :]
        matches = sorted({name for name in slug_owners.get(slug, ())})
        if len(matches) == 1:
            resolved[key] = matches[0]
        elif len(matches) > 1:
            resolved[key] = None  # ambiguous — never pick one
    return resolved


def group_environments(
    docs: Iterable[Mapping[str, Any]],
    *,
    inventory: set[str] | None,
    slug_owners: Mapping[str, Iterable[str]] | None = None,
    project_names: Mapping[str, str] | None = None,
    now: int | None = None,
    include_deleted: bool = False,
    agent_dead_after_s: int = _DEFAULT_AGENT_DEAD_AFTER_S,
    stuck_cloning_after_s: int = _DEFAULT_STUCK_CLONING_AFTER_S,
) -> list[EnvironmentSummary]:
    """Registry rows → the labs they belong to, newest first.

    ``slug_owners`` maps a name slug to every account that produces it (built
    from the users collection by the caller); supplying it is what turns a
    name-derived ``slug:alice`` group into ``alice@corp.com`` and merges it
    with the same user's field-backed rows.
    """
    now = now if now is not None else now_ms()
    rows = [
        doc
        for doc in docs
        if include_deleted or (doc.get("status") or "ready") != "deleted"
    ]

    # Pass 1 — raw identity per row.
    identified = [(doc, _identity(doc)) for doc in rows]

    # Pass 2 — reconcile name-derived slugs against real accounts, so a row
    # that predates persisted ownership lands in the same group as one that
    # doesn't.
    remap = _slug_remap({key for _, (key, _, _) in identified}, slug_owners or {})

    groups: dict[tuple[str, str | None], dict[str, Any]] = {}
    for doc, (owner_key, owner_label, code) in identified:
        resolved_owner = remap.get(owner_key, ...)
        ambiguous = resolved_owner is None
        if isinstance(resolved_owner, str):
            owner_key, owner_label = f"user:{resolved_owner}", resolved_owner
        group_key = (owner_key, code)
        group = groups.setdefault(
            group_key,
            {
                "owner": owner_label,
                "owner_resolved": owner_key.startswith("user:"),
                "owner_ambiguous": ambiguous,
                "project_code": code,
                "project_id": None,
                "docs": [],
            },
        )
        group["docs"].append(doc)
        if group["project_id"] is None and isinstance(doc.get("projectId"), str):
            group["project_id"] = doc["projectId"]

    summaries = [
        _summarize(
            key,
            group,
            inventory=inventory,
            project_names=project_names or {},
            now=now,
            agent_dead_after_s=agent_dead_after_s,
            stuck_cloning_after_s=stuck_cloning_after_s,
        )
        for key, group in groups.items()
    ]
    # Newest first, with the ungrouped bucket pinned last: it is a catch-all,
    # not a lab, and burying it keeps real environments at the top.
    summaries.sort(
        key=lambda env: (env.key == UNGROUPED_KEY, -(env.created_at or 0), env.key)
    )
    return summaries


def _summarize(
    group_key: tuple[str, str | None],
    group: Mapping[str, Any],
    *,
    inventory: set[str] | None,
    project_names: Mapping[str, str],
    now: int,
    agent_dead_after_s: int,
    stuck_cloning_after_s: int,
) -> EnvironmentSummary:
    owner_key, code = group_key
    vms = sorted(
        (
            classify_vm(
                doc,
                inventory=inventory,
                now=now,
                agent_dead_after_s=agent_dead_after_s,
                stuck_cloning_after_s=stuck_cloning_after_s,
            )
            for doc in group["docs"]
        ),
        key=lambda vm: vm.vm_name,
    )
    created = [vm.created_at for vm in vms if vm.created_at is not None]
    updated = [vm.updated_at for vm in vms if vm.updated_at is not None]
    project_id = group.get("project_id")
    return EnvironmentSummary(
        key=owner_key if owner_key == UNGROUPED_KEY else f"{owner_key}|{code or ''}",
        owner=group["owner"],
        owner_resolved=group["owner_resolved"],
        owner_ambiguous=group["owner_ambiguous"],
        project_code=code,
        project_id=project_id,
        project_name=project_names.get(project_id) if project_id else None,
        vms=vms,
        vm_count=len(vms),
        ip_count=sum(1 for vm in vms if vm.ip),
        agents_live=sum(1 for vm in vms if vm.agent_state == "live"),
        agents_total=sum(1 for vm in vms if vm.agent_state != "none"),
        flagged=sum(1 for vm in vms if vm.signals),
        created_at=min(created) if created else None,
        updated_at=max(updated) if updated else None,
    )
