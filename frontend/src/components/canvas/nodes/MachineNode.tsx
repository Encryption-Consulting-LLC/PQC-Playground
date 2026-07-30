import { Fragment, useEffect } from "react"
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  FileText,
  Loader2,
  Radio,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react"
import {
  Handle,
  Position,
  useEdges,
  useNodes,
  useUpdateNodeInternals,
} from "@xyflow/react"
import type { NodeProps, Node } from "@xyflow/react"
import { cn } from "@/lib/utils"
import { TEMPLATE_BY_ID } from "@/constants/templates"
import { EDGE_TYPE, LIFECYCLE, SERVICE_SOCKET } from "@/constants/topology"
import type { Lifecycle, ServiceSocket } from "@/constants/topology"
import {
  SERVICE_SOCKET_GUIDANCE,
  canConnectServiceSockets,
  caTier,
  caDepth,
  domainMembership,
  driftedFields,
  isConnectable,
  isDeployed,
  serviceSocketHandleId,
  serviceSocketsForNode,
} from "@/lib/topology"
import { findLabEvidence, nodeHealthWarning } from "@/lib/labEvidence"
import { OP_KIND, OP_STATUS } from "@/lib/staging"
import { useAgentConnected } from "@/hooks/useAgentConnected"
import { isPreExecutionPhase, useStagingStore } from "@/store/staging"
import { useTopologyStore } from "@/store/topology"
import { useConnectionGestureStore } from "@/store/connectionGesture"
import type { MachineData } from "@/store/topology"
import { Badge } from "@/components/ui/badge"
import { ProgressBar } from "./ProgressBar"

const MACHINE_NODE_WIDTH = 304
const MACHINE_NODE_MIN_HEIGHT = 160
const BOTTOM_SOCKET_GUTTER = 28
const DRAFT_NODE_HEIGHT = 92
const SOCKET_ROW_HEIGHT = 24
const COMPACT_CARD_SIDE_SOCKET_ROWS = 2

const SOCKET_APPEARANCE: Record<
  ServiceSocket,
  { icon: LucideIcon; dotClassName: string; iconClassName: string }
> = {
  [SERVICE_SOCKET.issuance]: {
    icon: ShieldCheck,
    dotClassName: "!bg-amber-500",
    iconClassName: "text-amber-500",
  },
  [SERVICE_SOCKET.publication]: {
    icon: FileText,
    dotClassName: "!bg-emerald-500",
    iconClassName: "text-emerald-500",
  },
  [SERVICE_SOCKET.ocsp]: {
    icon: Radio,
    dotClassName: "!bg-violet-500",
    iconClassName: "text-violet-500",
  },
}

function socketPlacement(socket: ServiceSocket, type: "source" | "target") {
  if (socket === SERVICE_SOCKET.issuance) {
    return type === "source"
      ? {
          position: Position.Bottom,
          handleStyle: { left: "50%" },
          labelStyle: { bottom: 8, left: "50%", transform: "translateX(-50%)" },
          labelClassName: "justify-center",
        }
      : {
          position: Position.Left,
          handleStyle: { top: 72 },
          labelStyle: { left: 3, top: 72, transform: "translateY(-50%)" },
          labelClassName: "justify-start",
        }
  }
  const top =
    socket === SERVICE_SOCKET.publication
      ? 72
      : socket === SERVICE_SOCKET.ocsp
        ? 96
        : 120
  return type === "source"
    ? {
        position: Position.Right,
        handleStyle: { top },
        labelStyle: { right: 3, top, transform: "translateY(-50%)" },
        labelClassName: "flex-row-reverse justify-start text-right",
      }
    : {
        position: Position.Left,
        handleStyle: { top },
        labelStyle: { left: 3, top, transform: "translateY(-50%)" },
        labelClassName: "justify-start",
      }
}

function machineNodeHeight(
  socketSpecs: Array<{ socket: ServiceSocket; type: "source" | "target" }>,
  isDraft: boolean,
): number {
  if (isDraft) return DRAFT_NODE_HEIGHT
  const hasBottomSocket = socketSpecs.some(
    (spec) => spec.socket === SERVICE_SOCKET.issuance && spec.type === "source",
  )
  const sideSocketRows = new Set(
    socketSpecs
      .filter(
        (spec) =>
          !(spec.socket === SERVICE_SOCKET.issuance && spec.type === "source"),
      )
      .map((spec) => socketPlacement(spec.socket, spec.type).handleStyle.top),
  ).size
  return (
    MACHINE_NODE_MIN_HEIGHT +
    (hasBottomSocket ? BOTTOM_SOCKET_GUTTER : 0) +
    Math.max(0, sideSocketRows - COMPACT_CARD_SIDE_SOCKET_ROWS) *
      SOCKET_ROW_HEIGHT
  )
}

function LifecycleBadge({
  lifecycle,
  preparing,
}: {
  lifecycle: Lifecycle
  /** True while a deploy including this node is pre-execution (preflight/queue/worker setup) — the node hasn't flipped to `deploying` yet, but "staged" would read as idle. */
  preparing?: boolean
}) {
  if (preparing && lifecycle === LIFECYCLE.staged)
    return (
      <Badge
        variant="secondary"
        className="flex items-center gap-1 text-[10px] text-sky-500 border-sky-500/30"
      >
        <Loader2 className="h-2.5 w-2.5 animate-spin" />
        preparing…
      </Badge>
    )
  if (lifecycle === LIFECYCLE.draft)
    return (
      <Badge
        variant="secondary"
        className="flex items-center gap-1 text-[10px] text-amber-500 border-amber-500/30"
      >
        <AlertTriangle className="h-2.5 w-2.5" />
        draft
      </Badge>
    )
  if (lifecycle === LIFECYCLE.staged)
    return (
      <Badge
        variant="secondary"
        className="flex items-center gap-1 text-[10px] text-sky-500 border-sky-500/30"
      >
        <Clock className="h-2.5 w-2.5" />
        staged
      </Badge>
    )
  if (lifecycle === LIFECYCLE.deploying)
    return (
      <Badge
        variant="secondary"
        className="flex items-center gap-1 text-[10px] text-muted-foreground"
      >
        <Loader2 className="h-2.5 w-2.5 animate-spin" />
        deploying…
      </Badge>
    )
  if (lifecycle === LIFECYCLE.provisioning)
    return (
      <Badge
        variant="secondary"
        className="flex items-center gap-1 text-[10px] text-emerald-500 border-emerald-500/30"
      >
        <Loader2 className="h-2.5 w-2.5 animate-spin" />
        provisioning
      </Badge>
    )
  if (lifecycle === LIFECYCLE.failed)
    return (
      <Badge
        variant="secondary"
        className="flex items-center gap-1 text-[10px] text-red-500 border-red-500/30"
      >
        <AlertTriangle className="h-2.5 w-2.5" />
        failed
      </Badge>
    )
  if (lifecycle === LIFECYCLE.destroying)
    return (
      <Badge
        variant="secondary"
        className="flex items-center gap-1 text-[10px] text-red-500 border-red-500/30"
      >
        <Loader2 className="h-2.5 w-2.5 animate-spin" />
        removing…
      </Badge>
    )
  if (lifecycle === LIFECYCLE.drifted)
    return (
      <Badge
        variant="secondary"
        className="flex items-center gap-1 text-[10px] text-orange-500 border-orange-500/30"
      >
        <AlertTriangle className="h-2.5 w-2.5" />
        drifted
      </Badge>
    )
  return (
    <Badge
      variant="secondary"
      className="flex items-center gap-1 text-[10px] text-emerald-500 border-emerald-500/30"
    >
      <CheckCircle2 className="h-2.5 w-2.5" />
      deployed
    </Badge>
  )
}

/**
 * Executor status across the node's whole lifecycle: grey before a real
 * VM exists, then green/red from the live agent-presence feed. `vmName` is the
 * durable signal that deployment produced a VM, including authored-ISO VMs
 * that do not have an executor identity to query.
 */
function AgentStatusDot({
  vmId,
  deployed,
}: {
  vmId?: string
  deployed: boolean
}) {
  const connected = useAgentConnected(vmId)
  const title = !deployed
    ? "Not yet deployed"
    : connected
      ? "Executor connected"
      : "Executor offline"
  return (
    <span
      title={title}
      aria-label={title}
      className={cn(
        "h-2 w-2 shrink-0 rounded-full",
        !deployed
          ? "bg-slate-400"
          : connected
            ? "bg-emerald-500 shadow-[0_0_6px_1px_rgba(16,185,129,0.7)]"
            : "bg-red-500 shadow-[0_0_6px_1px_rgba(239,68,68,0.6)]",
      )}
    />
  )
}

interface NodeFact {
  label: string
  value: string
}

function compactFacts({
  data,
  tier,
  depth,
  domain,
  memberCount,
}: {
  data: MachineData
  tier: ReturnType<typeof caTier> | null
  depth: number | null
  domain: string | null
  memberCount: number | null
}): [NodeFact, NodeFact] {
  if (data.typeId === "domainController") {
    return [
      { label: "Forest", value: data.config?.forestLevel ?? "Pending" },
      { label: "Members", value: String(memberCount ?? 0) },
    ]
  }
  if (data.typeId === "certificateAuthority") {
    if (tier === "root") {
      return [
        { label: "Trust tier", value: "Root" },
        { label: "Isolation", value: "Air-gapped" },
      ]
    }
    return [
      {
        label: "Trust tier",
        value:
          tier === "standalone"
            ? "Standalone"
            : `T${depth ?? 1} ${tier ?? "CA"}`,
      },
      { label: "Domain", value: domain ?? "Not joined" },
    ]
  }
  return [
    {
      label: "Endpoint",
      value: isDeployed(data) && data.ip ? data.ip : "Pending",
    },
    { label: "Domain", value: domain ?? "Not joined" },
  ]
}

export function MachineNode({
  id,
  data,
  selected,
}: NodeProps<Node<MachineData>>) {
  const def = TEMPLATE_BY_ID[data.typeId]
  const nodes = useNodes<Node<MachineData>>()
  const edges = useEdges()
  const isOverlapping = useTopologyStore((s) => s.overlapNodeId === id)
  // Pre-execution echo: this node's clone is in the in-flight plan but no op
  // has started yet (route preflight / worker queue / worker setup).
  const preparingDeploy = useStagingStore(
    (s) =>
      isPreExecutionPhase(s.planPhase) &&
      s.ops.some(
        (op) =>
          op.kind === OP_KIND.createVm &&
          op.targetNodeId === id &&
          op.status === OP_STATUS.pending,
      ),
  )
  const gesture = useConnectionGestureStore((s) => s.gesture)
  const hoverTarget = useConnectionGestureStore((s) => s.hoverTarget)
  const updateNodeInternals = useUpdateNodeInternals()

  // Derived chips — only meaningful once a node can carry real edges.
  const showDerived = isConnectable(data)
  const tier =
    showDerived && data.typeId === "certificateAuthority"
      ? caTier(id, edges)
      : null
  const depth =
    tier !== null && tier !== "root" && tier !== "standalone"
      ? caDepth(id, edges)
      : null
  const domain = showDerived ? domainMembership(id, edges, nodes) : null
  const memberCount =
    showDerived && data.typeId === "domainController"
      ? edges.filter(
          (e) => e.target === id && e.data?.edgeType === EDGE_TYPE.domainJoin,
        ).length
      : null
  const evidence = findLabEvidence(nodes)
  const evidenceWarning = nodeHealthWarning(
    { id, data, position: { x: 0, y: 0 } },
    evidence,
  )
  const driftFields = driftedFields(data)
  const warning =
    evidenceWarning ??
    (data.lifecycle === LIFECYCLE.failed
      ? (data.errorDetail ?? data.phase ?? "Deployment failed")
      : null) ??
    (driftFields.length > 0 ? "Configuration changed since deploy" : null)
  const facts = compactFacts({ data, tier, depth, domain, memberCount })
  const activePhase =
    data.lifecycle === LIFECYCLE.deploying ||
    data.lifecycle === LIFECYCLE.destroying

  const Icon = def?.icon ?? AlertTriangle
  const socketSpecs = serviceSocketsForNode(
    { id, data, position: { x: 0, y: 0 } },
    edges,
  )
  const socketLayoutKey = socketSpecs
    .map((spec) => serviceSocketHandleId(spec.socket, spec.type))
    .join("|")
  const hasBottomSocket = socketSpecs.some(
    (spec) => spec.socket === SERVICE_SOCKET.issuance && spec.type === "source",
  )
  useEffect(() => {
    updateNodeInternals(id)
  }, [id, socketLayoutKey, updateNodeInternals])
  const socketCompatibility = (socket: ServiceSocket) => {
    if (!gesture) return null
    return canConnectServiceSockets(
      {
        source: gesture.sourceNodeId,
        sourceHandle: gesture.sourceHandleId,
        target: id,
        targetHandle: serviceSocketHandleId(socket, "target"),
      },
      nodes,
      edges,
    )
  }
  const compatibleDestination =
    gesture &&
    gesture.sourceNodeId !== id &&
    socketSpecs.some(
      (spec) => spec.type === "target" && socketCompatibility(spec.socket)?.ok,
    )
  const dimmedByGesture =
    gesture && gesture.sourceNodeId !== id && !compatibleDestination
  const socketsConnectable = isConnectable(data)
  const isDraft = data.lifecycle === LIFECYCLE.draft
  const nodeHeight = machineNodeHeight(socketSpecs, isDraft)

  return (
    <div
      style={{
        width: MACHINE_NODE_WIDTH,
        minWidth: MACHINE_NODE_WIDTH,
        maxWidth: MACHINE_NODE_WIDTH,
        height: nodeHeight,
      }}
      onAnimationEnd={(event) => {
        if (event.animationName === "trust-gravity-settle")
          updateNodeInternals(id)
      }}
      onTransitionEnd={(event) => {
        if (
          event.target === event.currentTarget &&
          event.propertyName === "height"
        ) {
          updateNodeInternals(id)
        }
      }}
      className={cn(
        "group/node relative overflow-visible rounded-xl border bg-card text-card-foreground shadow-sm select-none",
        "transition-[height,box-shadow,opacity] duration-300 ease-out",
        tier === "root" && "trust-body trust-body-root",
        tier === "intermediate" && "trust-body trust-body-intermediate",
        tier === "issuing" && "trust-body trust-body-issuing",
        compatibleDestination &&
          "ring-2 ring-emerald-400 shadow-[0_0_22px_5px_rgba(52,211,153,0.28)]",
        dimmedByGesture && "opacity-35 saturate-50",
        selected && "ring-2 ring-primary shadow-md",
        data.lifecycle === LIFECYCLE.draft && "border-amber-500/40",
        data.lifecycle === LIFECYCLE.staged &&
          "border-sky-500/40 border-dashed opacity-80",
        data.lifecycle === LIFECYCLE.deploying && "border-muted",
        data.lifecycle === LIFECYCLE.provisioning &&
          "border-emerald-500/30 border-dashed",
        data.lifecycle === LIFECYCLE.deployed && "border-border",
        data.lifecycle === LIFECYCLE.drifted && "border-orange-500/40",
        data.lifecycle === LIFECYCLE.failed && "border-red-500/50",
        data.lifecycle === LIFECYCLE.destroying &&
          "border-red-500/40 opacity-70",
        !isOverlapping &&
          memberCount !== null &&
          memberCount > 0 &&
          "border-sky-500/60 shadow-[0_0_18px_4px_rgba(14,165,233,0.35)] " +
            "dark:shadow-[0_0_20px_5px_rgba(56,189,248,0.55)]",
        !isOverlapping &&
          data.typeId === "certificateAuthority" &&
          domain !== null &&
          "border-amber-500/60 shadow-[0_0_18px_4px_rgba(245,158,11,0.35)] " +
            "dark:shadow-[0_0_20px_5px_rgba(251,191,36,0.55)]",
        // Overlap warning takes precedence over selection/lifecycle styling.
        isOverlapping &&
          "border-red-500 bg-red-500/40 opacity-70 ring-2 ring-red-500/40",
      )}
    >
      {socketSpecs.map((spec) => {
        const handleId = serviceSocketHandleId(spec.socket, spec.type)
        const placement = socketPlacement(spec.socket, spec.type)
        const compatibility =
          spec.type === "target" ? socketCompatibility(spec.socket) : null
        const visible = gesture
          ? gesture.sourceNodeId === id
            ? spec.type === "source"
            : spec.type === "target" && compatibility?.ok === true
          : true
        const appearance = SOCKET_APPEARANCE[spec.socket]
        const SocketIcon = appearance.icon
        const guidance = SERVICE_SOCKET_GUIDANCE[spec.socket]
        return (
          <Fragment key={handleId}>
            <Handle
              id={handleId}
              type={spec.type}
              position={placement.position}
              style={placement.handleStyle}
              isConnectableStart={socketsConnectable && spec.type === "source"}
              isConnectableEnd={socketsConnectable && spec.type === "target"}
              title={`${guidance.label} · ${guidance.intent}`}
              aria-label={`${guidance.label} socket: ${guidance.intent}`}
              tabIndex={visible && socketsConnectable ? 0 : -1}
              onMouseEnter={() => {
                if (gesture && spec.type === "target") hoverTarget(id, handleId)
              }}
              onMouseLeave={() => {
                if (gesture && spec.type === "target") hoverTarget()
              }}
              className={cn(
                // Keep the compact 12px dot, but give it a 24px interaction
                // target. Root CAs expose only output sockets, so shrinking
                // the Handle itself to the visible dot made every relationship
                // drag needlessly difficult to start.
                "service-socket machine-node-service-reveal !z-20 !flex !h-6 !w-6 !items-center !justify-center",
                "!rounded-full !border-0 !bg-transparent !shadow-none",
                socketsConnectable ? "cursor-crosshair" : "cursor-default",
                "transition-opacity duration-150 focus-visible:!outline-none focus-visible:!ring-2 focus-visible:!ring-ring",
                visible ? "!opacity-100" : "pointer-events-none !opacity-0",
              )}
            >
              <span
                aria-hidden="true"
                className={cn(
                  "pointer-events-none h-3 w-3 rounded-full shadow-sm",
                  appearance.dotClassName,
                )}
              />
            </Handle>
            <span
              aria-hidden="true"
              style={placement.labelStyle}
              className={cn(
                "machine-node-service-reveal pointer-events-none absolute z-10 flex max-w-[132px] items-center gap-1 rounded bg-card/95 px-1 py-1",
                "text-[10px] font-medium leading-none text-muted-foreground transition-opacity duration-150",
                !gesture || visible ? "opacity-100" : "opacity-0",
                placement.labelClassName,
              )}
            >
              <SocketIcon
                className={cn("h-3 w-3 shrink-0", appearance.iconClassName)}
              />
              <span className="truncate">{guidance.label}</span>
            </span>
          </Fragment>
        )
      })}

      {/* Header */}
      <div
        className={cn(
          "flex h-13 items-center gap-3 rounded-t-xl border-b px-5 py-3",
          "bg-muted/40",
        )}
      >
        {def?.logo ? (
          <img
            src={def.logo}
            alt=""
            className="h-5 w-5 shrink-0"
            draggable={false}
          />
        ) : (
          <Icon
            className={cn(
              "h-4 w-4 shrink-0",
              def?.accent ?? "text-muted-foreground",
            )}
          />
        )}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-semibold">
            {data.name}
          </span>
          <span className="block truncate text-[9px] text-muted-foreground">
            {def?.label ?? data.typeId}
          </span>
        </span>
        <LifecycleBadge
          lifecycle={data.lifecycle}
          preparing={preparingDeploy}
        />
        {!activePhase &&
          (warning ? (
            <span
              className="flex shrink-0"
              title={warning}
              aria-label={warning}
            >
              <AlertTriangle
                aria-hidden="true"
                className="h-3.5 w-3.5 text-red-500"
              />
            </span>
          ) : (
            <span
              className="flex shrink-0"
              title="No active warnings"
              aria-label="No active warnings"
            >
              <CheckCircle2
                aria-hidden="true"
                className="h-3.5 w-3.5 text-emerald-500"
              />
            </span>
          ))}
        <AgentStatusDot
          vmId={data.executorVmId}
          deployed={Boolean(data.vmName || data.executorVmId)}
        />
      </div>

      {isDraft ? (
        <div aria-hidden="true" className="h-10" />
      ) : (
        <>
          {/* Keep the service-socket lanes clear. Lifecycle and warning state
              live in the header; only active deployment progress uses them. */}
          <div className="machine-node-reveal px-5 pt-2">
            <div className="flex h-12 min-w-0 items-center">
              {activePhase && (
                <div className="mx-auto w-36 min-w-0">
                  <span
                    className="block min-w-0 truncate text-center text-[10px] leading-tight text-muted-foreground"
                    title={data.phase ?? "Starting"}
                  >
                    {data.phase ?? "Starting"}
                  </span>
                  <ProgressBar pct={data.progress ?? 0} />
                </div>
              )}
              {/* Failure reasons must be readable at a glance, not only via
                  the header icon's hover tooltip. */}
              {!activePhase &&
                data.lifecycle === LIFECYCLE.failed &&
                warning && (
                  <p
                    className="mx-auto line-clamp-2 min-w-0 text-center text-[10px] leading-tight text-red-500"
                    title={warning}
                  >
                    {warning}
                  </p>
                )}
            </div>

            <dl className="grid grid-cols-2 gap-4 border-t pt-2">
              {facts.map((fact) => (
                <div key={fact.label} className="min-w-0">
                  <dt className="truncate text-[9px] uppercase tracking-wide text-muted-foreground">
                    {fact.label}
                  </dt>
                  <dd
                    className="truncate text-[10px] font-medium"
                    title={fact.value}
                  >
                    {fact.value}
                  </dd>
                </div>
              ))}
            </dl>
          </div>

          {hasBottomSocket && (
            <div
              aria-hidden="true"
              className="machine-node-reveal absolute inset-x-5 bottom-9 border-t"
            />
          )}
        </>
      )}
    </div>
  )
}
