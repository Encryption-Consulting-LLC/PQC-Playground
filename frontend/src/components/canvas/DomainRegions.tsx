import { ViewportPortal } from "@xyflow/react"

import { CONNECTION_HEALTH, EDGE_TYPE, LIFECYCLE } from "@/constants/topology"
import type { ConnectionHealth } from "@/constants/topology"
import {
  domainLabel,
  domainJoinEdge,
  domainRadius,
  domainRegionSummary,
  isConnectable,
  nodeCenter,
} from "@/lib/topology"
import { useTopologyStore } from "@/store/topology"

export interface DomainDragPreview {
  nodeId: string
  dcId: string
  allowed: boolean
  reason: string | null
  operations: string[]
}

const HEALTH_COLOR: Record<ConnectionHealth, string> = {
  [CONNECTION_HEALTH.planned]: "#38bdf8",
  [CONNECTION_HEALTH.applying]: "#8b5cf6",
  [CONNECTION_HEALTH.verified]: "#10b981",
  [CONNECTION_HEALTH.degraded]: "#f59e0b",
  [CONNECTION_HEALTH.broken]: "#ef4444",
}

/** A domain boundary that exposes forest state, member gravity, and service reach. */
export function DomainRegions({
  preview,
}: {
  preview?: DomainDragPreview | null
}) {
  const nodes = useTopologyStore((state) => state.nodes)
  const edges = useTopologyStore((state) => state.edges)
  const domains = nodes.filter(
    (node) =>
      node.data.typeId === "domainController" &&
      // `isConnectable` excludes `deploying` — correct for edge validation, but
      // it's the whole window in which the forest is being installed, so the
      // circle would vanish for exactly the minutes it matters most. Accepted
      // here only; the pulse below is what marks it as not-yet-real.
      (isConnectable(node.data) || node.data.lifecycle === LIFECYCLE.deploying),
  )

  if (domains.length === 0) return null

  return (
    <ViewportPortal>
      <div
        aria-hidden="true"
        className="pointer-events-none absolute left-0 top-0 -z-10"
      >
        {domains.map((dc) => {
          const center = nodeCenter(dc)
          const activePreview = preview?.dcId === dc.id ? preview : null
          const previewEdges =
            activePreview?.allowed &&
            !edges.some(
              (edge) =>
                edge.source === activePreview.nodeId &&
                edge.target === dc.id &&
                edge.data?.edgeType === EDGE_TYPE.domainJoin,
            )
              ? [
                  ...edges.filter(
                    (edge) =>
                      !(
                        edge.source === activePreview.nodeId &&
                        edge.data?.edgeType === EDGE_TYPE.domainJoin
                      ),
                  ),
                  domainJoinEdge(activePreview.nodeId, dc.id, true),
                ]
              : edges
          const radius = domainRadius(dc, nodes, previewEdges)
          const summary = domainRegionSummary(dc, edges)
          const rimColor = activePreview
            ? activePreview.allowed
              ? HEALTH_COLOR[CONNECTION_HEALTH.verified]
              : HEALTH_COLOR[CONNECTION_HEALTH.broken]
            : HEALTH_COLOR[summary.domainHealth]
          const pending =
            dc.data.lifecycle === LIFECYCLE.staged ||
            dc.data.lifecycle === LIFECYCLE.provisioning
          // The forest is being built right now: clone running, or booted and
          // working through `dc.install_forest`. Pulses until the node is
          // deployed, then holds steady.
          const installing =
            dc.data.lifecycle === LIFECYCLE.deploying ||
            dc.data.lifecycle === LIFECYCLE.provisioning
          const members = edges
            .filter(
              (edge) =>
                edge.target === dc.id &&
                edge.data?.edgeType === EDGE_TYPE.domainJoin,
            )
            .map((edge) => nodes.find((node) => node.id === edge.source))
            .filter((node) => !!node)

          return (
            <div
              key={dc.id}
              // One `animation` wins per element, so these stay a single
              // ternary: a live drag preview reads over an install, which reads
              // over the idle empty-domain breathe.
              className={
                activePreview
                  ? activePreview.allowed
                    ? "domain-region-accepting"
                    : "domain-region-rejecting"
                  : installing
                    ? "domain-region-installing"
                    : summary.memberCount === 0
                      ? "domain-region-empty"
                      : undefined
              }
              style={{
                position: "absolute",
                left: center.x,
                top: center.y,
                width: radius * 2,
                height: radius * 2,
                transform: "translate(-50%, -50%)",
                borderRadius: "9999px",
                border: `2px ${pending ? "dashed" : "solid"} ${rimColor}99`,
                background: `radial-gradient(circle at 50% 50%, ${rimColor}05 0%, ${rimColor}10 72%, ${rimColor}18 100%)`,
                boxShadow: `inset 0 0 90px ${rimColor}18, 0 0 28px ${rimColor}12`,
                transition:
                  "width 600ms cubic-bezier(0.34, 1.56, 0.64, 1), height 600ms cubic-bezier(0.34, 1.56, 0.64, 1), border-color 240ms ease, box-shadow 240ms ease",
              }}
            >
              {members.map((member) => {
                const memberCenter = nodeCenter(member)
                return (
                  <span
                    key={member.id}
                    className="domain-member-well"
                    style={{
                      left: radius + memberCenter.x - center.x,
                      top: radius + memberCenter.y - center.y,
                      background: `radial-gradient(circle, ${rimColor}24 0%, ${rimColor}0d 38%, transparent 72%)`,
                    }}
                  />
                )
              })}

              <div
                className="absolute left-1/2 top-0 flex -translate-x-1/2 -translate-y-1/2 items-center gap-1.5 rounded-full border bg-background/95 px-2.5 py-1 text-[10px] font-semibold shadow-sm"
                style={{ color: rimColor, borderColor: `${rimColor}70` }}
              >
                <span
                  className="h-1.5 w-1.5 rounded-full"
                  style={{ background: rimColor }}
                />
                {activePreview
                  ? activePreview.allowed
                    ? `Join ${domainLabel(dc)}?`
                    : "Domain rejects this node"
                  : `${domainLabel(dc)} · ${summary.domainHealth}`}
              </div>

              {activePreview && (
                <div
                  className="absolute left-5 top-1/2 w-56 -translate-y-1/2 rounded-lg border bg-background/95 p-2.5 text-[9px] shadow-lg"
                  style={{ color: rimColor, borderColor: `${rimColor}70` }}
                >
                  {activePreview.allowed ? (
                    <>
                      <p className="mb-1 font-semibold text-foreground">
                        Domain join preview
                      </p>
                      <ol className="space-y-0.5 font-mono">
                        {activePreview.operations.map((operation, index) => (
                          <li key={operation}>
                            {index + 1}. {operation}
                          </li>
                        ))}
                      </ol>
                    </>
                  ) : (
                    <p className="font-medium leading-snug">
                      {activePreview.reason}
                    </p>
                  )}
                </div>
              )}

              <div className="absolute bottom-0 left-1/2 flex -translate-x-1/2 translate-y-1/2 items-center gap-1.5 whitespace-nowrap">
                <span className="rounded-full border bg-background/95 px-2 py-1 text-[9px] font-medium text-foreground shadow-sm">
                  {summary.memberCount}{" "}
                  {summary.memberCount === 1 ? "member" : "members"}
                </span>
                <span
                  className="rounded-full border bg-background/95 px-2 py-1 text-[9px] font-medium shadow-sm"
                  style={{
                    color: HEALTH_COLOR[summary.forestHealth],
                    borderColor: `${HEALTH_COLOR[summary.forestHealth]}55`,
                  }}
                >
                  Forest · {summary.forestLevel} · {summary.forestHealth}
                </span>
              </div>
            </div>
          )
        })}
      </div>
    </ViewportPortal>
  )
}
