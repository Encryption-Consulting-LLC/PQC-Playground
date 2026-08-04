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
const localStorageStub = {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => storage.set(key, value),
  removeItem: (key: string) => storage.delete(key),
}

vi.stubGlobal("localStorage", localStorageStub)
vi.stubGlobal("window", {
  // The auth store persists through zustand's `persist`, which reads
  // `window.localStorage` rather than the bare global.
  localStorage: localStorageStub,
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
})

const deployPlanMock = vi.fn()
const getDeployRunMock = vi.fn()
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>()
  return {
    ...actual,
    deployPlan: (...args: unknown[]) => deployPlanMock(...args),
    getDeployRun: (...args: unknown[]) => getDeployRunMock(...args),
  }
})

const saveActiveProjectNowMock = vi.fn(async () => true)
vi.mock("@/lib/projectSync", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/projectSync")>()
  return {
    ...actual,
    saveActiveProjectNow: () => saveActiveProjectNowMock(),
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
let useAuthStore: typeof import("@/store/auth")["useAuthStore"]
let useTopologyStore: typeof import("@/store/topology")["useTopologyStore"]
let LIFECYCLE: typeof import("@/constants/topology")["LIFECYCLE"]

beforeAll(async () => {
  staging = await import("@/store/staging")
  lib = await import("@/lib/staging")
  api = await import("@/lib/api")
  useAuthStore = (await import("@/store/auth")).useAuthStore
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
  getDeployRunMock.mockReset()
  getDeployRunMock.mockRejectedValue(new Error("not stubbed"))
  saveActiveProjectNowMock.mockReset()
  saveActiveProjectNowMock.mockResolvedValue(true)
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

test("finishDeploy resets the phase fields but keeps the receipt and the clock", () => {
  staging.useStagingStore.setState({
    deploying: true,
    deployJobId: "job9",
    planPhase: "executing",
    planPhaseDetail: null,
    deployStartedAt: 111,
    deployFinishedAt: null,
    preflightReceipt: { ready: true, checks: [] },
  })

  staging.finishDeploy(
    { ops: { "op-dc": { status: "error", detail: "boom" } } },
    "job9",
  )

  expect(state().deploying).toBe(false)
  expect(state().planPhase).toBeNull()
  expect(state().preflightReceipt?.ready).toBe(true)
  // Both timestamps survive the run: the panel reports the total after the live
  // clock unmounts, which it cannot do if the start time is cleared here.
  expect(state().deployStartedAt).toBe(111)
  expect(state().deployFinishedAt).toBeGreaterThan(111)
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

test("deploy saves the project before sending the plan", async () => {
  deployPlanMock.mockResolvedValue({ job_id: "job9", preflight: null })

  state().deploy()
  await vi.waitFor(() => expect(state().deployJobId).toBe("job9"))

  expect(saveActiveProjectNowMock).toHaveBeenCalledOnce()
  // A deploy names real VMs after the project; the snapshot has to exist first.
  expect(saveActiveProjectNowMock.mock.invocationCallOrder[0]).toBeLessThan(
    deployPlanMock.mock.invocationCallOrder[0],
  )
})

test("a project that cannot be saved is not deployed", async () => {
  saveActiveProjectNowMock.mockResolvedValue(false)

  state().deploy()
  await vi.waitFor(() => expect(state().deploying).toBe(false))

  expect(deployPlanMock).not.toHaveBeenCalled()
  // Fully unwound: the ops are staged again and the canvas lock is released.
  expect(state().ops.every((op) => op.status === lib.OP_STATUS.staged)).toBe(
    true,
  )
  expect(state().deployJobId).toBeNull()
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

/**
 * Reload-resume: `loadOps` deliberately clears every client-only deploy field,
 * so the run's start time, preflight receipt, and compiled manifest have to come
 * back off the server or the panel silently loses its clock, its receipt, and
 * its step trees for the rest of the deploy.
 */
const RUN_FACTS = {
  jobId: "job9",
  createdAt: 1_785_781_000_000,
  preflight: {
    ready: true,
    checkedAt: 1_785_781_000_000,
    checks: [{ key: "vmNames", ok: true, detail: "No collisions." }],
  },
  groups: [
    {
      id: "op-dc",
      kind: "createVm",
      label: "Clone VM",
      target: "node-dc",
      dependsOn: [],
      sourceBase: "ws-2025-base",
      steps: [
        {
          id: "prepare",
          label: "Prepare guest IP and first-boot media",
          kind: "backend" as const,
          targetNodeId: "node-dc",
          dependsOn: [],
        },
      ],
    },
    {
      id: "op-dc::provision",
      kind: "provision",
      label: "Provision dc01 — AD DS forest",
      target: "node-dc",
      dependsOn: ["op-dc"],
      steps: [
        {
          id: "agent-ready",
          label: "Wait for executor agent",
          kind: "wait" as const,
          targetNodeId: "node-dc",
          dependsOn: [],
        },
      ],
    },
  ],
}

test("resuming a plan job restores the run's clock, receipt, and step trees", async () => {
  useAuthStore.setState({ token: "live-token" })
  getDeployRunMock.mockResolvedValue(RUN_FACTS)

  staging.useStagingStore.getState().loadOps([createVmOp()], "job9")

  // The socket is attached synchronously; only the run facts are awaited.
  expect(state().deploying).toBe(true)
  await vi.waitFor(() =>
    expect(state().deployStartedAt).toBe(1_785_781_000_000),
  )
  expect(getDeployRunMock).toHaveBeenCalledWith("job9")
  expect(state().preflightReceipt?.checks[0]?.detail).toBe("No collisions.")
  // The manifest lands on the rows, so an expanded op still shows its steps and
  // the read-only provision row is back.
  expect(state().ops[0].executionGroup?.label).toBe("Clone VM")
  expect(state().ops[1].id).toBe("op-dc::provision")
  expect(state().ops[1].synthesized).toBe(true)
})

test("run facts arriving after a project switch are dropped", async () => {
  useAuthStore.setState({ token: "live-token" })
  let release: (value: unknown) => void = () => {}
  getDeployRunMock.mockReturnValue(
    new Promise((resolve) => {
      release = resolve
    }),
  )

  staging.useStagingStore.getState().loadOps([createVmOp()], "job9")
  // The user switches projects while the request is still out.
  staging.useStagingStore.getState().loadOps([], null)
  release(RUN_FACTS)
  await Promise.resolve()

  expect(state().deployStartedAt).toBeNull()
  expect(state().preflightReceipt).toBeNull()
  expect(state().ops).toEqual([])
})

test("a resumed job whose run facts can't be fetched still streams", async () => {
  useAuthStore.setState({ token: "live-token" })
  getDeployRunMock.mockRejectedValue(new api.ApiError(404, "gone", "gone"))

  staging.useStagingStore.getState().loadOps([createVmOp()], "job9")
  await vi.waitFor(() => expect(getDeployRunMock).toHaveBeenCalled())

  // Best-effort: the socket is what makes the resume work, and it is attached.
  expect(state().deploying).toBe(true)
  expect(handlers).not.toBeNull()
  expect(state().deployStartedAt).toBeNull()
})
