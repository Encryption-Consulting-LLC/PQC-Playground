/**
 * Ephemeral topology store — nodes + edges live in memory for the session.
 *
 * Wraps React Flow's applyNodeChanges / applyEdgeChanges helpers so the rest
 * of the app talks through this store rather than calling React Flow directly.
 *
 * Seam: to add persistence, wrap the create() call with the zustand `persist`
 * middleware and point it at localStorage or a backend endpoint.
 */

import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type Viewport,
} from "@xyflow/react"
import { create } from "zustand"

import { AUTO_NAME_PREFIX } from "@/constants/templates"
import { CONNECTION_HEALTH, EDGE_TYPE, LIFECYCLE } from "@/constants/topology"
import { SERVICE_SOCKET } from "@/constants/topology"
import type { ConnectionHealth, Lifecycle } from "@/constants/topology"
import type { IsoMode } from "@/constants/iso"
import { toast } from "sonner"

import { ApiError, deleteIso, deleteVm } from "@/lib/api"
import type { CertificateJourney } from "@/lib/certificateJourney"
import type { LabEvidence, ServiceHealth } from "@/lib/labEvidence"
import { aggregateServiceHealth, serviceHealthForEdge } from "@/lib/labEvidence"
import { openJobSocket } from "@/lib/ws"
import {
  canConnectServiceSockets,
  connectionPorts,
  domainJoinEdge,
  domainLabel,
  edgeStyle,
  edgeServiceSocket,
  findDomainForNode,
  inferEdgeType,
  isDomainEligible,
  parseServiceSocketHandle,
  serviceSocketHandleId,
  serviceSocketEdgeType,
} from "@/lib/topology"
import { OP_KIND, findStagedOp } from "@/lib/staging"
import { useAuthStore } from "@/store/auth"
import { opsReferencingNode, useStagingStore } from "@/store/staging"

// ---------------------------------------------------------------------------
// Node data type
// ---------------------------------------------------------------------------

/** One authored firstboot script in the PACK panel. */
export interface IsoFileEntry {
  name: string
  content: string
}

/**
 * Operator-authored config-ISO state for one node. When `enabled`,
 * the deploy sends this instead of letting the server render the default
 * hostname/network/role scripts — the panel IS the disc, and the VM gets no
 * pool IP. `pack` carries editable text scripts inline; `uploadIso` references
 * a pre-built ISO already uploaded to the backend (`POST /api/iso`).
 * Mutations must stay immutable (fresh objects/arrays) — `lastDeployedIso`
 * keeps the previous reference as its drift snapshot.
 */
export interface IsoAuthoring {
  enabled: boolean
  mode: IsoMode
  files: IsoFileEntry[]
  /** Set once so re-enabling the toggle never re-seeds over user deletions. */
  seeded?: boolean
  isoId?: string
  isoName?: string
  isoSize?: number
}

export interface MachineData extends Record<string, unknown> {
  typeId: string
  name: string
  lifecycle: Lifecycle
  poweredOn: boolean
  /** Config as of the last successful deploy; compared against `config` to derive drift. */
  lastDeployedConfig?: Record<string, string>
  config?: Record<string, string>
  /** Operator-authored config ISO; read fresh at deploy time by `buildOpPayload`. */
  isoAuthoring?: IsoAuthoring
  /** ISO state as of the last successful deploy; compared against `isoAuthoring` to derive drift. */
  lastDeployedIso?: IsoAuthoring
  /** 0–100 while `lifecycle === deploying`; drives the node's progress bar. */
  progress?: number
  /** Human label of the current configuration step (from the progress stream). */
  phase?: string
  /** Durable terminal operation detail; unlike progress/phase, survives project saves. */
  errorDetail?: string
  /**
   * Deploy-confirmed identity of the real VM behind this node, from the
   * createVm op's result (`applyPlanState`): the pool-allocated guest IP and
   * the real (namespaced) ESXi inventory name. Distinct from `name`, the
   * renameable display label. `vmName` doubles as the "a real VM exists"
   * signal — teardown is offered iff it is set. Both persist (durable facts,
   * not run transients).
   */
  ip?: string
  vmName?: string
  /**
   * Backend clone job id while `lifecycle === deploying` on the `standalone`
   * template. Persisted (rides `MachineData` into localStorage) so a reload
   * can resubscribe to the job's WebSocket instead of losing it — see
   * `attachJobSocket`/`resumeJobs`.
   */
  jobId?: string
  /**
   * Backend teardown job id while `lifecycle === destroying`. Persisted for
   * the same reason as `jobId`: a reload mid-teardown resubscribes to the
   * job's WebSocket instead of losing it — see `resumeJobs`.
   */
  teardownJobId?: string
  /**
   * vm_id of an orchestrator agent this node is manually associated with,
   * from `POST /orchestrator/register` (see `Inspector.tsx`'s Orchestrator
   * section). There is no automatic VM<->agent correlation yet — vmkit has
   * no guest-correlation mechanism and isokit/configgen can't bake this in
   * at boot time (see `pki-orchestrator/README.md`) — so a human pastes it
   * in, standing in for what a real deployment will do automatically later.
   */
  executorVmId?: string
  /** Redacted terminal verification projection used by the certificate journey lens. */
  certificateJourney?: CertificateJourney
  /** Redacted final multi-host verification used by heatmap and evidence modes. */
  labEvidence?: LabEvidence
}

/**
 * One node's pending domain membership transition, produced by
 * `computeDomainChanges` and applied (after confirmation) by
 * `applyDomainChanges`. A null `dcId`/`domainName` means the node leaves its
 * current domain; the *Name fields are carried alongside the ids purely so the
 * confirmation prompt can describe the change without re-deriving names.
 */
export interface DomainSyncChange {
  nodeId: string
  nodeName: string
  dcId: string | null
  domainName: string | null
}

// ---------------------------------------------------------------------------
// Live job sockets
// ---------------------------------------------------------------------------

// Transient, per-node teardown for an open job-progress socket. Deliberately
// module-level (not store state): it holds closures, not serializable data,
// and mirrors `overlapNodeId`'s reasoning — it must never flow into
// persistence or trip the autosave subscription.
const activeSockets = new Map<string, () => void>()

/**
 * Opens (or, after a reload, resumes) the progress socket for one node's clone
 * job and wires it to `patch`. Shared by `configureNode` (fresh clone) and
 * `resumeJobs` (rehydration) so both paths handle queued/running/progress/
 * done/error identically. Skips if a socket for `nodeId` is already tracked.
 */
function attachJobSocket(
  nodeId: string,
  jobId: string,
  token: string | null | undefined,
  patch: (data: Partial<MachineData>) => void,
) {
  if (activeSockets.has(nodeId)) return

  const close = openJobSocket(jobId, token, {
    // Clones queue behind the backend's worker concurrency cap; surface
    // that wait as a phase label rather than a separate lifecycle state so
    // existing lifecycle-driven UI (badges, counts) doesn't need to change.
    onQueued: () => patch({ phase: "Queued", progress: 0 }),
    onRunning: () => patch({ phase: "Starting", progress: 0 }),
    onProgress: (e) => patch({ progress: e.percent, phase: e.phase }),
    onDone: () => {
      activeSockets.delete(nodeId)
      const node = useTopologyStore
        .getState()
        .nodes.find((n) => n.id === nodeId)
      patch({
        lifecycle: LIFECYCLE.deployed,
        poweredOn: true,
        lastDeployedConfig: node?.data.config,
        progress: 100,
        jobId: undefined,
      })
      close()
    },
    onError: (e) => {
      activeSockets.delete(nodeId)
      // status 0 is a synthetic frame from `lib/ws.ts` for a socket that
      // closed without a terminal frame — e.g. the backend's snapshot expired
      // (4404) or the WS dropped mid-clone. That's not necessarily a failed
      // clone, so revert to draft (retryable) rather than a hard error; a
      // real backend `error` frame (status > 0) is a genuine failure.
      if (e.status === 0) {
        patch({
          lifecycle: LIFECYCLE.draft,
          progress: undefined,
          phase: undefined,
          errorDetail: undefined,
          jobId: undefined,
        })
      } else {
        patch({
          lifecycle: LIFECYCLE.failed,
          phase: undefined,
          errorDetail: e.detail || "Deployment failed",
          jobId: undefined,
        })
      }
      close()
    },
  })
  activeSockets.set(nodeId, close)
}

/**
 * The actual node removal, shared by `removeNode` (user-initiated, guarded on
 * `deploying`) and a finished teardown (which must remove the node even if a
 * plan deploy started meanwhile — the VM is gone either way).
 */
function removeNodeCore(id: string) {
  activeSockets.get(id)?.()
  activeSockets.delete(id)
  // An uploaded-but-unconsumed ISO dies with its node — best-effort (a 404
  // just means the worker or the orphan sweep already deleted it).
  const isoId = useTopologyStore.getState().nodes.find((n) => n.id === id)?.data
    .isoAuthoring?.isoId
  if (isoId) deleteIso(isoId).catch(() => {})
  // Deleting the node makes any staged op referencing it meaningless —
  // cascade-remove them so the Staged panel never lists a dangling op.
  const staging = useStagingStore.getState()
  for (const op of opsReferencingNode(staging.ops, id)) {
    useStagingStore.getState().removeOpCascade(op.id)
  }
  useTopologyStore.setState((s) => ({
    nodes: s.nodes.filter((n) => n.id !== id),
    edges: s.edges.filter((e) => e.source !== id && e.target !== id),
    selectedNodeId: s.selectedNodeId === id ? null : s.selectedNodeId,
  }))
}

/**
 * Opens (or, after a reload, resumes) the progress socket for one node's
 * teardown job. On `done` the node is removed from the canvas; on error the
 * node reverts to `prevLifecycle` verbatim (a drifted node stays drifted) so
 * the same Tear down button is the retry.
 */
function attachTeardownSocket(
  nodeId: string,
  jobId: string,
  token: string | null | undefined,
  prevLifecycle: Lifecycle,
) {
  if (activeSockets.has(nodeId)) return

  const patch = (data: Partial<MachineData>) =>
    useTopologyStore.getState().patchNodeData(nodeId, data)

  const close = openJobSocket(jobId, token, {
    onQueued: () => patch({ phase: "Queued", progress: 0 }),
    onRunning: () => patch({ phase: "Removing", progress: 0 }),
    onProgress: (e) => patch({ progress: e.percent, phase: e.phase }),
    onDone: () => {
      activeSockets.delete(nodeId)
      close()
      removeNodeCore(nodeId)
      toast("VM torn down.")
    },
    onError: (e) => {
      activeSockets.delete(nodeId)
      close()
      patch({
        lifecycle: prevLifecycle,
        teardownJobId: undefined,
        progress: undefined,
        phase: undefined,
      })
      // status 0 = socket dropped without a terminal frame; the backend job
      // may still finish. A retried teardown converges either way (the
      // worker treats an already-absent VM as success).
      if (e.status === 0) {
        toast.warning(
          "Lost connection to the teardown job — tear down again to retry.",
        )
      } else {
        toast.error(e.detail || "Teardown failed.")
      }
    },
  })
  activeSockets.set(nodeId, close)
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const DEFAULT_VIEWPORT: Viewport = { x: 0, y: 0, zoom: 1 }

interface TopologyState {
  nodes: Node<MachineData>[]
  edges: Edge[]
  selectedNodeId: string | null
  counters: Record<string, number>
  /** Camera pan/zoom; not part of the graph data but persisted per project. */
  viewport: Viewport
  /**
   * Id of the node currently overlapping another mid-drag (drives the red
   * translucent warning state). Deliberately not part of `nodes`/`edges`/
   * `counters` so updating it doesn't trip the autosave/dirty subscription.
   */
  overlapNodeId: string | null

  addNode: (typeId: string, position: { x: number; y: number }) => void
  applyNodeChanges: (changes: NodeChange<Node<MachineData>>[]) => void
  applyEdgeChanges: (changes: EdgeChange[]) => void
  connect: (connection: Connection) => string | null
  computeDomainChanges: (movedId: string) => DomainSyncChange[]
  applyDomainChanges: (changes: DomainSyncChange[]) => void
  configureNode: (id: string, config?: Record<string, string>) => void
  /**
   * Persists the in-progress config-form values on a pre-deploy node so the
   * inspector's textboxes/dropdowns survive a selection switch or reload
   * instead of resetting to their defaults. Unlike `configureNode`, this does
   * NOT stage anything or change the lifecycle — it only keeps the draft.
   * Restricted to `draft`/`failed` nodes (the only states whose config form is
   * editable) and a no-op while deploying, so it can never clobber a
   * staged/deployed node's committed `config`.
   */
  setNodeConfig: (id: string, config: Record<string, string>) => void
  /**
   * Reattaches job-progress sockets for any node still `deploying` after a
   * reload (has a persisted `jobId`), and reverts any `deploying` node with
   * no `jobId` (a plan-driven op, or a reload mid-enqueue) back to `staged`
   * (if a matching staged op survived the reload) or `draft`. Called after
   * `loadSnapshot`.
   */
  resumeJobs: () => void
  renameNode: (id: string, name: string) => void
  /**
   * Merges a patch into one node's `isoAuthoring` (creating it with pack-mode
   * defaults on first touch). Like `config`, this is read fresh at deploy time
   * by the staging store, so edits on a `staged` node need no restage. Blocked
   * while deploying, same as every other mutation.
   */
  setIsoAuthoring: (id: string, patch: Partial<IsoAuthoring>) => void
  /** Merges a partial data patch into one node — the seam the staging store's undo/cascade reverts go through. */
  patchNodeData: (id: string, data: Partial<MachineData>) => void
  /**
   * Promotes a `provisioning` node to `deployed` — called when its orchestrator
   * agent first phones home (`useAgentPromotion`). This is the confirmation
   * that turns a dashed domain circle solid and reveals the node's IP. One-way:
   * a later agent drop doesn't demote (the live status dot tracks that).
   */
  promoteProvisioned: (id: string) => void
  removeNode: (id: string) => void
  /**
   * Destroys the real VM behind a node (DELETE /api/vm/{vmName}, 202 + job
   * stream), then removes the node from the canvas on success. No-op unless
   * the node carries a deploy-confirmed `vmName`. The caller is expected to
   * have confirmed (the Inspector's teardown dialog).
   */
  teardownNode: (id: string) => void
  removeEdge: (id: string) => void
  /** Re-adds a previously-removed edge verbatim — used by `domainLeave` undo to restore the exact membership edge it replaced. */
  restoreEdge: (edge: Edge) => void
  /** Clears an edge's ghost styling once its staged op has deployed successfully. */
  commitEdge: (edgeId: string) => void
  /** Persists deployment health on a typed connection. */
  setEdgeHealth: (edgeId: string, health: ConnectionHealth) => void
  /** Projects terminal verification onto every individual service segment. */
  applyLabEvidence: (evidence: LabEvidence) => void
  selectNode: (id: string | null) => void
  setViewport: (viewport: Viewport) => void
  setOverlapNode: (id: string | null) => void
  /** Replaces the working graph wholesale — used when switching/creating projects. */
  loadSnapshot: (
    nodes: Node<MachineData>[],
    edges: Edge[],
    counters: Record<string, number>,
    viewport: Viewport,
  ) => void
}

export const useTopologyStore = create<TopologyState>()((set, get) => ({
  nodes: [],
  edges: [],
  selectedNodeId: null,
  counters: {},
  viewport: DEFAULT_VIEWPORT,
  overlapNodeId: null,

  addNode(typeId, position) {
    const counters = { ...get().counters }
    const n = (counters[typeId] ?? 0) + 1
    counters[typeId] = n
    const prefix = AUTO_NAME_PREFIX[typeId] ?? typeId.slice(0, 4)
    const name = `${prefix}${String(n).padStart(2, "0")}`

    const newNode: Node<MachineData> = {
      id: `${typeId}-${n}-${Date.now()}`,
      type: "machine",
      position,
      selected: false,
      data: {
        typeId,
        name,
        lifecycle: LIFECYCLE.draft,
        poweredOn: false,
      },
    }

    set((s) => ({
      nodes: [...s.nodes, newNode],
      counters,
    }))
  },

  applyNodeChanges(changes) {
    set((s) => ({
      nodes: applyNodeChanges(changes, s.nodes),
    }))
  },

  applyEdgeChanges(changes) {
    set((s) => ({
      edges: applyEdgeChanges(changes, s.edges),
    }))
  },

  connect(connection) {
    if (useStagingStore.getState().deploying)
      return "Canvas is locked while deploying."
    const { nodes, edges } = get()
    const { source, target, sourceHandle, targetHandle } = connection
    const result = canConnectServiceSockets(connection, nodes, edges)
    if (!result.ok) return result.reason ?? "Connection blocked."

    const sourceNode = nodes.find((n) => n.id === source)!
    const targetNode = nodes.find((n) => n.id === target)!
    const type =
      serviceSocketEdgeType(connection, nodes) ??
      inferEdgeType(sourceNode.data.typeId, targetNode.data.typeId)
    const serviceSocket =
      type === EDGE_TYPE.webServerCert
        ? (parseServiceSocketHandle(sourceHandle)?.socket ??
          SERVICE_SOCKET.publication)
        : null

    if (type === EDGE_TYPE.domainJoin) {
      get().applyDomainChanges([
        {
          nodeId: sourceNode.id,
          nodeName: sourceNode.data.name,
          dcId: targetNode.id,
          domainName: domainLabel(targetNode),
        },
      ])
      return null
    }
    const rootIssuer = sourceNode.data.config?.caType === "Root"
    const style = edgeStyle(type, { rootIssuer, serviceSocket })

    const edgeId =
      type === EDGE_TYPE.webServerCert
        ? `e-${source}-${target}-${serviceSocket}`
        : `e-${source}-${target}`
    const newEdge: Edge = {
      id: edgeId,
      source,
      target,
      sourceHandle,
      targetHandle,
      type: "capability",
      markerEnd: { type: "arrowclosed" as const },
      data: {
        edgeType: type,
        ports: connectionPorts(type),
        staged: true,
        health: CONNECTION_HEALTH.planned,
        rootIssuer,
        ...(serviceSocket ? { serviceSocket } : {}),
      },
      ...style,
      // Ghost styling until this op is deployed — commitEdge (M4) clears it.
      style: {
        ...style.style,
        ...(!rootIssuer ? { strokeDasharray: "6 4" } : {}),
        opacity: 0.6,
      },
    }

    // A connection is a relationship edit, not a layout command. Keep every
    // authored coordinate byte-for-byte stable when the edge is added.
    set((s) => ({ edges: addEdge(newEdge, s.edges) }))

    // A CA-hierarchy op belongs to the child being issued to; a web-server
    // publish op belongs to the issuing CA — see lib/staging.ts's
    // inferDependsOn, which keys a webServerCert op's caConnect dependency
    // off this same targetNodeId.
    const isCaHierarchy = type === EDGE_TYPE.caHierarchy
    if (type === EDGE_TYPE.webServerCert) {
      // Root HTTP publication is fulfilled by the offline caConnect relay.
      // An issuing CA's CDP/AIA + OCSP sockets together describe the one
      // atomic backend webServerCert operation.
      if (rootIssuer) return null
      const relationshipEdges = get().edges.filter(
        (edge) =>
          edge.data?.edgeType === EDGE_TYPE.webServerCert &&
          edge.source === source &&
          edge.target === target,
      )
      const sockets = new Set(relationshipEdges.map(edgeServiceSocket))
      if (
        !sockets.has(SERVICE_SOCKET.publication) ||
        !sockets.has(SERVICE_SOCKET.ocsp)
      ) {
        return null
      }
      const alreadyStaged = useStagingStore
        .getState()
        .ops.some(
          (op) =>
            op.kind === OP_KIND.webServerCert &&
            op.targetNodeId === source &&
            op.secondaryNodeId === target,
        )
      if (alreadyStaged) return null
    }
    const opTarget = isCaHierarchy ? targetNode : sourceNode
    const opSecondary = isCaHierarchy ? sourceNode : targetNode

    useStagingStore.getState().stageOp({
      kind: isCaHierarchy ? OP_KIND.caConnect : OP_KIND.webServerCert,
      targetNodeId: opTarget.id,
      secondaryNodeId: opSecondary.id,
      params: {},
      label: isCaHierarchy
        ? `Issue from ${opSecondary.data.name}`
        : `Publish CDP/AIA and OCSP to ${opSecondary.data.name}`,
      edgeId,
    })

    return null // null = success
  },

  computeDomainChanges(movedId) {
    const { nodes, edges } = get()
    const moved = nodes.find((n) => n.id === movedId)
    if (!moved) return []

    // Moving a DC reshapes its region, so every other node is re-evaluated.
    // Moving anything else only changes that one node's membership.
    const candidates =
      moved.data.typeId === "domainController"
        ? nodes.filter((n) => n.id !== movedId)
        : [moved]

    const changes: DomainSyncChange[] = []

    for (const node of candidates) {
      if (!isDomainEligible(node, edges)) continue

      const dc = findDomainForNode(node, nodes, edges)
      const targetDcId = dc?.id ?? null
      const currentEdge = edges.find(
        (e) =>
          e.source === node.id && e.data?.edgeType === EDGE_TYPE.domainJoin,
      )
      if ((currentEdge?.target ?? null) === targetDcId) continue

      changes.push({
        nodeId: node.id,
        nodeName: node.data.name,
        dcId: targetDcId,
        domainName: dc ? domainLabel(dc) : null,
      })
    }

    return changes
  },

  applyDomainChanges(changes) {
    if (changes.length === 0) return
    if (useStagingStore.getState().deploying) return
    for (const c of changes) {
      // Capture what's being replaced before mutating — `domainLeave`'s
      // undo needs the DC it left to re-add the exact same edge.
      const prevEdge = get().edges.find(
        (e) =>
          e.source === c.nodeId && e.data?.edgeType === EDGE_TYPE.domainJoin,
      )
      const prevDcId = prevEdge?.target ?? null

      set((s) => ({
        edges: s.edges.filter(
          (e) =>
            !(
              e.source === c.nodeId && e.data?.edgeType === EDGE_TYPE.domainJoin
            ),
        ),
      }))

      // Retargeting (or leaving) a membership that only existed as a staged
      // op is a pure undo of that op — no domainLeave op needed.
      const existingJoinOp = findStagedOp(
        useStagingStore.getState().ops,
        OP_KIND.domainJoin,
        c.nodeId,
      )
      if (existingJoinOp)
        useStagingStore.getState().removeOpCascade(existingJoinOp.id)

      // A *deployed* prior membership isn't undone by cascading a staged op —
      // the backend needs an explicit leave, staged before the new join (both
      // whether this is a plain leave or a retarget onto a different DC).
      if (prevDcId && !existingJoinOp) {
        useStagingStore.getState().stageOp({
          kind: OP_KIND.domainLeave,
          targetNodeId: c.nodeId,
          params: { prevDcId },
          label: "Leave domain",
        })
      }

      if (c.dcId) {
        const newEdge = domainJoinEdge(c.nodeId, c.dcId, true)
        set((s) => ({ edges: [...s.edges, newEdge] }))
        useStagingStore.getState().stageOp({
          kind: OP_KIND.domainJoin,
          targetNodeId: c.nodeId,
          secondaryNodeId: c.dcId,
          // Carried so undoing just this join restores the exact edge it
          // replaced, without relying on the paired domainLeave op above
          // also being undone.
          params: prevDcId && !existingJoinOp ? { prevDcId } : {},
          label: `Join ${c.domainName}`,
          edgeId: newEdge.id,
        })
      }
    }
  },

  configureNode(id, config) {
    if (useStagingStore.getState().deploying) return
    // Patch one node's data; merges so callers set just the fields they touch.
    const patch = (data: Partial<MachineData>) =>
      set((s) => ({
        nodes: s.nodes.map((n) =>
          n.id === id ? { ...n, data: { ...n.data, ...data } } : n,
        ),
      }))

    const node = get().nodes.find((n) => n.id === id)
    if (!node) return
    const { lifecycle } = node.data

    // Deployed already — config edits mark it drifted rather than restaging;
    // reapplying a deployed node's config is out of scope for v1 (Deploy
    // skips drifted nodes).
    if (lifecycle === LIFECYCLE.deployed || lifecycle === LIFECYCLE.drifted) {
      patch({ ...(config ? { config } : {}), lifecycle: LIFECYCLE.drifted })
      return
    }

    // Already staged — node.config is the sole source of truth for a
    // pending createVm's params (read fresh at deploy time by
    // `buildOpParams`), so reconfiguring just updates it in place.
    if (lifecycle === LIFECYCLE.staged) {
      if (config) patch({ config })
      return
    }

    // draft or failed — stage a fresh createVm op. The wire kind retains its
    // compatibility name, but the user-facing action is the real operation:
    // cloning the configured golden image.
    patch({
      ...(config ? { config } : {}),
      lifecycle: LIFECYCLE.staged,
      errorDetail: undefined,
    })
    useStagingStore.getState().stageOp({
      kind: OP_KIND.createVm,
      targetNodeId: id,
      params: {},
      label: "Clone VM",
    })
  },

  setNodeConfig(id, config) {
    if (useStagingStore.getState().deploying) return
    set((s) => ({
      nodes: s.nodes.map((n) => {
        if (n.id !== id) return n
        // Only pre-deploy nodes have an editable form; guard so a stray call
        // can't overwrite a staged/deployed node's committed config.
        if (
          n.data.lifecycle !== LIFECYCLE.draft &&
          n.data.lifecycle !== LIFECYCLE.failed
        ) {
          return n
        }
        return { ...n, data: { ...n.data, config } }
      }),
    }))
  },

  resumeJobs() {
    const token = useAuthStore.getState().token
    // Without a live token we can neither authenticate a resumed socket nor
    // legitimately conclude a job failed — leave `deploying` nodes as-is
    // rather than reverting them (belt-and-suspenders alongside the
    // `sessionReady` gate in App.tsx that should prevent this from firing
    // with a null token in the first place).
    if (!token) return
    for (const node of get().nodes) {
      // In-flight teardown: resubscribe via the persisted teardownJobId, or —
      // reloaded between click and the 202 — revert to deployed (the safe
      // assumption; a retried teardown converges even if the job did run).
      if (node.data.lifecycle === LIFECYCLE.destroying) {
        if (activeSockets.has(node.id)) continue
        if (node.data.teardownJobId) {
          attachTeardownSocket(
            node.id,
            node.data.teardownJobId,
            token,
            LIFECYCLE.deployed,
          )
        } else {
          get().patchNodeData(node.id, {
            lifecycle: LIFECYCLE.deployed,
            progress: undefined,
            phase: undefined,
          })
        }
        continue
      }
      if (node.data.lifecycle !== LIFECYCLE.deploying) continue
      if (activeSockets.has(node.id)) continue

      const patch = (data: Partial<MachineData>) =>
        set((s) => ({
          nodes: s.nodes.map((n) =>
            n.id === node.id ? { ...n, data: { ...n.data, ...data } } : n,
          ),
        }))

      if (node.data.jobId) {
        attachJobSocket(node.id, node.data.jobId, token, patch)
      } else {
        // No job to resume — a plan-driven op mid-flight, or a reload before
        // one enqueued. Revert to staged if a matching op survived the
        // reload (retryable via Deploy), else draft.
        const hasStagedOp = !!findStagedOp(
          useStagingStore.getState().ops,
          OP_KIND.createVm,
          node.id,
        )
        patch({
          lifecycle: hasStagedOp ? LIFECYCLE.staged : LIFECYCLE.draft,
          progress: undefined,
          phase: undefined,
        })
      }
    }
  },

  renameNode(id, name) {
    if (useStagingStore.getState().deploying) return
    set((s) => ({
      nodes: s.nodes.map((n) =>
        n.id === id ? { ...n, data: { ...n.data, name } } : n,
      ),
    }))
  },

  setIsoAuthoring(id, patch) {
    if (useStagingStore.getState().deploying) return
    set((s) => ({
      nodes: s.nodes.map((n) => {
        if (n.id !== id) return n
        const current: IsoAuthoring = n.data.isoAuthoring ?? {
          enabled: false,
          mode: "pack",
          files: [],
        }
        // ISO edits on a deployed node mark it drifted, mirroring
        // `configureNode`'s handling of config edits.
        const wasDeployed =
          n.data.lifecycle === LIFECYCLE.deployed ||
          n.data.lifecycle === LIFECYCLE.drifted
        // Fresh object every time — `lastDeployedIso` may hold the previous
        // reference as its drift snapshot.
        return {
          ...n,
          data: {
            ...n.data,
            isoAuthoring: { ...current, ...patch },
            ...(wasDeployed ? { lifecycle: LIFECYCLE.drifted } : {}),
          },
        }
      }),
    }))
  },

  patchNodeData(id, data) {
    set((s) => ({
      nodes: s.nodes.map((n) =>
        n.id === id ? { ...n, data: { ...n.data, ...data } } : n,
      ),
    }))
  },

  promoteProvisioned(id) {
    const node = get().nodes.find((n) => n.id === id)
    if (!node || node.data.lifecycle !== LIFECYCLE.provisioning) return
    get().patchNodeData(id, { lifecycle: LIFECYCLE.deployed })
  },

  removeNode(id) {
    if (useStagingStore.getState().deploying) return
    // (The UI's own confirm dialog is expected to have already asked before
    // calling removeNode; removeNodeCore is the data-integrity backstop.)
    removeNodeCore(id)
  },

  teardownNode(id) {
    if (useStagingStore.getState().deploying) return
    const node = get().nodes.find((n) => n.id === id)
    const vmName = node?.data.vmName
    if (!node || !vmName) return
    if (node.data.lifecycle === LIFECYCLE.destroying) return

    // Staged ops referencing the node die with it — cascade now (the confirm
    // dialog already listed them) so the plan can't reference a VM that's
    // about to disappear.
    for (const op of opsReferencingNode(useStagingStore.getState().ops, id)) {
      useStagingStore.getState().removeOpCascade(op.id)
    }

    const prevLifecycle = node.data.lifecycle
    get().patchNodeData(id, {
      lifecycle: LIFECYCLE.destroying,
      phase: "Removing",
      progress: 0,
    })

    const token = useAuthStore.getState().token
    deleteVm(vmName)
      .then(({ job_id }) => {
        get().patchNodeData(id, { teardownJobId: job_id })
        attachTeardownSocket(id, job_id, token, prevLifecycle)
      })
      .catch((err) => {
        // 404 = the VM is already gone; converge to removed instead of erroring.
        if (err instanceof ApiError && err.status === 404) {
          removeNodeCore(id)
          toast("VM torn down.")
          return
        }
        get().patchNodeData(id, {
          lifecycle: prevLifecycle,
          teardownJobId: undefined,
          progress: undefined,
          phase: undefined,
        })
        toast.error(
          err instanceof Error ? err.message : "Failed to start teardown.",
        )
      })
  },

  removeEdge(id) {
    set((s) => ({ edges: s.edges.filter((e) => e.id !== id) }))
  },

  restoreEdge(edge) {
    // Idempotent: a retarget stages both a domainLeave and a domainJoin that
    // each carry the same `prevDcId`-derived edge, so undoing both in
    // sequence would otherwise restore it twice.
    set((s) =>
      s.edges.some((e) => e.id === edge.id) ? s : { edges: [...s.edges, edge] },
    )
  },

  commitEdge(edgeId) {
    set((s) => ({
      edges: s.edges.map((e) => {
        if (e.id !== edgeId) return e
        const edgeType = e.data?.edgeType as
          | ReturnType<typeof inferEdgeType>
          | undefined
        if (!edgeType) {
          return {
            ...e,
            data: {
              ...e.data,
              staged: false,
              health: CONNECTION_HEALTH.verified,
            },
          }
        }
        const clean = edgeStyle(edgeType, {
          rootIssuer: e.data?.rootIssuer as boolean | undefined,
          serviceSocket: edgeServiceSocket(e),
        })
        return {
          ...e,
          ...clean,
          data: {
            ...e.data,
            staged: false,
            health: CONNECTION_HEALTH.verified,
          },
        }
      }),
    }))
  },

  setEdgeHealth(edgeId, health) {
    set((s) => ({
      edges: s.edges.map((edge) =>
        edge.id === edgeId ? { ...edge, data: { ...edge.data, health } } : edge,
      ),
    }))
  },

  applyLabEvidence(evidence) {
    set((s) => ({
      edges: s.edges.map((edge) => {
        const serviceHealth: ServiceHealth = serviceHealthForEdge(
          edge,
          s.nodes,
          evidence,
        )
        if (Object.keys(serviceHealth).length === 0) return edge
        const fallback =
          (edge.data?.health as ConnectionHealth | undefined) ??
          CONNECTION_HEALTH.verified
        return {
          ...edge,
          data: {
            ...edge.data,
            serviceHealth,
            health: aggregateServiceHealth(serviceHealth, fallback),
          },
        }
      }),
    }))
  },

  selectNode(id) {
    if (get().selectedNodeId === id) return
    set({ selectedNodeId: id })
  },

  setViewport(viewport) {
    set({ viewport })
  },

  setOverlapNode(id) {
    if (get().overlapNodeId === id) return
    set({ overlapNodeId: id })
  },

  loadSnapshot(nodes, edges, counters, viewport) {
    // Tear down the outgoing graph's live sockets before swapping — a project
    // switch shouldn't leak sockets tied to nodes that are about to unmount.
    for (const close of activeSockets.values()) close()
    activeSockets.clear()
    const migratedEdges = edges.flatMap((edge) => {
      if (
        edge.data?.edgeType !== EDGE_TYPE.webServerCert ||
        edge.data?.serviceSocket
      ) {
        return [edge]
      }
      const publication: Edge = {
        ...edge,
        sourceHandle: serviceSocketHandleId(
          SERVICE_SOCKET.publication,
          "source",
        ),
        targetHandle: serviceSocketHandleId(
          SERVICE_SOCKET.publication,
          "target",
        ),
        data: { ...edge.data, serviceSocket: SERVICE_SOCKET.publication },
      }
      const source = nodes.find((node) => node.id === edge.source)
      if (source?.data.config?.caType === "Root") return [publication]
      const ocsp: Edge = {
        ...edge,
        id: `${edge.id}-ocsp`,
        sourceHandle: serviceSocketHandleId(SERVICE_SOCKET.ocsp, "source"),
        targetHandle: serviceSocketHandleId(SERVICE_SOCKET.ocsp, "target"),
        data: { ...edge.data, serviceSocket: SERVICE_SOCKET.ocsp },
      }
      return [publication, ocsp]
    })
    const hydratedEdges = migratedEdges.map((edge) => {
      const edgeType = edge.data?.edgeType as
        | ReturnType<typeof inferEdgeType>
        | undefined
      if (!edgeType || edgeType === EDGE_TYPE.network) return edge
      const rootIssuer = edge.data?.rootIssuer === true
      const serviceSocket = edgeServiceSocket(edge)
      const visual = edgeStyle(edgeType, { rootIssuer, serviceSocket })
      const staged = edge.data?.staged === true
      const savedHealth = edge.data?.health as ConnectionHealth | undefined
      const health = Object.values(CONNECTION_HEALTH).includes(
        savedHealth as ConnectionHealth,
      )
        ? savedHealth!
        : staged
          ? CONNECTION_HEALTH.planned
          : CONNECTION_HEALTH.verified
      return {
        ...edge,
        type: "capability",
        // Domain membership is a logical relationship rendered by the DC's
        // surrounding region, not by a React Flow edge. Older snapshots may
        // omit `hidden`, but passing such an edge to React Flow makes it try
        // to resolve generic handles that MachineNode intentionally does not
        // expose (error 008).
        hidden: edgeType === EDGE_TYPE.domainJoin,
        ...visual,
        data: {
          ...edge.data,
          edgeType,
          ports: connectionPorts(edgeType),
          staged,
          health,
          rootIssuer,
          ...(serviceSocket ? { serviceSocket } : {}),
        },
        style: staged
          ? {
              ...visual.style,
              ...(!rootIssuer ? { strokeDasharray: "6 4" } : {}),
              opacity: 0.6,
            }
          : visual.style,
      }
    })
    set({
      nodes,
      edges: hydratedEdges,
      counters,
      viewport,
      selectedNodeId: null,
    })
    // Reattach/revert any `configuring` node in the graph just loaded — this
    // is what makes an in-flight clone resume after a reload or project switch.
    get().resumeJobs()
  },
}))
