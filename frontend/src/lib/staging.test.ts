/**
 * Synthetic provision rows: materialization from plan-state frames, exclusion
 * from the deploy payload, the split node-lifecycle transitions, and the
 * finishDeploy retain/drop pairing with the parent createVm row.
 */

import { beforeAll, beforeEach, expect, test, vi } from "vitest"

import type { StagedOp } from "@/lib/staging"
import type { OpRunState } from "@/lib/ws"

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

let staging: typeof import("@/store/staging")
let lib: typeof import("@/lib/staging")
let useTopologyStore: typeof import("@/store/topology")["useTopologyStore"]
let LIFECYCLE: typeof import("@/constants/topology")["LIFECYCLE"]

beforeAll(async () => {
  staging = await import("@/store/staging")
  lib = await import("@/lib/staging")
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
    status: lib.OP_STATUS.pending,
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
    deployJobId: "job1",
    deploying: true,
  })
}

function node() {
  return useTopologyStore.getState().nodes.find((n) => n.id === "node-dc")!
}

beforeEach(() => {
  seed()
})

test("materializes one synthetic row after its parent, idempotently", () => {
  const frame: Record<string, OpRunState> = {
    "op-dc::provision": { status: "pending" },
  }
  staging.applyPlanState(frame, "job1")
  staging.applyPlanState(frame, "job1")

  const ops = staging.useStagingStore.getState().ops
  expect(ops.map((o) => o.id)).toEqual(["op-dc", "op-dc::provision"])
  const row = ops[1]
  expect(row.synthesized).toBe(true)
  expect(row.kind).toBe(lib.OP_KIND.provision)
  expect(row.label).toBe("Provision dc01 — AD DS forest")
  expect(row.dependsOn).toEqual(["op-dc"])
})

test("caches compiler steps and provision rows for reload recovery", () => {
  staging.useStagingStore.getState().cacheExecutionGroups([
    {
      id: "op-dc",
      kind: "createVm",
      label: "Clone VM",
      target: "node-dc",
      dependsOn: [],
      steps: [
        {
          id: "op-dc:clone",
          label: "Clone the domain controller",
          kind: "clone",
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
          id: "op-dc::provision:forest",
          label: "Install the forest",
          command: "dc.install_forest",
          kind: "agent",
          targetNodeId: "node-dc",
          dependsOn: [],
        },
      ],
    },
  ])

  const ops = staging.useStagingStore.getState().ops
  expect(ops.map((op) => op.id)).toEqual(["op-dc", "op-dc::provision"])
  expect(ops[0].executionGroup?.steps[0].label).toBe(
    "Clone the domain controller",
  )
  expect(ops[1].executionGroup?.steps[0].command).toBe("dc.install_forest")
  expect(ops[1].synthesized).toBe(true)
})

test("unknown provision frames without a parent row are skipped", () => {
  staging.applyPlanState({ "ghost::provision": { status: "running" } }, "job1")

  expect(staging.useStagingStore.getState().ops.map((o) => o.id)).toEqual([
    "op-dc",
  ])
})

test("clone done parks the node in provisioning with its identity facts", () => {
  staging.applyPlanState(
    {
      "op-dc": {
        status: "done",
        result: { vmName: "guest-dc01", ip: "10.0.0.5", agentVmId: "vm-1" },
      },
      "op-dc::provision": { status: "pending" },
    },
    "job1",
  )

  expect(node().data.lifecycle).toBe(LIFECYCLE.provisioning)
  expect(node().data.vmName).toBe("guest-dc01")
  expect(node().data.ip).toBe("10.0.0.5")
})

test("the provision op owns the final transition even when the frame lists it first", () => {
  staging.applyPlanState(
    {
      "op-dc::provision": {
        status: "done",
        result: { vmName: "guest-dc01", agentVmId: "vm-1" },
      },
      "op-dc": { status: "done", result: { vmName: "guest-dc01" } },
    },
    "job1",
  )

  expect(node().data.lifecycle).toBe(LIFECYCLE.deployed)
})

test("provision running drives the node's deploying progress", () => {
  staging.applyPlanState(
    {
      "op-dc": { status: "done", result: { vmName: "guest-dc01" } },
      "op-dc::provision": {
        status: "running",
        percent: 40,
        phase: "Step 1/3 · install-forest",
      },
    },
    "job1",
  )

  expect(node().data.lifecycle).toBe(LIFECYCLE.deploying)
  expect(node().data.progress).toBe(40)
})

test("a failed provision leaves the node failed but keeps vmName for teardown", () => {
  staging.applyPlanState(
    {
      "op-dc": {
        status: "done",
        result: { vmName: "guest-dc01", ip: "10.0.0.5" },
      },
    },
    "job1",
  )
  staging.applyPlanState(
    {
      "op-dc": {
        status: "done",
        result: { vmName: "guest-dc01", ip: "10.0.0.5" },
      },
      "op-dc::provision": { status: "error", detail: "forest install failed" },
    },
    "job1",
  )

  expect(node().data.lifecycle).toBe(LIFECYCLE.failed)
  expect(node().data.errorDetail).toBe("forest install failed")
  expect(node().data.vmName).toBe("guest-dc01")
  expect(node().data.ip).toBe("10.0.0.5")
})

test("synthetic rows never enter the deploy payload", () => {
  staging.applyPlanState({ "op-dc::provision": { status: "pending" } }, "job1")
  const ops = staging.useStagingStore
    .getState()
    .ops.map((op) => ({ ...op, status: lib.OP_STATUS.staged }))

  const prepared = staging.prepareDeployPlan(ops)

  expect(prepared.payload.map((op) => op.kind)).toEqual([lib.OP_KIND.createVm])
  expect(prepared.ops.map((op) => op.id)).toEqual(["op-dc", "op-dc::provision"])
})

test("a synthetic row is dropped from a retry when its done parent is dropped", () => {
  staging.applyPlanState({ "op-dc::provision": { status: "pending" } }, "job1")
  const ops = staging.useStagingStore
    .getState()
    .ops.map((op) =>
      op.synthesized
        ? { ...op, status: lib.OP_STATUS.error }
        : { ...op, status: lib.OP_STATUS.done },
    )

  const prepared = staging.prepareDeployPlan(ops)

  expect(prepared.ops).toEqual([])
  expect(prepared.payload).toEqual([])
})

test("finishDeploy retains a done parent whose provision sibling failed", () => {
  staging.applyPlanState({ "op-dc::provision": { status: "pending" } }, "job1")

  staging.finishDeploy(
    {
      ops: {
        "op-dc": { status: "done", result: { vmName: "guest-dc01" } },
        "op-dc::provision": { status: "error", detail: "boom" },
      },
    },
    "job1",
  )

  const ops = staging.useStagingStore.getState().ops
  expect(ops.map((o) => o.id)).toEqual(["op-dc", "op-dc::provision"])
  expect(ops[0].status).toBe(lib.OP_STATUS.done)
  expect(ops[1].status).toBe(lib.OP_STATUS.error)
})

function issuingCaScenario() {
  useTopologyStore.setState({
    nodes: [
      {
        id: "node-ca2",
        type: "machine",
        position: { x: 0, y: 0 },
        data: {
          name: "ca02",
          typeId: "certificateAuthority",
          lifecycle: LIFECYCLE.staged,
          config: { caType: "Issuing" },
          poweredOn: false,
        },
      },
      {
        id: "node-web",
        type: "machine",
        position: { x: 0, y: 0 },
        data: {
          name: "srv01",
          typeId: "webServer",
          lifecycle: LIFECYCLE.staged,
          config: {},
          poweredOn: false,
        },
      },
    ],
    edges: [],
  })
  staging.useStagingStore.setState({
    ops: [
      {
        id: "op-ca2",
        kind: lib.OP_KIND.createVm,
        targetNodeId: "node-ca2",
        params: {},
        dependsOn: [],
        label: "Create ca02",
        status: lib.OP_STATUS.pending,
      },
      {
        id: "op-web",
        kind: lib.OP_KIND.createVm,
        targetNodeId: "node-web",
        params: {},
        dependsOn: [],
        label: "Create srv01",
        status: lib.OP_STATUS.pending,
      },
      {
        id: "op-join",
        kind: lib.OP_KIND.domainJoin,
        targetNodeId: "node-ca2",
        secondaryNodeId: "node-dc",
        params: {},
        dependsOn: ["op-ca2"],
        label: "Join ca02 to encon.pki",
        status: lib.OP_STATUS.pending,
      },
      {
        id: "op-connect",
        kind: lib.OP_KIND.caConnect,
        targetNodeId: "node-ca2",
        secondaryNodeId: "node-ca1",
        params: {},
        dependsOn: ["op-ca2"],
        label: "Connect ca02 to ca01",
        status: lib.OP_STATUS.pending,
      },
      {
        id: "op-cert",
        kind: lib.OP_KIND.webServerCert,
        targetNodeId: "node-ca2",
        secondaryNodeId: "node-web",
        params: {},
        dependsOn: ["op-ca2", "op-web"],
        label: "Issue srv01 certificate",
        status: lib.OP_STATUS.pending,
      },
    ],
    deployJobId: "job1",
    deploying: true,
  })
}

function nodeById(id: string) {
  return useTopologyStore.getState().nodes.find((n) => n.id === id)!
}

const CA2_SETTLED: Record<string, OpRunState> = {
  "op-ca2": {
    status: "done",
    result: { vmName: "guest-ca02", ip: "10.0.0.6" },
  },
  "op-ca2::provision": { status: "done", result: { vmName: "guest-ca02" } },
}

test("a boot-settled node stays provisioning while its realization ops run", () => {
  issuingCaScenario()
  staging.applyPlanState(
    { ...CA2_SETTLED, "op-join": { status: "running", percent: 10 } },
    "job1",
  )

  expect(nodeById("node-ca2").data.lifecycle).toBe(LIFECYCLE.provisioning)
})

test("cancelled realization ops mark a boot-settled node failed as blocked", () => {
  issuingCaScenario()
  staging.applyPlanState(
    {
      ...CA2_SETTLED,
      "op-join": {
        status: "cancelled",
        detail: "Skipped: a dependency failed or was cancelled.",
      },
      "op-connect": {
        status: "cancelled",
        detail: "Skipped: a dependency failed or was cancelled.",
      },
    },
    "job1",
  )

  const data = nodeById("node-ca2").data
  expect(data.lifecycle).toBe(LIFECYCLE.failed)
  expect(data.errorDetail).toBe(
    "Blocked: domain join, CA connection cancelled because an upstream dependency failed.",
  )
  expect(data.vmName).toBe("guest-ca02")
})

test("a failed realization op surfaces its detail on the node", () => {
  issuingCaScenario()
  staging.applyPlanState(
    {
      ...CA2_SETTLED,
      "op-join": { status: "error", detail: "domain join timed out" },
    },
    "job1",
  )

  const data = nodeById("node-ca2").data
  expect(data.lifecycle).toBe(LIFECYCLE.failed)
  expect(data.errorDetail).toBe("domain join timed out")
})

test("the node deploys only once every realization op is done", () => {
  issuingCaScenario()
  staging.applyPlanState(
    {
      ...CA2_SETTLED,
      "op-join": { status: "done" },
      "op-connect": { status: "done" },
      "op-cert": { status: "done" },
    },
    "job1",
  )

  expect(nodeById("node-ca2").data.lifecycle).toBe(LIFECYCLE.deployed)
})

test("a web host is gated by the webServerCert op that realizes it", () => {
  issuingCaScenario()
  staging.applyPlanState(
    {
      "op-web": { status: "done", result: { vmName: "guest-srv01" } },
      "op-web::provision": {
        status: "done",
        result: { vmName: "guest-srv01" },
      },
      "op-cert": {
        status: "cancelled",
        detail: "Skipped: a dependency failed or was cancelled.",
      },
    },
    "job1",
  )

  const data = nodeById("node-web").data
  expect(data.lifecycle).toBe(LIFECYCLE.failed)
  expect(data.errorDetail).toBe(
    "Blocked: web server certificate cancelled because an upstream dependency failed.",
  )
})

test("nodeAwaitingRealization gates agent-presence promotion", () => {
  issuingCaScenario()
  const ops = staging.useStagingStore
    .getState()
    .ops.map((op) =>
      op.id === "op-join"
        ? { ...op, status: lib.OP_STATUS.running }
        : { ...op, status: lib.OP_STATUS.done },
    )

  expect(lib.nodeAwaitingRealization(ops, "node-ca2")).toBe(true)
  expect(
    lib.nodeAwaitingRealization(
      ops.map((op) => ({ ...op, status: lib.OP_STATUS.done })),
      "node-ca2",
    ),
  ).toBe(false)
})

test("nodeOpsInFlight is per-node, so a finished machine is reachable mid-plan", () => {
  issuingCaScenario()
  const ops = staging.useStagingStore
    .getState()
    .ops.map((op) =>
      op.id === "op-web"
        ? { ...op, status: lib.OP_STATUS.running }
        : { ...op, status: lib.OP_STATUS.done },
    )

  // The whole plan is still `deploying`, but the CA's own steps have landed —
  // its desktop must be openable while the web host is still being stood up.
  expect(lib.nodeOpsInFlight(ops, "node-web")).toBe(true)
  expect(lib.nodeOpsInFlight(ops, "node-ca2")).toBe(false)

  // Staging more work for a deployed node touches nothing yet, so it does not
  // take the desktop away; a terminal failure does not either.
  const staged = ops.map((op) => ({ ...op, status: lib.OP_STATUS.staged }))
  expect(lib.nodeOpsInFlight(staged, "node-web")).toBe(false)
  const failed = ops.map((op) => ({ ...op, status: lib.OP_STATUS.error }))
  expect(lib.nodeOpsInFlight(failed, "node-web")).toBe(false)
})

test("finishDeploy drops synthetic rows alongside their successful parents", () => {
  staging.applyPlanState({ "op-dc::provision": { status: "pending" } }, "job1")

  staging.finishDeploy(
    {
      ops: {
        "op-dc": { status: "done", result: { vmName: "guest-dc01" } },
        "op-dc::provision": {
          status: "done",
          result: { vmName: "guest-dc01" },
        },
      },
    },
    "job1",
  )

  expect(staging.useStagingStore.getState().ops).toEqual([])
  expect(staging.useStagingStore.getState().deploying).toBe(false)
})

test("a webServerCert op's destination is the web host, not the CA it targets", () => {
  issuingCaScenario()
  const ops = staging.useStagingStore.getState().ops
  const byId = (id: string) => ops.find((op) => op.id === id)!

  // The panel subtitle reads this: "Configure PKI services on srv01" used to
  // sit above a grey "ca02" because the op targets the issuing CA.
  expect(lib.opDestinationNodeId(byId("op-cert"))).toBe("node-web")
  expect(lib.opDestinationNodeId(byId("op-connect"))).toBe("node-ca2")
  expect(lib.opDestinationNodeId(byId("op-join"))).toBe("node-ca2")
  expect(lib.opDestinationNodeId(byId("op-ca2"))).toBe("node-ca2")
})

test("a webServerCert op with no secondary falls back to its target", () => {
  issuingCaScenario()
  const op = staging.useStagingStore
    .getState()
    .ops.find((o) => o.id === "op-cert")!
  expect(lib.opDestinationNodeId({ ...op, secondaryNodeId: undefined })).toBe(
    "node-ca2",
  )
})

test("a failed deploy keeps its succeeded rows for context", () => {
  issuingCaScenario()

  staging.finishDeploy(
    {
      ops: {
        "op-ca2": { status: "done", result: { vmName: "guest-ca02" } },
        "op-web": { status: "done", result: { vmName: "guest-srv01" } },
        "op-join": { status: "done" },
        "op-connect": { status: "done" },
        "op-cert": {
          status: "error",
          detail: "agent command 'iis.setup_certenroll' timed out after 300s",
        },
      },
    },
    "job1",
  )

  const ops = staging.useStagingStore.getState().ops
  // Every row survives, in order — the failed op keeps its position (5th, not
  // renumbered to 1) and its dependsOn still resolves inside the list.
  expect(ops.map((o) => o.id)).toEqual([
    "op-ca2",
    "op-web",
    "op-join",
    "op-connect",
    "op-cert",
  ])
  expect(ops.map((o) => o.status)).toEqual([
    "done",
    "done",
    "done",
    "done",
    "error",
  ])
  expect(lib.sanitizeOps(ops).map((o) => o.id)).toEqual(ops.map((o) => o.id))
  // Only the failure is outstanding.
  expect(lib.actionableOps(ops).map((o) => o.id)).toEqual(["op-cert"])
})

test("a clean deploy still empties the list", () => {
  issuingCaScenario()

  staging.finishDeploy(
    {
      ops: {
        "op-ca2": { status: "done", result: { vmName: "guest-ca02" } },
        "op-web": { status: "done", result: { vmName: "guest-srv01" } },
        "op-join": { status: "done" },
        "op-connect": { status: "done" },
        "op-cert": { status: "done" },
      },
    },
    "job1",
  )

  expect(staging.useStagingStore.getState().ops).toEqual([])
})

test("retained rows are never re-sent and never undone", () => {
  issuingCaScenario()
  staging.finishDeploy(
    {
      ops: {
        "op-ca2": { status: "done", result: { vmName: "guest-ca02" } },
        "op-web": { status: "done", result: { vmName: "guest-srv01" } },
        "op-join": { status: "done" },
        "op-connect": { status: "done" },
        "op-cert": { status: "error", detail: "timed out" },
      },
    },
    "job1",
  )

  // A retry sends only the failure, with its dropped dependencies pruned.
  const prepared = staging.prepareDeployPlan()
  expect(prepared.payload.map((op) => op.id)).toEqual(["op-cert"])
  expect(prepared.payload[0].dependsOn).toEqual([])

  // Undo stops at the failed row and refuses to walk into the done ones.
  staging.useStagingStore.getState().undo()
  expect(staging.useStagingStore.getState().ops.map((o) => o.id)).toEqual([
    "op-ca2",
    "op-web",
    "op-join",
    "op-connect",
  ])
  staging.useStagingStore.getState().undo()
  expect(staging.useStagingStore.getState().ops).toHaveLength(4)
})

test("a retry keeps the earlier attempt's succeeded rows, then clears on success", () => {
  // The POST never settles: this test is about what `deploy()` leaves on the
  // list, and a resolved/rejected request would race the assertions (and the
  // next test) with its own state writes.
  // (restored at the end by hand — `unstubAllGlobals` would also tear down the
  // module-level localStorage/window stubs this file runs on.)
  const realFetch = globalThis.fetch
  vi.stubGlobal("fetch", () => new Promise(() => {}))
  issuingCaScenario()
  const failedRun = {
    ops: {
      "op-ca2": { status: "done", result: { vmName: "guest-ca02" } },
      "op-web": { status: "done", result: { vmName: "guest-srv01" } },
      "op-join": { status: "done" },
      "op-connect": { status: "done" },
      "op-cert": { status: "error", detail: "iis.setup_certenroll timed out" },
    },
  }
  staging.finishDeploy(failedRun, "job1")

  // Retry: only the failure goes to the wire, but the four succeeded rows stay
  // on the list — this is where they used to vanish for a second time.
  staging.useStagingStore.getState().deploy()
  const afterRetry = staging.useStagingStore.getState().ops
  expect(afterRetry.map((o) => o.id)).toEqual([
    "op-ca2",
    "op-web",
    "op-join",
    "op-connect",
    "op-cert",
  ])
  expect(afterRetry.map((o) => o.status)).toEqual([
    "done",
    "done",
    "done",
    "done",
    "pending",
  ])

  // The retry fails somewhere else: still the whole picture.
  staging.finishDeploy(
    {
      ops: { "op-cert": { status: "error", detail: "ocsp.configure failed" } },
    },
    "job2",
  )
  expect(staging.useStagingStore.getState().ops).toHaveLength(5)

  // It finally passes — nothing is outstanding, so the queue empties.
  staging.useStagingStore.getState().deploy()
  staging.finishDeploy({ ops: { "op-cert": { status: "done" } } }, "job3")
  expect(staging.useStagingStore.getState().ops).toEqual([])
  vi.stubGlobal("fetch", realFetch)
})
