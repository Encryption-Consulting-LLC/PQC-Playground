import { describe, expect, it } from "vitest"
import type { Edge, Node } from "@xyflow/react"

import {
  CONNECTION_HEALTH,
  CONNECTION_PORT,
  EDGE_TYPE,
} from "@/constants/topology"
import {
  aggregateServiceHealth,
  buildAuditSnapshot,
  createLabEvidence,
  serviceHealthForEdge,
  type LabHealthReport,
} from "@/lib/labEvidence"
import type { MachineData } from "@/store/topology"

const checks = {
  certificate: Object.fromEntries(
    [
      "chain",
      "aia",
      "cdp",
      "ocsp",
      "mlDsa",
      "validity",
      "revocationFreshness",
    ].map((key) => [key, { ok: true }]),
  ),
  enterprisePki: Object.fromEntries(
    ["containers", "templates", "httpArtifacts"].map((key) => [
      key,
      { ok: true },
    ]),
  ),
  caServices: { root: { ok: true }, issuing: { ok: true } },
  ocspResponder: { ok: true },
  dnsPublication: { web: { ok: true }, issuing: { ok: true } },
  runtimeIdentities: {
    dc: { ok: true },
    root: { ok: true },
    issuing: { ok: true },
    web: { ok: true },
  },
}

function evidence(
  report: LabHealthReport = { healthy: true, failures: [], checks },
) {
  return createLabEvidence("job-1", report, {
    schemaVersion: 1,
    healthy: report.healthy,
    lastVerifiedAt: "2026-07-14T00:00:00Z",
    signatureAlgorithm: "ML-DSA-87",
    hops: [],
  })
}

const nodes: Node<MachineData>[] = [
  {
    id: "ca",
    position: { x: 0, y: 0 },
    data: {
      name: "CA02",
      typeId: "certificateAuthority",
      config: { caType: "Issuing" },
      lifecycle: "deployed",
      poweredOn: true,
    },
  },
  {
    id: "web",
    position: { x: 0, y: 0 },
    data: {
      name: "SRV1",
      typeId: "webServer",
      lifecycle: "deployed",
      poweredOn: true,
    },
  },
]
const publication: Edge = {
  id: "publish",
  source: "ca",
  target: "web",
  data: { edgeType: EDGE_TYPE.webServerCert },
}

describe("lab evidence projection", () => {
  it("colors only the failed service segment", () => {
    const report: LabHealthReport = {
      healthy: false,
      failures: ["OCSP failed"],
      checks: {
        ...checks,
        certificate: { ...checks.certificate, ocsp: { ok: false } },
      },
    }

    expect(serviceHealthForEdge(publication, nodes, evidence(report))).toEqual({
      [CONNECTION_PORT.caPublication]: CONNECTION_HEALTH.verified,
      [CONNECTION_PORT.webHost]: CONNECTION_HEALTH.verified,
      [CONNECTION_PORT.probeCertificate]: CONNECTION_HEALTH.broken,
    })
  })

  it("degrades rather than breaks a service whose only faults are advisory", () => {
    const report: LabHealthReport = {
      healthy: true,
      failures: [],
      warnings: ["the Online Responder returned HTTP 500"],
      checks: {
        ...checks,
        certificate: {
          ...checks.certificate,
          ocsp: { ok: false, advisory: true },
          revocationFreshness: { ok: false, advisory: true },
        },
        ocspResponder: { ok: false, advisory: true },
      },
    }

    expect(serviceHealthForEdge(publication, nodes, evidence(report))).toEqual({
      [CONNECTION_PORT.caPublication]: CONNECTION_HEALTH.verified,
      [CONNECTION_PORT.webHost]: CONNECTION_HEALTH.verified,
      [CONNECTION_PORT.probeCertificate]: CONNECTION_HEALTH.degraded,
    })
  })

  it("breaks a service that mixes an advisory fault with a real one", () => {
    const report: LabHealthReport = {
      healthy: false,
      failures: ["the chain did not build"],
      warnings: ["the Online Responder returned HTTP 500"],
      checks: {
        ...checks,
        certificate: {
          ...checks.certificate,
          chain: { ok: false },
          ocsp: { ok: false, advisory: true },
        },
      },
    }

    expect(
      serviceHealthForEdge(publication, nodes, evidence(report))[
        CONNECTION_PORT.probeCertificate
      ],
    ).toBe(CONNECTION_HEALTH.broken)
  })

  it("uses the least healthy service as the edge summary", () => {
    expect(
      aggregateServiceHealth(
        {
          [CONNECTION_PORT.caPublication]: CONNECTION_HEALTH.verified,
          [CONNECTION_PORT.probeCertificate]: CONNECTION_HEALTH.broken,
        },
        CONNECTION_HEALTH.verified,
      ),
    ).toBe(CONNECTION_HEALTH.broken)
  })

  it("builds a redacted shareable topology and verification snapshot", () => {
    const snapshot = buildAuditSnapshot(nodes, [publication], evidence())

    expect(snapshot.deploymentJobId).toBe("job-1")
    expect(snapshot.topology.nodes.map((node) => node.name)).toEqual([
      "CA02",
      "SRV1",
    ])
    expect(snapshot.verification.healthy).toBe(true)
  })
})
