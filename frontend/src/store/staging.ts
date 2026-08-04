/**
 * Staging store — the linear, undo-able list of operations queued ahead of a
 * deploy. Each op mirrors an optimistic canvas effect that `store/topology.ts`
 * already applied (a node flipped to `staged`, a ghost edge was drawn);
 * undoing/cascading an op here reverts exactly that effect via `revertOp`.
 *
 * Persisted through the active Project snapshot (`store/projects.ts` /
 * `lib/projectAutosave.ts`), not zustand's own `persist` middleware — staged
 * ops reference node/edge ids and must swap in lockstep with the topology
 * graph on project switch, so `loadOps` is called alongside `loadSnapshot`
 * rather than rehydrating independently from localStorage.
 *
 * `stageOp` is the sole insertion point and always appends, so the list is
 * already a valid topological order (see `lib/staging.ts`) — that's what
 * makes `undo`'s plain pop safe.
 */

import { create } from "zustand"
import { toast } from "sonner"

import {
  CONNECTION_HEALTH,
  EDGE_TYPE,
  LIFECYCLE,
  SERVICE_SOCKET,
} from "@/constants/topology"
import {
  ApiError,
  type CompiledExecutionGroup,
  deployPlan,
  type DeployPreflightReceipt,
  getDeployRun,
  type PlanOpPayload,
} from "@/lib/api"
import {
  OP_KIND,
  OP_STATUS,
  actionableOps,
  blockedRealizationDetail,
  inferDependsOn,
  isProvisionOpId,
  nodeRealizationOps,
  provisionParentId,
  sanitizeOps,
  transitiveDependents,
  type OpKind,
  type StagedOp,
} from "@/lib/staging"
import { connectionHealthForOperation, domainJoinEdge } from "@/lib/topology"
import { buildDeployTopology } from "@/lib/deployTopology"
import { isCertificateJourney } from "@/lib/certificateJourney"
import { createLabEvidence, isLabHealthReport } from "@/lib/labEvidence"
import {
  openJobSocket,
  type OpRunState,
  WS_CLOSE_JOB_GONE,
  WS_CLOSE_UNAUTHORIZED,
} from "@/lib/ws"
import { ROLES } from "@/constants"
import { useAuthStore } from "@/store/auth"
import { useProjectsStore } from "@/store/projects"
import { saveActiveProjectNow } from "@/lib/projectSync"
import { useTopologyStore } from "@/store/topology"

/** Reverts one op's optimistic canvas effect — the inverse of whatever `store/topology.ts` applied when it was staged. */
function revertOp(op: StagedOp) {
  const topology = useTopologyStore.getState()
  switch (op.kind) {
    case OP_KIND.createVm:
      // Config is kept so the config form re-opens pre-filled.
      topology.patchNodeData(op.targetNodeId, { lifecycle: LIFECYCLE.draft })
      return
    case OP_KIND.provision:
      // Synthesized display row — no optimistic canvas effect to unwind.
      return
    case OP_KIND.domainJoin:
      if (op.edgeId) topology.removeEdge(op.edgeId)
      // Retargeting away from a *deployed* membership carries the old DC id
      // so undoing the join restores exactly the edge it replaced.
      if (op.params.prevDcId) {
        topology.restoreEdge(
          domainJoinEdge(op.targetNodeId, op.params.prevDcId),
        )
      }
      return
    case OP_KIND.caConnect:
      for (const edgeId of operationEdgeIds(op)) topology.removeEdge(edgeId)
      return
    case OP_KIND.webServerCert:
      for (const edgeId of operationEdgeIds(op)) topology.removeEdge(edgeId)
      return
    case OP_KIND.domainLeave:
      if (op.params.prevDcId) {
        topology.restoreEdge(
          domainJoinEdge(op.targetNodeId, op.params.prevDcId),
        )
      }
      return
  }
}

function operationEdgeIds(op: StagedOp): string[] {
  if (op.kind === OP_KIND.caConnect && op.secondaryNodeId) {
    return useTopologyStore
      .getState()
      .edges.filter(
        (edge) =>
          edge.id === op.edgeId ||
          (edge.data?.edgeType === EDGE_TYPE.webServerCert &&
            edge.data?.serviceSocket === SERVICE_SOCKET.publication &&
            edge.source === op.secondaryNodeId),
      )
      .map((edge) => edge.id)
  }
  if (op.kind !== OP_KIND.webServerCert || !op.secondaryNodeId) {
    return op.edgeId ? [op.edgeId] : []
  }
  return useTopologyStore
    .getState()
    .edges.filter(
      (edge) =>
        edge.data?.edgeType === EDGE_TYPE.webServerCert &&
        edge.source === op.targetNodeId &&
        edge.target === op.secondaryNodeId,
    )
    .map((edge) => edge.id)
}

interface StageOpInput {
  kind: OpKind
  targetNodeId: string
  secondaryNodeId?: string
  params: Record<string, string>
  label: string
  edgeId?: string
}

/**
 * Where an in-flight plan is before per-op progress exists: `posting` while
 * the deploy POST (route-side compile + preflight) is in flight, `queued`
 * once accepted but not yet picked up by a worker, `preparing` while the
 * worker runs its setup (connection, preflight re-verify), and `executing`
 * from the first plan-state frame on. Null when no plan is in flight.
 */
export type PlanPhase = "posting" | "queued" | "preparing" | "executing"

/** True while the plan has produced no per-op progress yet. */
export function isPreExecutionPhase(phase: PlanPhase | null): boolean {
  return phase === "posting" || phase === "queued" || phase === "preparing"
}

interface StagingState {
  ops: StagedOp[]
  deployJobId: string | null
  deploying: boolean
  /** Pre-execution position of the in-flight plan; null when idle or unknown (e.g. resumed jobs until the stream says otherwise). */
  planPhase: PlanPhase | null
  /** Worker setup breadcrumb (`planSetup` progress frames) shown verbatim during `preparing`. */
  planPhaseDetail: string | null
  /** Epoch ms the run started, for the elapsed-time escalation in the panel — the Deploy click locally, the run document's `createdAt` on a resumed job (`rehydrateRunFacts`). Outlives the run so the total can be reported. */
  deployStartedAt: number | null
  /** Epoch ms the run reached a terminal state; null while one is in flight or idle. Paired with `deployStartedAt` it is the run's total duration, which used to vanish the instant it became interesting. */
  deployFinishedAt: number | null
  /** What the last deploy attempt verified (202) or tripped over (409) — rendered as the receipt checklist. Survives both the plan and a reload, so the wait it explains reads as deliberate. */
  preflightReceipt: DeployPreflightReceipt | null

  stageOp: (input: StageOpInput) => StagedOp
  /** Pops the last op and reverts it. No-op while deploying or when empty — the last op never has dependents, so this is always safe. */
  undo: () => void
  /** Reverts and removes `opId` plus everything that transitively depends on it. */
  removeOpCascade: (opId: string) => void
  setOpState: (opId: string, patch: Partial<StagedOp>) => void
  /** Cache the reviewed compiler manifest and pre-materialize its read-only provision rows so expanded steps survive a reload. */
  cacheExecutionGroups: (groups: CompiledExecutionGroup[]) => void
  /** Swaps in another project's op list — called alongside `loadSnapshot` on project switch. Resumes an in-flight plan job, if any. */
  loadOps: (ops: StagedOp[], deployJobId: string | null) => void
  /** Sends the current ops to the backend plan runner and attaches the plan-progress socket. */
  deploy: () => void
  /** Re-attaches to an in-flight plan job's WebSocket after a reload/project switch, and refetches the run facts the socket doesn't carry. */
  resumePlanJob: () => void
}

export interface PreparedDeployPlan {
  ops: StagedOp[]
  payload: PlanOpPayload[]
  topology: ReturnType<typeof buildDeployTopology>
  projectId: string | null
}

/**
 * Builds one op's wire params (and, for authored createVm ops, the inline
 * file list). `createVm` params are never persisted on the op itself —
 * `node.data.config` / `node.data.isoAuthoring` (the drift baselines) are the
 * single source of truth, so this reads them fresh at deploy time alongside
 * the deploy-time VM name and the template id. `vmName` is sent as the plain
 * canvas node name — the backend namespaces guest names server-side from the
 * authenticated identity (`enforce_guest_vm_name`), so the client must never
 * prefix it itself (a client-side prefix just gets prefixed again). Every
 * createVm is a real clone — the backend allowlists `template`
 * and decides for itself; there is no client `simulate` flag anymore. An
 * enabled ISO panel rides as either name-sorted inline `files` (PACK —
 * matching the 10-/20-/30- manifest order convention) or an `isoId` param
 * (UPLOAD-ISO). Only UPLOAD-ISO makes the backend inject nothing and allocate
 * no pool IP; PACK files are layered over the rendered disc.
 *
 * `files` are withheld from a non-operator: `ISO_AUTHOR` is operator-only, so
 * the backend 403s the whole plan on any inline content. That is reachable
 * without the panel — a share-linked project can carry `isoAuthoring` seeded by
 * the operator who owns it — and the seeded default is exactly what the server
 * would render anyway, so dropping it costs nothing.
 */
function buildOpPayload(op: StagedOp): Pick<PlanOpPayload, "params" | "files"> {
  if (op.kind !== OP_KIND.createVm) return { params: op.params }
  const node = useTopologyStore
    .getState()
    .nodes.find((n) => n.id === op.targetNodeId)
  const params: Record<string, string> = {
    ...(node?.data.config ?? {}),
    vmName: node?.data.name ?? op.targetNodeId,
    template: node?.data.typeId ?? "",
  }

  const iso = node?.data.isoAuthoring
  if (!iso?.enabled) return { params }
  if (iso.mode === "uploadIso" && iso.isoId) {
    params.isoId = iso.isoId
    return { params }
  }
  if (
    iso.mode === "pack" &&
    iso.files.length > 0 &&
    useAuthStore.getState().role === ROLES.operator
  ) {
    const files = [...iso.files]
      .sort((a, b) => a.name.localeCompare(b.name))
      .map(({ name, content }) => ({ name, content }))
    return { params, files }
  }
  return { params }
}

/**
 * Nodes whose ISO panel is in UPLOAD-ISO mode with nothing uploaded — deploying
 * one would silently fall back to the server-rendered disc, the one thing that
 * mode says won't happen. Deploy refuses until an ISO is attached or the mode
 * changes. An empty PACK grid is *not* an error: those files only override the
 * rendered set, so none of them simply means "render all of it".
 */
function emptyIsoNodes(ops: StagedOp[]): string[] {
  const nodes = useTopologyStore.getState().nodes
  const names: string[] = []
  for (const op of ops) {
    if (op.kind !== OP_KIND.createVm) continue
    const node = nodes.find((n) => n.id === op.targetNodeId)
    const iso = node?.data.isoAuthoring
    if (!iso?.enabled) continue
    if (iso.mode === "uploadIso" && !iso.isoId) {
      names.push(node?.data.name ?? op.targetNodeId)
    }
  }
  return names
}

/** Build the exact retry-pruned request used by both compiler review and deploy. */
export function prepareDeployPlan(
  ops = useStagingStore.getState().ops,
): PreparedDeployPlan {
  const resettable = ops
    .filter((op) => op.status !== OP_STATUS.done)
    .map((op) =>
      op.status === OP_STATUS.error || op.status === OP_STATUS.cancelled
        ? {
            ...op,
            status: OP_STATUS.staged,
            progress: undefined,
            detail: undefined,
          }
        : op,
    )
  const resettableIds = new Set(resettable.map((op) => op.id))
  // Synthetic provision rows only make sense under a createVm being (re)sent —
  // a dropped `done` parent takes its display row with it (the backend
  // re-synthesizes the op, and this list re-materializes the row, on deploy).
  const kept = resettable.filter(
    (op) => !op.synthesized || resettableIds.has(provisionParentId(op.id)),
  )
  const keptIds = new Set(kept.map((op) => op.id))
  const pruned = kept.map((op) => ({
    ...op,
    dependsOn: op.dependsOn.filter((dep) => keptIds.has(dep)),
  }))
  // Synthetic rows are display-only and never POSTed — the backend strips
  // client-supplied provision ops anyway; this is defense in depth.
  const payload: PlanOpPayload[] = pruned
    .filter((op) => !op.synthesized)
    .map((op) => {
      const { params, files } = buildOpPayload(op)
      return {
        id: op.id,
        kind: op.kind,
        target: op.targetNodeId,
        ...(op.secondaryNodeId ? { secondary: op.secondaryNodeId } : {}),
        params,
        ...(files ? { files } : {}),
        dependsOn: op.dependsOn,
      }
    })
  const topologyState = useTopologyStore.getState()
  return {
    ops: pruned,
    payload,
    topology: buildDeployTopology(topologyState.nodes, topologyState.edges),
    projectId: useProjectsStore.getState().activeProjectId,
  }
}

/** Display label for a synthesized provision row, derived from the parent createVm's node. */
function provisionRowLabel(parent: StagedOp): string {
  const node = useTopologyStore
    .getState()
    .nodes.find((n) => n.id === parent.targetNodeId)
  const name = node?.data.name ?? parent.targetNodeId
  const typeId = node?.data.typeId
  const detail =
    typeId === "domainController"
      ? "AD DS forest"
      : typeId === "certificateAuthority" &&
          node?.data.config?.caType === "Root"
        ? "Root CA setup"
        : "Boot & settle"
  return `Provision ${name} — ${detail}`
}

/**
 * Materializes read-only rows for backend-synthesized provision ops
 * (`{createVmOpId}::provision`) the first time a plan-state frame mentions
 * them, inserted directly after their parent createVm row. Idempotent —
 * keyed by op id, and the deterministic backend ids mean a retry reuses the
 * same rows.
 */
function ensureSyntheticRows(opsState: Record<string, OpRunState>): void {
  const ops = useStagingStore.getState().ops
  const known = new Set(ops.map((o) => o.id))
  const insertsByParent = new Map<string, StagedOp>()
  for (const opId of Object.keys(opsState)) {
    if (!isProvisionOpId(opId) || known.has(opId)) continue
    const parent = ops.find(
      (o) => o.id === provisionParentId(opId) && o.kind === OP_KIND.createVm,
    )
    if (!parent) continue
    insertsByParent.set(parent.id, {
      id: opId,
      kind: OP_KIND.provision,
      targetNodeId: parent.targetNodeId,
      params: {},
      dependsOn: [parent.id],
      label: provisionRowLabel(parent),
      status: OP_STATUS.pending,
      synthesized: true,
    })
  }
  if (insertsByParent.size === 0) return
  useStagingStore.setState((s) => ({
    ops: s.ops.flatMap((op) => {
      const child = insertsByParent.get(op.id)
      return child ? [op, child] : [op]
    }),
  }))
}

/** Folds one `plan-state` snapshot into the staging list and mirrors createVm/edge transitions onto the canvas. Idempotent — safe to apply the same snapshot more than once (reconnects/replays). Exported for tests. */
export function applyPlanState(
  opsState: Record<string, OpRunState>,
  deploymentJobId?: string,
) {
  ensureSyntheticRows(opsState)
  const { ops, setOpState } = useStagingStore.getState()
  const topology = useTopologyStore.getState()

  // Provision ops own their node's final lifecycle transition — process them
  // after every other op so a whole-state snapshot carrying both a done
  // createVm and its terminal provision sibling lands on the sibling's word.
  const entries = Object.entries(opsState).sort(
    ([a], [b]) => Number(isProvisionOpId(a)) - Number(isProvisionOpId(b)),
  )
  for (const [opId, runState] of entries) {
    const op = ops.find((o) => o.id === opId)
    if (!op) continue

    setOpState(opId, {
      status:
        runState.status === "queued" ? OP_STATUS.pending : runState.status,
      progress: runState.percent,
      phase: runState.status === "running" ? runState.phase : undefined,
      detail: runState.detail,
      trace: runState.trace,
      executionSteps: runState.steps
        ? Object.fromEntries(
            Object.entries(runState.steps).map(([id, step]) => [
              id,
              {
                ...step,
                status:
                  step.status === "queued" ? OP_STATUS.pending : step.status,
              },
            ]),
          )
        : undefined,
    })

    if (op.kind === OP_KIND.provision) {
      // The synthesized provision op owns the node's final transition — the
      // clone op parks the node in `provisioning`; this branch takes it to
      // `deployed` (or `failed`, keeping the clone's vmName/ip so the
      // Tear down VM affordance stays available over the surviving VM).
      if (runState.status === "running") {
        topology.patchNodeData(op.targetNodeId, {
          lifecycle: LIFECYCLE.deploying,
          progress: runState.percent,
          phase: runState.phase,
          errorDetail: undefined,
          // The agent identity rides on running pushes (partial result) so the
          // presence dot can appear while provisioning is still underway.
          ...(typeof runState.result?.agentVmId === "string"
            ? { executorVmId: runState.result.agentVmId }
            : {}),
        })
      } else if (runState.status === "done") {
        const result = runState.result
        // Conditional spreads so a result-less replay of an older snapshot
        // can never clobber an already-recorded identity with undefined.
        const identity = {
          poweredOn: true,
          ...(typeof result?.ip === "string" ? { ip: result.ip } : {}),
          ...(typeof result?.vmName === "string"
            ? { vmName: result.vmName }
            : {}),
          ...(typeof result?.agentVmId === "string"
            ? { executorVmId: result.agentVmId }
            : {}),
        }
        // Boot-settle alone doesn't make the node functional — its role
        // standup lives in the relationship ops (caConnect/domainJoin/
        // webServerCert). Reading statuses straight from the whole-state
        // snapshot keeps this fold idempotent: every frame re-decides the
        // lifecycle from scratch.
        const realization = nodeRealizationOps(ops, op.targetNodeId)
        const erroredOp = realization.find(
          (r) => opsState[r.id]?.status === "error",
        )
        const cancelledOps = realization.filter(
          (r) => opsState[r.id]?.status === "cancelled",
        )
        if (erroredOp) {
          topology.patchNodeData(op.targetNodeId, {
            lifecycle: LIFECYCLE.failed,
            progress: undefined,
            phase: undefined,
            errorDetail: opsState[erroredOp.id]?.detail || "Deployment failed",
            ...identity,
          })
        } else if (cancelledOps.length > 0) {
          // The node's own VM is fine, but an upstream failure cancelled the
          // ops that would make it a functioning PKI member — surface that as
          // `failed` rather than a lying green badge.
          topology.patchNodeData(op.targetNodeId, {
            lifecycle: LIFECYCLE.failed,
            progress: undefined,
            phase: undefined,
            errorDetail: blockedRealizationDetail(cancelledOps),
            ...identity,
          })
        } else if (
          realization.every((r) => opsState[r.id]?.status === "done")
        ) {
          topology.patchNodeData(op.targetNodeId, {
            lifecycle: LIFECYCLE.deployed,
            progress: 100,
            phase: undefined,
            ...identity,
          })
        } else {
          // Realization ops still pending/running — VM up, role not yet
          // installed. Hold `provisioning`; a later snapshot finishes the job.
          topology.patchNodeData(op.targetNodeId, {
            lifecycle: LIFECYCLE.provisioning,
            progress: 100,
            phase: undefined,
            ...identity,
          })
        }
      } else if (runState.status === "error") {
        topology.patchNodeData(op.targetNodeId, {
          lifecycle: LIFECYCLE.failed,
          progress: undefined,
          phase: undefined,
          errorDetail: runState.detail || "Provisioning failed",
        })
      }
      // `cancelled` (its clone failed) patches nothing — the createVm error
      // branch already marked the node failed with the clone's detail.
      continue
    }

    if (op.kind === OP_KIND.createVm) {
      if (runState.status === "running") {
        topology.patchNodeData(op.targetNodeId, {
          lifecycle: LIFECYCLE.deploying,
          progress: runState.percent,
          phase: runState.phase,
          errorDetail: undefined,
          ...(typeof runState.result?.agentVmId === "string"
            ? { executorVmId: runState.result.agentVmId }
            : {}),
        })
      } else if (runState.status === "done") {
        const node = topology.nodes.find((n) => n.id === op.targetNodeId)
        const result = runState.result
        const agentVmId =
          typeof result?.agentVmId === "string" ? result.agentVmId : undefined
        // The clone is done, but the node isn't *confirmed* deployed until
        // its synthesized provision sibling finishes — park it in
        // `provisioning` (dashed circle, IP hidden) and let the provision
        // branch above own the final transition. Snapshot entries are
        // processed provision-last, so a frame that already carries the
        // sibling's terminal state still lands correctly. `useAgentPromotion`
        // remains a harmless backstop for agent-online promotion.
        topology.patchNodeData(op.targetNodeId, {
          lifecycle: LIFECYCLE.provisioning,
          poweredOn: true,
          lastDeployedConfig: node?.data.config,
          // ISO drift baseline, mirroring lastDeployedConfig. Safe to hold by
          // reference — setIsoAuthoring always builds a fresh object.
          lastDeployedIso: node?.data.isoAuthoring,
          progress: 100,
          phase: undefined,
          // Conditional spreads so a result-less replay of an older snapshot
          // can never clobber an already-recorded identity with undefined.
          ...(typeof result?.ip === "string" ? { ip: result.ip } : {}),
          ...(typeof result?.vmName === "string"
            ? { vmName: result.vmName }
            : {}),
          // Auto-provisioned executor identity: the agent baked
          // into the ISO phones home under this vm_id; surfaces in the Inspector.
          ...(agentVmId !== undefined ? { executorVmId: agentVmId } : {}),
        })
      } else if (runState.status === "error") {
        topology.patchNodeData(op.targetNodeId, {
          lifecycle: LIFECYCLE.failed,
          progress: undefined,
          phase: undefined,
          // The online dot only reports agent presence; retain the terminal
          // command detail separately so project serialization does not strip
          // it with transient progress/phase state.
          errorDetail: runState.detail || "Deployment failed",
        })
      }
      continue
    }

    // Edge ops (domainJoin/caConnect/webServerCert) — clear ghost styling once
    // deployed. domainLeave has no edgeId; there's nothing left to commit.
    for (const edgeId of operationEdgeIds(op)) {
      topology.setEdgeHealth(
        edgeId,
        connectionHealthForOperation(runState.status),
      )
      if (runState.status === "done") topology.commitEdge(edgeId)
    }
    if (
      op.kind === OP_KIND.webServerCert &&
      op.secondaryNodeId &&
      deploymentJobId &&
      isCertificateJourney(runState.result?.certificateJourney) &&
      isLabHealthReport(runState.result?.health)
    ) {
      const labEvidence = createLabEvidence(
        deploymentJobId,
        runState.result.health,
        runState.result.certificateJourney,
      )
      topology.patchNodeData(op.secondaryNodeId, {
        certificateJourney: runState.result.certificateJourney,
        labEvidence,
      })
      topology.applyLabEvidence(labEvidence)
    }
  }
}

/**
 * Reverts every non-`done` op back to `staged` (and any `createVm`-target
 * node still `deploying` back to `staged`) — the shared unwind for a plan
 * that ended before every op resolved (socket drop, plan-level crash).
 * `done` ops are kept, untouched: their canvas effect was already committed by
 * `applyPlanState` when the `done` transition arrived, and they are the record
 * of how far the run got. They can't be re-sent — every path to the wire goes
 * through `prepareDeployPlan`, which drops them.
 *
 * A node whose clone already landed is the exception: `staged` means "no VM yet",
 * and it has one — with an IP, a name, and possibly a half-installed role. It
 * unwinds to `failed` instead, which keeps that identity (and the Tear down VM
 * affordance) rather than inviting a second clone under the same name.
 */
function revertNonTerminalToStaged(): void {
  const { ops } = useStagingStore.getState()
  const topology = useTopologyStore.getState()

  const unwindNode = (nodeId: string) => {
    const realized = !!useTopologyStore
      .getState()
      .nodes.find((n) => n.id === nodeId)?.data.vmName
    topology.patchNodeData(nodeId, {
      lifecycle: realized ? LIFECYCLE.failed : LIFECYCLE.staged,
      progress: undefined,
      phase: undefined,
      errorDetail: realized
        ? "Lost contact with the deployment before it was confirmed."
        : undefined,
    })
  }

  const remaining: StagedOp[] = []
  for (const op of ops) {
    if (op.status === OP_STATUS.done) {
      remaining.push(op)
      continue
    }
    if (op.synthesized) {
      // Display-only row: drop it (the next deploy re-materializes it) and
      // unwind its node like any interrupted createVm — a `done` parent clone
      // won't be re-sent, so nobody else would reset the node out of
      // `provisioning`/`deploying`.
      unwindNode(op.targetNodeId)
      continue
    }
    if (op.kind === OP_KIND.createVm) {
      unwindNode(op.targetNodeId)
    }
    for (const edgeId of operationEdgeIds(op)) {
      topology.setEdgeHealth(edgeId, CONNECTION_HEALTH.planned)
    }
    remaining.push({
      ...op,
      status: OP_STATUS.staged,
      progress: undefined,
      detail: undefined,
    })
  }

  useStagingStore.setState({
    ops: remaining,
    deployJobId: null,
    deploying: false,
    planPhase: null,
    planPhaseDetail: null,
    // Every op reverted to `staged`, so there is no run left for a total to
    // describe — unlike `finishDeploy`, which keeps one.
    deployStartedAt: null,
    deployFinishedAt: null,
  })
}

/**
 * Final reconcile once the plan job reaches `done`: apply the last snapshot,
 * reopen `cancelled` ops (skipped only because a dependency failed) as `staged`
 * so "Retry deploy" resends them alongside the op that actually failed, and
 * decide what stays on the list.
 *
 * **A run that failed keeps every `done` row** — its own and any carried over
 * from an earlier attempt. Dropping them left the panel showing one orphaned
 * error row: the deploy's whole context (which clones landed, which
 * relationships were wired) gone, its number reset to 1, and its `dependsOn`
 * pointing at ids no longer in the list, so the next project load would
 * `sanitizeOps` away even that.
 *
 * **A run with nothing failed empties the list**, including rows carried from
 * previous attempts — the panel is a queue again once the queue is clear. The
 * retained rows are never re-sent in the meantime: every path to the wire goes
 * through `prepareDeployPlan`, which drops `done`.
 *
 * Exported for tests.
 */
export function finishDeploy(
  result: Record<string, unknown>,
  deploymentJobId: string,
): void {
  const opsResult = (result?.ops ?? {}) as Record<string, OpRunState>
  applyPlanState(opsResult, deploymentJobId)

  const { ops } = useStagingStore.getState()
  const failed = Object.values(opsResult).some(
    (state) => state?.status === "error",
  )
  // A done createVm whose provision sibling failed is retained alongside it —
  // dropping the parent would orphan the synthetic error row, and the pair
  // reads as one failed deployment of the node (teardown is the exit).
  const failedProvisionParents = new Set(
    ops
      .filter((op) => op.synthesized && opsResult[op.id]?.status === "error")
      .map((op) => provisionParentId(op.id)),
  )
  let errorCount = 0
  const remaining: StagedOp[] = []
  for (const op of ops) {
    const finalState = opsResult[op.id]
    // `?? op.status` covers a row carried over from an earlier attempt: this
    // run never mentioned it, but it is still `done` and still history.
    const status = finalState?.status ?? op.status
    if (status === "done" && !failed && !failedProvisionParents.has(op.id))
      continue
    if (finalState?.status === "error") errorCount++
    remaining.push(
      finalState?.status === "cancelled"
        ? {
            ...op,
            status: OP_STATUS.staged,
            progress: undefined,
            detail: undefined,
          }
        : op,
    )
  }

  useStagingStore.setState({
    ops: remaining,
    deployJobId: null,
    deploying: false,
    planPhase: null,
    planPhaseDetail: null,
    // `deployStartedAt` deliberately survives: clearing it here is what made a
    // finished run report no duration at all, since the live clock unmounts with
    // `deploying` and nothing else had ever recorded how long the run took.
    deployFinishedAt: Date.now(),
  })

  if (errorCount > 0) {
    toast.error(
      `Deploy finished with ${errorCount} failed operation${errorCount === 1 ? "" : "s"}.`,
    )
  } else {
    toast.success("Deploy complete.")
  }
}

/** Pulls the structured preflight report out of a deploy 409's `detail`, if that's what failed — the same checklist then renders with its ✗ rows. */
function extractPreflightReceipt(err: unknown): DeployPreflightReceipt | null {
  if (
    !(err instanceof ApiError) ||
    !err.detail ||
    typeof err.detail !== "object"
  ) {
    return null
  }
  const preflight = (
    err.detail as { preflight?: DeployPreflightReceipt | null }
  ).preflight
  return Array.isArray(preflight?.checks) ? preflight : null
}

/** Advances the pre-execution phase; ignored once the plan is executing (late `queued` replays from a reconnect must not walk the label backwards). */
function setPlanPhase(phase: PlanPhase, detail: string | null = null): void {
  useStagingStore.setState((s) => {
    if (!s.deploying || s.planPhase === "executing") return s
    return { planPhase: phase, planPhaseDetail: detail }
  })
}

// Single in-flight plan socket — a project only ever has one active deploy.
let planSocketClose: (() => void) | null = null
let planRetryTimer: ReturnType<typeof setTimeout> | null = null

// A dropped socket (status 0) doesn't mean the plan died — the worker may
// still be running. Retry with backoff before treating it as gone; only
// unwind (reverting completed-looking state to staged) once retries are
// exhausted, so a blip doesn't race a still-running job into a duplicate
// plan on the next Deploy click.
const PLAN_SOCKET_RETRY_DELAYS_MS = [500, 1500, 3000]

function attachPlanSocket(
  jobId: string,
  token: string | null | undefined,
  attempt = 0,
) {
  planSocketClose?.()
  if (planRetryTimer) {
    clearTimeout(planRetryTimer)
    planRetryTimer = null
  }
  planSocketClose = openJobSocket(jobId, token, {
    onQueued: () => setPlanPhase("queued"),
    // The worker's setup breadcrumbs ride as `planSetup` progress frames
    // between `queued` and `running` — any other progress key is not ours.
    onProgress: (e) => {
      if (e.key === "planSetup") setPlanPhase("preparing", e.phase)
    },
    onRunning: () => setPlanPhase("preparing"),
    onPlanState: (e) => {
      setPlanPhase("executing")
      applyPlanState(e.ops, jobId)
    },
    onDone: (e) => {
      planSocketClose = null
      finishDeploy(e.result, jobId)
    },
    onError: (e) => {
      planSocketClose = null
      // 4401/4404 can never recover; retrying them just delays the real
      // message behind three backoffs that were meant for transport blips.
      const recoverable =
        e.status === 0 &&
        e.code !== WS_CLOSE_UNAUTHORIZED &&
        e.code !== WS_CLOSE_JOB_GONE
      if (recoverable && attempt < PLAN_SOCKET_RETRY_DELAYS_MS.length) {
        planRetryTimer = setTimeout(
          () => attachPlanSocket(jobId, token, attempt + 1),
          PLAN_SOCKET_RETRY_DELAYS_MS[attempt],
        )
        return
      }
      revertNonTerminalToStaged()
      if (e.code === WS_CLOSE_UNAUTHORIZED) {
        // The next HTTP call's 401 handler gates back to the login form.
        toast.error(
          "Your session expired — sign in again to follow the deploy.",
        )
      } else if (e.code === WS_CLOSE_JOB_GONE) {
        toast.warning(
          "That deploy's progress stream has expired — the job may still have finished on the server.",
        )
      } else if (e.status === 0) {
        toast.warning(
          "Lost connection to the deploy job — operations reverted to staged, you can retry.",
        )
      } else {
        toast.error(e.detail || "Deploy failed.")
      }
    },
  })
}

/**
 * Restores the three things a resumed plan can't reconstruct locally: when the
 * run started (the elapsed clock), what preflight verified (the receipt), and
 * the compiled manifest behind every row's label and expandable step tree.
 *
 * All three used to live only in the tab that clicked Deploy, so a reload showed
 * the same plan with no clock, no receipt, and — for any row whose group the
 * panel's own compile-preview had supplied rather than `cacheExecutionGroups` —
 * no steps. The preview can't fill the gap on a remount either: its effect
 * deliberately doesn't compile while a deploy is in flight.
 *
 * Best-effort by design. The socket is what makes a resumed deploy work; this
 * only makes it legible, so a failure here stays silent rather than toasting
 * over a deploy that is streaming fine.
 */
function rehydrateRunFacts(jobId: string): void {
  getDeployRun(jobId)
    .then((run) => {
      // A project switch (or a finished plan) between request and response must
      // not land another run's facts on the current one.
      if (useStagingStore.getState().deployJobId !== jobId) return
      useStagingStore.setState({
        deployStartedAt: run.createdAt ?? null,
        preflightReceipt: run.preflight ?? null,
      })
      if (run.groups.length > 0) {
        useStagingStore.getState().cacheExecutionGroups(run.groups)
      }
    })
    .catch(() => {})
}

export const useStagingStore = create<StagingState>()((set, get) => ({
  ops: [],
  deployJobId: null,
  deploying: false,
  planPhase: null,
  planPhaseDetail: null,
  deployStartedAt: null,
  deployFinishedAt: null,
  preflightReceipt: null,

  stageOp(input) {
    const { ops, deploying } = get()
    const dependsOn = inferDependsOn(
      input.kind,
      input.targetNodeId,
      input.secondaryNodeId,
      ops,
    )
    const op: StagedOp = {
      ...input,
      id: crypto.randomUUID(),
      status: OP_STATUS.staged,
      dependsOn,
    }
    // Defense in depth: every caller already checks `deploying` itself, but
    // this is the sole insertion point, so guard it here too.
    if (!deploying) set({ ops: [...ops, op] })
    return op
  },

  undo() {
    const { ops, deploying } = get()
    if (deploying || ops.length === 0) return
    // Synthetic provision rows are display-only — undo targets the last
    // user-staged op and carries the op's synthetic child away with it.
    let index = ops.length - 1
    while (index >= 0 && ops[index].synthesized) index--
    if (index < 0) return
    const last = ops[index]
    // `done` rows retained after a failed run are history, not queue: they
    // already happened, so `revertOp` would unwind canvas state for a VM that
    // really exists. Skipping past one to an earlier op would break the
    // "last op has no dependents" invariant, so undo simply stops here.
    if (last.status === OP_STATUS.done) return
    revertOp(last)
    set({
      ops: ops.filter(
        (op, i) =>
          i !== index &&
          !(op.synthesized && provisionParentId(op.id) === last.id),
      ),
    })
  },

  removeOpCascade(opId) {
    const { ops } = get()
    const op = ops.find((o) => o.id === opId)
    if (!op) return
    const toRemove = [...transitiveDependents(opId, ops), op]
    // Revert in reverse so the deepest dependents unwind before what they depend on.
    for (let i = toRemove.length - 1; i >= 0; i--) revertOp(toRemove[i])
    const removedIds = new Set(toRemove.map((o) => o.id))
    set((s) => ({ ops: s.ops.filter((o) => !removedIds.has(o.id)) }))
  },

  setOpState(opId, patch) {
    set((s) => ({
      ops: s.ops.map((op) => (op.id === opId ? { ...op, ...patch } : op)),
    }))
  },

  cacheExecutionGroups(groups) {
    const groupsById = new Map(groups.map((group) => [group.id, group]))
    set((state) => {
      const knownIds = new Set(state.ops.map((op) => op.id))
      return {
        ops: state.ops.flatMap((op) => {
          const parent = {
            ...op,
            ...(groupsById.has(op.id)
              ? { executionGroup: groupsById.get(op.id) }
              : {}),
          }
          if (op.kind !== OP_KIND.createVm) return [parent]

          const provisionId = `${op.id}::provision`
          const provisionGroup = groupsById.get(provisionId)
          if (!provisionGroup || knownIds.has(provisionId)) return [parent]
          knownIds.add(provisionId)
          return [
            parent,
            {
              id: provisionId,
              kind: OP_KIND.provision,
              targetNodeId: op.targetNodeId,
              params: {},
              dependsOn: [op.id],
              label: provisionGroup.label || provisionRowLabel(op),
              status: OP_STATUS.pending,
              synthesized: true,
              executionGroup: provisionGroup,
            },
          ]
        }),
      }
    })
  },

  loadOps(ops, deployJobId) {
    planSocketClose?.()
    planSocketClose = null
    if (planRetryTimer) {
      clearTimeout(planRetryTimer)
      planRetryTimer = null
    }
    set({
      ops: sanitizeOps(ops),
      deployJobId,
      deploying: false,
      planPhase: null,
      planPhaseDetail: null,
      deployStartedAt: null,
      deployFinishedAt: null,
      preflightReceipt: null,
    })
    get().resumePlanJob()
  },

  deploy() {
    const { ops, deploying } = get()
    // A list of nothing but retained `done` rows has nothing left to send.
    if (deploying || actionableOps(ops).length === 0) return

    // Pre-flight: UPLOAD-ISO mode with nothing uploaded means the operator asked
    // for a verbatim disc but hasn't provided one — refuse rather than silently
    // deploying the default config.
    const missingIso = emptyIsoNodes(ops)
    if (missingIso.length > 0) {
      toast.error(
        `"${missingIso[0]}"${missingIso.length > 1 ? ` (+${missingIso.length - 1} more)` : ""}: ` +
          "no ISO uploaded — upload one, switch to Pack files, or turn it off.",
      )
      return
    }

    // Set synchronously, before the POST even goes out — closes the window
    // between click and the 202 response where a second click could enqueue
    // a duplicate plan (double real clone → VmExists) and undo/stage/canvas
    // edits were still allowed on an in-flight plan.
    set({
      deploying: true,
      planPhase: "posting",
      planPhaseDetail: null,
      deployStartedAt: Date.now(),
      deployFinishedAt: null,
      preflightReceipt: null,
    })

    const token = useAuthStore.getState().token

    // `done` ops already succeeded — they're excluded from the payload so a
    // retry never re-sends (and double-executes) them. Previously failed/
    // cancelled ops reset to staged so a retry resends them alongside whatever
    // hadn't run yet. `dependsOn` is then pruned to only ids being sent, so a
    // withheld `done` op's id never reaches the backend as an unknown dep.
    const prepared = prepareDeployPlan(ops)
    const pruned = prepared.ops

    const topologyStore = useTopologyStore.getState()
    for (const op of pruned) {
      for (const edgeId of operationEdgeIds(op)) {
        topologyStore.setEdgeHealth(edgeId, CONNECTION_HEALTH.planned)
      }
    }

    // Withheld from the wire, kept on the list: a retry after a partial
    // failure must not erase the rows for the work that already succeeded —
    // that history is most of what makes the one failure readable. Rows that
    // are neither sent nor terminal (a synthetic provision row whose parent
    // isn't being re-sent) have nothing left to say and go.
    const sending = new Map(pruned.map((op) => [op.id, op]))
    set({
      ops: ops.flatMap((op) => {
        const next = sending.get(op.id)
        if (next) return [{ ...next, status: OP_STATUS.pending }]
        return op.status === OP_STATUS.done || op.status === OP_STATUS.error
          ? [op]
          : []
      }),
    })

    // Save before deploying, and refuse if the save doesn't land. A deploy names
    // real VMs after `projectId`, so a plan whose project was never persisted
    // leaves machines attributed to a project that does not exist — which is
    // exactly what happened before this: registry rows and plan runs pointing at
    // ids no `projects` document ever matched.
    saveActiveProjectNow()
      .then((saved) => {
        if (!saved) {
          throw new Error(
            "Couldn't save this project — deploy cancelled so nothing runs against an unsaved topology.",
          )
        }
        return deployPlan(
          prepared.payload,
          prepared.topology,
          prepared.projectId,
        )
      })
      .then(({ job_id, preflight }) => {
        set({
          deployJobId: job_id,
          planPhase: "queued",
          preflightReceipt: preflight ?? null,
        })
        attachPlanSocket(job_id, token)
      })
      .catch((err) => {
        set((s) => ({
          ops: s.ops.map((op) => ({ ...op, status: OP_STATUS.staged })),
          deploying: false,
          planPhase: null,
          planPhaseDetail: null,
          deployStartedAt: null,
          deployFinishedAt: null,
          preflightReceipt: extractPreflightReceipt(err),
        }))
        toast.error(
          err instanceof Error ? err.message : "Failed to start deploy.",
        )
      })
  },

  resumePlanJob() {
    const { deployJobId, deploying } = get()
    if (!deployJobId || deploying) return
    const token = useAuthStore.getState().token
    // Without a live token the socket can't authenticate — leave the job id
    // in place so a later resume (once the session is ready) can pick it up.
    if (!token) return
    set({ deploying: true })
    attachPlanSocket(deployJobId, token)
    rehydrateRunFacts(deployJobId)
  },
}))

/** Ops that reference `nodeId` (as target or secondary) plus everything transitively dependent on them — the full set a node deletion would need to cascade-remove. */
export function opsReferencingNode(
  ops: StagedOp[],
  nodeId: string,
): StagedOp[] {
  const referencing = ops.filter(
    (op) => op.targetNodeId === nodeId || op.secondaryNodeId === nodeId,
  )
  const ids = new Set<string>()
  for (const op of referencing) {
    ids.add(op.id)
    for (const dep of transitiveDependents(op.id, ops)) ids.add(dep.id)
  }
  return ops.filter((op) => ids.has(op.id))
}
