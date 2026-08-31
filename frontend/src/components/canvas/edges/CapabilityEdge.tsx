import { useId, type CSSProperties } from "react"
import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  getSmoothStepPath,
  type EdgeProps,
} from "@xyflow/react"

import {
  CONNECTION_HEALTH,
  EDGE_TYPE,
  SERVICE_SOCKET,
} from "@/constants/topology"
import type {
  ConnectionHealth,
  ConnectionPort,
  EdgeType,
} from "@/constants/topology"
import type { ServiceHealth } from "@/lib/labEvidence"
import {
  CONNECTION_HEALTH_GUIDANCE,
  CONNECTION_PORT_GUIDANCE,
  connectionGuidance,
  edgeServiceSocket,
} from "@/lib/topology"
import { cn } from "@/lib/utils"

const OFFLINE_RELAY_LABEL_OFFSET_Y = 28

export function CapabilityEdge(props: EdgeProps) {
  const gradientId = `service-health-${useId().replaceAll(":", "")}`
  const markerId = `${gradientId}-arrow`
  const edgeType = props.data?.edgeType as EdgeType | undefined
  if (!edgeType) return null

  const guidance = connectionGuidance(edgeType, {
    rootIssuer: props.data?.rootIssuer === true,
    serviceSocket: edgeServiceSocket(props),
  })
  const [path, labelX, labelY] =
    edgeType === EDGE_TYPE.webServerCert
      ? getBezierPath(props)
      : getSmoothStepPath(props)
  const hasOfflineRelay =
    edgeType === EDGE_TYPE.caHierarchy && props.data?.rootIssuer === true
  // The label pill + details only appear once the edge is clicked (selected),
  // so the canvas stays uncluttered until the user asks for the arrow's story.
  const expanded = props.selected === true
  const health =
    (props.data?.health as ConnectionHealth | undefined) ??
    (props.data?.staged === true
      ? CONNECTION_HEALTH.planned
      : CONNECTION_HEALTH.verified)
  const healthGuidance = CONNECTION_HEALTH_GUIDANCE[health]
  const serviceHealth =
    (props.data?.serviceHealth as ServiceHealth | undefined) ?? {}
  const liveServices = guidance.ports
    .map((port) => ({ port, health: serviceHealth[port] }))
    .filter(
      (item): item is { port: ConnectionPort; health: ConnectionHealth } =>
        !!item.health,
    )
  const liveProbe = liveServices.length > 0
  // The stroke carries the *relationship* — amber CA issuance, emerald CDP/AIA
  // publication, violet OCSP — and health is layered onto that, never swapped
  // for it. Repainting a live-probed edge with one "verified" emerald erased
  // the coding at exactly the moment the graph was fully working: every line in
  // a healthy lab came out the same green, so the picture stopped saying which
  // relationship was which. `verified` therefore renders as the edge's own
  // colour, lit up by `healthyGlow`, and only the states that want attention
  // take the stroke over.
  const baseColor =
    edgeType === EDGE_TYPE.caHierarchy
      ? "#f59e0b"
      : edgeServiceSocket(props) === SERVICE_SOCKET.ocsp
        ? "#8b5cf6"
        : typeof props.style?.stroke === "string"
          ? props.style.stroke
          : "#10b981"
  /** Status palette, for the places that name the state in words beside it. */
  const healthColor = (state: ConnectionHealth) => {
    switch (state) {
      case CONNECTION_HEALTH.planned:
        return "#38bdf8"
      case CONNECTION_HEALTH.applying:
        return "#8b5cf6"
      case CONNECTION_HEALTH.verified:
        return "#10b981"
      case CONNECTION_HEALTH.degraded:
        return "#f59e0b"
      case CONNECTION_HEALTH.broken:
        return "#ef4444"
    }
  }
  /** Stroke palette: a verified service defers to the edge's own colour. */
  const strokeColor = (state: ConnectionHealth) =>
    state === CONNECTION_HEALTH.verified ? baseColor : healthColor(state)
  // Because verified no longer changes hue, the evidence that the probes came
  // back clean has to read some other way: the live-probe branch below already
  // thickens the line, and this gives it a faint halo in its own colour.
  const healthyGlow: CSSProperties =
    liveProbe &&
    liveServices.every((item) => item.health === CONNECTION_HEALTH.verified)
      ? { filter: `drop-shadow(0 0 3px ${baseColor})` }
      : {}
  const pathStyle: CSSProperties = {
    ...props.style,
    ...(health === CONNECTION_HEALTH.applying
      ? { strokeDasharray: "7 4", opacity: 1 }
      : health === CONNECTION_HEALTH.degraded
        ? {
            stroke: healthColor(CONNECTION_HEALTH.degraded),
            strokeDasharray: "4 4",
            opacity: 1,
          }
        : health === CONNECTION_HEALTH.broken
          ? {
              stroke: healthColor(CONNECTION_HEALTH.broken),
              strokeWidth: 2.5,
              opacity: 1,
            }
          : {}),
    ...(liveProbe
      ? { stroke: `url(#${gradientId})`, strokeWidth: 3, opacity: 1 }
      : {}),
    ...healthyGlow,
    ...(edgeType === EDGE_TYPE.webServerCert && props.data?.rootIssuer === true
      ? { strokeDasharray: "1 6", strokeLinecap: "round" }
      : {}),
  }
  const arrowColor =
    liveServices.length > 0
      ? strokeColor(liveServices[liveServices.length - 1].health)
      : health === CONNECTION_HEALTH.broken ||
          health === CONNECTION_HEALTH.degraded ||
          health === CONNECTION_HEALTH.applying
        ? healthColor(health)
        : baseColor

  return (
    <>
      <defs>
        <marker
          id={markerId}
          markerWidth="7"
          markerHeight="7"
          refX="6.3"
          refY="3.5"
          orient="auto"
          markerUnits="strokeWidth"
        >
          <path d="M 0 0 L 7 3.5 L 0 7 z" fill={arrowColor} />
        </marker>
        {liveProbe && (
          <linearGradient
            id={gradientId}
            gradientUnits="userSpaceOnUse"
            x1={props.sourceX}
            y1={props.sourceY}
            x2={props.targetX}
            y2={props.targetY}
          >
            {liveServices.flatMap((service, index) => {
              const start = `${(index / liveServices.length) * 100}%`
              const end = `${((index + 1) / liveServices.length) * 100}%`
              const color = strokeColor(service.health)
              return [
                <stop
                  key={`${service.port}-start`}
                  offset={start}
                  stopColor={color}
                />,
                <stop
                  key={`${service.port}-end`}
                  offset={end}
                  stopColor={color}
                />,
              ]
            })}
          </linearGradient>
        )}
      </defs>
      <BaseEdge
        id={props.id}
        path={path}
        markerEnd={`url(#${markerId})`}
        style={pathStyle}
      />
      {hasOfflineRelay && (
        <g
          className="offline-relay-package pointer-events-none"
          aria-hidden="true"
        >
          <title>Sealed certificate request and signed certificate relay</title>
          <rect
            x="-9"
            y="-7"
            width="18"
            height="14"
            rx="3"
            fill="#1c1917"
            stroke="#fbbf24"
            strokeWidth="1.5"
          />
          <path
            d="M -7 -4 L 0 1 L 7 -4 M -7 5 L -2 0 M 7 5 L 2 0"
            fill="none"
            stroke="#fde68a"
            strokeWidth="1"
          />
          <animateMotion
            dur="4s"
            repeatCount="indefinite"
            path={path}
            keyPoints="0;1;0"
            keyTimes="0;0.5;1"
            calcMode="linear"
          />
        </g>
      )}
      {expanded && (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan absolute z-10"
            style={{
              // Leave the path clear for the sealed relay package animation.
              // Other edge labels stay centered on their paths as before.
              transform: `translate(-50%, -50%) translate(${labelX}px, ${
                labelY + (hasOfflineRelay ? OFFLINE_RELAY_LABEL_OFFSET_Y : 0)
              }px)`,
            }}
          >
            <button
              type="button"
              aria-expanded={expanded}
              aria-label={`${guidance.intent}. Connection requirements.`}
              className={cn(
                "flex max-w-64 items-center gap-1.5 rounded-full border bg-background/95 px-2 py-1 text-[10px] font-semibold shadow-sm",
                "transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                props.selected && "ring-2 ring-ring",
              )}
            >
              <span className="truncate">{guidance.intent}</span>
              <span
                className={cn(
                  "shrink-0 rounded-full px-1.5 py-0.5 text-[9px] uppercase tracking-wide",
                  health === CONNECTION_HEALTH.planned &&
                    "bg-sky-500/15 text-sky-500",
                  health === CONNECTION_HEALTH.applying &&
                    "bg-violet-500/15 text-violet-500",
                  health === CONNECTION_HEALTH.verified &&
                    "bg-emerald-500/15 text-emerald-500",
                  health === CONNECTION_HEALTH.degraded &&
                    "bg-amber-500/15 text-amber-500",
                  health === CONNECTION_HEALTH.broken &&
                    "bg-red-500/15 text-red-500",
                )}
              >
                {liveProbe ? "Live probes" : healthGuidance.label}
              </span>
            </button>

            {expanded && (
              <div className="absolute left-1/2 top-full mt-2 w-80 -translate-x-1/2 rounded-lg border bg-popover p-3 text-popover-foreground shadow-xl">
                <p className="text-[11px] font-semibold">Capabilities</p>
                <div className="mt-1.5 space-y-1.5">
                  {guidance.ports.map((port) => {
                    const item = CONNECTION_PORT_GUIDANCE[port]
                    return (
                      <div key={port} className="text-[10px] leading-snug">
                        <span className="font-medium">{item.label}:</span>{" "}
                        <span className="text-muted-foreground">
                          {item.capabilities.join(" · ")}
                        </span>
                      </div>
                    )
                  })}
                </div>

                <p className="mt-3 text-[11px] font-semibold">Health</p>
                {liveProbe ? (
                  <div className="mt-1.5 space-y-1.5">
                    {guidance.ports.map((port) => {
                      const item = CONNECTION_PORT_GUIDANCE[port]
                      const state = serviceHealth[port] ?? health
                      return (
                        <div
                          key={port}
                          className="flex items-start gap-2 text-[10px]"
                        >
                          <span
                            className="mt-1 h-2 w-2 shrink-0 rounded-full"
                            style={{ backgroundColor: healthColor(state) }}
                          />
                          <span className="min-w-0">
                            <span className="font-medium">{item.label}</span>{" "}
                            <span className="text-muted-foreground">
                              · {CONNECTION_HEALTH_GUIDANCE[state].label} ·{" "}
                              {item.capabilities.join(" / ")}
                            </span>
                          </span>
                        </div>
                      )
                    })}
                  </div>
                ) : (
                  <p className="mt-1 text-[10px] text-muted-foreground">
                    {healthGuidance.label}: {healthGuidance.detail}
                  </p>
                )}

                <p className="mt-3 text-[11px] font-semibold">Requirements</p>
                <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[10px] text-muted-foreground">
                  {guidance.requirements.map((requirement) => (
                    <li key={requirement}>{requirement}</li>
                  ))}
                </ul>

                <p className="mt-3 text-[11px] font-semibold">
                  Generated operations
                </p>
                <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[10px] text-muted-foreground">
                  {guidance.operations.map((operation) => (
                    <li key={operation}>{operation}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  )
}
