import type { CompiledExecutionGroup } from "@/lib/api"

/**
 * Pure staging helpers — no React, no store imports. Mirrors `lib/topology.ts`.
 *
 * A `StagedOp` is one pending action (create a VM, join a domain, wire up a
 * CA hierarchy edge, ...) queued ahead of a deploy. `stageOp` (in
 * `store/staging.ts`) is the sole insertion point and always appends, so the
 * list is already a valid topological order: an op only depends on ops
 * earlier in the list. That invariant is what makes plain pop-undo safe
 * (the last op never has dependents) and lets `transitiveDependents` do a
 * simple forward scan instead of a general graph walk.
 */

export const OP_KIND = {
  createVm: "createVm",
  /**
   * Backend-synthesized companion of a createVm (agent phone-home, boot
   * settle, role install). Never staged or POSTed by the client — rows of
   * this kind are read-only mirrors inserted when a plan-state frame first
   * mentions the op (see `applyPlanState`).
   */
  provision: "provision",
  domainJoin: "domainJoin",
  domainLeave: "domainLeave",
  caConnect: "caConnect",
  webServerCert: "webServerCert",
} as const

export type OpKind = (typeof OP_KIND)[keyof typeof OP_KIND]

export const OP_STATUS = {
  staged: "staged",
  pending: "pending",
  running: "running",
  done: "done",
  error: "error",
  cancelled: "cancelled",
} as const

export type OpStatus = (typeof OP_STATUS)[keyof typeof OP_STATUS]

export interface StagedOp extends Record<string, unknown> {
  id: string
  kind: OpKind
  /** Drives label + lifecycle updates — the node this op ultimately mutates. */
  targetNodeId: string
  /** DC / parent CA / web server, when the op involves a second node. */
  secondaryNodeId?: string
  params: Record<string, string>
  /** Op ids this depends on — always earlier in the list. */
  dependsOn: string[]
  label: string
  status: OpStatus
  /** The optimistic edge this op created, if any — lets undo revert it exactly. */
  edgeId?: string
  progress?: number
  /** Live phase label while running (e.g. "Step 1/3 · install-forest"). */
  phase?: string
  /** Error detail after a failed deploy. */
  detail?: string
  /** Backend traceback for unexpected failures — collapsible technical detail. */
  trace?: string
  /** Backend-authored child execution states, keyed by compiled step id. */
  executionSteps?: Record<
    string,
    {
      status: OpStatus
      percent?: number
      phase?: string
      detail?: string
    }
  >
  /**
   * Compiler-authored labels/commands for this operation's expandable step
   * tree. Cached on the op so a deployment remount can combine it with the
   * persisted/runtime `executionSteps` state without recompiling a topology
   * whose resources may already have become realized.
   */
  executionGroup?: CompiledExecutionGroup
  /**
   * True for read-only rows mirroring backend-synthesized provision ops.
   * Excluded from the deploy payload, hidden from removal controls, and
   * dropped/retained in lockstep with their parent createVm row.
   */
  synthesized?: boolean
}

/** Id suffix of backend-synthesized provision ops: `{createVmOpId}::provision`. */
export const PROVISION_SUFFIX = "::provision"

export function isProvisionOpId(opId: string): boolean {
  return opId.endsWith(PROVISION_SUFFIX)
}

/** The createVm op id a synthesized provision op id derives from. */
export function provisionParentId(opId: string): string {
  return isProvisionOpId(opId) ? opId.slice(0, -PROVISION_SUFFIX.length) : opId
}

/** Ops (anywhere in the list) that transitively depend on `opId`, in list order. */
export function transitiveDependents(
  opId: string,
  ops: StagedOp[],
): StagedOp[] {
  const seen = new Set<string>([opId])
  const result: StagedOp[] = []
  // Single forward pass suffices because dependsOn always points earlier in
  // the list — by the time we reach a dependent, its dependency is already
  // in `seen`.
  for (const op of ops) {
    if (seen.has(op.id)) continue
    if (op.dependsOn.some((dep) => seen.has(dep))) {
      seen.add(op.id)
      result.push(op)
    }
  }
  return result
}

/**
 * Validates the append-only-topological-order invariant `stageOp` guarantees
 * at runtime — a persisted list (localStorage) might predate a schema change
 * or otherwise have drifted. Drops any op whose `dependsOn` references an id
 * that isn't among the ops already kept (earlier in the list), which also
 * transitively drops anything that in turn depended on a dropped op.
 */
export function sanitizeOps(ops: StagedOp[]): StagedOp[] {
  const kept: StagedOp[] = []
  const keptIds = new Set<string>()
  for (const op of ops) {
    if (op.dependsOn.every((dep) => keptIds.has(dep))) {
      kept.push(op)
      keptIds.add(op.id)
    }
  }
  return kept
}

/** Human noun per realization op kind, for blocked-node messaging. */
const REALIZATION_LABELS: Partial<Record<OpKind, string>> = {
  [OP_KIND.domainJoin]: "domain join",
  [OP_KIND.domainLeave]: "domain leave",
  [OP_KIND.caConnect]: "CA connection",
  [OP_KIND.webServerCert]: "web server certificate",
}

/**
 * The relationship ops that must succeed before `nodeId` counts as deployed,
 * beyond its own clone + provision. After the clone/provision split the
 * synthesized provision op is only agent phone-home + boot settle for some
 * roles — the actual role standup lives in these ops (an issuing CA is stood
 * up by its caConnect, a web host by its domainJoin + webServerCert).
 * webServerCert targets the issuing CA but realizes its `secondary` web host;
 * a CA is never gated by its consumers.
 */
export function nodeRealizationOps(
  ops: StagedOp[],
  nodeId: string,
): StagedOp[] {
  return ops.filter(
    (op) =>
      ((op.kind === OP_KIND.domainJoin ||
        op.kind === OP_KIND.domainLeave ||
        op.kind === OP_KIND.caConnect) &&
        op.targetNodeId === nodeId) ||
      (op.kind === OP_KIND.webServerCert && op.secondaryNodeId === nodeId),
  )
}

/**
 * True while a plan op touching `nodeId` is queued with the runner or running —
 * this machine is being changed right now.
 *
 * Deliberately per-node rather than the store's global `deploying`: a plan is
 * one lock but many machines, and a guest whose own steps finished twenty
 * minutes ago is not busy because a CA elsewhere in the DAG still is. That is
 * what the remote-desktop button reads, so a lab can be walked into as it
 * finishes instead of only after the whole plan lands.
 *
 * `staged` is not in flight — nothing has been sent, so the guest is untouched
 * — and neither is any terminal status: a step that has finished, however it
 * finished, will not reach into this VM again.
 */
export function nodeOpsInFlight(ops: StagedOp[], nodeId: string): boolean {
  return ops.some(
    (op) =>
      (op.targetNodeId === nodeId || op.secondaryNodeId === nodeId) &&
      (op.status === OP_STATUS.pending || op.status === OP_STATUS.running),
  )
}

/**
 * The ops a Deploy would actually send. `done` rows retained after a failed
 * run are history — they stay listed for context, but they are never counted,
 * re-sent, or undone.
 */
export function actionableOps(ops: StagedOp[]): StagedOp[] {
  return ops.filter((op) => op.status !== OP_STATUS.done)
}

/**
 * The node an op's label names — the one it ultimately configures. Same
 * inversion `nodeRealizationOps` encodes: a `webServerCert` op *targets* the
 * issuing CA (that's whose certificate service it draws on) but realizes its
 * `secondary` web host, which is what the compiled label says it configures.
 */
export function opDestinationNodeId(op: StagedOp): string {
  return op.kind === OP_KIND.webServerCert
    ? (op.secondaryNodeId ?? op.targetNodeId)
    : op.targetNodeId
}

/**
 * True while `nodeId` still has plan work in flight (or terminally failed/
 * cancelled) that gates its `deployed` promotion — its synthesized provision
 * op plus every realization op. Agent presence alone must never promote such
 * a node (`useAgentPromotion`): the boot-settled VM may still be waiting on —
 * or blocked from — the ops that install its actual role.
 */
export function nodeAwaitingRealization(
  ops: StagedOp[],
  nodeId: string,
): boolean {
  const related = [
    ...ops.filter(
      (op) => op.kind === OP_KIND.provision && op.targetNodeId === nodeId,
    ),
    ...nodeRealizationOps(ops, nodeId),
  ]
  return related.some(
    (op) => op.status !== OP_STATUS.done && op.status !== OP_STATUS.staged,
  )
}

/** "Blocked: …" node detail when upstream failures cancelled realization ops. */
export function blockedRealizationDetail(cancelled: StagedOp[]): string {
  const kinds = [
    ...new Set(cancelled.map((op) => REALIZATION_LABELS[op.kind] ?? op.kind)),
  ]
  return `Blocked: ${kinds.join(", ")} cancelled because an upstream dependency failed.`
}

/**
 * The *outstanding* op of `kind` targeting `nodeId`, if one is still in the
 * list. `done` rows are excluded on purpose: callers use this to decide
 * "is this still just a staged intention?" — a retained `done` domain join is
 * a real membership, and treating it as undoable would cascade away a
 * relationship that exists and skip the explicit leave it needs.
 */
export function findStagedOp(
  ops: StagedOp[],
  kind: OpKind,
  nodeId: string,
): StagedOp | undefined {
  return actionableOps(ops).find(
    (op) => op.kind === kind && op.targetNodeId === nodeId,
  )
}

/**
 * Dependencies for a new op of `kind`, restricted to prerequisites that are
 * still staged (anything already deployed doesn't need a dependency edge —
 * it already exists).
 */
export function inferDependsOn(
  kind: OpKind,
  targetNodeId: string,
  secondaryNodeId: string | undefined,
  ops: StagedOp[],
): string[] {
  const createVmIds = (...nodeIds: (string | undefined)[]) =>
    nodeIds
      .filter((id): id is string => !!id)
      .map((id) => findStagedOp(ops, OP_KIND.createVm, id))
      .filter((op): op is StagedOp => !!op)
      .map((op) => op.id)

  switch (kind) {
    case OP_KIND.createVm:
      return []
    case OP_KIND.provision:
      // Synthesized rows never enter through stageOp; nothing to infer.
      return []
    case OP_KIND.domainJoin:
    case OP_KIND.caConnect:
      return createVmIds(targetNodeId, secondaryNodeId)
    case OP_KIND.domainLeave:
      // Membership being left is deployed by definition — nothing staged to wait on.
      return []
    case OP_KIND.webServerCert: {
      const caConnectIds = ops
        .filter(
          (op) =>
            op.kind === OP_KIND.caConnect && op.targetNodeId === targetNodeId,
        )
        .map((op) => op.id)
      return [...createVmIds(targetNodeId, secondaryNodeId), ...caConnectIds]
    }
  }
}
