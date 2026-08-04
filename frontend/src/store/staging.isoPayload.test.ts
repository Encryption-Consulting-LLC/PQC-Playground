/**
 * What the Config ISO panel actually puts on the wire.
 *
 * Two rules worth pinning. Inline PACK files are withheld from a non-operator:
 * `ISO_AUTHOR` is operator-only, so the backend 403s the *whole plan* on any
 * inline content — and a share-linked project can carry `isoAuthoring` seeded by
 * its operator owner, so this is reachable without the (operator-gated) panel.
 * And an empty PACK grid is no longer a deploy blocker: since those files only
 * override the rendered set, none of them just means "render all of it".
 */

import { beforeAll, beforeEach, expect, test, vi } from "vitest"

import type { StagedOp } from "@/lib/staging"
import type { IsoAuthoring } from "@/store/topology"

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
let useAuthStore: typeof import("@/store/auth")["useAuthStore"]
let useTopologyStore: typeof import("@/store/topology")["useTopologyStore"]
let LIFECYCLE: typeof import("@/constants/topology")["LIFECYCLE"]
let ROLES: typeof import("@/constants")["ROLES"]

beforeAll(async () => {
  staging = await import("@/store/staging")
  lib = await import("@/lib/staging")
  useAuthStore = (await import("@/store/auth")).useAuthStore
  useTopologyStore = (await import("@/store/topology")).useTopologyStore
  LIFECYCLE = (await import("@/constants/topology")).LIFECYCLE
  ROLES = (await import("@/constants")).ROLES
})

function createVmOp(): StagedOp {
  return {
    id: "op-dc",
    kind: lib.OP_KIND.createVm,
    targetNodeId: "node-dc",
    params: {},
    dependsOn: [],
    label: "Clone VM",
    status: lib.OP_STATUS.staged,
  }
}

function seed(iso?: IsoAuthoring) {
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
          config: { domainName: "lab.local" },
          poweredOn: false,
          ...(iso ? { isoAuthoring: iso } : {}),
        },
      },
    ],
    edges: [],
  })
  staging.useStagingStore.setState({ ops: [createVmOp()] })
}

const PACK_FILES = [{ name: "10-hostname.ps1", content: "Rename-Computer x" }]

function packIso(): IsoAuthoring {
  return { enabled: true, mode: "pack", files: PACK_FILES, seeded: true }
}

function opPayload() {
  return staging.prepareDeployPlan().payload.find((op) => op.id === "op-dc")
}

beforeEach(() => {
  useAuthStore.setState({ role: ROLES.operator })
})

test("an operator's pack files ride as inline files", () => {
  seed(packIso())

  const op = opPayload()
  expect(op?.files).toEqual(PACK_FILES)
  // Config still rides as params — the ISO doesn't replace it.
  expect(op?.params.domainName).toBe("lab.local")
  expect(op?.params.vmName).toBe("dc01")
})

test("a guest's pack files are withheld so the plan isn't 403'd", () => {
  useAuthStore.setState({ role: ROLES.guest })
  seed(packIso())

  const op = opPayload()
  expect(op?.files).toBeUndefined()
  // The op itself still deploys — it just gets the server-rendered disc.
  expect(op?.params.vmName).toBe("dc01")
})

test("an uploaded ISO rides as an isoId param for an operator", () => {
  seed({
    enabled: true,
    mode: "uploadIso",
    files: [],
    isoId: "abc123",
  })

  expect(opPayload()?.params.isoId).toBe("abc123")
})

test("a disabled panel sends neither files nor an isoId", () => {
  seed({ enabled: false, mode: "pack", files: PACK_FILES })

  const op = opPayload()
  expect(op?.files).toBeUndefined()
  expect(op?.params.isoId).toBeUndefined()
})
