import { describe, expect, it } from "vitest"

import { EDGE_TYPE, LIFECYCLE } from "@/constants/topology"
import { buildDeployTopology } from "@/lib/deployTopology"
import { connectionPorts } from "@/lib/topology"
import type { Edge, Node } from "@xyflow/react"
import type { MachineData } from "@/store/topology"

function machine(
  id: string,
  name: string,
  typeId: string,
  config: Record<string, string> = {},
): Node<MachineData> {
  return {
    id,
    position: { x: 0, y: 0 },
    data: {
      name,
      typeId,
      config,
      lifecycle: LIFECYCLE.staged,
      poweredOn: false,
    },
  }
}

function integration(source: string, target: string): Edge {
  return {
    id: `e-integration-${source}-${target}`,
    source,
    target,
    data: {
      edgeType: EDGE_TYPE.productIntegration,
      ports: connectionPorts(EDGE_TYPE.productIntegration),
      staged: true,
    },
  }
}

const DC = machine("dc", "DC01", "domainController", {
  domainName: "encon.pki",
  netbiosName: "ENCON",
})

const PRODUCT = machine("product", "CS01", "certsecure", {
  frontendHost: "certsecure.encon.pki",
  backendHost: "certsecure-api.encon.pki",
  keycloakHost: "kc.encon.pki",
  keycloakRealm: "encon.pki",
})

describe("product appliances in the deploy topology", () => {
  it("compiles as its own role, not as a standalone Windows box", () => {
    // `standalone` is what every Linux product used to send, and it is what the
    // backend still accepts from a topology saved before this role existed —
    // but a product integration can only be validated against a node the
    // backend knows is a product.
    const payload = buildDeployTopology([DC, PRODUCT], [])
    const node = payload.nodes.find((n) => n.id === "product")
    expect(node?.role).toBe("product")
  })

  it("sends the integration as its own relationship kind", () => {
    const payload = buildDeployTopology(
      [DC, PRODUCT],
      [integration("product", "dc")],
    )
    const edge = payload.edges.find((e) => e.source === "product")
    expect(edge?.kind).toBe("productIntegration")
    expect(edge?.target).toBe("dc")
    // Not a membership: nothing joins the forest and no computer account is
    // created, and compiling it as one would stage a domain join on Ubuntu.
    expect(payload.edges.some((e) => e.kind === "domainMembership")).toBe(false)
  })

  it("registers one A record per configured service name", () => {
    const payload = buildDeployTopology(
      [DC, PRODUCT],
      [integration("product", "dc")],
    )
    const records = payload.dnsRecords.filter((r) => r.subject === "product")
    expect(records.map((r) => r.name).sort()).toEqual([
      "certsecure",
      "certsecure-api",
      "kc",
    ])
    expect(records.every((r) => r.kind === "A" && r.zone === "encon.pki")).toBe(
      true,
    )
    expect(records.every((r) => r.server === "dc")).toBe(true)
  })

  it("plans no record for a name outside the lab's zone", () => {
    // The product still installs under that name; the lab's DNS is simply not
    // authoritative for it, and inventing a record there would be a lie.
    const product = machine("product", "CS01", "certsecure", {
      ...PRODUCT.data.config,
      backendHost: "certsecure-api.example.com",
    })
    const payload = buildDeployTopology(
      [DC, product],
      [integration("product", "dc")],
    )
    expect(
      payload.dnsRecords
        .filter((r) => r.subject === "product")
        .map((r) => r.name),
    ).toEqual(["certsecure", "kc"])
  })

  it("plans no records at all for an unintegrated product", () => {
    const payload = buildDeployTopology([DC, PRODUCT], [])
    expect(payload.dnsRecords.filter((r) => r.subject === "product")).toEqual(
      [],
    )
  })
})
