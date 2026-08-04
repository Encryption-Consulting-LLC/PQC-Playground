import type { Edge, Node } from "@xyflow/react"

import {
  CONNECTION_HEALTH,
  CONNECTION_PORT,
  EDGE_TYPE,
  SERVICE_SOCKET,
} from "@/constants/topology"
import type {
  ConnectionHealth,
  ConnectionPort,
  EdgeType,
} from "@/constants/topology"
import type { CertificateJourney } from "@/lib/certificateJourney"
import type { MachineData } from "@/store/topology"
import { edgeServiceSocket } from "@/lib/topology"

export interface HealthCheck {
  ok: boolean
  /**
   * Set by the backend gate on a check that failed without failing the deploy —
   * an OCSP fault in a lab whose CRL revocation still verifies, or a fact fully
   * explained by a failure already reported. Renders degraded, not broken.
   */
  advisory?: boolean
  detail?: unknown
}

export interface LabHealthReport {
  healthy: boolean
  failures: string[]
  /** Non-fatal counterparts to `failures`; absent on evidence written before they existed. */
  warnings?: string[]
  checks: {
    certificate?: Record<string, HealthCheck>
    enterprisePki?: Record<string, HealthCheck>
    caServices?: Record<string, HealthCheck>
    ocspResponder?: HealthCheck
    dnsPublication?: Record<string, HealthCheck>
    runtimeIdentities?: Record<string, HealthCheck>
  }
}

export interface LabEvidence {
  schemaVersion: 1
  deploymentJobId: string
  verifiedAt: string
  health: LabHealthReport
  journey: CertificateJourney
}

export type ServiceHealth = Partial<Record<ConnectionPort, ConnectionHealth>>

export function isLabHealthReport(value: unknown): value is LabHealthReport {
  if (!value || typeof value !== "object") return false
  const report = value as Partial<LabHealthReport>
  return (
    typeof report.healthy === "boolean" &&
    Array.isArray(report.failures) &&
    report.failures.every((failure) => typeof failure === "string") &&
    !!report.checks &&
    typeof report.checks === "object"
  )
}

export function createLabEvidence(
  deploymentJobId: string,
  health: LabHealthReport,
  journey: CertificateJourney,
): LabEvidence {
  return {
    schemaVersion: 1,
    deploymentJobId,
    verifiedAt: journey.lastVerifiedAt,
    health,
    journey,
  }
}

export function findLabEvidence(
  nodes: Node<MachineData>[],
): LabEvidence | null {
  return nodes.find((node) => node.data.labEvidence)?.data.labEvidence ?? null
}

function resolve(
  report: LabHealthReport,
  path: string,
): Partial<HealthCheck> | null {
  let value: unknown = report.checks
  for (const part of path.split(".")) {
    if (!value || typeof value !== "object") return null
    value = (value as Record<string, unknown>)[part]
  }
  return value && typeof value === "object"
    ? (value as Partial<HealthCheck>)
    : null
}

function check(report: LabHealthReport, path: string): boolean {
  return resolve(report, path)?.ok === true
}

/**
 * Project a set of check paths onto one service's health.
 *
 * A service whose only failing checks are `advisory` reads degraded rather than
 * broken: the backend has decided those faults don't fail the deploy, and a red
 * segment on a lab the gate passed would contradict it. An unresolvable path is
 * treated as broken, not advisory — absent evidence is not a soft failure.
 */
function health(report: LabHealthReport, ...paths: string[]): ConnectionHealth {
  const failing = paths.filter((path) => !check(report, path))
  if (failing.length === 0) return CONNECTION_HEALTH.verified
  return failing.every((path) => resolve(report, path)?.advisory === true)
    ? CONNECTION_HEALTH.degraded
    : CONNECTION_HEALTH.broken
}

function runtimeRole(
  node: Node<MachineData>,
): "dc" | "root" | "issuing" | "web" | null {
  if (node.data.typeId === "domainController") return "dc"
  if (node.data.typeId === "webServer") return "web"
  if (node.data.typeId !== "certificateAuthority") return null
  return node.data.config?.caType === "Root" ? "root" : "issuing"
}

/** Project terminal verification facts onto the individual services carried by an edge. */
export function serviceHealthForEdge(
  edge: Edge,
  nodes: Node<MachineData>[],
  evidence: LabEvidence,
): ServiceHealth {
  const type = edge.data?.edgeType as EdgeType | undefined
  const report = evidence.health
  if (type === EDGE_TYPE.caHierarchy) {
    return {
      [CONNECTION_PORT.caParent]: health(
        report,
        "caServices.root",
        "caServices.issuing",
        "certificate.mlDsa",
        "certificate.chain",
      ),
    }
  }
  if (type === EDGE_TYPE.domainJoin) {
    const source = nodes.find((node) => node.id === edge.source)
    const role = source ? runtimeRole(source) : null
    const paths = role ? [`runtimeIdentities.${role}`] : []
    if (role === "web" || role === "issuing")
      paths.push(`dnsPublication.${role}`)
    if (role === "issuing") paths.push("enterprisePki.containers")
    return {
      [CONNECTION_PORT.domainBoundary]: health(report, ...paths),
    }
  }
  if (type === EDGE_TYPE.webServerCert) {
    const legacyAggregate = edge.data?.serviceSocket === undefined
    if (edgeServiceSocket(edge) === SERVICE_SOCKET.ocsp) {
      return {
        [CONNECTION_PORT.probeCertificate]: health(
          report,
          "certificate.chain",
          "certificate.ocsp",
          "certificate.mlDsa",
          "certificate.validity",
          "certificate.revocationFreshness",
          "enterprisePki.templates",
          "ocspResponder",
        ),
      }
    }
    return {
      [CONNECTION_PORT.caPublication]: health(
        report,
        "certificate.aia",
        "certificate.cdp",
        "enterprisePki.httpArtifacts",
        "dnsPublication.web",
        "dnsPublication.issuing",
      ),
      [CONNECTION_PORT.webHost]: health(
        report,
        "enterprisePki.httpArtifacts",
        "runtimeIdentities.web",
      ),
      ...(legacyAggregate
        ? {
            [CONNECTION_PORT.probeCertificate]: health(
              report,
              "certificate.chain",
              "certificate.ocsp",
              "certificate.mlDsa",
              "certificate.validity",
              "certificate.revocationFreshness",
              "enterprisePki.templates",
            ),
          }
        : {}),
    }
  }
  return {}
}

const HEALTH_PRIORITY: Record<ConnectionHealth, number> = {
  [CONNECTION_HEALTH.verified]: 0,
  [CONNECTION_HEALTH.planned]: 1,
  [CONNECTION_HEALTH.applying]: 2,
  [CONNECTION_HEALTH.degraded]: 3,
  [CONNECTION_HEALTH.broken]: 4,
}

export function aggregateServiceHealth(
  services: ServiceHealth,
  fallback: ConnectionHealth,
): ConnectionHealth {
  return Object.values(services).reduce(
    (worst, state) =>
      state && HEALTH_PRIORITY[state] > HEALTH_PRIORITY[worst] ? state : worst,
    fallback,
  )
}

const NODE_CHECKS: Record<string, Array<[string, string]>> = {
  dc: [
    ["runtimeIdentities.dc", "Runtime identity mismatch"],
    ["enterprisePki.containers", "Enterprise PKI containers unhealthy"],
  ],
  root: [
    ["runtimeIdentities.root", "Runtime identity mismatch"],
    ["caServices.root", "Root CA service unavailable"],
    ["certificate.mlDsa", "ML-DSA-87 verification failed"],
  ],
  issuing: [
    ["runtimeIdentities.issuing", "Runtime identity mismatch"],
    ["caServices.issuing", "Issuing CA service unavailable"],
    ["dnsPublication.issuing", "Issuing-host DNS or publication failed"],
  ],
  web: [
    ["runtimeIdentities.web", "Runtime identity mismatch"],
    ["dnsPublication.web", "Web-host DNS or publication failed"],
    ["ocspResponder", "Online Responder unhealthy"],
    ["certificate.ocsp", "Probe OCSP verification failed"],
  ],
}

export function nodeHealthWarning(
  node: Node<MachineData>,
  evidence: LabEvidence | null,
): string | null {
  if (!evidence) return null
  const role = runtimeRole(node)
  if (!role) return null
  const failed = NODE_CHECKS[role].find(
    ([path]) => !check(evidence.health, path),
  )
  return failed?.[1] ?? null
}

function collectFingerprints(value: unknown, output: string[] = []): string[] {
  if (Array.isArray(value)) {
    for (const item of value) collectFingerprints(item, output)
  } else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      if (
        /(fingerprint|thumbprint|sha256)/i.test(key) &&
        typeof item === "string"
      ) {
        output.push(item)
      } else {
        collectFingerprints(item, output)
      }
    }
  }
  return Array.from(new Set(output))
}

/** Redacted, deterministic snapshot suitable for copy/share from evidence mode. */
export function buildAuditSnapshot(
  nodes: Node<MachineData>[],
  edges: Edge[],
  evidence: LabEvidence,
) {
  const validity = evidence.health.checks.certificate?.validity?.detail
  return {
    schemaVersion: 1,
    deploymentJobId: evidence.deploymentJobId,
    verifiedAt: evidence.verifiedAt,
    topology: {
      nodes: nodes.map((node) => ({
        id: node.id,
        name: node.data.name,
        role: runtimeRole(node) ?? node.data.typeId,
        lifecycle: node.data.lifecycle,
        position: node.position,
      })),
      relationships: edges
        .filter((edge) => edge.data?.edgeType)
        .map((edge) => ({
          id: edge.id,
          source: edge.source,
          target: edge.target,
          type: edge.data?.edgeType,
          health: edge.data?.health,
          services: edge.data?.serviceHealth ?? {},
        })),
    },
    mlDsa: evidence.health.checks.certificate?.mlDsa?.detail ?? {
      algorithm: evidence.journey.signatureAlgorithm,
    },
    certificateFingerprints: collectFingerprints(validity),
    revocationFreshness:
      evidence.health.checks.certificate?.revocationFreshness?.detail ?? null,
    verification: evidence.health,
  }
}
