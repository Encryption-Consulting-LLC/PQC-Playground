/**
 * Pre-execution deploy phases: the posting → queued → preparing → executing
 * walk driven by the plan job stream, the no-walking-backwards guard on late
 * replays, the preflight receipt captured from both the 202 and a structured
 * 409, and how a stream that dies mid-plan unwinds the canvas.
 */

import { beforeAll, beforeEach, expect, test, vi } from "vitest"

import type { StagedOp } from "@/lib/staging"
import type { JobSocketHandlers } from "@/lib/ws"

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

const deployPlanMock = vi.fn()
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    deployPlan: (...args: unknown[]) => deployPlanMock(...args),
  }
})

let handlers: JobSocketHandlers | null = null
vi.mock("@/lib/ws", () => ({
  openJobSocket: vi.fn(
    (_jobId: string, _token: unknown, socketHandlers: JobSocketHandlers) => {
      handlers = socketHandlers
      return vi.fn()
    },
  ),
  openAgentsSocket: vi.fn(() => vi.fn()),
  WS_CLOSE_JOB_GONE: 4404,
  WS_CLOSE_UNAUTHORIZED: 4401,
}))

let staging: typeof import("@/store/staging")
let lib: typeof import("@/lib/staging")
let api: typeof import("@/lib/api")
let useTopologyStore: typeof import("@/store/topology")["useTopologyStore"]
let LIFECYCLE: typeof import("@/constants/topology")["LIFECYCLE"]

beforeAll(async () => {
  staging = await import("@/store/staging")
  lib = await import("@/lib/staging")
  api = await import("@/lib/api")
  useTopologyStore = (await import("@/store/topology")).useTopologyStore
  LIFECYCLE = (await import("@/constants/topology")).LIFECYCLE
})

function createVmOp(): StagedOp {
  return {
    id: "op-dc",
    kind: lib.OP_KIND.createVm,
    targetNodeId: "node-dc",
    params: {},
    dependsOn: [],
    label: "Create dc01",
    status: lib.OP_STATUS.staged,
  }
}

function seed() {
  useTopologyStore.setState({
    nodes: [
      {
        id: "node-dc",
        type: "machine",
        position: { x: 0, y: 0 },
        data: {
          name: "dc01",
          typeId: "domainController",
          lifecycle: LIFECYCLE.staged,
          config: {},
          poweredOn: false,
        },
      },
    ],
    edges: [],
  })
  staging.useStagingStore.setState({
    ops: [createVmOp()],
    deployJobId: null,
    deploying: false,
    planPhase: null,
    planPhaseDetail: null,
    deployStartedAt: null,
    preflightReceipt: null,
  })
}

function state() {
  return staging.useStagingStore.getState()
}

beforeEach(() => {
  handlers = null
  deployPlanMock.mockReset()
  seed()
})

test("deploy walks posting → queued → preparing → executing off the job stream", async () => {
  deployPlanMock.mockResolvedValue({
    job_id: "job9",
    preflight: {
      ready: true,
      checkedAt: 123,
      checks: [{ key: "vmNames", ok: true, detail: "No collisions." }],
    },
  })

  state().deploy()
  expect(state().deploying).toBe(true)
  expect(state().planPhase).toBe("posting")
  expect(state().deployStartedAt).not.toBeNull()
  expect(state().preflightReceipt).toBeNull()

  await vi.waitFor(() => expect(state().planPhase).toBe("queued"))
  expect(state().deployJobId).toBe("job9")
  expect(state().preflightReceipt?.ready).toBe(true)

  handlers!.onProgress!({
    type: "progress",
    percent: 0,
    phase: "Connecting to the ESXi host…",
    key: "planSetup",
    unit: "%",
  })
  expect(state().planPhase).toBe("preparing")
  expect(state().planPhaseDetail).toBe("Connecting to the ESXi host…")

  // Progress frames with any other key are not plan-setup breadcrumbs.
  handlers!.onProgress!({
    type: "progress",
    percent: 40,
    phase: "Cloning disks",
    key: "clone",
    unit: "%",
  })
  expect(state().planPhaseDetail).toBe("Connecting to the ESXi host…")

  handlers!.onRunning!({ type: "running" })
  expect(state().planPhase).toBe("preparing")
  expect(state().planPhaseDetail).toBeNull()

  handlers!.onPlanState!({
    type: "plan-state",
    ops: { "op-dc": { status: "queued" } },
  })
  expect(state().planPhase).toBe("executing")

  // A late `queued` replay (snapshot re-send on reconnect) must not walk back.
  handlers!.onQueued!({ type: "queued" })
  expect(state().planPhase).toBe("executing")
})

test("finishDeploy resets the phase fields but keeps the receipt", () => {
  staging.useStagingStore.setState({
    deploying: true,
    deployJobId: "job9",
    planPhase: "executing",
    planPhaseDetail: null,
    deployStartedAt: 111,
    preflightReceipt: { ready: true, checks: [] },
  })

  staging.finishDeploy(
    { ops: { "op-dc": { status: "error", detail: "boom" } } },
    "job9",
  )

  expect(state().deploying).toBe(false)
  expect(state().planPhase).toBeNull()
  expect(state().deployStartedAt).toBeNull()
  expect(state().preflightReceipt?.ready).toBe(true)
})

test("a structured 409 captures the failed preflight as the receipt", async () => {
  deployPlanMock.mockRejectedValue(
    new api.ApiError(409, "Infrastructure preflight failed.", {
      message: "Infrastructure preflight failed.",
      preflight: {
        ready: false,
        checks: [{ key: "capacity", ok: false, detail: "Datastore is full." }],
      },
    }),
  )

  state().deploy()
  await vi.waitFor(() => expect(state().deploying).toBe(false))

  expect(state().planPhase).toBeNull()
  expect(state().deployStartedAt).toBeNull()
  expect(state().preflightReceipt?.ready).toBe(false)
  expect(state().preflightReceipt?.checks[0]?.detail).toBe("Datastore is full.")
  expect(state().ops.every((op) => op.status === lib.OP_STATUS.staged)).toBe(
    true,
  )
})

test("a plain failure leaves no receipt and phase updates are ignored while idle", async () => {
  deployPlanMock.mockRejectedValue(new api.ApiError(500, "boom", "boom"))

  state().deploy()
  await vi.waitFor(() => expect(state().deploying).toBe(false))
  expect(state().preflightReceipt).toBeNull()
  expect(state().planPhase).toBeNull()
})

/** Deploys until the plan socket is attached and its handlers are captured. */
async function deployUntilStreaming() {
  deployPlanMock.mockResolvedValue({ job_id: "job9", preflight: null })
  state().deploy()
  await vi.waitFor(() => expect(state().deployJobId).toBe("job9"))
}

/** An expired-job close: unrecoverable, so it unwinds without retrying first. */
function loseStream() {
  handlers!.onError!({
    type: "error",
    status: 0,
    detail: "Progress connection closed before completion.",
    code: 4404,
  })
}

function node() {
  return useTopologyStore.getState().nodes[0].data
}

test("a lost stream leaves a node whose clone landed as failed, not staged", async () => {
  await deployUntilStreaming()
  useTopologyStore.getState().patchNodeData("node-dc", {
    lifecycle: LIFECYCLE.provisioning,
    vmName: "guest-guest-afba6e-dc01",
    ip: "192.168.181.6",
  })

  loseStream()

  // `staged` would claim there is no VM yet; there is one, with an IP.
  expect(node().lifecycle).toBe(LIFECYCLE.failed)
  expect(node().errorDetail).toBeTruthy()
  expect(node().vmName).toBe("guest-guest-afba6e-dc01")
  // The op is still retryable, and the deploy lock is released either way.
  expect(state().ops[0].status).toBe(lib.OP_STATUS.staged)
  expect(state().deploying).toBe(false)
})

test("a lost stream still reverts a node with no VM to staged", async () => {
  await deployUntilStreaming()

  loseStream()

  expect(node().lifecycle).toBe(LIFECYCLE.staged)
  expect(node().errorDetail).toBeUndefined()
})
