/**
 * Server persistence engine for projects (operator mode only).
 *
 * `store/projects.ts` stays the in-memory source of truth with unchanged
 * action signatures; this module hydrates it from the Mongo-backed
 * /api/projects CRUD on init, then subscribes and write-through-syncs changed
 * projects with a per-project debounce. Guests never call into here — they
 * keep the store's localStorage persist (see `lib/persistenceMode.ts`).
 *
 * Change detection compares `serializeProject` JSON against the last acked
 * copy (`lastSynced`), so dirty-flag flips, progress ticks, and client
 * `updatedAt` stamps never cause writes. Flushes happen immediately on:
 * explicit save (dirty true→false), project switch, the window coming back
 * online, and beforeunload (with `keepalive`).
 * Failed writes stay in `pendingIds` (arming the unload warning) and retry
 * every few seconds — nothing is lost while the tab lives.
 */

import { create } from "zustand"
import { toast } from "sonner"

import { STORAGE_KEYS } from "@/constants"
import {
  ApiError,
  createProject,
  deleteProject,
  getProject,
  listProjects,
  updateProject,
} from "@/lib/api"
import {
  disableServerPersistence,
  enableServerPersistence,
  isServerPersistence,
  scopedKey,
  setAccountScope,
} from "@/lib/persistenceMode"
import { deserializeProject, serializeProject } from "@/lib/projectSerialize"
import { migrateNodeData, useProjectsStore } from "@/store/projects"
import type { Project } from "@/store/projects"

const SAVE_DEBOUNCE_MS = 1500
const RETRY_MS = 5000

interface ProjectSyncState {
  /** Startup load lifecycle — App gates the workspace on it in server mode. */
  status: "idle" | "loading" | "ready" | "error"
  loadError?: string
  /** Project ids with unflushed or in-flight writes (arms the unload warning). */
  pendingIds: string[]
  /** Last flush attempt errored — drives the retry loop + one-toast-per-outage. */
  saveFailed: boolean
}

export const useProjectSyncStore = create<ProjectSyncState>()(() => ({
  status: "idle",
  pendingIds: [],
  saveFailed: false,
}))

// Module-level (not store state): baselines and timers are never rendered.
const lastSynced = new Map<string, string>()
const serverKnownIds = new Set<string>()
const timers = new Map<string, ReturnType<typeof setTimeout>>()
const inFlight = new Set<string>()
// The live request per id, so an awaiting caller can wait for it rather than
// bouncing off `inFlight` and reporting a save it never confirmed.
const inFlightWrites = new Map<string, Promise<boolean>>()
const changedWhileInFlight = new Set<string>()
let retryTimer: ReturnType<typeof setTimeout> | null = null
let unsubscribeProjects: (() => void) | null = null
let onlineHandler: (() => void) | null = null
let syncGeneration = 0
let writesForbidden = false

// --- device-local meta (active tab + numbering — deliberately not server state)

interface ProjectsMeta {
  activeProjectId?: string | null
  nextProjectNumber?: number
}

function readMeta(): ProjectsMeta {
  const key = scopedKey(STORAGE_KEYS.projectsMeta)
  if (key === null) return {}
  try {
    return JSON.parse(localStorage.getItem(key) ?? "{}")
  } catch {
    return {}
  }
}

function writeMeta(activeProjectId: string | null, nextProjectNumber: number) {
  const key = scopedKey(STORAGE_KEYS.projectsMeta)
  if (key === null) return
  localStorage.setItem(
    key,
    JSON.stringify({ activeProjectId, nextProjectNumber }),
  )
}

// --- init / hydration --------------------------------------------------------

/**
 * This account's browser-local snapshot, as import candidates for a server that
 * has none. Only read when the server list is empty, so a wiped DB re-imports
 * old local data (acceptable for a playground) but a populated server never gets
 * clobbered.
 *
 * Scoped like every other local read: a snapshot written under a *different*
 * account is not a migration candidate. Importing browser-wide local data was
 * how one person's projects got promoted into the next person's server account.
 */
function readLocalProjects(): Project[] {
  return readLocalProjectState().projects.map((project) => ({
    ...project,
    // An imported snapshot is fully acknowledged once POST succeeds; local
    // dirty flags are device-local editing state, not server data.
    dirty: false,
  }))
}

interface LocalProjectState {
  projects: Project[]
  activeProjectId: string | null
  nextProjectNumber: number
}

/** Read and normalize this account's persisted store without retaining live state. */
function readLocalProjectState(): LocalProjectState {
  const key = scopedKey(STORAGE_KEYS.projects)
  if (key === null)
    return { projects: [], activeProjectId: null, nextProjectNumber: 1 }
  try {
    const raw = localStorage.getItem(key)
    if (!raw)
      return { projects: [], activeProjectId: null, nextProjectNumber: 1 }
    const envelope = JSON.parse(raw) as {
      state?: {
        projects?: Project[]
        activeProjectId?: string | null
        nextProjectNumber?: number
      }
    }
    const projects = (envelope.state?.projects ?? []).map((p) => ({
      ...p,
      // Idempotent v0→v1 node migration, same as the persist `migrate` fn.
      nodes: (p.nodes ?? []).map((n) => ({
        ...n,
        data: migrateNodeData(n.data),
      })),
      stagedOps: p.stagedOps ?? [],
      deployJobId: p.deployJobId ?? null,
      dirty: p.dirty ?? false,
    }))
    const storedActiveId = envelope.state?.activeProjectId
    const activeProjectId =
      storedActiveId && projects.some((p) => p.id === storedActiveId)
        ? storedActiveId
        : (projects[0]?.id ?? null)
    return {
      projects,
      activeProjectId,
      nextProjectNumber:
        envelope.state?.nextProjectNumber ?? inferNextProjectNumber(projects),
    }
  } catch {
    return { projects: [], activeProjectId: null, nextProjectNumber: 1 }
  }
}

function inferNextProjectNumber(projects: Project[]): number {
  let max = 0
  for (const p of projects) {
    const m = /^Project (\d+)$/.exec(p.name)
    if (m) max = Math.max(max, Number(m[1]))
  }
  return max + 1
}

export async function initServerProjects(username: string): Promise<void> {
  stopServerProjects()
  const generation = syncGeneration
  setAccountScope(username)
  enableServerPersistence()
  useProjectSyncStore.setState({ status: "loading", loadError: undefined })
  try {
    const { projects: summaries } = await listProjects()
    if (generation !== syncGeneration) return

    let projects: Project[]
    let imported = false
    if (summaries.length > 0) {
      const docs = await Promise.all(summaries.map((s) => getProject(s.id)))
      if (generation !== syncGeneration) return
      projects = docs.map(deserializeProject)
    } else {
      const local = readLocalProjects()
      if (local.length > 0) {
        projects = local
        imported = true
      } else {
        // Nothing on the server and nothing to import → stay empty so the
        // workspace lands on <ProjectLanding> rather than an auto-created
        // blank project.
        projects = []
      }
    }

    const meta = readMeta()
    const activeId =
      meta.activeProjectId &&
      projects.some((p) => p.id === meta.activeProjectId)
        ? meta.activeProjectId
        : projects.length > 0
          ? projects.reduce((a, b) => (b.updatedAt > a.updatedAt ? b : a)).id
          : null
    const nextNumber =
      meta.nextProjectNumber ?? inferNextProjectNumber(projects)

    // Seed baselines BEFORE hydrating/subscribing: server-fetched docs are in
    // sync; imported/local/default ones are unknown and get pushed below.
    lastSynced.clear()
    serverKnownIds.clear()
    if (summaries.length > 0) {
      for (const p of projects) {
        lastSynced.set(p.id, JSON.stringify(serializeProject(p)))
        serverKnownIds.add(p.id)
      }
    }

    useProjectsStore
      .getState()
      .hydrateFromServer(projects, activeId, nextNumber)
    writeMeta(activeId, nextNumber)
    startSubscriptions(generation)
    useProjectSyncStore.setState({ status: "ready" })

    for (const p of projects) {
      if (!serverKnownIds.has(p.id)) {
        markPending(p.id)
        void flushProject(p.id)
      }
    }
    if (imported) {
      const n = projects.length
      toast.success(
        `Imported ${n} locally saved project${n === 1 ? "" : "s"} to the server.`,
      )
    }
  } catch (e) {
    if (generation !== syncGeneration) return
    useProjectSyncStore.setState({
      status: "error",
      loadError: e instanceof Error ? e.message : String(e),
    })
  }
}

/**
 * Tear down every server-mode side effect. This is required on logout/account
 * changes: otherwise a guest signing in after an operator inherits the old
 * project subscription and sends operator-only PUTs with the guest token.
 */
export function stopServerProjects() {
  syncGeneration += 1
  unsubscribeProjects?.()
  unsubscribeProjects = null
  if (onlineHandler) window.removeEventListener("online", onlineHandler)
  onlineHandler = null
  for (const timer of timers.values()) clearTimeout(timer)
  timers.clear()
  if (retryTimer) clearTimeout(retryTimer)
  retryTimer = null
  lastSynced.clear()
  serverKnownIds.clear()
  inFlight.clear()
  changedWhileInFlight.clear()
  writesForbidden = false
  useProjectSyncStore.setState({
    status: "idle",
    loadError: undefined,
    pendingIds: [],
    saveFailed: false,
  })
}

/**
 * Rehydrate one account's device-local project set after leaving server mode.
 *
 * Every canvas role now holds the project capabilities, so this is a fallback
 * rather than the guest path it used to be — but the scoping matters more here
 * than anywhere: this is the only mode that still writes to the browser.
 */
export async function initLocalProjects(username: string): Promise<void> {
  stopServerProjects()
  // Bind the drawer, then open the write gate — in that order, so nothing can
  // be written before it is known whose drawer it belongs in.
  setAccountScope(username)
  disableServerPersistence()
  const local = readLocalProjectState()
  // Replace the project slice even when local storage is empty. Merging a
  // missing snapshot would otherwise leave the previous account's projects
  // visible to the next person in the same browser tab.
  useProjectsStore.setState(local)
  useProjectsStore.getState().restoreProjects()
}

/**
 * Detach browser-local storage from any account, and drop whatever the previous
 * session left in the store. Called on sign-out: the store lives in module
 * memory, so without this the next account's pre-hydration render would show the
 * previous one's tabs.
 */
export function releaseAccountProjects() {
  stopServerProjects()
  disableServerPersistence()
  setAccountScope(null)
  useProjectsStore.setState({
    projects: [],
    activeProjectId: null,
    nextProjectNumber: 1,
  })
}

export function retryInitServerProjects(username: string) {
  void initServerProjects(username)
}

// --- change detection ---------------------------------------------------------

function startSubscriptions(generation: number) {
  unsubscribeProjects = useProjectsStore.subscribe((state, prev) => {
    if (generation !== syncGeneration) return
    if (
      state.activeProjectId !== prev.activeProjectId ||
      state.nextProjectNumber !== prev.nextProjectNumber
    ) {
      writeMeta(state.activeProjectId, state.nextProjectNumber)
    }

    if (state.projects !== prev.projects) {
      const prevById = new Map(prev.projects.map((p) => [p.id, p]))
      for (const p of state.projects) {
        const prevP = prevById.get(p.id)
        prevById.delete(p.id)
        if (prevP === p) continue
        const serialized = JSON.stringify(serializeProject(p))
        if (serialized === lastSynced.get(p.id)) continue
        markPending(p.id)
        // dirty true→false is the explicit-save/checkpoint signature (Ctrl+S,
        // Save button, autosave checkpoints) — flush now, not debounced.
        if (prevP && prevP.dirty && !p.dirty) void flushProject(p.id)
        else scheduleFlush(p.id)
      }
      // Removed ids → DELETE. No delete UI exists yet; future-proofing.
      for (const id of prevById.keys()) void removeProject(id)
    }

    // A switch flushes the outgoing draft so switch-then-reload can't lose it.
    if (state.activeProjectId !== prev.activeProjectId) flushAllPending()
  })

  onlineHandler = () => flushAllPending()
  window.addEventListener("online", onlineHandler)
}

// --- flushing -----------------------------------------------------------------

function scheduleFlush(id: string) {
  const existing = timers.get(id)
  if (existing) clearTimeout(existing)
  timers.set(
    id,
    setTimeout(() => {
      timers.delete(id)
      void flushProject(id)
    }, SAVE_DEBOUNCE_MS),
  )
}

/**
 * Write one project if it differs from the last acked copy.
 *
 * Returns whether the server is now known to hold the current serialization —
 * true for a successful write *and* for a no-op where it already did. Callers
 * that only fire-and-forget ignore it; `saveActiveProjectNow` is the one that
 * needs a real answer.
 */
async function flushProject(id: string, init?: RequestInit): Promise<boolean> {
  if (!isServerPersistence() || writesForbidden) return false
  const generation = syncGeneration
  const timer = timers.get(id)
  if (timer) {
    clearTimeout(timer)
    timers.delete(id)
  }
  if (inFlight.has(id)) {
    changedWhileInFlight.add(id)
    return false
  }
  const project = useProjectsStore.getState().projects.find((p) => p.id === id)
  if (!project) return false

  const payload = serializeProject(project)
  const serialized = JSON.stringify(payload)
  if (serialized === lastSynced.get(id)) {
    clearPending(id)
    return true
  }

  inFlight.add(id)
  const write = (async () => {
    try {
      if (serverKnownIds.has(id)) {
        await updateProject(payload, init)
      } else {
        await createProject(payload, init)
      }
      if (generation !== syncGeneration) return false
      serverKnownIds.add(id)
      lastSynced.set(id, serialized)
      clearPending(id)
      clearSaveFailure()
      return true
    } catch (e) {
      if (generation !== syncGeneration) return false
      if (e instanceof ApiError) {
        // 401: api.ts already cleared auth and the UI gates to login. Stay
        // pending until the session teardown resets this sync instance.
        if (e.status === 401) return false
        // Unlike network/5xx failures, a capability denial will not heal on a
        // timer. Stop all project writes for this session so it cannot become a
        // permanent PUT/403 loop. Signing in again reselects persistence mode.
        if (e.status === 403) {
          reportAuthorizationFailure()
          return false
        }
        // The doc vanished server-side (e.g. wiped DB): recreate on next flush.
        if (e.status === 404 && serverKnownIds.has(id))
          serverKnownIds.delete(id)
        // A duplicate id on POST means the doc exists after all: PUT next time.
        if (e.status === 409 && !serverKnownIds.has(id)) serverKnownIds.add(id)
      }
      reportSaveFailure()
      return false
    }
  })()
  inFlightWrites.set(id, write)
  try {
    return await write
  } finally {
    inFlightWrites.delete(id)
    if (generation === syncGeneration) {
      inFlight.delete(id)
      if (changedWhileInFlight.delete(id) && !writesForbidden) scheduleFlush(id)
    }
  }
}

async function removeProject(id: string) {
  const timer = timers.get(id)
  if (timer) {
    clearTimeout(timer)
    timers.delete(id)
  }
  clearPending(id)
  lastSynced.delete(id)
  if (!serverKnownIds.has(id)) return
  try {
    await deleteProject(id)
  } catch (e) {
    // 404 = already gone; anything else leaves a stale doc the next full
    // load surfaces — acceptable for a path with no UI yet.
    if (!(e instanceof ApiError && e.status === 404)) return
  }
  serverKnownIds.delete(id)
}

/**
 * Checkpoint the active project and, in server mode, wait for the server to
 * acknowledge it. Resolves to whether it is now safely saved.
 *
 * This is the one save the caller must be able to *wait* on: deploying is the
 * moment a project stops being a sketch and starts naming real VMs, so it has to
 * be attributable to a persisted snapshot rather than to whatever happened to be
 * on screen. Everything else in this module is deliberately fire-and-forget.
 */
export async function saveActiveProjectNow(): Promise<boolean> {
  const store = useProjectsStore.getState()
  const id = store.activeProjectId
  if (!id) return true
  // Copy the working stores into the snapshot first — otherwise this would
  // faithfully persist the state as of the last checkpoint and report success.
  store.saveActiveSnapshot()
  // Local mode writes synchronously through the persist middleware.
  if (!isServerPersistence()) return true
  // `saveActiveSnapshot`'s dirty true→false already triggered an immediate
  // flush; let it finish before adding another, so the second call sees the
  // final serialization and short-circuits instead of racing.
  await inFlightWrites.get(id)?.catch(() => false)
  return flushProject(id)
}

export function flushAllPending(opts?: { keepalive?: boolean }) {
  const init = opts?.keepalive ? { keepalive: true } : undefined
  for (const id of [...useProjectSyncStore.getState().pendingIds]) {
    void flushProject(id, init)
  }
}

// --- pending / failure bookkeeping ---------------------------------------------

function markPending(id: string) {
  const { pendingIds } = useProjectSyncStore.getState()
  if (!pendingIds.includes(id)) {
    useProjectSyncStore.setState({ pendingIds: [...pendingIds, id] })
  }
}

function clearPending(id: string) {
  const { pendingIds } = useProjectSyncStore.getState()
  if (pendingIds.includes(id)) {
    useProjectSyncStore.setState({
      pendingIds: pendingIds.filter((x) => x !== id),
    })
  }
}

function reportSaveFailure() {
  if (!useProjectSyncStore.getState().saveFailed) {
    useProjectSyncStore.setState({ saveFailed: true })
    toast.error("Couldn't save to the server — will keep retrying.")
  }
  if (!retryTimer) {
    retryTimer = setTimeout(() => {
      retryTimer = null
      flushAllPending()
    }, RETRY_MS)
  }
}

function reportAuthorizationFailure() {
  if (writesForbidden) return
  writesForbidden = true
  if (retryTimer) clearTimeout(retryTimer)
  retryTimer = null
  useProjectSyncStore.setState({ saveFailed: true })
  toast.error(
    "Project saving is not permitted for this account. Sign out and back in.",
  )
}

function clearSaveFailure() {
  if (useProjectSyncStore.getState().saveFailed) {
    useProjectSyncStore.setState({ saveFailed: false })
  }
}
