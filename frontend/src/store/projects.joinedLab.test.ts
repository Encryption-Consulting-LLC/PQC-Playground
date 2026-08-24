/**
 * What opening a joined lab is allowed to do to the project list.
 *
 * A lab redeemed with a code is somebody else's deployed environment. Two
 * things therefore have to hold, and neither is obvious from the call site:
 * the tab must be marked `collaborative` (so `lib/projectSync.ts` never writes
 * it into this account's projects, nor DELETEs the owner's document when the tab
 * is closed) *and* `joinedLab` (which is what every read-only lock reads).
 *
 * Session restore adds the same tabs without stealing focus, so the third rule
 * is that a non-activating open leaves the active project exactly where it was.
 */

import { beforeAll, beforeEach, expect, test, vi } from "vitest"

const storage = new Map<string, string>()
const localStorageStub = {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => storage.set(key, value),
  removeItem: (key: string) => storage.delete(key),
}

vi.stubGlobal("localStorage", localStorageStub)
vi.stubGlobal("window", {
  localStorage: localStorageStub,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
})

vi.mock("@/lib/ws", () => ({
  openJobSocket: vi.fn(() => vi.fn()),
  openAgentsSocket: vi.fn(() => vi.fn()),
  WS_CLOSE_JOB_GONE: 4404,
  WS_CLOSE_UNAUTHORIZED: 4401,
}))

let useProjectsStore: typeof import("@/store/projects")["useProjectsStore"]
let emptyProject: typeof import("@/store/projects")["emptyProject"]

beforeAll(async () => {
  const module = await import("@/store/projects")
  useProjectsStore = module.useProjectsStore
  emptyProject = module.emptyProject
})

beforeEach(() => {
  useProjectsStore.setState({
    projects: [],
    activeProjectId: null,
    nextProjectNumber: 1,
  })
})

const LAB = { code: "ABCD2345", owner: "operator" }

test("a joined lab opens as a collaborative, lab-flagged tab", () => {
  const lab = { ...emptyProject("Workshop PKI"), dirty: true }
  useProjectsStore.getState().openJoinedLab(lab, LAB)

  const { projects, activeProjectId } = useProjectsStore.getState()
  expect(activeProjectId).toBe(lab.id)
  expect(projects[0].collaborative).toBe(true)
  expect(projects[0].joinedLab).toEqual(LAB)
  // Never unsaved: there is nothing of this account's to save.
  expect(projects[0].dirty).toBe(false)
})

test("restoring joined labs does not steal the active project", () => {
  const own = emptyProject("My project")
  useProjectsStore.setState({ projects: [own], activeProjectId: own.id })

  const lab = emptyProject("Workshop PKI")
  useProjectsStore.getState().openJoinedLab(lab, LAB, { activate: false })

  const { projects, activeProjectId } = useProjectsStore.getState()
  expect(activeProjectId).toBe(own.id)
  expect(projects.map((p) => p.id)).toEqual([own.id, lab.id])
})

test("rejoining the same lab replaces its tab rather than duplicating it", () => {
  const first = emptyProject("Workshop PKI")
  useProjectsStore.getState().openJoinedLab(first, LAB)
  useProjectsStore
    .getState()
    .openJoinedLab({ ...first, name: "Workshop PKI (renamed)" }, LAB)

  const { projects } = useProjectsStore.getState()
  expect(projects).toHaveLength(1)
  expect(projects[0].name).toBe("Workshop PKI (renamed)")
})
