/** Convert the React Flow canvas into the backend's versioned semantic graph. */

import type { Edge, Node } from "@xyflow/react"

import { EDGE_TYPE, LIFECYCLE } from "@/constants/topology"
import type { EdgeType } from "@/constants/topology"
import type { TopologyPayload, TopologyRole } from "@/lib/api"
import {
  connectionPorts,
  hasCompleteWebServiceRelationship,
  isDeployed,
  webServiceEdges,
} from "@/lib/topology"
import { templatePlatform } from "@/constants/templates"
import type { MachineData } from "@/store/topology"

function topologyRole(data: MachineData): TopologyRole {
  switch (data.typeId) {
    case "domainController":
      return "domainController"
    case "certificateAuthority":
      return data.config?.caType === "Issuing" ? "issuingCa" : "rootCa"
    case "webServer":
      return "webServer"
    case "client":
      return "client"
    default:
      // One role for all three Linux product appliances. They used to compile
      // as `standalone`, which the backend still accepts so a topology saved
      // before this role existed keeps compiling; a fresh one says what it is,
      // which is what lets a product integration be validated at all.
      return templatePlatform(data.typeId) === "linux"
        ? "product"
        : "standalone"
  }
}

/**
 * The label a product's configured FQDN takes inside `zone`, or null when the
 * name lives somewhere else entirely.
 *
 * An operator is free to point CertSecure at `certsecure.example.com` while the
 * lab domain is `encon.pki`; the product still installs under that name, but the
 * lab's DNS is not authoritative for it and inventing a record there would be a
 * lie. So the record is simply not planned — which is also why this returns null
 * rather than falling back to the bare first label.
 */
function labelWithinZone(
  fqdn: string | undefined,
  zone: string,
): string | null {
  const name = fqdn?.trim().replace(/\.+$/, "")
  if (!name) return null
  const suffix = `.${zone}`
  if (!name.toLocaleLowerCase().endsWith(suffix.toLocaleLowerCase()))
    return null
  const label = name.slice(0, -suffix.length)
  return label.length > 0 && !label.includes(".") ? label : null
}

export function buildDeployTopology(
  nodes: Node<MachineData>[],
  edges: Edge[],
): TopologyPayload {
  const domainControllers = new Map(
    nodes
      .filter((node) => topologyRole(node.data) === "domainController")
      .map((node) => [node.id, node]),
  )
  const memberships = new Map(
    edges
      .filter((edge) => edge.data?.edgeType === EDGE_TYPE.domainJoin)
      .map((edge) => [edge.source, edge.target]),
  )
  const integrations = new Map(
    edges
      .filter((edge) => edge.data?.edgeType === EDGE_TYPE.productIntegration)
      .map((edge) => [edge.source, edge.target]),
  )
  const dnsRecords: TopologyPayload["dnsRecords"] = []
  const servicePairs = new Map<
    string,
    { source: string; target: string; edges: Edge[] }
  >()
  for (const edge of webServiceEdges(edges)) {
    const source = nodes.find((node) => node.id === edge.source)
    if (source?.data.config?.caType !== "Issuing") continue
    const key = `${edge.source}\u0000${edge.target}`
    const pair = servicePairs.get(key) ?? {
      source: edge.source,
      target: edge.target,
      edges: [],
    }
    pair.edges.push(edge)
    servicePairs.set(key, pair)
  }
  const completeServicePairs = [...servicePairs.values()].filter((pair) =>
    hasCompleteWebServiceRelationship(pair.edges, pair.source, pair.target),
  )

  for (const node of nodes) {
    const role = topologyRole(node.data)
    if (
      !(
        ["domainController", "issuingCa", "webServer"] as TopologyRole[]
      ).includes(role)
    ) {
      continue
    }
    const dcId =
      role === "domainController" ? node.id : memberships.get(node.id)
    const dc = dcId ? domainControllers.get(dcId) : undefined
    const zone = dc?.data.config?.domainName?.trim()
    if (!dcId || !zone) continue
    dnsRecords.push({
      id: `dns:a:${dcId}:${node.id}`,
      kind: "A",
      server: dcId,
      subject: node.id,
      zone,
    })
    const reverseZone = dc?.data.config?.reverseZone?.trim()
    if (reverseZone) {
      dnsRecords.push({
        id: `dns:ptr:${dcId}:${node.id}`,
        kind: "PTR",
        server: dcId,
        subject: node.id,
        zone: reverseZone,
      })
    }
  }

  // A product publishes the three service names the installer was configured
  // with, all pointing at its single address — so each record carries its own
  // label rather than taking the subject's guest hostname the way a PKI
  // component's A record does.
  for (const node of nodes) {
    if (topologyRole(node.data) !== "product") continue
    const dcId = integrations.get(node.id)
    const dc = dcId ? domainControllers.get(dcId) : undefined
    const zone = dc?.data.config?.domainName?.trim()
    if (!dcId || !zone) continue
    for (const field of ["frontendHost", "backendHost", "keycloakHost"]) {
      const label = labelWithinZone(node.data.config?.[field], zone)
      if (!label) continue
      dnsRecords.push({
        id: `dns:a:${dcId}:${node.id}:${label}`,
        kind: "A",
        server: dcId,
        subject: node.id,
        zone,
        name: label,
      })
    }
  }

  for (const pair of completeServicePairs) {
    const dcId = memberships.get(pair.source)
    const dc = dcId ? domainControllers.get(dcId) : undefined
    const zone = dc?.data.config?.domainName?.trim()
    if (!dcId || !zone) continue
    dnsRecords.push({
      id: `dns:cname:${dcId}:pki`,
      kind: "CNAME",
      server: dcId,
      subject: pair.target,
      zone,
      name: "pki",
    })
  }

  return {
    version: 1,
    nodes: nodes.map((node) => ({
      id: node.id,
      name: node.data.name,
      role: topologyRole(node.data),
      state:
        isDeployed(node.data) || node.data.lifecycle === LIFECYCLE.provisioning
          ? "realized"
          : "planned",
      config: node.data.config ?? {},
    })),
    edges: [
      ...edges.flatMap((edge) => {
        const edgeType = edge.data?.edgeType as EdgeType | undefined
        const kind:
          | "domainMembership"
          | "caParent"
          | "productIntegration"
          | null =
          edgeType === EDGE_TYPE.domainJoin
            ? "domainMembership"
            : edgeType === EDGE_TYPE.caHierarchy
              ? "caParent"
              : edgeType === EDGE_TYPE.productIntegration
                ? "productIntegration"
                : null
        return kind && edgeType
          ? [
              {
                id: edge.id,
                kind,
                source: edge.source,
                target: edge.target,
                state:
                  edge.data?.staged === true
                    ? ("planned" as const)
                    : ("realized" as const),
                ports: connectionPorts(edgeType),
              },
            ]
          : []
      }),
      ...completeServicePairs.map((pair) => ({
        id: `service:${pair.source}:${pair.target}`,
        kind: "caPublication" as const,
        source: pair.source,
        target: pair.target,
        state: pair.edges.some((edge) => edge.data?.staged === true)
          ? ("planned" as const)
          : ("realized" as const),
        ports: connectionPorts(EDGE_TYPE.webServerCert),
      })),
    ],
    dnsRecords,
  }
}
