import { describe, expect, it } from "vitest"

import {
  CONNECTION_HEALTH,
  CONNECTION_PORT,
  EDGE_TYPE,
  LIFECYCLE,
  SERVICE_SOCKET,
} from "@/constants/topology"
import {
  CONNECTION_PORT_GUIDANCE,
  canConnect,
  canConnectServiceSockets,
  connectionMissingRequirements,
  connectionGuidance,
  connectionHealthForOperation,
  connectionPorts,
  configurationNameCollision,
  domainJoinBlockReason,
  domainRelationshipBlockReason,
  domainRelationshipKind,
  inferEdgeType,
  productIntegrationEdge,
  domainJoinEdge,
  domainJoinOperations,
  domainRadius,
  domainRegionBlocker,
  domainRegionSummary,
  findDomainForNode,
  edgeStyle,
  isConnectable,
  lintTopologyRelationships,
  serviceSocketHandleId,
  serviceSocketsForNode,
  trustGravityLayout,
} from "@/lib/topology"
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
      lifecycle: "staged",
      poweredOn: false,
    },
  }
}

function relationship(
  id: string,
  source: string,
  target: string,
  edgeType: string,
  health = "planned",
): Edge {
  return { id, source, target, data: { edgeType, health } }
}

describe("connection capability guidance", () => {
  it("keeps configured staged nodes connectable before deployment", () => {
    const root = machine("root", "CA01", "certificateAuthority", {
      caType: "Root",
    })
    const issuing = machine("issuing", "CA02", "certificateAuthority", {
      caType: "Issuing",
    })

    expect(isConnectable(root.data)).toBe(true)
    expect(isConnectable(issuing.data)).toBe(true)
    expect(canConnect(root.id, issuing.id, [root, issuing], [])).toEqual({
      ok: true,
    })
  })

  it("maps deployable relationships to typed capability ports", () => {
    expect(connectionPorts(EDGE_TYPE.caHierarchy)).toEqual([
      CONNECTION_PORT.caParent,
    ])
    expect(connectionPorts(EDGE_TYPE.domainJoin)).toEqual([
      CONNECTION_PORT.domainBoundary,
    ])
    expect(connectionPorts(EDGE_TYPE.webServerCert)).toEqual([
      CONNECTION_PORT.caPublication,
      CONNECTION_PORT.webHost,
      CONNECTION_PORT.probeCertificate,
    ])
  })

  it("defines every capability named by the roadmap", () => {
    expect(CONNECTION_PORT_GUIDANCE).toMatchObject({
      caParent: { capabilities: ["Issues CA certificate"] },
      caPublication: { capabilities: ["HTTP CDP", "HTTP AIA"] },
      domainBoundary: {
        capabilities: ["AD membership", "DNS resolver", "LDAP publication"],
      },
      webHost: {
        capabilities: ["CertEnroll directory/share", "HTTP CertEnroll"],
      },
      probeCertificate: {
        capabilities: ["OCSP URL", "Online Responder", "Response validation"],
      },
    })
  })

  it("explains prerequisites and generated operations", () => {
    const guidance = connectionGuidance(EDGE_TYPE.webServerCert)

    expect(guidance.intent).toContain("Publishes certificates")
    expect(guidance.requirements).toContain(
      "Issuing CA and web host share an AD domain",
    )
    expect(guidance.operations[0]).toContain("webServerCert")
  })

  it("maps operation progress to the five connection health states", () => {
    expect(connectionHealthForOperation("staged")).toBe(
      CONNECTION_HEALTH.planned,
    )
    expect(connectionHealthForOperation("running")).toBe(
      CONNECTION_HEALTH.applying,
    )
    expect(connectionHealthForOperation("done")).toBe(
      CONNECTION_HEALTH.verified,
    )
    expect(connectionHealthForOperation("cancelled")).toBe(
      CONNECTION_HEALTH.degraded,
    )
    expect(connectionHealthForOperation("error")).toBe(CONNECTION_HEALTH.broken)
  })

  it("colors publication and OCSP edges and dots offline root publication", () => {
    expect(
      edgeStyle(EDGE_TYPE.webServerCert, {
        serviceSocket: SERVICE_SOCKET.publication,
      }).style.stroke,
    ).toBe("#10b981")
    expect(
      edgeStyle(EDGE_TYPE.webServerCert, {
        serviceSocket: SERVICE_SOCKET.ocsp,
      }).style.stroke,
    ).toBe("#8b5cf6")
    expect(
      edgeStyle(EDGE_TYPE.webServerCert, {
        rootIssuer: true,
        serviceSocket: SERVICE_SOCKET.publication,
      }).style,
    ).toMatchObject({
      strokeDasharray: "1 6",
      strokeLinecap: "round",
    })
  })
})

describe("topology relationship linter", () => {
  const nodes = [
    machine("dc", "DC01", "domainController"),
    machine("root", "CA01", "certificateAuthority", { caType: "Root" }),
    machine("issuing", "CA02", "certificateAuthority", { caType: "Issuing" }),
    machine("web", "SRV1", "webServer", { enableOcsp: "Enabled" }),
  ]

  it("reports missing domain, publication, and OCSP grant relationships", () => {
    const diagnostics = lintTopologyRelationships(nodes, [
      relationship("parent", "root", "issuing", EDGE_TYPE.caHierarchy),
    ])

    expect(diagnostics.map((item) => item.message)).toEqual([
      "CA02 is an issuing CA and must be joined to an AD domain.",
      "CA02 publishes HTTP CDP/AIA, but no web host is connected.",
      "SRV1 has OCSP enabled, but no issuing CA grants its enrollment templates.",
    ])
  })

  it("reports failed nodes first instead of silently dropping them", () => {
    const failedRoot = machine("root", "CA01", "certificateAuthority", {
      caType: "Root",
    })
    failedRoot.data.lifecycle = LIFECYCLE.failed
    failedRoot.data.errorDetail =
      "provisioning failed: agent command 'ca.install' timed out"
    const failedDc = machine("dc", "DC01", "domainController")
    failedDc.data.lifecycle = LIFECYCLE.failed

    const diagnostics = lintTopologyRelationships(
      [failedRoot, failedDc, ...nodes.slice(2)],
      [relationship("parent", "root", "issuing", EDGE_TYPE.caHierarchy)],
    )

    expect(diagnostics[0]).toMatchObject({
      code: "deployment-failed",
      severity: "error",
      message:
        "CA01 failed to deploy: provisioning failed: agent command 'ca.install' timed out",
      nodeIds: ["root"],
    })
    expect(diagnostics[1]).toMatchObject({
      code: "deployment-failed",
      message: "DC01 failed to deploy.",
    })
  })

  it("reports a planned CNAME whose web target has no A record", () => {
    const diagnostics = lintTopologyRelationships(nodes, [
      relationship("parent", "root", "issuing", EDGE_TYPE.caHierarchy),
      relationship("issuing-domain", "issuing", "dc", EDGE_TYPE.domainJoin),
      relationship("publication", "issuing", "web", EDGE_TYPE.webServerCert),
    ])

    expect(diagnostics.map((item) => item.message)).toContain(
      "PKI CNAME is planned, but its target SRV1 has no A record.",
    )
  })

  it("reports a failed probe revocation path", () => {
    const diagnostics = lintTopologyRelationships(nodes, [
      relationship("parent", "root", "issuing", EDGE_TYPE.caHierarchy),
      relationship("issuing-domain", "issuing", "dc", EDGE_TYPE.domainJoin),
      relationship("web-domain", "web", "dc", EDGE_TYPE.domainJoin),
      relationship(
        "publication",
        "issuing",
        "web",
        EDGE_TYPE.webServerCert,
        CONNECTION_HEALTH.broken,
      ),
    ])

    expect(diagnostics).toContainEqual(
      expect.objectContaining({
        code: "probe-ocsp-path-unverified",
        severity: "error",
        message:
          "SRV1 can enroll its probe, but no verified OCSP path reaches its certificate.",
      }),
    )
  })
})

describe("living domain model", () => {
  const dc = machine("dc", "DC01", "domainController", {
    domainName: "encon.pki",
    forestLevel: "Windows Server 2025",
  })

  it("uses one eligibility explanation for spatial and accessible joins", () => {
    const draft = machine("draft", "SRV2", "webServer")
    draft.data.lifecycle = "draft"
    const root = machine("root", "CA01", "certificateAuthority", {
      caType: "Root",
    })

    expect(domainJoinBlockReason(draft, dc, [])).toBe(
      "Configure SRV2 before joining it to a domain.",
    )
    expect(domainJoinBlockReason(root, dc, [])).toBe(
      "CA01 is an offline root CA and must remain outside Active Directory.",
    )
    // A product appliance cannot *join*, and the explanation now names the
    // relationship it can take on instead of stopping at a refusal.
    expect(
      domainJoinBlockReason(machine("product", "CBOM01", "cbom"), dc, []),
    ).toBe(
      "CBOM01 is a Linux product server and cannot join a domain; connect it " +
        "to encon.pki's domain controller to register its DNS and trust instead.",
    )
    expect(
      domainJoinBlockReason(machine("web", "SRV1", "webServer"), dc, []),
    ).toBeNull()
  })

  it("routes a product appliance to integration, never to a domain join", () => {
    // The two relationships connect the same pair of shapes and mean different
    // things: one creates a computer account in the forest, the other only
    // registers DNS and pushes trust. Inferring a `domainJoin` here would stage
    // an op that runs `Add-Computer` on Ubuntu.
    const product = machine("product", "CS01", "certsecure")
    expect(inferEdgeType("certsecure", "domainController")).toBe(
      EDGE_TYPE.productIntegration,
    )
    expect(inferEdgeType("webServer", "domainController")).toBe(
      EDGE_TYPE.domainJoin,
    )
    expect(domainRelationshipKind(product)).toBe("integration")
    expect(domainRelationshipKind(machine("web", "SRV1", "webServer"))).toBe(
      "membership",
    )
    expect(domainRelationshipBlockReason(product, dc, [])).toBeNull()
  })

  it("lets a product appliance take on only one domain integration", () => {
    const product = machine("product", "CS01", "certsecure")
    const other = machine("dc2", "DC02", "domainController", {
      domainName: "other.pki",
    })
    const edges = [productIntegrationEdge("product", "dc")]
    expect(domainRelationshipBlockReason(product, dc, edges)).toBe(
      "CS01 already integrates with encon.pki.",
    )
    expect(domainRelationshipBlockReason(product, other, edges)).toBe(
      "CS01 already integrates with another domain.",
    )
  })

  it("grows the domain circle around an integrated product", () => {
    // The circle clears the footprint of everything that belongs to the domain,
    // whichever way it belongs. Counting only joins left a product sitting
    // outside a circle it had just been dropped into, and the next drag read
    // that as having left and proposed dropping the integration.
    const movedDc = { ...dc, position: { x: 0, y: 0 } }
    const product = {
      ...machine("product", "CS01", "certsecure"),
      // Inside the exit margin (radius 520 + 40), so the drag reads as
      // "belongs here" rather than as the drag-out-to-leave gesture.
      position: { x: 540, y: 0 },
    }
    const nodes = [movedDc, product]
    const bare = domainRadius(movedDc, nodes, [])
    const withProduct = domainRadius(movedDc, nodes, [
      productIntegrationEdge("product", "dc"),
    ])
    expect(withProduct).toBeGreaterThan(bare)
    expect(
      findDomainForNode(product, nodes, [
        productIntegrationEdge("product", "dc"),
      ])?.id,
    ).toBe("dc")
  })

  it("rejects a domain bubble that touches an offline root CA", () => {
    const movedDc = { ...dc, position: { x: 0, y: 0 } }
    const root = {
      ...machine("root", "CA01", "certificateAuthority", { caType: "Root" }),
      position: { x: 510, y: 0 },
    }

    expect(domainRegionBlocker(movedDc, [movedDc, root], []))?.toMatchObject({
      node: { id: "root" },
      reason:
        "CA01 is an offline root CA and must remain outside Active Directory.",
    })
  })

  it("finds domain and CA name collisions before configuration", () => {
    const nodes = [
      dc,
      machine("root", "CA01", "certificateAuthority", {
        commonName: "EC-Root-CA",
      }),
      machine("draft-dc", "DC02", "domainController"),
    ]

    expect(
      configurationNameCollision("draft-dc", "domainName", "ENCON.PKI.", nodes),
    ).toBe("This domain name is already used by DC01.")
    expect(
      configurationNameCollision("issuing", "commonName", "ec-root-ca", nodes),
    ).toBe("This CA common name is already used by CA01.")
  })

  it("starts with a large domain boundary before members expand it", () => {
    expect(domainRadius(dc, [dc], [])).toBe(520)
  })

  it("previews the exact role-specific domain join command sequence", () => {
    expect(
      domainJoinOperations(machine("web", "SRV1", "webServer"), dc, []),
    ).toEqual([
      "dns.set_client · use DC01",
      "domain.join · encon.pki",
      "system.reboot → domain.verify",
      "dns.apply_resources → dns.verify · A/PTR",
      "iis.setup_certenroll · share/ACL",
    ])
  })

  it("summarizes forest, member, and domain reach health for the rim", () => {
    const summary = domainRegionSummary(dc, [
      relationship(
        "member",
        "web",
        "dc",
        EDGE_TYPE.domainJoin,
        CONNECTION_HEALTH.degraded,
      ),
    ])

    expect(summary).toMatchObject({
      memberCount: 1,
      forestLevel: "Windows Server 2025",
      forestHealth: CONNECTION_HEALTH.planned,
      domainHealth: CONNECTION_HEALTH.degraded,
    })
  })
})

describe("PKI trust gravity", () => {
  it("settles CA descendants into stable hierarchy tiers", () => {
    const root = {
      ...machine("root", "CA01", "certificateAuthority"),
      position: { x: 500, y: 80 },
    }
    const issuingB = machine("issuing-b", "CA03", "certificateAuthority")
    const issuingA = machine("issuing-a", "CA02", "certificateAuthority")
    const web = machine("web", "SRV1", "webServer")
    const dc = {
      ...machine("dc", "DC01", "domainController"),
      position: { x: 40, y: 40 },
    }
    const edges = [
      relationship("root-b", "root", "issuing-b", EDGE_TYPE.caHierarchy),
      relationship("root-a", "root", "issuing-a", EDGE_TYPE.caHierarchy),
      relationship("publish", "issuing-a", "web", EDGE_TYPE.webServerCert),
    ]

    const settled = trustGravityLayout(
      [root, issuingB, issuingA, web, dc],
      edges,
      "issuing-a",
    )
    const position = (id: string) =>
      settled.find((node) => node.id === id)!.position

    expect(position("root")).toEqual({ x: 500, y: 80 })
    expect(position("issuing-a")).toEqual({ x: 328, y: 424 })
    expect(position("issuing-b")).toEqual({ x: 672, y: 424 })
    expect(position("web")).toEqual({ x: 500, y: 768 })
    expect(position("dc")).toEqual({ x: 40, y: 40 })
  })

  it("leaves unrelated standalone CAs untouched", () => {
    const root = machine("root", "CA01", "certificateAuthority")
    const standalone = {
      ...machine("other", "CA99", "certificateAuthority"),
      position: { x: 900, y: 700 },
    }

    expect(trustGravityLayout([root, standalone], [], "root")).toEqual([
      root,
      standalone,
    ])
  })
})

describe("service socket compatibility", () => {
  const socketConnection = (
    source: string,
    target: string,
    socket: (typeof SERVICE_SOCKET)[keyof typeof SERVICE_SOCKET],
  ) => ({
    source,
    target,
    sourceHandle: serviceSocketHandleId(socket, "source"),
    targetHandle: serviceSocketHandleId(socket, "target"),
  })

  it("accepts only matching role-specific service sockets", () => {
    const root = machine("root", "CA01", "certificateAuthority", {
      caType: "Root",
    })
    const issuing = machine("issuing", "CA02", "certificateAuthority", {
      caType: "Issuing",
    })
    const web = machine("web", "SRV1", "webServer")

    expect(
      canConnectServiceSockets(
        socketConnection("root", "issuing", SERVICE_SOCKET.issuance),
        [root, issuing, web],
        [],
      ),
    ).toEqual({ ok: true })
    expect(
      canConnectServiceSockets(
        socketConnection("root", "web", SERVICE_SOCKET.issuance),
        [root, issuing, web],
        [],
      ).ok,
    ).toBe(false)
  })

  it("exposes only non-spatial service sockets on their supported roles", () => {
    const root = machine("root", "CA01", "certificateAuthority", {
      caType: "Root",
    })
    const issuing = machine("issuing", "CA02", "certificateAuthority", {
      caType: "Issuing",
    })
    const web = machine("web", "SRV1", "webServer")
    const dc = machine("dc", "DC01", "domainController")

    expect(
      serviceSocketsForNode(root, []).map(
        ({ socket, type }) => `${socket}:${type}`,
      ),
    ).toEqual(["issuance:source", "publication:source"])
    expect(serviceSocketsForNode(issuing, [])).not.toContainEqual(
      expect.objectContaining({ socket: "domain" }),
    )
    expect(serviceSocketsForNode(web, [])).toContainEqual({
      socket: SERVICE_SOCKET.ocsp,
      type: "target",
    })
    expect(serviceSocketsForNode(web, [])).not.toContainEqual(
      expect.objectContaining({ socket: "domain" }),
    )
    expect(serviceSocketsForNode(dc, [])).toEqual([])
    expect(domainJoinEdge(web.id, dc.id)).toMatchObject({
      source: web.id,
      target: dc.id,
      hidden: true,
      data: { edgeType: EDGE_TYPE.domainJoin },
    })
  })

  it("keeps persisted-edge handles mounted while endpoints are deploying or failed", () => {
    const root = machine("root", "CA01", "certificateAuthority", {
      caType: "Root",
    })
    const issuing = machine("issuing", "CA02", "certificateAuthority", {
      caType: "Issuing",
    })
    const web = machine("web", "SRV1", "webServer")
    root.data.lifecycle = "failed"
    issuing.data.lifecycle = "deploying"
    web.data.lifecycle = "deploying"

    const hierarchy = relationship(
      "hierarchy",
      "root",
      "issuing",
      EDGE_TYPE.caHierarchy,
    )
    hierarchy.sourceHandle = serviceSocketHandleId(
      SERVICE_SOCKET.issuance,
      "source",
    )
    hierarchy.targetHandle = serviceSocketHandleId(
      SERVICE_SOCKET.issuance,
      "target",
    )
    const ocsp = relationship("ocsp", "issuing", "web", EDGE_TYPE.webServerCert)
    ocsp.sourceHandle = serviceSocketHandleId(SERVICE_SOCKET.ocsp, "source")
    ocsp.targetHandle = serviceSocketHandleId(SERVICE_SOCKET.ocsp, "target")
    ocsp.data = { ...ocsp.data, serviceSocket: SERVICE_SOCKET.ocsp }
    const edges = [hierarchy, ocsp]

    expect(serviceSocketsForNode(root, edges)).toContainEqual({
      socket: SERVICE_SOCKET.issuance,
      type: "source",
    })
    expect(serviceSocketsForNode(issuing, edges)).toEqual(
      expect.arrayContaining([
        { socket: SERVICE_SOCKET.issuance, type: "target" },
        { socket: SERVICE_SOCKET.ocsp, type: "source" },
      ]),
    )
    expect(serviceSocketsForNode(web, edges)).toContainEqual({
      socket: SERVICE_SOCKET.ocsp,
      type: "target",
    })

    // The lifecycle guard still blocks authoring new relationships.
    expect(
      canConnectServiceSockets(
        socketConnection("issuing", "web", SERVICE_SOCKET.publication),
        [root, issuing, web],
        edges,
      ).ok,
    ).toBe(false)
  })

  it("resists second parents and hierarchy cycles during the gesture", () => {
    const root = machine("root", "CA01", "certificateAuthority")
    const issuing = machine("issuing", "CA02", "certificateAuthority")
    const other = machine("other", "CA03", "certificateAuthority")
    const hierarchy = [
      relationship("parent", "root", "other", EDGE_TYPE.caHierarchy),
      relationship("child", "other", "issuing", EDGE_TYPE.caHierarchy),
    ]

    expect(
      canConnectServiceSockets(
        socketConnection("root", "issuing", SERVICE_SOCKET.issuance),
        [root, issuing, other],
        hierarchy,
      ).reason,
    ).toContain("already has an issuer")
    expect(
      canConnectServiceSockets(
        socketConnection("issuing", "root", SERVICE_SOCKET.issuance),
        [root, issuing, other],
        hierarchy,
      ).reason,
    ).toBe("3+ Tier PKI is not supported yet.")
  })

  it("allows CDP/AIA and OCSP between the same issuing CA and web host", () => {
    const issuing = machine("issuing", "CA02", "certificateAuthority", {
      caType: "Issuing",
    })
    const web = machine("web", "SRV1", "webServer")
    const publication = relationship(
      "publication",
      "issuing",
      "web",
      EDGE_TYPE.webServerCert,
    )
    publication.sourceHandle = serviceSocketHandleId(
      SERVICE_SOCKET.publication,
      "source",
    )
    publication.targetHandle = serviceSocketHandleId(
      SERVICE_SOCKET.publication,
      "target",
    )
    publication.data = {
      ...publication.data,
      serviceSocket: SERVICE_SOCKET.publication,
    }

    expect(
      canConnectServiceSockets(
        socketConnection("issuing", "web", SERVICE_SOCKET.ocsp),
        [issuing, web],
        [publication],
      ),
    ).toEqual({ ok: true })
    expect(
      canConnectServiceSockets(
        socketConnection("issuing", "web", SERVICE_SOCKET.publication),
        [issuing, web],
        [publication],
      ).reason,
    ).toBe("CDP/AIA is already connected.")
  })

  it("reports concrete missing publication prerequisites", () => {
    const issuing = machine("issuing", "CA02", "certificateAuthority", {
      caType: "Issuing",
    })
    const web = machine("web", "SRV1", "webServer", { enableOcsp: "Disabled" })

    expect(
      connectionMissingRequirements(
        EDGE_TYPE.webServerCert,
        "issuing",
        "web",
        [issuing, web],
        [],
      ),
    ).toEqual([
      "CA02 still needs a root CA parent",
      "CA02 and SRV1 must share an AD domain",
      "SRV1 must enable Online Responder",
    ])
  })
})
