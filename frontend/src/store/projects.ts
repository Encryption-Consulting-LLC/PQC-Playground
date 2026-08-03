/**
 * Persisted project store.
 *
 * A "project" is a named, saved snapshot of a topology graph (nodes/edges/
 * counters). This is the seam called out in `topology.ts`: the working graph
 * there stays ephemeral/in-memory, and this store is what actually persists
 * it, one snapshot per project. Persistence is dual-mode:
 *   - guest: localStorage via zustand `persist`, same pattern as `auth.ts`.
 *   - operator: the Mongo-backed /api/projects CRUD — `lib/projectSync.ts`
 *     hydrates this store on init and write-through-syncs changes; the
 *     persist storage is gated read-only (`lib/persistenceMode.ts`).
 *
 * Snapshot writes are checkpointed (see `lib/projectAutosave.ts`) rather than
 * happening on every topology mutation, so dragging/dropping nodes around
 * doesn't spam the persistence layer. Between checkpoints the project reads as
 * unsaved — and `reconcileActiveDirty` decides that by comparing what would be
 * written against what is stored, never by watching for store churn.
 */

import { create } from "zustand"
import { createJSONStorage, persist } from "zustand/middleware"
import type { Edge, Node } from "@xyflow/react"

import type { Viewport } from "@xyflow/react"

import { STORAGE_KEYS } from "@/constants"
import { LIFECYCLE } from "@/constants/topology"
import type { StagedOp } from "@/lib/staging"
import type { MachineData } from "@/store/topology"
import { DEFAULT_VIEWPORT, useTopologyStore } from "@/store/topology"
import { useStagingStore } from "@/store/staging"
import { withSuppressedAutosave } from "@/lib/projectAutosave"
import { projectFingerprint } from "@/lib/projectSerialize"
import { gatedLocalStorage } from "@/lib/persistenceMode"
import { buildPkiTemplateIntoStores } from "@/lib/projectTemplate"

export interface Project {
  id: string
  name: string
  nodes: Node<MachineData>[]
  edges: Edge[]
  counters: Record<string, number>
  /** Camera pan/zoom, restored when this project's tab is reopened. */
  viewport: Viewport
  /** Staged ops queued ahead of a deploy — see `store/staging.ts`. Optional so pre-M2 saves keep loading (missing → treated as `[]`). */
  stagedOps?: StagedOp[]
  deployJobId?: string | null
  dirty: boolean
  /**
   * Opened from another account's share link, so the shares collection — not
   * this account's project collection — is where it lives. Client-only, and
   * deliberately absent from `serializeProject`: it describes where a project is
   * stored, which is never part of the project itself.
   */
  collaborative?: boolean
  updatedAt: number
}

/** A v0 node's `data` shape — pre-lifecycle, keyed by the old `status` field. */
interface LegacyMachineData {
  typeId: string
  name: string
  status?: string
  config?: Record<string, string>
  progress?: number
  phase?: string
  jobId?: string
}

/**
 * v0 → v1: `status` → `lifecycle` + `poweredOn` (+ `lastDeployedConfig` for
 * already-configured nodes, so domains/CA chains hanging off them don't read
 * as drifted the moment they load). Idempotent — already-migrated data (has
 * `lifecycle`) passes through unchanged.
 */
export function migrateNodeData(
  data: LegacyMachineData | MachineData,
): MachineData {
  if ("lifecycle" in data) return data
  const { status, ...rest } = data
  switch (status) {
    case "configuring":
      return {
        ...rest,
        lifecycle: rest.jobId ? LIFECYCLE.deploying : LIFECYCLE.draft,
        poweredOn: false,
      }
    case "configured":
      return {
        ...rest,
        lifecycle: LIFECYCLE.deployed,
        poweredOn: true,
        lastDeployedConfig: rest.config,
      }
    case "error":
      return { ...rest, lifecycle: LIFECYCLE.failed, poweredOn: false }
    default:
      return { ...rest, lifecycle: LIFECYCLE.draft, poweredOn: false }
  }
}

export function emptyProject(name: string): Project {
  return {
    id: crypto.randomUUID(),
    name,
    nodes: [],
    edges: [],
    counters: {},
    viewport: DEFAULT_VIEWPORT,
    stagedOps: [],
    deployJobId: null,
    dirty: false,
    updatedAt: Date.now(),
  }
}

/**
 * The live working state, in the shape a project snapshot stores it.
 *
 * One read shared by every write path (checkpoint, draft, dirty comparison) so
 * "what would be saved" cannot drift between deciding to save and saving.
 */
function workingSnapshot() {
  const { nodes, edges, counters, viewport } = useTopologyStore.getState()
  const { ops: stagedOps, deployJobId } = useStagingStore.getState()
  return { nodes, edges, counters, viewport, stagedOps, deployJobId }
}

/** Load a project's ops + snapshot into the working stores (autosave-suppressed). */
function activate(project: Project) {
  withSuppressedAutosave(() => {
    // Ops load first so `loadSnapshot`'s resumeJobs can see them — a mid-plan
    // node reverting to `staged` rather than `draft` depends on the matching
    // op already being in the staging store.
    useStagingStore
      .getState()
      .loadOps(project.stagedOps ?? [], project.deployJobId ?? null)
    useTopologyStore
      .getState()
      .loadSnapshot(
        project.nodes,
        project.edges,
        project.counters,
        project.viewport ?? DEFAULT_VIEWPORT,
      )
  })
}

interface ProjectsState {
  projects: Project[]
  activeProjectId: string | null
  nextProjectNumber: number

  restoreProjects: () => void
  hydrateFromServer: (
    projects: Project[],
    activeProjectId: string | null,
    nextProjectNumber: number,
  ) => void
  addProject: () => void
  /** Creates a new project pre-populated with a deploy-ready PKI lab topology. */
  addProjectFromTemplate: () => void
  /**
   * Adds/replaces a project loaded from an accepted share link and opens it.
   * `collaborative` marks someone else's share, which keeps it out of this
   * account's owned-project sync (see `lib/projectSync.ts`).
   */
  openSharedProject: (
    project: Project,
    opts?: { collaborative?: boolean },
  ) => void
  renameProject: (id: string, name: string) => void
  switchProject: (id: string) => void
  /**
   * Removes a project. Deleting the active tab falls through to a neighbour;
   * deleting the last one leaves `activeProjectId` null (the landing page shows)
   * and clears the working graph. Server-mode DELETE is handled by the
   * projectSync subscription watching for removed ids.
   */
  deleteProject: (id: string) => void
  /**
   * Re-decide whether the active project reads as unsaved, by comparing what
   * would be written against what is stored. Replaces a blanket "mark dirty on
   * any node/edge churn", which flipped the marker on selection and on React
   * Flow's mount-time measurement — neither of which is ever written.
   */
  reconcileActiveDirty: () => void
  saveActiveSnapshot: () => void
  persistActiveDraft: () => void
  persistActiveViewport: () => void
}

export const useProjectsStore = create<ProjectsState>()(
  persist(
    (set, get) => ({
      projects: [],
      activeProjectId: null,
      nextProjectNumber: 1,

      // Session entry point (guest mode): re-open the previously active project
      // if one persisted. With no saved projects the store stays empty and
      // `activeProjectId` null, so the workspace shows <ProjectLanding> — a
      // fresh launch lands on "How do you wish to start?" rather than a blank
      // auto-created project.
      restoreProjects() {
        const { projects } = get()
        if (projects.length === 0) return
        const active =
          projects.find((p) => p.id === get().activeProjectId) ?? projects[0]
        set({ activeProjectId: active.id })
        activate(active)
      },

      // Server-mode entry point (lib/projectSync.ts): wholesale-replace the
      // project list with Mongo-loaded docs and activate the chosen one.
      hydrateFromServer(projects, activeProjectId, nextProjectNumber) {
        set({ projects, activeProjectId, nextProjectNumber })
        const active =
          projects.find((p) => p.id === activeProjectId) ?? projects[0]
        if (active) activate(active)
      },

      addProject() {
        get().persistActiveDraft()
        const n = get().nextProjectNumber
        const project = emptyProject(`Project ${n}`)
        set((s) => ({
          projects: [...s.projects, project],
          activeProjectId: project.id,
          nextProjectNumber: n + 1,
        }))
        activate(project)
      },

      addProjectFromTemplate() {
        get().persistActiveDraft()
        const n = get().nextProjectNumber
        const project = emptyProject(`PKI Lab ${n}`)
        set((s) => ({
          projects: [...s.projects, project],
          activeProjectId: project.id,
          nextProjectNumber: n + 1,
        }))
        // Populate the working stores with the ready-to-deploy PKI, then
        // snapshot it into the freshly-created project. Suppressed so the
        // build's per-node/edge churn doesn't autosave on every step.
        withSuppressedAutosave(() => {
          buildPkiTemplateIntoStores(project.id)
        })
        get().saveActiveSnapshot()
      },

      openSharedProject(incoming, opts) {
        get().persistActiveDraft()
        // Reopening one's *own* share is just reopening one's own project, so
        // the flag only rides along for someone else's.
        const project: Project = opts?.collaborative
          ? { ...incoming, collaborative: true }
          : incoming
        set((s) => ({
          projects: s.projects.some((candidate) => candidate.id === project.id)
            ? s.projects.map((candidate) =>
                candidate.id === project.id ? project : candidate,
              )
            : [...s.projects, project],
          activeProjectId: project.id,
        }))
        activate(project)
      },

      renameProject(id, name) {
        const trimmed = name.trim()
        if (!trimmed) return
        set((s) => ({
          projects: s.projects.map((p) =>
            p.id === id ? { ...p, name: trimmed } : p,
          ),
        }))
      },

      switchProject(id) {
        if (get().activeProjectId === id) return
        get().persistActiveDraft()
        const target = get().projects.find((p) => p.id === id)
        if (!target) return
        set({ activeProjectId: id })
        activate(target)
      },

      deleteProject(id) {
        const { projects, activeProjectId } = get()
        const idx = projects.findIndex((p) => p.id === id)
        if (idx === -1) return
        const remaining = projects.filter((p) => p.id !== id)

        // Deleting a background tab leaves the active project (and the working
        // stores) untouched — just drop it from the list.
        if (id !== activeProjectId) {
          set({ projects: remaining })
          return
        }

        // Deleting the active tab: fall through to the next tab (then the
        // previous), else nothing is left.
        const next = remaining[idx] ?? remaining[idx - 1] ?? null
        set({ projects: remaining, activeProjectId: next?.id ?? null })
        if (next) {
          activate(next)
        } else {
          // No projects left — clear the working graph so the landing page
          // starts from a clean slate and the deleted project's live sockets
          // are torn down (loadSnapshot closes them).
          withSuppressedAutosave(() => {
            useStagingStore.getState().loadOps([], null)
            useTopologyStore
              .getState()
              .loadSnapshot([], [], {}, DEFAULT_VIEWPORT)
          })
        }
      },

      reconcileActiveDirty() {
        const { activeProjectId, projects } = get()
        const active = projects.find((p) => p.id === activeProjectId)
        if (!active) return
        // Two-way on purpose: dragging a node and dragging it back leaves
        // nothing to save, and the marker should say so.
        const dirty =
          projectFingerprint({ ...active, ...workingSnapshot() }) !==
          projectFingerprint(active)
        if (dirty === active.dirty) return
        set((s) => ({
          projects: s.projects.map((p) =>
            p.id === activeProjectId ? { ...p, dirty } : p,
          ),
        }))
      },

      saveActiveSnapshot() {
        const { activeProjectId } = get()
        if (!activeProjectId) return
        const snapshot = workingSnapshot()
        set((s) => ({
          projects: s.projects.map((p) =>
            p.id === activeProjectId
              ? { ...p, ...snapshot, dirty: false, updatedAt: Date.now() }
              : p,
          ),
        }))
      },

      persistActiveDraft() {
        const { activeProjectId } = get()
        if (!activeProjectId) return
        const snapshot = workingSnapshot()
        set((s) => ({
          projects: s.projects.map((p) =>
            p.id === activeProjectId ? { ...p, ...snapshot } : p,
          ),
        }))
      },

      // Camera-only checkpoint: a bare pan/zoom shouldn't mark the project
      // dirty (no graph data changed) but should still survive a reload.
      persistActiveViewport() {
        const { activeProjectId } = get()
        if (!activeProjectId) return
        const { viewport } = useTopologyStore.getState()
        set((s) => ({
          projects: s.projects.map((p) =>
            p.id === activeProjectId ? { ...p, viewport } : p,
          ),
        }))
      },
    }),
    {
      name: STORAGE_KEYS.projects,
      // Same envelope/version/migrate as the default localStorage storage —
      // guests are unaffected; in server mode the gate makes writes no-ops.
      storage: createJSONStorage(() => gatedLocalStorage),
      version: 1,
      migrate: (persistedState, version) => {
        if (version >= 1) return persistedState as ProjectsState
        const state = persistedState as ProjectsState
        return {
          ...state,
          projects: (state.projects ?? []).map((p) => ({
            ...p,
            nodes: p.nodes.map((n) => ({
              ...n,
              data: migrateNodeData(n.data),
            })),
          })),
        }
      },
    },
  ),
)
