import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Panel,
  type NodeChange,
  type EdgeChange,
  type Connection,
  type OnConnect,
  type OnConnectStart,
  type IsValidConnection,
  type OnSelectionChangeParams,
  type OnMoveEnd,
  type OnNodeDrag,
  useReactFlow,
} from "@xyflow/react"
import { toast } from "sonner"
import type { Node } from "@xyflow/react"
import { MachineNode } from "./nodes/MachineNode"
import { DomainRegions, type DomainDragPreview } from "./DomainRegions"
import { DomainConfirmDialog } from "./DomainConfirmDialog"
import { DomainJoinAction } from "./DomainJoinAction"
import { StagedRemoveDialog } from "./StagedRemoveDialog"
import {
  useTopologyStore,
  type MachineData,
  type DomainSyncChange,
} from "@/store/topology"
import { opsReferencingNode, useStagingStore } from "@/store/staging"
import { useIsTemplateAvailable } from "@/hooks/useAllowedProducts"
import { useIsJoinedLab } from "@/hooks/useJoinedLab"
import { OP_KIND, actionableOps, type StagedOp } from "@/lib/staging"
import { TEMPLATE_BY_ID } from "@/constants/templates"
import { DRAFT_NODE_SIZE, EDGE_TYPE } from "@/constants/topology"
import {
  domainJoinBlockReason,
  domainJoinOperations,
  domainRegionBlocker,
  canConnectServiceSockets,
  findDomainForNode,
  findDomainOverlappingNode,
  findOverlappingId,
  isDeployed,
  nearestFreePosition,
  nodeCenter,
} from "@/lib/topology"
import { useResolvedTheme } from "@/hooks/useTheme"
import { CapabilityEdge } from "./edges/CapabilityEdge"
import { TopologyGuidance } from "./TopologyGuidance"
import { ConnectionPreview } from "./ConnectionPreview"
import { useConnectionGestureStore } from "@/store/connectionGesture"
import { projectCertificateJourney } from "@/lib/certificateJourney"
import {
  CertificateJourneyLens,
  CertificateJourneyToggle,
} from "./CertificateJourneyLens"
import { EvidenceModePanel, EvidenceModeToggle } from "./EvidenceMode"
import { findLabEvidence } from "@/lib/labEvidence"
import type { LabEvidence } from "@/lib/labEvidence"

const DRAG_TYPE = "application/reactflow"

const nodeTypes = { machine: MachineNode }
const edgeTypes = { capability: CapabilityEdge }

/**
 * Snapshot of a drag in progress. When the dragged node is a domain
 * controller, `members` carries its *committed* members (a `domainJoin` edge
 * targeting it) so they can be dragged along rigidly and reverted together.
 */
interface DragSnapshot {
  id: string
  position: { x: number; y: number }
  members: { id: string; position: { x: number; y: number } }[]
}

export function Canvas() {
  const nodes = useTopologyStore((s) => s.nodes)
  const edges = useTopologyStore((s) => s.edges)
  const selectedNodeId = useTopologyStore((s) => s.selectedNodeId)
  const deploying = useStagingStore((s) => s.deploying)
  // A lab joined with a code is somebody else's finished environment: it is
  // there to be explored and connected to, never edited. The lock rides
  // alongside `deploying` at every site rather than in a new mechanism, so a
  // gesture that is impossible mid-deploy is impossible here too.
  const joinedLab = useIsJoinedLab()
  const readOnly = deploying || joinedLab
  const isAvailable = useIsTemplateAvailable()
  // Select individual action refs (stable across renders) rather than the
  // whole store, so the callbacks below aren't recreated on every state
  // change — recreating onSelectionChange feeds a React Flow update loop.
  const applyNodeChanges = useTopologyStore((s) => s.applyNodeChanges)
  const applyEdgeChanges = useTopologyStore((s) => s.applyEdgeChanges)
  const connect = useTopologyStore((s) => s.connect)
  const computeDomainChanges = useTopologyStore((s) => s.computeDomainChanges)
  const applyDomainChanges = useTopologyStore((s) => s.applyDomainChanges)
  const selectNode = useTopologyStore((s) => s.selectNode)
  const addNode = useTopologyStore((s) => s.addNode)
  const removeNode = useTopologyStore((s) => s.removeNode)
  const setViewport = useTopologyStore((s) => s.setViewport)
  const setOverlapNode = useTopologyStore((s) => s.setOverlapNode)
  const startConnectionGesture = useConnectionGestureStore((s) => s.start)
  const endConnectionGesture = useConnectionGestureStore((s) => s.end)
  const { screenToFlowPosition, setViewport: rfSetViewport } = useReactFlow()
  const wrapperRef = useRef<HTMLDivElement>(null)
  const resolvedTheme = useResolvedTheme()
  const [initialViewport] = useState(() => useTopologyStore.getState().viewport)
  const [journeyActive, setJourneyActive] = useState(false)
  const [evidenceActive, setEvidenceActive] = useState(false)
  const labEvidence = useMemo(() => findLabEvidence(nodes), [nodes])
  const [evidenceSnapshot, setEvidenceSnapshot] = useState<{
    nodes: typeof nodes
    edges: typeof edges
    evidence: LabEvidence
  } | null>(null)
  const displayedNodes =
    evidenceActive && evidenceSnapshot ? evidenceSnapshot.nodes : nodes
  const displayedEdges =
    evidenceActive && evidenceSnapshot ? evidenceSnapshot.edges : edges
  const journeyProjection = useMemo(
    () => projectCertificateJourney(nodes, edges),
    [nodes, edges],
  )
  const visibleNodes = useMemo(() => {
    if (!journeyActive || !journeyProjection) return displayedNodes
    const involved = new Set(journeyProjection.nodeIds)
    return displayedNodes.map((node) => ({
      ...node,
      style: {
        ...node.style,
        opacity: involved.has(node.id) ? 1 : 0.18,
        filter: involved.has(node.id) ? "none" : "grayscale(1)",
      },
    }))
  }, [displayedNodes, journeyActive, journeyProjection])
  const visibleEdges = useMemo(() => {
    // Domain membership is represented by the surrounding region, while its
    // hidden edge exists only for persistence and topology calculations.
    // Do not give those logical-only edges to React Flow: it still resolves
    // handles for hidden edges, and domain controllers intentionally expose
    // no generic target handle (which otherwise emits React Flow error 008).
    const renderableEdges = displayedEdges.filter(
      (edge) => !edge.hidden && edge.data?.edgeType !== EDGE_TYPE.domainJoin,
    )
    if (!journeyActive || !journeyProjection) return renderableEdges
    const involved = new Set(journeyProjection.edgeIds)
    return renderableEdges.map((edge) => ({
      ...edge,
      animated: involved.has(edge.id),
      style: involved.has(edge.id)
        ? { ...edge.style, stroke: "#8b5cf6", strokeWidth: 3, opacity: 1 }
        : { ...edge.style, opacity: 0.12 },
    }))
  }, [displayedEdges, journeyActive, journeyProjection])

  // The canvas's own pan/zoom is uncontrolled (smooth, no per-frame store
  // writes); we only need to (a) imperatively snap the camera when a project
  // switch loads a different viewport into the store, and (b) capture the
  // final position back into the store once the user stops panning/zooming.
  useEffect(() => {
    return useTopologyStore.subscribe((state, prev) => {
      if (state.viewport !== prev.viewport) {
        rfSetViewport(state.viewport, { duration: 0 })
      }
    })
  }, [rfSetViewport])

  const onMoveEnd: OnMoveEnd = useCallback(
    (_, viewport) => setViewport(viewport),
    [setViewport],
  )

  // Pending domain join/leave awaiting confirmation. We remember where the
  // dragged node (and, if it's a domain controller, its committed members)
  // started so a declined change can snap the whole cluster back — keeping
  // the circle (geometry) and membership in agreement.
  const dragStart = useRef<DragSnapshot | null>(null)
  const connectionSucceeded = useRef(false)
  const [pendingChanges, setPendingChanges] = useState<
    DomainSyncChange[] | null
  >(null)
  const [pendingOperations, setPendingOperations] = useState<string[]>([])
  const [domainDragPreview, setDomainDragPreview] =
    useState<DomainDragPreview | null>(null)

  // Backspace/Delete is handled here instead of via React Flow's built-in
  // `deleteKeyCode` (disabled below) — that default path deletes through
  // `applyNodeChanges` directly, bypassing `removeNode`'s staged-op cascade,
  // socket teardown, and deploying guard, leaving dangling staged ops.
  const [pendingNodeDelete, setPendingNodeDelete] = useState<{
    nodeId: string
    ops: StagedOp[]
    hostNote: boolean
  } | null>(null)

  const requestNodeDelete = useCallback(
    (nodeId: string) => {
      if (readOnly || evidenceActive) return
      const target = nodes.find((n) => n.id === nodeId)
      if (!target) return
      const affected = opsReferencingNode(
        useStagingStore.getState().ops,
        nodeId,
      )
      // A real VM may exist even before the node is a confirmed deployment —
      // a `provisioning` node (or any node carrying a deploy-confirmed vmName)
      // needs the same "this leaves a VM behind" warning as a deployed one.
      const deployed = isDeployed(target.data) || !!target.data.vmName
      if (affected.length === 0 && !deployed) {
        removeNode(nodeId)
        toast("Node removed.")
        return
      }
      setPendingNodeDelete({ nodeId, ops: affected, hostNote: deployed })
    },
    [nodes, readOnly, evidenceActive, removeNode],
  )

  const confirmNodeDelete = useCallback(() => {
    if (!pendingNodeDelete) return
    removeNode(pendingNodeDelete.nodeId)
    toast("Node removed.")
    setPendingNodeDelete(null)
  }, [pendingNodeDelete, removeNode])

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== "Backspace" && e.key !== "Delete") return
      const target = e.target as HTMLElement | null
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return
      }
      if (!selectedNodeId) return
      e.preventDefault()
      requestNodeDelete(selectedNodeId)
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [selectedNodeId, requestNodeDelete])

  const onNodesChange = useCallback(
    (changes: NodeChange<Node<MachineData>>[]) => applyNodeChanges(changes),
    [applyNodeChanges],
  )

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      for (const change of changes) {
        if (change.type !== "remove") continue
        const edge = useTopologyStore
          .getState()
          .edges.find((candidate) => candidate.id === change.id)
        if (!edge) continue
        // Only the op that still owns this edge unstages with it. A `done` row
        // retained after a failed run describes work that already happened —
        // cascading it would unwind canvas state for a real relationship.
        const op = actionableOps(useStagingStore.getState().ops).find(
          (candidate) =>
            candidate.edgeId === edge.id ||
            (candidate.kind === OP_KIND.webServerCert &&
              candidate.targetNodeId === edge.source &&
              candidate.secondaryNodeId === edge.target),
        )
        if (op) useStagingStore.getState().removeOpCascade(op.id)
      }
      applyEdgeChanges(changes)
    },
    [applyEdgeChanges],
  )

  const onConnect: OnConnect = useCallback(
    (connection: Connection) => {
      const error = connect(connection)
      if (error) toast.error(error)
      else connectionSucceeded.current = true
    },
    [connect],
  )

  const onConnectStart: OnConnectStart = useCallback(
    (_, params) => {
      connectionSucceeded.current = false
      if (params.nodeId && params.handleId && params.handleType === "source") {
        startConnectionGesture(params.nodeId, params.handleId)
      }
    },
    [startConnectionGesture],
  )

  const onConnectEnd = useCallback(() => {
    const gesture = useConnectionGestureStore.getState().gesture
    if (
      !connectionSucceeded.current &&
      gesture?.targetNodeId &&
      gesture.targetHandleId
    ) {
      const validation = canConnectServiceSockets(
        {
          source: gesture.sourceNodeId,
          sourceHandle: gesture.sourceHandleId,
          target: gesture.targetNodeId,
          targetHandle: gesture.targetHandleId,
        },
        useTopologyStore.getState().nodes,
        useTopologyStore.getState().edges,
      )
      if (validation.reason) toast.error(validation.reason)
    }
    endConnectionGesture()
  }, [endConnectionGesture])

  const isValidConnection: IsValidConnection = useCallback(
    (connection) => canConnectServiceSockets(connection, nodes, edges).ok,
    [nodes, edges],
  )

  const onSelectionChange = useCallback(
    ({ nodes: selected }: OnSelectionChangeParams) => {
      // Only surface a single node to the Inspector; 0 or 2+ selected both
      // collapse to "nothing selected" so the panel hides for multi-select.
      selectNode(selected.length === 1 ? selected[0].id : null)
    },
    [selectNode],
  )

  const onNodeDragStart = useCallback(
    (_: MouseEvent | TouchEvent, node: Node<MachineData>) => {
      // A multi-node selection is carried as a group by React Flow itself;
      // skip the single-node domain-member/overlap orchestration below.
      if (nodes.filter((n) => n.selected).length > 1) {
        dragStart.current = null
        setOverlapNode(null)
        setDomainDragPreview(null)
        return
      }
      // Dragging a domain controller carries its committed members along
      // rigidly — snapshot their starting positions too.
      const members =
        node.data.typeId === "domainController"
          ? edges
              .filter(
                (e) =>
                  e.target === node.id &&
                  e.data?.edgeType === EDGE_TYPE.domainJoin,
              )
              .map((e) => nodes.find((n) => n.id === e.source))
              .filter((n): n is Node<MachineData> => !!n)
              .map((n) => ({ id: n.id, position: { ...n.position } }))
          : []
      dragStart.current = {
        id: node.id,
        position: { ...node.position },
        members,
      }
      setOverlapNode(null)
      setDomainDragPreview(null)
    },
    [nodes, edges, setOverlapNode],
  )

  // Live drag tick: carry a domain controller's members along with it
  // (preserving their offsets), and flag the dragged node red if it's
  // currently overlapping another node.
  const onNodeDrag: OnNodeDrag<Node<MachineData>> = useCallback(
    (_, node) => {
      if (nodes.filter((n) => n.selected).length > 1) {
        setOverlapNode(null)
        return
      }

      const start = dragStart.current
      const memberIds = start?.members.map((m) => m.id) ?? []

      if (start && start.id === node.id && start.members.length > 0) {
        const delta = {
          x: node.position.x - start.position.x,
          y: node.position.y - start.position.y,
        }
        applyNodeChanges(
          start.members.map((m) => ({
            id: m.id,
            type: "position" as const,
            position: { x: m.position.x + delta.x, y: m.position.y + delta.y },
          })),
        )
      }

      const dragNodes = nodes.map((candidate) => {
        if (candidate.id === node.id) return node
        const memberStart = start?.members.find(
          (member) => member.id === candidate.id,
        )
        if (!memberStart || !start) return candidate
        return {
          ...candidate,
          position: {
            x: memberStart.position.x + node.position.x - start.position.x,
            y: memberStart.position.y + node.position.y - start.position.y,
          },
        }
      })
      const others = dragNodes.filter(
        (n) => n.id !== node.id && !memberIds.includes(n.id),
      )
      setOverlapNode(findOverlappingId(node, others) ? node.id : null)

      if (node.data.typeId === "domainController") {
        const blocker = domainRegionBlocker(node, dragNodes, edges)
        setDomainDragPreview(
          blocker
            ? {
                nodeId: blocker.node.id,
                dcId: node.id,
                allowed: false,
                reason: blocker.reason,
                operations: [],
              }
            : null,
        )
        return
      }

      const centeredDomain = findDomainForNode(node, dragNodes, edges)
      const overlappingDomain = findDomainOverlappingNode(
        node,
        dragNodes,
        edges,
      )
      const dc =
        centeredDomain ??
        (overlappingDomain &&
        domainJoinBlockReason(node, overlappingDomain, edges)
          ? overlappingDomain
          : null)
      const currentDomain = edges.find(
        (edge) =>
          edge.source === node.id &&
          edge.data?.edgeType === EDGE_TYPE.domainJoin,
      )?.target
      if (!dc || currentDomain === dc.id) {
        setDomainDragPreview(null)
        return
      }
      const reason = domainJoinBlockReason(node, dc, edges)
      setDomainDragPreview({
        nodeId: node.id,
        dcId: dc.id,
        allowed: reason === null,
        reason,
        operations: reason ? [] : domainJoinOperations(node, dc, dragNodes),
      })
    },
    [nodes, edges, applyNodeChanges, setOverlapNode],
  )

  // When a node is dropped: if it lands on top of another, relocate it (and
  // its cluster, if any) to the nearest clear spot. Then see whether the move
  // changes domain membership — if so, defer to a confirmation prompt rather
  // than applying immediately.
  const onNodeDragStop = useCallback(
    (_: MouseEvent | TouchEvent, node: Node<MachineData>) => {
      if (nodes.filter((n) => n.selected).length > 1) {
        setOverlapNode(null)
        setDomainDragPreview(null)
        return
      }

      const start = dragStart.current
      const memberIds = start?.members.map((m) => m.id) ?? []
      const others = nodes.filter(
        (n) => n.id !== node.id && !memberIds.includes(n.id),
      )

      if (findOverlappingId(node, others)) {
        const freePosition = nearestFreePosition(node, others, node.position)
        const delta = {
          x: freePosition.x - node.position.x,
          y: freePosition.y - node.position.y,
        }
        const corrections: NodeChange<Node<MachineData>>[] = [
          { id: node.id, type: "position", position: freePosition },
        ]
        for (const id of memberIds) {
          const member = nodes.find((n) => n.id === id)
          if (!member) continue
          corrections.push({
            id,
            type: "position",
            position: {
              x: member.position.x + delta.x,
              y: member.position.y + delta.y,
            },
          })
        }
        applyNodeChanges(corrections)
      }
      setOverlapNode(null)

      const droppedNode = {
        ...node,
        position: findOverlappingId(node, others)
          ? nearestFreePosition(node, others, node.position)
          : node.position,
      }
      const dragNodes = nodes.map((candidate) =>
        candidate.id === node.id
          ? droppedNode
          : start?.members.some((member) => member.id === candidate.id) && start
            ? {
                ...candidate,
                position: {
                  x:
                    start.members.find((member) => member.id === candidate.id)!
                      .position.x +
                    droppedNode.position.x -
                    start.position.x,
                  y:
                    start.members.find((member) => member.id === candidate.id)!
                      .position.y +
                    droppedNode.position.y -
                    start.position.y,
                },
              }
            : candidate,
      )
      const dcBlocker =
        droppedNode.data.typeId === "domainController"
          ? domainRegionBlocker(droppedNode, dragNodes, edges)
          : null
      const centeredDropDomain = findDomainForNode(
        droppedNode,
        dragNodes,
        edges,
      )
      const overlappingDropDomain = findDomainOverlappingNode(
        droppedNode,
        dragNodes,
        edges,
      )
      const dropDomain =
        centeredDropDomain ??
        (overlappingDropDomain &&
        domainJoinBlockReason(droppedNode, overlappingDropDomain, edges)
          ? overlappingDropDomain
          : null)
      const currentDomain = edges.find(
        (edge) =>
          edge.source === node.id &&
          edge.data?.edgeType === EDGE_TYPE.domainJoin,
      )?.target
      const invalidReason = dcBlocker
        ? dcBlocker.reason
        : dropDomain && currentDomain !== dropDomain.id
          ? domainJoinBlockReason(droppedNode, dropDomain, edges)
          : null
      if (invalidReason) {
        if (start) {
          applyNodeChanges([
            { id: start.id, type: "position", position: start.position },
            ...start.members.map((member) => ({
              id: member.id,
              type: "position" as const,
              position: member.position,
            })),
          ])
        }
        toast.error(invalidReason)
        dragStart.current = null
        setDomainDragPreview(null)
        return
      }

      const changes = computeDomainChanges(node.id)
      if (changes.length === 0) {
        dragStart.current = null
        setDomainDragPreview(null)
        return
      }
      setPendingOperations(
        dropDomain
          ? domainJoinOperations(droppedNode, dropDomain, dragNodes)
          : [],
      )
      setPendingChanges(changes)
    },
    [nodes, edges, applyNodeChanges, computeDomainChanges, setOverlapNode],
  )

  const confirmDomainChanges = useCallback(() => {
    if (!pendingChanges) return
    applyDomainChanges(pendingChanges)
    for (const c of pendingChanges) {
      if (c.domainName) toast.success(`${c.nodeName} joined ${c.domainName}`)
      else toast(`${c.nodeName} left its domain`)
    }
    dragStart.current = null
    setPendingChanges(null)
    setPendingOperations([])
    setDomainDragPreview(null)
  }, [pendingChanges, applyDomainChanges])

  const cancelDomainChanges = useCallback(() => {
    // Snap the dragged node (and any members carried along with it) back so
    // nothing sits in a region it didn't join.
    const start = dragStart.current
    if (start) {
      applyNodeChanges([
        { id: start.id, type: "position", position: start.position },
        ...start.members.map((m) => ({
          id: m.id,
          type: "position" as const,
          position: m.position,
        })),
      ])
    }
    dragStart.current = null
    setPendingChanges(null)
    setPendingOperations([])
    setDomainDragPreview(null)
  }, [applyNodeChanges])

  const requestAccessibleDomainJoin = useCallback(
    (node: Node<MachineData>, dc: Node<MachineData>) => {
      const reason = domainJoinBlockReason(node, dc, edges)
      if (reason) {
        toast.error(reason)
        return
      }

      const dcCenter = nodeCenter(dc)
      const nodeWidth = node.measured?.width ?? 160
      const nodeHeight = node.measured?.height ?? 80
      const desired = {
        x: dcCenter.x + 145 - nodeWidth / 2,
        y: dcCenter.y + 115 - nodeHeight / 2,
      }
      const position = nearestFreePosition(
        node,
        nodes.filter((candidate) => candidate.id !== node.id),
        desired,
      )
      const previewNode = { ...node, position }
      const previewNodes = nodes.map((candidate) =>
        candidate.id === node.id ? previewNode : candidate,
      )
      const operations = domainJoinOperations(previewNode, dc, previewNodes)

      dragStart.current = {
        id: node.id,
        position: { ...node.position },
        members: [],
      }
      applyNodeChanges([{ id: node.id, type: "position", position }])
      setDomainDragPreview({
        nodeId: node.id,
        dcId: dc.id,
        allowed: true,
        reason: null,
        operations,
      })
      setPendingOperations(operations)
      setPendingChanges([
        {
          nodeId: node.id,
          nodeName: node.data.name,
          dcId: dc.id,
          domainName: dc.data.config?.domainName ?? dc.data.name,
        },
      ])
    },
    [nodes, edges, applyNodeChanges],
  )

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = "copy"
  }, [])

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      if (readOnly || evidenceActive) return
      const typeId = e.dataTransfer.getData(DRAG_TYPE)
      if (!typeId) return
      // Second half of the palette's greying-out — a drag that got started
      // some other way must not place a node this account cannot deploy, or
      // the refusal arrives much later as a 403 on a plan it is part of.
      if (!isAvailable(typeId)) {
        toast.error(
          `${TEMPLATE_BY_ID[typeId]?.label ?? typeId} is not available to your account.`,
        )
        return
      }

      // React Flow positions a node by its top-left corner, so handing it the
      // pointer verbatim drops the card down-right of the cursor and the user
      // aims by guessing at an offset. Centre it instead: the pointer lands in
      // the middle of the card it just placed, which is also what the
      // domain-overlap test below should be asked about.
      const dropped = screenToFlowPosition({ x: e.clientX, y: e.clientY })
      const position = {
        x: dropped.x - DRAFT_NODE_SIZE.width / 2,
        y: dropped.y - DRAFT_NODE_SIZE.height / 2,
      }
      const draftNode: Node<MachineData> = {
        id: "drop-preview",
        type: "machine",
        position,
        data: {
          typeId,
          name: "This unconfigured node",
          lifecycle: "draft",
          poweredOn: false,
        },
      }
      const domain = findDomainOverlappingNode(draftNode, nodes, edges)
      if (domain) {
        toast.error("Configure the node before placing it inside a domain.")
        setDomainDragPreview({
          nodeId: draftNode.id,
          dcId: domain.id,
          allowed: false,
          reason: "Unconfigured nodes cannot be placed inside a domain.",
          operations: [],
        })
        window.setTimeout(() => setDomainDragPreview(null), 700)
        return
      }
      addNode(typeId, position)
    },
    [
      screenToFlowPosition,
      addNode,
      readOnly,
      evidenceActive,
      isAvailable,
      nodes,
      edges,
    ],
  )

  return (
    <div ref={wrapperRef} className="canvas-surface relative flex-1 h-full">
      <ReactFlow
        nodes={visibleNodes}
        edges={visibleEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onConnectStart={onConnectStart}
        onConnectEnd={onConnectEnd}
        isValidConnection={isValidConnection}
        onSelectionChange={onSelectionChange}
        onNodeDragStart={onNodeDragStart}
        onNodeDrag={onNodeDrag}
        onNodeDragStop={onNodeDragStop}
        onDragOver={onDragOver}
        onDrop={onDrop}
        onMoveEnd={onMoveEnd}
        deleteKeyCode={null}
        multiSelectionKeyCode="Shift"
        selectionKeyCode="Shift"
        defaultViewport={initialViewport}
        colorMode={resolvedTheme}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={!readOnly && !evidenceActive}
        nodesConnectable={!readOnly && !evidenceActive}
        // Selection stays live through both locks on purpose: picking a
        // machine is how its inspector is reached, and the inspector is where
        // a deploy is *read* — config, live progress, per-step detail, and the
        // remote desktop of a machine the plan has already finished with. With
        // this off, whatever happened to be selected when Deploy was pressed
        // was the only node inspectable for the length of the run, which on a
        // long plan means the one thing a watching user wants to do is the one
        // thing they cannot. Editing stays locked where it matters —
        // `nodesDraggable`/`nodesConnectable` above, the store's own guards,
        // and every mutating inspector control disabled on `deploying`.
        elementsSelectable={!evidenceActive}
      >
        <DomainRegions preview={domainDragPreview} />
        <Background gap={16} size={1} />
        <Controls />
        <MiniMap zoomable pannable nodeColor={miniMapColor} />
        <Panel position="top-right">
          <div className="flex items-start gap-2">
            <CertificateJourneyToggle
              active={journeyActive}
              available={journeyProjection !== null}
              onToggle={() => {
                setEvidenceActive(false)
                setJourneyActive((active) => !active)
              }}
            />
            <EvidenceModeToggle
              active={evidenceActive}
              available={labEvidence !== null}
              onToggle={() => {
                setJourneyActive(false)
                if (evidenceActive) {
                  setEvidenceActive(false)
                  setEvidenceSnapshot(null)
                } else if (labEvidence) {
                  setEvidenceSnapshot({ nodes, edges, evidence: labEvidence })
                  setEvidenceActive(true)
                }
              }}
            />
          </div>
        </Panel>
        {!journeyActive && !evidenceActive && (
          <Panel position="top-left">
            <TopologyGuidance />
          </Panel>
        )}
        {!journeyActive && !evidenceActive && (
          <Panel position="top-center">
            <ConnectionPreview />
          </Panel>
        )}
        {!journeyActive && !evidenceActive && (
          <Panel position="bottom-center">
            <DomainJoinAction onRequest={requestAccessibleDomainJoin} />
          </Panel>
        )}
        {journeyActive && journeyProjection && (
          <Panel position="bottom-center">
            <CertificateJourneyLens projection={journeyProjection} />
          </Panel>
        )}
        {evidenceActive && evidenceSnapshot && (
          <Panel position="bottom-center">
            <EvidenceModePanel
              nodes={evidenceSnapshot.nodes}
              edges={evidenceSnapshot.edges}
              evidence={evidenceSnapshot.evidence}
            />
          </Panel>
        )}
      </ReactFlow>
      <DomainConfirmDialog
        changes={pendingChanges}
        operations={pendingOperations}
        onConfirm={confirmDomainChanges}
        onCancel={cancelDomainChanges}
      />
      <StagedRemoveDialog
        ops={pendingNodeDelete?.ops ?? null}
        hostNote={pendingNodeDelete?.hostNote}
        onConfirm={confirmNodeDelete}
        onCancel={() => setPendingNodeDelete(null)}
      />
      {deploying ? (
        <div className="pointer-events-none absolute inset-x-0 top-0 z-10 flex justify-center pt-3">
          <div className="pointer-events-auto flex items-center gap-2 rounded-full border bg-background/95 px-3 py-1 text-xs text-muted-foreground shadow-sm">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" />
            Deploying — editing locked, inspector live
          </div>
        </div>
      ) : (
        joinedLab && (
          <div className="pointer-events-none absolute inset-x-0 top-0 z-10 flex justify-center pt-3">
            <div className="pointer-events-auto flex items-center gap-2 rounded-full border bg-background/95 px-3 py-1 text-xs text-muted-foreground shadow-sm">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              Joined lab — select a machine to open its desktop
            </div>
          </div>
        )
      )}
    </div>
  )
}

function miniMapColor(node: Node<MachineData>): string {
  const typeId = (node.data as MachineData).typeId
  switch (typeId) {
    case "domainController":
      return "#3b82f6"
    case "certificateAuthority":
      return "#f59e0b"
    case "webServer":
      return "#10b981"
    case "client":
      return "#a78bfa"
    case "standalone":
      return "#94a3b8"
    case "certsecure":
      return "#06b6d4"
    case "cbom":
      return "#14b8a6"
    case "codesign":
      return "#6366f1"
    default:
      return "#94a3b8"
  }
}
