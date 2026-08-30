"""Declarative step model for reboot-spanning provisioning sequences.

A plan op (``createVm``, ``domainJoin``, ``caConnect``, …) expands backend-side
into an ordered list of :class:`Step`\\ s. Each step is one agent command
dispatched at a resolved *target* node, optionally followed by a reboot wait
and a verify probe. Steps are pure data — no I/O — so the sequence library is
unit-testable and the engine (:mod:`app.core.sequences.engine`) owns every
side effect.

Cross-node resolution: a step names its target by a **context key** (usually a
canvas node id, but role aliases like ``"root"`` / ``"dc"`` are just keys the
op expansion populates in :class:`RunContext.nodes`). Params are either a
static mapping or a callable resolved against the live context at run time, so
a step on CA02 can reference DC01's real guest-namespaced hostname.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NodeContext:
    """Everything a step needs to know about one node in the plan."""

    node_id: str
    vm_name: str
    hostname: str
    agent_vm_id: str | None = None
    ip: str | None = None
    template_id: str | None = None
    template_config: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DnsRecordContext:
    """One symbolic topology DNS resource available to sequence resolvers."""

    id: str
    kind: str
    server: str
    subject: str
    zone: str
    name: str | None = None


@dataclass
class RunContext:
    """Cross-VM state threaded through a sequence: the resolved nodes, the
    shared domain facts, and the artifact relay map (key → base64 payload).

    ``artifacts`` doubles as the persisted relay store — a step's ``produces``
    lifts ``result.contentB64`` into it, and a later step's ``consumes`` reads
    it back as a param, which is the cross-sign sneakernet path.
    """

    nodes: dict[str, NodeContext]
    domain_name: str | None = None
    netbios: str | None = None
    pki_host: str | None = None
    dns_records: tuple[DnsRecordContext, ...] = ()
    artifacts: dict[str, str] = field(default_factory=dict)

    def node(self, key: str) -> NodeContext:
        try:
            return self.nodes[key]
        except KeyError as exc:
            raise KeyError(f"sequence references unknown node '{key}'") from exc


# A param resolver runs at dispatch time with the full context + the resolved
# target node, returning the flat str→str param map for the command.
ParamResolver = Callable[["StepRuntime"], dict[str, str]]


@dataclass(frozen=True)
class StepRuntime:
    """What a :class:`Step`'s param resolver (and verify predicate) sees."""

    ctx: RunContext
    node: NodeContext


# A verify predicate inspects a probe command's result dict and decides whether
# the target has reached the desired state yet (True = ready, stop retrying).
VerifyPredicate = Callable[[dict[str, Any]], bool]

# A local aggregate consumes the results of earlier steps without dispatching
# another agent command.  This is how a final health gate can combine facts
# gathered from several VMs while keeping each remote probe single-purpose.
ResultAggregator = Callable[
    ["StepRuntime", Mapping[str, dict[str, Any]]], dict[str, Any]
]


@dataclass(frozen=True)
class Step:
    """One agent command in a sequence.

    ``target`` is a context key (see module docstring). ``expects_disconnect``
    marks a reboot step whose success looks like a dropped socket — the engine
    then waits for the agent to phone home again before continuing. ``verify``
    is a read-only probe re-dispatched with backoff (inside ``verify_window_s``)
    until ``verify_predicate`` accepts its result.
    """

    id: str
    command: str
    target: str
    params: ParamResolver | Mapping[str, str] = field(default_factory=dict)
    #: Reboot step — the engine waits for reconnect after dispatching it.
    expects_disconnect: bool = False
    #: Read-only readiness probe run after this step (with retry backoff).
    verify: "Step | None" = None
    verify_predicate: VerifyPredicate | None = None
    verify_window_s: int = 600
    #: Re-dispatch *this* step until its **own** result satisfies this, keeping
    #: the last result either way.
    #:
    #: Distinct from ``verify`` in the one way that matters: a ``verify`` window
    #: closing raises and fails the op, while a ``settle`` window closing is not
    #: an error at all — it simply means the condition never settled, and the
    #: last result stands for a downstream advisory gate to interpret. That is
    #: the difference between "this lab is broken" and "this lab shipped with a
    #: warning", and it is why an advisory fact (OCSP, which a lab revoking over
    #: CRL can live without) can be waited for without a slow responder being
    #: able to strand a deployment.
    #:
    #: The step is re-dispatched verbatim, so only ever set this on a step that
    #: is safe to run repeatedly — in practice a read-only probe.
    settle_predicate: VerifyPredicate | None = None
    settle_window_s: int = 300
    #: Backend-local result aggregator. When present, ``command`` is the
    #: display/metric label only and no agent dispatch occurs.
    aggregate: ResultAggregator | None = None
    #: Artifact keys: ``produces`` lifts ``result.contentB64`` into the relay
    #: map; ``consumes`` injects one relay payload as the ``contentB64`` param.
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    #: Result field -> persisted artifact key. Used for observed filenames and
    #: other small facts that later operations must consume verbatim.
    result_artifacts: Mapping[str, str] = field(default_factory=dict)
    #: Optional result field -> compatibility value resolver. Only consulted
    #: when an older agent omits a field named by ``result_artifacts``.
    result_artifact_defaults: ParamResolver | Mapping[str, str] = field(
        default_factory=dict
    )
    #: Best-effort result field -> artifact key mappings used for richer new
    #: agent results while retaining compatibility with older agents.
    optional_result_artifacts: Mapping[str, str] = field(default_factory=dict)
    #: Skip this step when all named artifacts were already produced upstream.
    skip_if_artifacts: tuple[str, ...] = ()
    #: Param keys to redact from every progress/error frame (passwords, blobs).
    secret_keys: tuple[str, ...] = ()
    timeout_s: int = 300
    #: Delays before bounded redispatch attempts for transient service failures.
    #: Empty means one attempt. Mutating commands using this must be convergent.
    retry_delays_s: tuple[int, ...] = ()
    #: Case-insensitive failure-detail substrings meaning "the target has a
    #: reboot pending". Matching once buys a reboot plus one redispatch of this
    #: same command, so the command must be convergent across a restart.
    reboot_recovery_signatures: tuple[str, ...] = ()

    def resolve_params(self, ctx: RunContext) -> dict[str, str]:
        node = ctx.node(self.target)
        if callable(self.params):
            params = dict(self.params(StepRuntime(ctx=ctx, node=node)))
        else:
            params = dict(self.params)
        # A consuming step pulls its payload from the relay map.
        for key in self.consumes:
            if key in ctx.artifacts:
                params["contentB64"] = ctx.artifacts[key]
        return params

    def resolve_result_artifact_defaults(self, ctx: RunContext) -> dict[str, str]:
        node = ctx.node(self.target)
        if callable(self.result_artifact_defaults):
            return dict(self.result_artifact_defaults(StepRuntime(ctx=ctx, node=node)))
        return dict(self.result_artifact_defaults)
