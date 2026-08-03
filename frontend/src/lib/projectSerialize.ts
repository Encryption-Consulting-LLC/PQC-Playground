/**
 * Project ↔ wire-document conversion for the Mongo-backed /api/projects CRUD.
 *
 * `serializeProject` produces the write payload (POST/PUT body) and doubles as
 * the change-detection canonicalizer in `lib/projectSync.ts` — its JSON string
 * is compared against the last acked copy, so it must be deterministic and
 * must exclude anything that changes without the content changing:
 *   - `dirty` (client-only sync flag)
 *   - `updatedAt` (client stamp; the server sets its own)
 *   - React Flow render transients on nodes/edges (`selected`, `dragging`,
 *     `measured`) and transient run-state in node data (`progress`, `phase`)
 *   - transient op run-state (`progress`, `detail`)
 * `jobId` / `deployJobId` are deliberately KEPT — reload-resume of an
 * in-flight clone/plan (`resumeJobs`/`resumePlanJob`) depends on them.
 */

import type { Edge, Node, Viewport } from "@xyflow/react"

import { DEFAULT_VIEWPORT } from "@/store/topology"
import type { MachineData } from "@/store/topology"
import { migrateNodeData } from "@/store/projects"
import type { Project } from "@/store/projects"
import type { StagedOp } from "@/lib/staging"

/** What the client writes (POST/PUT body). The server stamps timestamps. */
export interface ProjectPayload {
  id: string
  name: string
  nodes: Node<MachineData>[]
  edges: Edge[]
  counters: Record<string, number>
  viewport: Viewport
  stagedOps: StagedOp[]
  deployJobId: string | null
}

/** What the server returns (extra server-owned fields are ignored on load). */
export interface ProjectDoc extends ProjectPayload {
  createdAt: number
  updatedAt: number
}

function cleanNode(n: Node<MachineData>): Node<MachineData> {
  const data = { ...n.data }
  delete data.progress
  delete data.phase
  return {
    id: n.id,
    type: n.type,
    position: { x: n.position.x, y: n.position.y },
    data,
  } as Node<MachineData>
}

function cleanEdge(e: Edge): Edge {
  const edge = { ...e }
  delete edge.selected
  return edge
}

function cleanOp(op: StagedOp): StagedOp {
  const cleaned = { ...op }
  delete cleaned.progress
  delete cleaned.detail
  return cleaned
}

/**
 * Canonical JSON for one project — the single definition of "this differs".
 *
 * Both change detectors read it: `lib/projectSync.ts` compares against the last
 * server-acked copy to decide whether to write, and `store/projects.ts` compares
 * the working stores against the saved snapshot to decide whether the project
 * reads as unsaved. Deriving both from `serializeProject` is what keeps the
 * unsaved marker honest — React Flow's `selected`/`measured`/`dragging` churn is
 * stripped here, so clicking a node or merely measuring one on mount cannot
 * claim there is something to save.
 */
export function projectFingerprint(p: Project): string {
  return JSON.stringify(serializeProject(p))
}

export function serializeProject(p: Project): ProjectPayload {
  return {
    id: p.id,
    name: p.name,
    nodes: p.nodes.map(cleanNode),
    edges: p.edges.map(cleanEdge),
    counters: p.counters,
    viewport: { x: p.viewport.x, y: p.viewport.y, zoom: p.viewport.zoom },
    stagedOps: (p.stagedOps ?? []).map(cleanOp),
    deployJobId: p.deployJobId ?? null,
  }
}

export function deserializeProject(doc: ProjectDoc): Project {
  return {
    id: doc.id,
    name: doc.name,
    // Idempotent v0→v1 migration — defense against pre-migration imports.
    nodes: (doc.nodes ?? []).map((n) => ({
      ...n,
      data: migrateNodeData(n.data),
    })),
    edges: doc.edges ?? [],
    counters: doc.counters ?? {},
    viewport: doc.viewport ?? DEFAULT_VIEWPORT,
    stagedOps: doc.stagedOps ?? [],
    deployJobId: doc.deployJobId ?? null,
    dirty: false,
    updatedAt: doc.updatedAt,
  }
}
