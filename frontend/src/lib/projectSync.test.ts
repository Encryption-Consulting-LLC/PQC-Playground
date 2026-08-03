import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest"

import { STORAGE_KEYS } from "@/constants"
import type { Project } from "@/store/projects"

const storage = new Map<string, string>()

vi.stubGlobal("localStorage", {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => storage.set(key, value),
  removeItem: (key: string) => storage.delete(key),
})
vi.stubGlobal("window", {
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
})

let initLocalProjects: typeof import("@/lib/projectSync")["initLocalProjects"]
let initServerProjects: typeof import("@/lib/projectSync")["initServerProjects"]
let releaseAccountProjects: typeof import("@/lib/projectSync")["releaseAccountProjects"]
let saveActiveProjectNow: typeof import("@/lib/projectSync")["saveActiveProjectNow"]
let stopServerProjects: typeof import("@/lib/projectSync")["stopServerProjects"]
let useProjectsStore: typeof import("@/store/projects")["useProjectsStore"]

/** Browser-local project storage is keyed per account — see persistenceMode.ts. */
function drawer(username: string) {
  return `${STORAGE_KEYS.projects}:${username}`
}

function project(id: string, name: string): Project {
  return {
    id,
    name,
    nodes: [],
    edges: [],
    counters: {},
    viewport: { x: 0, y: 0, zoom: 1 },
    stagedOps: [],
    deployJobId: null,
    dirty: false,
    updatedAt: 1,
  }
}

beforeAll(async () => {
  ;({
    initLocalProjects,
    initServerProjects,
    releaseAccountProjects,
    saveActiveProjectNow,
    stopServerProjects,
  } = await import("@/lib/projectSync"))
  ;({ useProjectsStore } = await import("@/store/projects"))
})

beforeEach(() => {
  storage.clear()
  useProjectsStore.setState({
    projects: [],
    activeProjectId: null,
    nextProjectNumber: 1,
  })
  storage.clear()
})

function persisted(username: string, ...projects: Project[]) {
  storage.set(
    drawer(username),
    JSON.stringify({
      state: {
        projects,
        activeProjectId: projects[0]?.id ?? null,
        nextProjectNumber: 7,
      },
      version: 1,
    }),
  )
}

describe("local project session initialization", () => {
  it("does not expose the previous account's in-memory projects to the next", async () => {
    useProjectsStore.setState({
      projects: [project("operator-project", "Operator project")],
      activeProjectId: "operator-project",
      nextProjectNumber: 2,
    })

    await initLocalProjects("bob")

    expect(useProjectsStore.getState().projects).toEqual([])
    expect(useProjectsStore.getState().activeProjectId).toBeNull()
  })

  it("replaces live state with the signed-in account's saved project set", async () => {
    useProjectsStore.setState({
      projects: [project("operator-project", "Operator project")],
      activeProjectId: "operator-project",
      nextProjectNumber: 2,
    })
    persisted("alice", project("alice-project", "Alice project"))

    await initLocalProjects("alice")

    expect(useProjectsStore.getState().projects.map((p) => p.id)).toEqual([
      "alice-project",
    ])
    expect(useProjectsStore.getState().activeProjectId).toBe("alice-project")
    expect(useProjectsStore.getState().nextProjectNumber).toBe(7)
  })

  it("keeps one account's persisted projects out of another's session", async () => {
    // The reported bug: a project saved by whoever used this browser last
    // showed up for the next person to sign in.
    persisted("alice", project("alice-project", "Alice project"))

    await initLocalProjects("bob")

    expect(useProjectsStore.getState().projects).toEqual([])
    // Alice's drawer is untouched — isolation, not deletion.
    expect(storage.has(drawer("alice"))).toBe(true)

    await initLocalProjects("alice")
    expect(useProjectsStore.getState().projects.map((p) => p.id)).toEqual([
      "alice-project",
    ])
  })

  it("writes only into the signed-in account's drawer", async () => {
    await initLocalProjects("bob")
    useProjectsStore.getState().addProject()

    expect(storage.has(drawer("bob"))).toBe(true)
    expect(storage.has(drawer("alice"))).toBe(false)
    // Never the bare, account-agnostic key that caused the leak.
    expect(storage.has(STORAGE_KEYS.projects)).toBe(false)
  })

  it("detaches storage entirely on sign-out", async () => {
    await initLocalProjects("bob")
    useProjectsStore.getState().addProject()
    const beforeSignOut = storage.get(drawer("bob"))

    releaseAccountProjects()
    // A late async update after sign-out must not land in anyone's drawer.
    useProjectsStore.getState().addProject()

    expect(storage.get(drawer("bob"))).toBe(beforeSignOut)
  })

  it("does not retry a permanent project-write 403", async () => {
    vi.useFakeTimers()
    const serverProject = project("server-project", "Server project")
    const fetchMock = vi.fn(
      async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (init?.method === "PUT") {
          return new Response(
            JSON.stringify({ detail: "Missing project:write." }),
            {
              status: 403,
              headers: { "content-type": "application/json" },
            },
          )
        }
        if (url.endsWith("/api/projects")) {
          return Response.json({
            projects: [
              {
                id: serverProject.id,
                name: serverProject.name,
                createdAt: 1,
                updatedAt: 1,
              },
            ],
            count: 1,
          })
        }
        return Response.json({
          ...serverProject,
          createdAt: 1,
          updatedAt: 1,
        })
      },
    )
    vi.stubGlobal("fetch", fetchMock)

    await initServerProjects("olivia")
    useProjectsStore.getState().renameProject(serverProject.id, "First edit")
    await vi.advanceTimersByTimeAsync(1_500)
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT"),
    ).toHaveLength(1)

    useProjectsStore.getState().renameProject(serverProject.id, "Second edit")
    await vi.advanceTimersByTimeAsync(10_000)
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT"),
    ).toHaveLength(1)

    stopServerProjects()
    vi.useRealTimers()
  })
})

describe("saveActiveProjectNow", () => {
  /** A server that holds one project and answers writes with `status`. */
  function serverHolding(existing: Project, writeStatus = 200) {
    return vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === "PUT" || init?.method === "POST") {
        return writeStatus === 200
          ? Response.json({ ...existing, createdAt: 1, updatedAt: 2 })
          : new Response(JSON.stringify({ detail: "nope" }), {
              status: writeStatus,
              headers: { "content-type": "application/json" },
            })
      }
      if (url.endsWith("/api/projects")) {
        return Response.json({
          projects: [
            {
              id: existing.id,
              name: existing.name,
              createdAt: 1,
              updatedAt: 1,
            },
          ],
          count: 1,
        })
      }
      return Response.json({ ...existing, createdAt: 1, updatedAt: 1 })
    })
  }

  it("reports success only once the server has acknowledged the write", async () => {
    const existing = project("server-project", "Server project")
    vi.stubGlobal("fetch", serverHolding(existing))

    await initServerProjects("olivia")
    useProjectsStore.getState().renameProject(existing.id, "Edited")

    await expect(saveActiveProjectNow()).resolves.toBe(true)
    stopServerProjects()
  })

  it("reports failure when the write is rejected", async () => {
    const existing = project("server-project", "Server project")
    vi.stubGlobal("fetch", serverHolding(existing, 500))

    await initServerProjects("olivia")
    useProjectsStore.getState().renameProject(existing.id, "Edited")

    // This is what makes the deploy refuse rather than run against a topology
    // the server never stored.
    await expect(saveActiveProjectNow()).resolves.toBe(false)
    stopServerProjects()
  })

  it("captures edits made since the last checkpoint", async () => {
    const existing = project("server-project", "Server project")
    const fetchMock = serverHolding(existing)
    vi.stubGlobal("fetch", fetchMock)

    await initServerProjects("olivia")
    useProjectsStore.getState().renameProject(existing.id, "Renamed late")
    await saveActiveProjectNow()

    const write = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PUT",
    )
    expect(JSON.parse(String(write?.[1]?.body)).name).toBe("Renamed late")
    stopServerProjects()
  })

  it("is a no-op that still succeeds when there is nothing to save", async () => {
    const existing = project("server-project", "Server project")
    const fetchMock = serverHolding(existing)
    vi.stubGlobal("fetch", fetchMock)

    await initServerProjects("olivia")

    await expect(saveActiveProjectNow()).resolves.toBe(true)
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT"),
    ).toHaveLength(0)
    stopServerProjects()
  })
})
