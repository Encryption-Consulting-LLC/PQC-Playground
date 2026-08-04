/**
 * A node's name is only editable before it's configured.
 *
 * From `staged` on, a createVm op names the real clone after `data.name` (read
 * fresh at deploy by `buildOpPayload`) and the authored `10-hostname` script is
 * rendered from it — so a late rename would silently disagree with what actually
 * boots. Removing the staged op sends the node back to `draft` (`revertOp`),
 * which is what re-opens renaming without any extra state.
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

let staging: typeof import("@/store/staging")
let useTopologyStore: typeof import("@/store/topology")["useTopologyStore"]
let LIFECYCLE: typeof import("@/constants/topology")["LIFECYCLE"]

beforeAll(async () => {
  staging = await import("@/store/staging")
  useTopologyStore = (await import("@/store/topology")).useTopologyStore
  LIFECYCLE = (await import("@/constants/topology")).LIFECYCLE
})

function seed(lifecycle: string) {
  useTopologyStore.setState({
    nodes: [
      {
        id: "node-dc",
        type: "machine",
        position: { x: 0, y: 0 },
        data: {
          name: "DC01",
          typeId: "domainController",
          lifecycle: lifecycle as never,
          poweredOn: false,
        },
      },
    ],
    edges: [],
  })
}

function name() {
  return useTopologyStore.getState().nodes[0]?.data.name
}

beforeEach(() => {
  staging.useStagingStore.setState({ ops: [], deploying: false })
})

test("a draft node renames", () => {
  seed(LIFECYCLE.draft)
  useTopologyStore.getState().renameNode("node-dc", "DC-LAB")
  expect(name()).toBe("DC-LAB")
})

test("a failed node renames — retrying may be the point of the rename", () => {
  seed(LIFECYCLE.failed)
  useTopologyStore.getState().renameNode("node-dc", "DC-LAB")
  expect(name()).toBe("DC-LAB")
})

test.each([
  ["staged", () => LIFECYCLE.staged],
  ["deploying", () => LIFECYCLE.deploying],
  ["provisioning", () => LIFECYCLE.provisioning],
  ["deployed", () => LIFECYCLE.deployed],
  ["drifted", () => LIFECYCLE.drifted],
])("a %s node refuses to rename", (_label, lifecycle) => {
  seed(lifecycle())
  useTopologyStore.getState().renameNode("node-dc", "DC-LAB")
  expect(name()).toBe("DC01")
})

test("configure then remove the staged op re-opens renaming", () => {
  seed(LIFECYCLE.draft)
  useTopologyStore.getState().configureNode("node-dc", { domainName: "lab" })
  expect(useTopologyStore.getState().nodes[0]?.data.lifecycle).toBe(
    LIFECYCLE.staged,
  )

  useTopologyStore.getState().renameNode("node-dc", "DC-LAB")
  expect(name()).toBe("DC01")

  const op = staging.useStagingStore.getState().ops[0]
  staging.useStagingStore.getState().removeOpCascade(op.id)

  expect(useTopologyStore.getState().nodes[0]?.data.lifecycle).toBe(
    LIFECYCLE.draft,
  )
  useTopologyStore.getState().renameNode("node-dc", "DC-LAB")
  expect(name()).toBe("DC-LAB")
})

test("a rename is refused outright while a deploy is in flight", () => {
  seed(LIFECYCLE.draft)
  staging.useStagingStore.setState({ deploying: true })
  useTopologyStore.getState().renameNode("node-dc", "DC-LAB")
  expect(name()).toBe("DC01")
})
