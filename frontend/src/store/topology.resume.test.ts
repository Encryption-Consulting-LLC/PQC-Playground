/**
 * Rehydrating a reload that landed mid-deploy.
 *
 * The incident: a plan deploy ran overnight with the tab closed. On reopening,
 * `resumeJobs` demoted every plan-driven node to `draft` — a node whose clone
 * had already succeeded reads as one that was never deployed, and it did so
 * while the plan socket was resuming and about to report the truth.
 */

import { beforeAll, beforeEach, expect, test, vi } from "vitest"

import type { StagedOp } from "@/lib/staging"

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
let lib: typeof import("@/lib/staging")
let useTopologyStore: typeof import("@/store/topology")["useTopologyStore"]
let useAuthStore: typeof import("@/store/auth")["useAuthStore"]
let LIFECYCLE: typeof import("@/constants/topology")["LIFECYCLE"]

beforeAll(async () => {
  staging = await import("@/store/staging")
  lib = await import("@/lib/staging")
  useTopologyStore = (await import("@/store/topology")).useTopologyStore
  useAuthStore = (await import("@/store/auth")).useAuthStore
  LIFECYCLE = (await import("@/constants/topology")).LIFECYCLE
})

/** A clone op that already succeeded — `done`, so not "outstanding" any more. */
function doneCloneOp(): StagedOp {
  return {
    id: "op-dc",
    kind: lib.OP_KIND.createVm,
    targetNodeId: "node-dc",
    params: {},
    dependsOn: [],
    label: "Create dc01",
    status: lib.OP_STATUS.done,
  }
}

function seed(nodeData: Partial<import("@/store/topology").MachineData> = {}) {
  useTopologyStore.setState({
    nodes: [
      {
        id: "node-dc",
        type: "machine",
        position: { x: 0, y: 0 },
        data: {
          name: "dc01",
          typeId: "domainController",
          lifecycle: LIFECYCLE.deploying,
          config: {},
          poweredOn: false,
          ...nodeData,
        },
      },
    ],
    edges: [],
  })
}

function node() {
  return useTopologyStore.getState().nodes[0].data
}

beforeEach(() => {
  useAuthStore.setState({ token: "live-token" })
  staging.useStagingStore.setState({
    ops: [doneCloneOp()],
    deployJobId: null,
    deploying: false,
    planPhase: null,
    planPhaseDetail: null,
    deployStartedAt: null,
    preflightReceipt: null,
  })
})

test("a resuming plan job keeps its nodes deploying instead of reverting them", () => {
  seed()
  staging.useStagingStore.setState({ deployJobId: "job-overnight" })

  useTopologyStore.getState().resumeJobs()

  // Previously `draft`: the clone op is `done`, so it no longer counts as an
  // outstanding staged op and the fallback guessed "never deployed".
  expect(node().lifecycle).toBe(LIFECYCLE.deploying)
})

test("without a plan job, a node with a real VM reads failed rather than draft", () => {
  seed({ vmName: "guest-guest-afba6e-dc01", ip: "192.168.181.6" })

  useTopologyStore.getState().resumeJobs()

  expect(node().lifecycle).toBe(LIFECYCLE.failed)
  expect(node().errorDetail).toBeTruthy()
  // Identity survives, so the Tear down VM affordance stays reachable.
  expect(node().vmName).toBe("guest-guest-afba6e-dc01")
})

test("without a plan job or a VM, an outstanding clone op still reverts to staged", () => {
  seed()
  staging.useStagingStore.setState({
    ops: [{ ...doneCloneOp(), status: lib.OP_STATUS.running }],
  })

  useTopologyStore.getState().resumeJobs()

  expect(node().lifecycle).toBe(LIFECYCLE.staged)
})

test("without a plan job, a VM-less node with no outstanding op falls back to draft", () => {
  seed()

  useTopologyStore.getState().resumeJobs()

  expect(node().lifecycle).toBe(LIFECYCLE.draft)
})
