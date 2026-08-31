import { beforeEach, describe, expect, it } from "vitest"

import { EDGE_TYPE } from "@/constants/topology"
import { buildDeployTopology } from "@/lib/deployTopology"
import { buildPkiTemplateIntoStores } from "@/lib/projectTemplate"
import type { StagedOp } from "@/lib/staging"
import { useStagingStore } from "@/store/staging"
import { useTopologyStore } from "@/store/topology"

function operationShape(ops: StagedOp[]) {
  const names = new Map(
    useTopologyStore.getState().nodes.map((node) => [node.id, node.data.name]),
  )
  return ops.map((op) => ({
    kind: op.kind,
    target: names.get(op.targetNodeId),
    secondary: op.secondaryNodeId ? names.get(op.secondaryNodeId) : undefined,
  }))
}

describe("supplied PKI project template", () => {
  beforeEach(() => buildPkiTemplateIntoStores("template-regression"))

  it("matches the guide identities, forest level, and CA algorithm", () => {
    const byName = new Map(
      useTopologyStore.getState().nodes.map((node) => [node.data.name, node]),
    )

    expect([...byName.keys()].sort()).toEqual(["CA01", "CA02", "DC01", "SRV1"])
    expect(byName.get("DC01")?.data.config?.forestLevel).toBe(
      "Windows Server 2016",
    )
    expect(byName.get("CA01")?.data.config).toMatchObject({
      caType: "Root",
      keyAlgorithm: "ML-DSA-87",
    })
    expect(byName.get("CA02")?.data.config).toMatchObject({
      caType: "Issuing",
      keyAlgorithm: "ML-DSA-87",
    })
  })

  it("preserves authored node positions while relationships are connected", () => {
    const positions = Object.fromEntries(
      useTopologyStore
        .getState()
        .nodes.map((node) => [node.data.name, node.position]),
    )

    expect(positions).toEqual({
      CA01: { x: 80, y: 60 },
      CA02: { x: 441, y: 380 },
      DC01: { x: 813, y: 494 },
      SRV1: { x: 989, y: 202 },
    })
  })

  it("stages domain membership before dependent PKI services", () => {
    const { ops } = useStagingStore.getState()

    expect(operationShape(ops)).toEqual([
      { kind: "createVm", target: "CA01", secondary: undefined },
      { kind: "createVm", target: "CA02", secondary: undefined },
      { kind: "createVm", target: "DC01", secondary: undefined },
      { kind: "createVm", target: "SRV1", secondary: undefined },
      { kind: "domainJoin", target: "CA02", secondary: "DC01" },
      { kind: "domainJoin", target: "SRV1", secondary: "DC01" },
      { kind: "caConnect", target: "CA02", secondary: "CA01" },
      { kind: "webServerCert", target: "CA02", secondary: "SRV1" },
    ])

    const indexes = new Map(ops.map((op, index) => [op.id, index]))
    for (const [index, op] of ops.entries()) {
      expect(
        op.dependsOn.every((id) => (indexes.get(id) ?? index) < index),
      ).toBe(true)
    }
  })

  it("preserves the compiler-safe order across persisted reloads", () => {
    const expected = operationShape(useStagingStore.getState().ops)
    const restored = JSON.parse(
      JSON.stringify(useStagingStore.getState().ops),
    ) as StagedOp[]

    useStagingStore.getState().loadOps(restored, null)

    expect(operationShape(useStagingStore.getState().ops)).toEqual(expected)
  })

  it("emits all semantic relationships required by the backend compiler", () => {
    const { nodes, edges } = useTopologyStore.getState()
    const topology = buildDeployTopology(nodes, edges)
    const names = new Map(topology.nodes.map((node) => [node.id, node.name]))

    expect(topology.nodes.every((node) => node.state === "planned")).toBe(true)
    expect(
      topology.edges.map((edge) => ({
        kind: edge.kind,
        source: names.get(edge.source),
        target: names.get(edge.target),
        state: edge.state,
        ports: edge.ports,
      })),
    ).toEqual([
      {
        kind: "domainMembership",
        source: "CA02",
        target: "DC01",
        state: "planned",
        ports: ["domainBoundary"],
      },
      {
        kind: "domainMembership",
        source: "SRV1",
        target: "DC01",
        state: "planned",
        ports: ["domainBoundary"],
      },
      {
        kind: "caParent",
        source: "CA01",
        target: "CA02",
        state: "planned",
        ports: ["caParent"],
      },
      {
        kind: "caPublication",
        source: "CA02",
        target: "SRV1",
        state: "planned",
        ports: ["caPublication", "webHost", "probeCertificate"],
      },
    ])

    expect(
      topology.dnsRecords.map((record) => ({
        kind: record.kind,
        server: names.get(record.server),
        subject: names.get(record.subject),
        zone: record.zone,
        name: record.name,
      })),
    ).toEqual([
      {
        kind: "A",
        server: "DC01",
        subject: "CA02",
        zone: "encon.pki",
        name: undefined,
      },
      {
        kind: "A",
        server: "DC01",
        subject: "DC01",
        zone: "encon.pki",
        name: undefined,
      },
      {
        kind: "A",
        server: "DC01",
        subject: "SRV1",
        zone: "encon.pki",
        name: undefined,
      },
      {
        kind: "CNAME",
        server: "DC01",
        subject: "SRV1",
        zone: "encon.pki",
        name: "pki",
      },
    ])
  })

  it("requires both issuing service sockets before exporting publication", () => {
    const { nodes, edges } = useTopologyStore.getState()
    const withoutOcsp = edges.filter(
      (edge) => edge.data?.serviceSocket !== "ocsp",
    )
    const topology = buildDeployTopology(nodes, withoutOcsp)

    expect(topology.edges.some((edge) => edge.kind === "caPublication")).toBe(
      false,
    )
    expect(topology.dnsRecords.some((record) => record.kind === "CNAME")).toBe(
      false,
    )
  })

  it("upgrades a legacy aggregate service edge on snapshot load", () => {
    const { nodes, edges, counters, viewport } = useTopologyStore.getState()
    const publication = edges.find(
      (edge) =>
        edge.data?.edgeType === EDGE_TYPE.webServerCert &&
        edge.data?.serviceSocket === "publication" &&
        nodes.find((node) => node.id === edge.source)?.data.config?.caType ===
          "Issuing",
    )
    expect(publication).toBeDefined()
    const legacy = {
      ...publication!,
      data: { ...publication!.data, serviceSocket: undefined },
    }

    useTopologyStore
      .getState()
      .loadSnapshot(nodes, [legacy], counters, viewport)

    expect(
      useTopologyStore
        .getState()
        .edges.map((edge) => edge.data?.serviceSocket)
        .sort(),
    ).toEqual(["ocsp", "publication"])
  })

  it("keeps domain membership edges logical-only on snapshot load", () => {
    const { nodes, edges, counters, viewport } = useTopologyStore.getState()
    const persistedEdges = edges.map((edge) =>
      edge.data?.edgeType === EDGE_TYPE.domainJoin
        ? { ...edge, hidden: false }
        : edge,
    )

    useTopologyStore
      .getState()
      .loadSnapshot(nodes, persistedEdges, counters, viewport)

    const domainEdges = useTopologyStore
      .getState()
      .edges.filter((edge) => edge.data?.edgeType === EDGE_TYPE.domainJoin)
    expect(domainEdges).toHaveLength(2)
    expect(domainEdges.every((edge) => edge.hidden === true)).toBe(true)
  })

  it("derives PTR resources only when a reverse zone is configured", () => {
    const dc = useTopologyStore
      .getState()
      .nodes.find((node) => node.data.name === "DC01")
    expect(dc).toBeDefined()
    useTopologyStore.getState().patchNodeData(dc!.id, {
      config: {
        ...dc!.data.config,
        reverseZone: "100.168.192.in-addr.arpa",
      },
    })

    const { nodes, edges } = useTopologyStore.getState()
    const topology = buildDeployTopology(nodes, edges)
    expect(
      topology.dnsRecords.filter((record) => record.kind === "PTR"),
    ).toHaveLength(3)
  })
})

describe("the DC's seeded NetBIOS name", () => {
  function seededNetbios() {
    return useTopologyStore
      .getState()
      .nodes.find((node) => node.data.name === "DC01")?.data.config?.netbiosName
  }

  it("carries the per-project prefix that keeps guest labs distinct", () => {
    buildPkiTemplateIntoStores("cc92f3-lab", { netbiosPrefixed: true })

    expect(seededNetbios()).toBe("cc92f3-ENCON")
  })

  it("is left bare for an operator, who gets the full 15-char budget", () => {
    buildPkiTemplateIntoStores("cc92f3-lab", { netbiosPrefixed: false })

    expect(seededNetbios()).toBe("ENCON")
  })
})
