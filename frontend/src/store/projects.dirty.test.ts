/**
 * What the unsaved marker is allowed to mean.
 *
 * It used to flip on any change that produced a new nodes/edges array, so
 * clicking a node — or merely opening a project, since React Flow measures every
 * node on mount — showed unsaved work that did not exist. It now answers the
 * only question that matters: would saving write anything different?
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
let useTopologyStore: typeof import("@/store/topology")["useTopologyStore"]
let initProjectAutosave: typeof import("@/lib/projectAutosave")["initProjectAutosave"]

beforeAll(async () => {
  useProjectsStore = (await import("@/store/projects")).useProjectsStore
  useTopologyStore = (await import("@/store/topology")).useTopologyStore
  initProjectAutosave = (await import("@/lib/projectAutosave"))
    .initProjectAutosave
  initProjectAutosave()
})

function active() {
  const { projects, activeProjectId } = useProjectsStore.getState()
  return projects.find((p) => p.id === activeProjectId)!
}

beforeEach(() => {
  useProjectsStore.getState().addProjectFromTemplate()
  // The template build ends in a checkpoint, so we start from "saved".
  useProjectsStore.getState().saveActiveSnapshot()
  expect(active().dirty).toBe(false)
})

test("selecting a node does not make the project unsaved", () => {
  const target = useTopologyStore.getState().nodes[0]

  useTopologyStore
    .getState()
    .applyNodeChanges([{ id: target.id, type: "select", selected: true }])

  expect(useTopologyStore.getState().nodes[0].selected).toBe(true)
  expect(active().dirty).toBe(false)
})

test("React Flow measuring nodes on mount does not make the project unsaved", () => {
  const target = useTopologyStore.getState().nodes[0]

  useTopologyStore.getState().applyNodeChanges([
    {
      id: target.id,
      type: "dimensions",
      dimensions: { width: 240, height: 120 },
      setAttributes: true,
    },
  ])

  expect(active().dirty).toBe(false)
})

test("moving a node does make the project unsaved", () => {
  const target = useTopologyStore.getState().nodes[0]

  useTopologyStore.getState().applyNodeChanges([
    {
      id: target.id,
      type: "position",
      position: { x: target.position.x + 80, y: target.position.y },
    },
  ])

  expect(active().dirty).toBe(true)
})

test("moving a node back clears the marker again", () => {
  const target = useTopologyStore.getState().nodes[0]
  const origin = { ...target.position }

  useTopologyStore
    .getState()
    .applyNodeChanges([
      { id: target.id, type: "position", position: { x: 999, y: 999 } },
    ])
  expect(active().dirty).toBe(true)

  useTopologyStore
    .getState()
    .applyNodeChanges([{ id: target.id, type: "position", position: origin }])

  // Nothing left to write, so nothing left to warn about.
  expect(active().dirty).toBe(false)
})
