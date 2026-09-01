/**
 * Pure topology helpers — no React, no store imports.
 * All inputs are plain arrays of Node/Edge data so these are trivially testable.
 */

import type { Edge, Node } from "@xyflow/react"
import {
  CONNECTION_HEALTH,
  CONNECTION_PORT,
  EDGE_TYPE,
  LIFECYCLE,
  SERVICE_SOCKET,
} from "@/constants/topology"
import type {
  ConnectionHealth,
  ConnectionPort,
  EdgeType,
  ServiceSocket,
} from "@/constants/topology"
import type { IsoAuthoring, MachineData } from "@/store/topology"
import { templatePlatform } from "@/constants/templates"

// ---------------------------------------------------------------------------
// Lifecycle derived state
// ---------------------------------------------------------------------------

/** Deployed on the host, whether or not its config has since drifted. */
export function isDeployed(data: MachineData): boolean {
  return (
    data.lifecycle === LIFECYCLE.deployed ||
    data.lifecycle === LIFECYCLE.drifted
  )
}

/**
 * Canonical comparison key for a node's authored-ISO state. A disabled or
 * absent panel collapses to "off" (nodes without either field never read as
 * ISO-drifted); PACK compares the name-sorted file
 * set, UPLOAD-ISO the uploaded file's identity.
 */
function isoSignature(iso: IsoAuthoring | undefined): string {
  if (!iso?.enabled) return "off"
  if (iso.mode === "uploadIso") return `iso:${iso.isoId ?? ""}`
  return (
    "pack:" +
    JSON.stringify(
      [...iso.files]
        .sort((a, b) => a.name.localeCompare(b.name))
        .map(({ name, content }) => [name, content]),
    )
  )
}

/** Deployed with an authored-ISO state that no longer matches what was last deployed. */
export function isIsoDrifted(data: MachineData): boolean {
  if (!isDeployed(data)) return false
  return isoSignature(data.isoAuthoring) !== isoSignature(data.lastDeployedIso)
}

/** Synthetic `driftedFields` key for authored-ISO drift (not a config field). */
export const ISO_DRIFT_FIELD = "isoContents"

function configDriftedKeys(data: MachineData): string[] {
  if (!data.config && !data.lastDeployedConfig) return []
  if (!data.lastDeployedConfig)
    return data.config ? Object.keys(data.config) : []
  if (!data.config) return []
  const last = data.lastDeployedConfig
  const keys = new Set([...Object.keys(data.config), ...Object.keys(last)])
  return [...keys].filter((key) => data.config![key] !== last[key])
}

/** Deployed with config (or authored-ISO) state that no longer matches what was last deployed. */
export function isDrifted(data: MachineData): boolean {
  if (!isDeployed(data)) return false
  if (isIsoDrifted(data)) return true
  if (!data.lastDeployedConfig) return data.config !== undefined
  if (!data.config) return false
  return configDriftedKeys(data).length > 0
}

/** Config keys that differ since the last deploy, plus `ISO_DRIFT_FIELD` when the authored ISO changed. Empty when not drifted. */
export function driftedFields(data: MachineData): string[] {
  if (!isDrifted(data)) return []
  const keys = data.config ? configDriftedKeys(data) : []
  if (isIsoDrifted(data)) keys.push(ISO_DRIFT_FIELD)
  return keys
}

/** Has a concrete identity on the canvas beyond a bare, unstaged draft. */
export function isRealized(data: MachineData): boolean {
  return (
    data.lifecycle === LIFECYCLE.staged ||
    data.lifecycle === LIFECYCLE.deploying ||
    data.lifecycle === LIFECYCLE.provisioning ||
    isDeployed(data)
  )
}

/**
 * Valid endpoint for a new edge or domain join — staged nodes can be wired up
 * ahead of deploy, and a `provisioning` node (clone done, agent not yet home)
 * is a real VM that can carry edges while its domain circle stays dashed.
 */
export function isConnectable(data: MachineData): boolean {
  return (
    data.lifecycle === LIFECYCLE.staged ||
    data.lifecycle === LIFECYCLE.provisioning ||
    isDeployed(data)
  )
}

// ---------------------------------------------------------------------------
// Edge type inference
// ---------------------------------------------------------------------------

export function inferEdgeType(
  sourceTypeId: string,
  targetTypeId: string,
): EdgeType {
  // Before the domain-controller catch-all, not after: a Linux product wired to
  // a DC integrates with it, and reading that as a `domainJoin` would stage an
  // op that tries to run `Add-Computer` on Ubuntu.
  if (
    targetTypeId === "domainController" &&
    templatePlatform(sourceTypeId) === "linux"
  )
    return EDGE_TYPE.productIntegration
  if (targetTypeId === "domainController") return EDGE_TYPE.domainJoin
  if (
    sourceTypeId === "certificateAuthority" &&
    targetTypeId === "certificateAuthority"
  )
    return EDGE_TYPE.caHierarchy
  if (sourceTypeId === "certificateAuthority" && targetTypeId === "webServer")
    return EDGE_TYPE.webServerCert
  return EDGE_TYPE.network
}

/** Capability ports carried by each deployable relationship. */
export function connectionPorts(type: EdgeType): ConnectionPort[] {
  switch (type) {
    case EDGE_TYPE.caHierarchy:
      return [CONNECTION_PORT.caParent]
    case EDGE_TYPE.webServerCert:
      return [
        CONNECTION_PORT.caPublication,
        CONNECTION_PORT.webHost,
        CONNECTION_PORT.probeCertificate,
      ]
    case EDGE_TYPE.domainJoin:
      return [CONNECTION_PORT.domainBoundary]
    case EDGE_TYPE.productIntegration:
      return [CONNECTION_PORT.productIntegration]
    case EDGE_TYPE.network:
      return []
  }
}

/** The single canvas capability represented by a CA-to-web edge. */
export function edgeServiceSocket(
  edge: Pick<Edge, "sourceHandle" | "data">,
): ServiceSocket | null {
  if (edge.data?.edgeType !== EDGE_TYPE.webServerCert) return null
  const persisted = edge.data?.serviceSocket as ServiceSocket | undefined
  if (
    persisted === SERVICE_SOCKET.publication ||
    persisted === SERVICE_SOCKET.ocsp
  ) {
    return persisted
  }
  const parsed = parseServiceSocketHandle(edge.sourceHandle)
  return parsed?.socket === SERVICE_SOCKET.ocsp
    ? SERVICE_SOCKET.ocsp
    : SERVICE_SOCKET.publication
}

export function webServiceEdges(
  edges: Edge[],
  sourceId?: string,
  targetId?: string,
): Edge[] {
  return edges.filter(
    (edge) =>
      edge.data?.edgeType === EDGE_TYPE.webServerCert &&
      (sourceId === undefined || edge.source === sourceId) &&
      (targetId === undefined || edge.target === targetId),
  )
}

export function hasCompleteWebServiceRelationship(
  edges: Edge[],
  sourceId: string,
  targetId: string,
): boolean {
  const sockets = new Set(
    webServiceEdges(edges, sourceId, targetId).map(edgeServiceSocket),
  )
  return (
    sockets.has(SERVICE_SOCKET.publication) && sockets.has(SERVICE_SOCKET.ocsp)
  )
}

export interface ConnectionPortGuidance {
  label: string
  capabilities: string[]
}

export const CONNECTION_PORT_GUIDANCE: Record<
  ConnectionPort,
  ConnectionPortGuidance
> = {
  [CONNECTION_PORT.caParent]: {
    label: "CA parent",
    capabilities: ["Issues CA certificate"],
  },
  [CONNECTION_PORT.caPublication]: {
    label: "CA publication",
    capabilities: ["HTTP CDP", "HTTP AIA"],
  },
  [CONNECTION_PORT.domainBoundary]: {
    label: "Domain boundary",
    capabilities: ["AD membership", "DNS resolver", "LDAP publication"],
  },
  [CONNECTION_PORT.productIntegration]: {
    label: "Product integration",
    capabilities: ["DNS registration", "Machine-root trust"],
  },
  [CONNECTION_PORT.webHost]: {
    label: "Web host",
    capabilities: ["CertEnroll directory/share", "HTTP CertEnroll"],
  },
  [CONNECTION_PORT.probeCertificate]: {
    label: "OCSP service",
    capabilities: ["OCSP URL", "Online Responder", "Response validation"],
  },
}

export interface ServiceSocketGuidance {
  label: string
  intent: string
  operation: string
}

export const SERVICE_SOCKET_GUIDANCE: Record<
  ServiceSocket,
  ServiceSocketGuidance
> = {
  [SERVICE_SOCKET.issuance]: {
    label: "CA Issue",
    intent: "Issue a subordinate CA certificate",
    operation: "caConnect · request, relay, sign, install, and verify",
  },
  [SERVICE_SOCKET.publication]: {
    label: "CDP/AIA",
    intent: "Publish certificates and revocation artifacts",
    operation: "webServerCert · publish CertEnroll and verify HTTP artifacts",
  },
  [SERVICE_SOCKET.ocsp]: {
    label: "OCSP",
    intent: "Attach an Online Responder path",
    operation: "webServerCert · configure, enroll, and verify OCSP",
  },
}

export type ServiceSocketHandleType = "source" | "target"

export interface ServiceSocketConnection {
  source: string
  target: string
  sourceHandle?: string | null
  targetHandle?: string | null
}

export function serviceSocketHandleId(
  socket: ServiceSocket,
  type: ServiceSocketHandleType,
): string {
  return `socket:${socket}:${type}`
}

export function parseServiceSocketHandle(
  handleId: string | null | undefined,
): { socket: ServiceSocket; type: ServiceSocketHandleType } | null {
  if (!handleId) return null
  const [prefix, socket, type, extra] = handleId.split(":")
  if (
    prefix !== "socket" ||
    extra !== undefined ||
    !Object.values(SERVICE_SOCKET).includes(socket as ServiceSocket) ||
    (type !== "source" && type !== "target")
  ) {
    return null
  }
  return { socket: socket as ServiceSocket, type }
}

/** Resolves a matching socket pair to the one relationship it can create. */
export function serviceSocketEdgeType(
  connection: ServiceSocketConnection,
  nodes: Node<MachineData>[],
): EdgeType | null {
  const sourceHandle = parseServiceSocketHandle(connection.sourceHandle)
  const targetHandle = parseServiceSocketHandle(connection.targetHandle)
  if (
    !sourceHandle ||
    !targetHandle ||
    sourceHandle.type !== "source" ||
    targetHandle.type !== "target" ||
    sourceHandle.socket !== targetHandle.socket
  ) {
    return null
  }

  const source = nodes.find((node) => node.id === connection.source)
  const target = nodes.find((node) => node.id === connection.target)
  if (!source || !target) return null

  switch (sourceHandle.socket) {
    case SERVICE_SOCKET.issuance:
      return source.data.typeId === "certificateAuthority" &&
        target.data.typeId === "certificateAuthority"
        ? EDGE_TYPE.caHierarchy
        : null
    case SERVICE_SOCKET.publication:
    case SERVICE_SOCKET.ocsp:
      return source.data.typeId === "certificateAuthority" &&
        target.data.typeId === "webServer"
        ? EDGE_TYPE.webServerCert
        : null
  }
  return null
}

export interface NodeServiceSocket {
  socket: ServiceSocket
  type: ServiceSocketHandleType
}

/** Service sockets available for authoring, plus handles required by existing edges. */
export function serviceSocketsForNode(
  node: Node<MachineData>,
  edges: Edge[],
): NodeServiceSocket[] {
  if (node.data.typeId === "domainController") return []

  // An in-flight or failed deployment is not eligible for *new* connections,
  // but its authored edges still need their exact handles to remain mounted.
  // React Flow resolves every persisted edge against the rendered handles; if
  // lifecycle changes remove those handles, it drops the visual edge and emits
  // error 008 on every render/rehydration pass.
  const sockets = new Map<string, NodeServiceSocket>()
  const add = (spec: NodeServiceSocket) => {
    sockets.set(serviceSocketHandleId(spec.socket, spec.type), spec)
  }

  if (isConnectable(node.data) && node.data.typeId === "certificateAuthority") {
    const root =
      node.data.config?.caType === "Root" || caTier(node.id, edges) === "root"
    for (const spec of [
      ...(!root
        ? [{ socket: SERVICE_SOCKET.issuance, type: "target" } as const]
        : []),
      { socket: SERVICE_SOCKET.issuance, type: "source" },
      { socket: SERVICE_SOCKET.publication, type: "source" },
      ...(!root
        ? [{ socket: SERVICE_SOCKET.ocsp, type: "source" } as const]
        : []),
    ] as NodeServiceSocket[])
      add(spec)
  }
  if (isConnectable(node.data) && node.data.typeId === "webServer") {
    for (const spec of [
      { socket: SERVICE_SOCKET.publication, type: "target" },
      { socket: SERVICE_SOCKET.ocsp, type: "target" },
    ] as const)
      add(spec)
  }

  for (const edge of edges) {
    const handleId =
      edge.source === node.id
        ? edge.sourceHandle
        : edge.target === node.id
          ? edge.targetHandle
          : null
    const parsed = parseServiceSocketHandle(handleId)
    if (!parsed) continue
    if (edge.source === node.id && parsed.type !== "source") continue
    if (edge.target === node.id && parsed.type !== "target") continue
    add(parsed)
  }

  return [...sockets.values()]
}

export interface ConnectionGuidance {
  intent: string
  requirements: string[]
  operations: string[]
  ports: ConnectionPort[]
}

export const CONNECTION_HEALTH_GUIDANCE: Record<
  ConnectionHealth,
  { label: string; detail: string }
> = {
  [CONNECTION_HEALTH.planned]: {
    label: "Planned",
    detail: "The relationship is staged and has not been applied.",
  },
  [CONNECTION_HEALTH.applying]: {
    label: "Applying",
    detail: "The generated operation is currently running.",
  },
  [CONNECTION_HEALTH.verified]: {
    label: "Verified",
    detail: "The operation and its verification checks completed successfully.",
  },
  [CONNECTION_HEALTH.degraded]: {
    label: "Degraded",
    detail:
      "The relationship exists, but a dependency or verification path is incomplete.",
  },
  [CONNECTION_HEALTH.broken]: {
    label: "Broken",
    detail: "The generated operation failed and needs operator attention.",
  },
}

export function connectionHealthForOperation(status: string): ConnectionHealth {
  switch (status) {
    case "running":
      return CONNECTION_HEALTH.applying
    case "done":
      return CONNECTION_HEALTH.verified
    case "cancelled":
      return CONNECTION_HEALTH.degraded
    case "error":
      return CONNECTION_HEALTH.broken
    default:
      return CONNECTION_HEALTH.planned
  }
}

export interface TopologyGuidanceItem {
  code: string
  message: string
  severity: "warning" | "error"
  nodeIds: string[]
  edgeIds: string[]
}

/** Actionable relationship guidance available before a backend dry run. */
export function lintTopologyRelationships(
  nodes: Node<MachineData>[],
  edges: Edge[],
): TopologyGuidanceItem[] {
  const diagnostics: TopologyGuidanceItem[] = []

  // Failed nodes fall out of every relationship check below (isConnectable
  // excludes them), so without an explicit diagnostic a half-failed deploy
  // reads as an almost-clean topology. Surface each one first, as an error.
  for (const node of nodes) {
    if (node.data.lifecycle !== LIFECYCLE.failed) continue
    diagnostics.push({
      code: "deployment-failed",
      message: node.data.errorDetail
        ? `${node.data.name} failed to deploy: ${node.data.errorDetail}`
        : `${node.data.name} failed to deploy.`,
      severity: "error",
      nodeIds: [node.id],
      edgeIds: [],
    })
  }
  const memberships = new Map(
    edges
      .filter((edge) => edge.data?.edgeType === EDGE_TYPE.domainJoin)
      .map((edge) => [edge.source, edge]),
  )
  const parents = new Map(
    edges
      .filter((edge) => edge.data?.edgeType === EDGE_TYPE.caHierarchy)
      .map((edge) => [edge.target, edge]),
  )
  const publications = webServiceEdges(edges).filter(
    (edge) => edgeServiceSocket(edge) === SERVICE_SOCKET.publication,
  )
  const ocspConnections = webServiceEdges(edges).filter(
    (edge) =>
      edgeServiceSocket(edge) === SERVICE_SOCKET.ocsp ||
      edge.data?.serviceSocket === undefined,
  )

  const issuingCas = nodes.filter(
    (node) =>
      isConnectable(node.data) &&
      node.data.typeId === "certificateAuthority" &&
      node.data.config?.caType === "Issuing",
  )
  for (const issuing of issuingCas) {
    const parent = parents.get(issuing.id)
    if (!parent) {
      diagnostics.push({
        code: "missing-ca-parent",
        message: `${issuing.data.name} is an issuing CA but has no root CA parent.`,
        severity: "warning",
        nodeIds: [issuing.id],
        edgeIds: [],
      })
    }
    if (!memberships.has(issuing.id)) {
      diagnostics.push({
        code: "issuing-ca-outside-domain",
        message: `${issuing.data.name} is an issuing CA and must be joined to an AD domain.`,
        severity: "error",
        nodeIds: [issuing.id, ...(parent ? [parent.source] : [])],
        edgeIds: parent ? [parent.id] : [],
      })
    }

    if (!publications.some((edge) => edge.source === issuing.id)) {
      diagnostics.push({
        code: "missing-publication-host",
        message: `${issuing.data.name} publishes HTTP CDP/AIA, but no web host is connected.`,
        severity: "warning",
        nodeIds: [issuing.id],
        edgeIds: [],
      })
    }
  }

  const webHosts = nodes.filter(
    (node) => isConnectable(node.data) && node.data.typeId === "webServer",
  )
  for (const web of webHosts) {
    const publication = publications.find((edge) => edge.target === web.id)
    const ocspConnection = ocspConnections.find(
      (edge) => edge.target === web.id,
    )
    const ocspEnabled = web.data.config?.enableOcsp !== "Disabled"
    if (ocspEnabled && !ocspConnection) {
      diagnostics.push({
        code: "ocsp-template-grant-missing",
        message: `${web.data.name} has OCSP enabled, but no issuing CA grants its enrollment templates.`,
        severity: "warning",
        nodeIds: [web.id],
        edgeIds: [],
      })
    }
    if (!publication) continue

    const health = ocspConnection?.data?.health as ConnectionHealth | undefined
    if (
      health === CONNECTION_HEALTH.degraded ||
      health === CONNECTION_HEALTH.broken
    ) {
      diagnostics.push({
        code: "probe-ocsp-path-unverified",
        message: `${web.data.name} can enroll its probe, but no verified OCSP path reaches its certificate.`,
        severity: health === CONNECTION_HEALTH.broken ? "error" : "warning",
        nodeIds: [ocspConnection?.source ?? publication.source, web.id],
        edgeIds: ocspConnection ? [ocspConnection.id] : [],
      })
    }

    const issuingMembership = memberships.get(publication.source)
    if (issuingMembership && !memberships.has(web.id)) {
      diagnostics.push({
        code: "pki-cname-target-missing-a",
        message: `PKI CNAME is planned, but its target ${web.data.name} has no A record.`,
        severity: "warning",
        nodeIds: [issuingMembership.target, web.id],
        edgeIds: [publication.id],
      })
    }
  }

  return diagnostics
}

/** Operator-facing meaning of a connection before it is deployed. */
export function connectionGuidance(
  type: EdgeType,
  opts?: EdgeStyleOptions,
): ConnectionGuidance {
  const ports = connectionPorts(type)
  switch (type) {
    case EDGE_TYPE.caHierarchy:
      return {
        intent: opts?.rootIssuer
          ? "Issues CA certificate via offline relay"
          : "Issues CA certificate",
        requirements: [
          "Configured parent and issuing CA",
          "Issuing CA has no other parent",
          "Hierarchy remains acyclic",
        ],
        operations: [
          "caConnect: request, sign, install, configure, and verify the issuing CA",
        ],
        ports,
      }
    case EDGE_TYPE.webServerCert: {
      const ocsp = opts?.serviceSocket === SERVICE_SOCKET.ocsp
      return {
        intent: ocsp
          ? "Provides online certificate status through OCSP"
          : "Publishes certificates and revocation artifacts through HTTP",
        requirements: opts?.rootIssuer
          ? ["Configured offline root CA", "Configured PKI web services host"]
          : [
              "Issuing CA has a root parent",
              "Issuing CA and web host share an AD domain",
              ...(ocsp ? ["Web host has Online Responder enabled"] : []),
            ],
        operations: opts?.rootIssuer
          ? ["caConnect: relay root certificate and CRL through the issuing CA"]
          : [
              ocsp
                ? "webServerCert: configure, enroll, and verify OCSP"
                : "webServerCert: publish and verify CertEnroll HTTP artifacts",
            ],
        ports: ocsp
          ? [CONNECTION_PORT.probeCertificate]
          : [CONNECTION_PORT.caPublication, CONNECTION_PORT.webHost],
      }
    }
    case EDGE_TYPE.domainJoin:
      return {
        intent: "Provides AD membership, DNS, and LDAP publication",
        requirements: [
          "Configured domain controller and member",
          "Member is not an offline root CA",
          "Member belongs to no other domain",
        ],
        operations: [
          "domainJoin: join the member, reboot, and verify domain membership",
        ],
        ports,
      }
    case EDGE_TYPE.productIntegration:
      return {
        intent: "Registers the product's names and trusts its certificate",
        requirements: [
          "Configured domain controller and product appliance",
          "Product belongs to no other domain",
        ],
        operations: [
          "provision: register A records at the domain controller and add the " +
            "product's certificate to every domain machine's root store",
        ],
        ports,
      }
    case EDGE_TYPE.network:
      return {
        intent: "Unsupported generic connection",
        requirements: ["Choose a typed PKI relationship"],
        operations: [],
        ports,
      }
  }
}

// ---------------------------------------------------------------------------
// CA tier and hierarchy helpers
// ---------------------------------------------------------------------------

export type CaTier = "root" | "intermediate" | "issuing" | "standalone"

export function caTier(nodeId: string, edges: Edge[]): CaTier {
  const hasOutgoing = edges.some(
    (e) => e.source === nodeId && e.data?.edgeType === EDGE_TYPE.caHierarchy,
  )
  const hasIncoming = edges.some(
    (e) => e.target === nodeId && e.data?.edgeType === EDGE_TYPE.caHierarchy,
  )
  if (hasOutgoing && !hasIncoming) return "root"
  if (hasIncoming && hasOutgoing) return "intermediate"
  if (hasIncoming) return "issuing"
  return "standalone"
}

/** Returns the node ID of the single issuing parent CA, if any. */
export function caParent(nodeId: string, edges: Edge[]): string | null {
  const inEdge = edges.find(
    (e) => e.target === nodeId && e.data?.edgeType === EDGE_TYPE.caHierarchy,
  )
  return inEdge?.source ?? null
}

/**
 * Returns true if `maybeAncestorId` is an ancestor of `nodeId` in the CA
 * hierarchy. Uses a visited set so any accidental pre-existing loop can't
 * cause an infinite walk.
 */
export function isAncestor(
  maybeAncestorId: string,
  nodeId: string,
  edges: Edge[],
): boolean {
  const visited = new Set<string>()
  let current: string | null = nodeId
  while (current !== null) {
    if (visited.has(current)) break // defensive — shouldn't happen in a valid tree
    visited.add(current)
    const parent = caParent(current, edges)
    if (parent === maybeAncestorId) return true
    current = parent
  }
  return false
}

/**
 * Returns the depth of `nodeId` in the CA hierarchy tree.
 * Root / standalone → 0. Direct child of root → 1. And so on.
 */
export function caDepth(nodeId: string, edges: Edge[]): number {
  const visited = new Set<string>()
  let depth = 0
  let current: string | null = nodeId
  while (true) {
    const parent = caParent(current, edges)
    if (parent === null) break
    if (visited.has(parent)) break // defensive
    visited.add(parent)
    depth++
    current = parent
  }
  return depth
}

// ---------------------------------------------------------------------------
// PKI trust gravity
// ---------------------------------------------------------------------------

export const TRUST_TIER_GAP = 344
export const TRUST_ORBIT_GAP = 344

/**
 * Settles one CA trust tree into deterministic visual tiers. The root remains
 * the anchor at tier zero, subordinate CAs orbit below it by hierarchy depth,
 * and attached publication workloads occupy the next tier downstream.
 *
 * Only nodes in the selected trust tree move; domain controllers and unrelated
 * machines keep their authored positions. Stable id ordering prevents a saved
 * project from shuffling when the same hierarchy is recompiled.
 */
export function trustGravityLayout(
  nodes: Node<MachineData>[],
  edges: Edge[],
  memberId: string,
): Node<MachineData>[] {
  const nodeById = new Map(nodes.map((node) => [node.id, node]))
  const member = nodeById.get(memberId)
  if (!member || member.data.typeId !== "certificateAuthority") return nodes

  let rootId = memberId
  const visited = new Set<string>()
  while (!visited.has(rootId)) {
    visited.add(rootId)
    const parent = caParent(rootId, edges)
    if (!parent) break
    rootId = parent
  }

  const root = nodeById.get(rootId)
  if (!root) return nodes

  const tierById = new Map<string, number>([[rootId, 0]])
  const queue = [rootId]
  while (queue.length > 0) {
    const parentId = queue.shift()!
    const parentTier = tierById.get(parentId)!
    const children = edges
      .filter(
        (edge) =>
          edge.source === parentId &&
          edge.data?.edgeType === EDGE_TYPE.caHierarchy,
      )
      .map((edge) => edge.target)
      .filter((id) => nodeById.get(id)?.data.typeId === "certificateAuthority")
      .sort()
    for (const childId of children) {
      if (tierById.has(childId)) continue
      tierById.set(childId, parentTier + 1)
      queue.push(childId)
    }
  }

  for (const edge of edges) {
    if (edge.data?.edgeType !== EDGE_TYPE.webServerCert) continue
    const sourceTier = tierById.get(edge.source)
    if (sourceTier === undefined || !nodeById.has(edge.target)) continue
    tierById.set(edge.target, sourceTier + 1)
  }

  const tierMembers = new Map<number, string[]>()
  for (const [id, tier] of tierById) {
    if (tier === 0) continue
    const members = tierMembers.get(tier) ?? []
    members.push(id)
    tierMembers.set(tier, members)
  }
  for (const members of tierMembers.values()) members.sort()

  return nodes.map((node) => {
    const tier = tierById.get(node.id)
    if (tier === undefined || tier === 0) return node
    const members = tierMembers.get(tier)!
    const index = members.indexOf(node.id)
    return {
      ...node,
      position: {
        x:
          root.position.x +
          (index - (members.length - 1) / 2) * TRUST_ORBIT_GAP,
        y: root.position.y + tier * TRUST_TIER_GAP,
      },
    }
  })
}

// ---------------------------------------------------------------------------
// Domain membership
// ---------------------------------------------------------------------------

export function domainMembership(
  nodeId: string,
  edges: Edge[],
  nodes: Node<MachineData>[],
): string | null {
  const joinEdge = edges.find(
    (e) => e.source === nodeId && e.data?.edgeType === EDGE_TYPE.domainJoin,
  )
  if (!joinEdge) return null
  const dcNode = nodes.find((n) => n.id === joinEdge.target)
  return dcNode ? domainLabel(dcNode) : null
}

// ---------------------------------------------------------------------------
// Domain regions (spatial domain join)
// ---------------------------------------------------------------------------
//
// A domain controller projects a circular "domain" region around itself. Any
// eligible node whose centre falls inside that circle is domain-joined. The
// radius is expressed in flow units (== node-pixel units at zoom 1), so the
// rendered circle and the geometry test below stay in lock-step at any zoom.

export const DOMAIN_RADIUS = 520

/** Extra clearance kept between a domain's farthest member and its circle edge. */
const DOMAIN_MEMBER_PADDING = 90

/**
 * How close another node must be to a drifting member for that member to
 * still count as "huddled with the pack" rather than being carried out on
 * its own — keeps the circle from snapping back mid-shuffle when a cluster
 * of members moves together.
 */
const DOMAIN_EXIT_NEIGHBOR_CLEARANCE = 100

/**
 * Extra distance, past the radius the domain would have without a given
 * member, that member must clear before it's treated as leaving — gives the
 * boundary some hysteresis so the circle doesn't flicker right at the edge.
 */
const DOMAIN_EXIT_MARGIN = 40

/** Centre of a node in flow coordinates (position is the top-left corner). */
export function nodeCenter(node: Node<MachineData>): { x: number; y: number } {
  const w = node.measured?.width ?? 160
  const h = node.measured?.height ?? 80
  return { x: node.position.x + w / 2, y: node.position.y + h / 2 }
}

/** Extra clearance kept between a domain's farthest member's rect and its circle edge. */
const DOMAIN_CIRCLE_MARGIN = 40

/** Distance from `point` to the farthest corner of `node`'s rect — used so the
 * circle clears a member's whole footprint, not just its center point. */
function farthestCornerDistance(
  node: Node<MachineData>,
  point: { x: number; y: number },
): number {
  const { x, y, w, h } = nodeRect(node)
  const corners = [
    { x, y },
    { x: x + w, y },
    { x, y: y + h },
    { x: x + w, y: y + h },
  ]
  return Math.max(
    ...corners.map((c) => Math.hypot(c.x - point.x, c.y - point.y)),
  )
}

/**
 * A domain controller's circle grows to keep its committed members inside
 * with padding, rather than staying a fixed size. Never shrinks below
 * `DOMAIN_RADIUS` so an empty/sparse domain still reads as a region.
 *
 * A member that has drifted well past where the circle would sit without it,
 * and isn't huddled near any other node, is being carried out (e.g. dragged
 * toward the boundary) — its distance is excluded from the growth
 * calculation so the circle snaps back instead of inflating to chase it
 * forever.
 */
export function domainRadius(
  dc: Node<MachineData>,
  nodes: Node<MachineData>[],
  edges: Edge[],
): number {
  const dcCenter = nodeCenter(dc)
  // Both relationship kinds count. The circle's job is to clear the footprint
  // of everything that belongs to the domain, and an integrated product does —
  // it just belongs differently. Counting only joins left a product sitting
  // outside a circle it had been dropped into, at which point the next drag
  // read it as having left and proposed dropping the integration.
  const members = edges
    .filter(
      (e) =>
        e.target === dc.id &&
        (e.data?.edgeType === EDGE_TYPE.domainJoin ||
          e.data?.edgeType === EDGE_TYPE.productIntegration),
    )
    .map((e) => nodes.find((n) => n.id === e.source))
    .filter((n): n is Node<MachineData> => !!n)

  const distances = members.map((m) => {
    const c = nodeCenter(m)
    return Math.hypot(c.x - dcCenter.x, c.y - dcCenter.y)
  })
  // Drives the actual radius growth — the farthest corner of each member's
  // rect, so the circle always clears its footprint, not just its center.
  const edgeDistances = members.map((m) => farthestCornerDistance(m, dcCenter))

  let maxDist = 0
  members.forEach((member, i) => {
    const dist = distances[i]

    const othersMaxDist = distances.reduce(
      (m, d, j) => (j === i ? m : Math.max(m, d)),
      0,
    )
    const radiusWithoutMember = Math.max(
      DOMAIN_RADIUS,
      othersMaxDist + DOMAIN_MEMBER_PADDING,
    )

    const c = nodeCenter(member)
    const hasNearbyNeighbor = nodes.some((other) => {
      if (other.id === member.id || other.id === dc.id) return false
      const oc = nodeCenter(other)
      return Math.hypot(oc.x - c.x, oc.y - c.y) < DOMAIN_EXIT_NEIGHBOR_CLEARANCE
    })

    const isLeaving =
      dist > radiusWithoutMember + DOMAIN_EXIT_MARGIN && !hasNearbyNeighbor
    if (isLeaving) return

    if (edgeDistances[i] > maxDist) maxDist = edgeDistances[i]
  })

  return Math.max(DOMAIN_RADIUS, maxDist + DOMAIN_CIRCLE_MARGIN)
}

/** Human label for a domain — the DC's configured domain name, else its node name. */
export function domainLabel(dc: Node<MachineData>): string {
  return dc.data.config?.domainName ?? dc.data.name
}

/** Case-insensitive authored identity collision surfaced while a form is edited. */
export function configurationNameCollision(
  nodeId: string,
  fieldKey: string,
  value: string,
  nodes: Node<MachineData>[],
): string | null {
  const normalized = value.trim().replace(/\.+$/, "").toLocaleLowerCase()
  if (!normalized) return null

  const spec =
    fieldKey === "domainName"
      ? { typeId: "domainController", label: "domain name" }
      : fieldKey === "commonName"
        ? { typeId: "certificateAuthority", label: "CA common name" }
        : null
  if (!spec) return null

  const duplicate = nodes.find((candidate) => {
    if (candidate.id === nodeId || candidate.data.typeId !== spec.typeId)
      return false
    const candidateValue = candidate.data.config?.[fieldKey]
    return (
      candidateValue?.trim().replace(/\.+$/, "").toLocaleLowerCase() ===
      normalized
    )
  })
  return duplicate
    ? `This ${spec.label} is already used by ${duplicate.data.name}.`
    : null
}

const DOMAIN_LABEL_MAX_CHARS = 24

/** Truncates a label to `max` characters, appending an ellipsis if it was cut. */
export function truncateLabel(
  label: string,
  max = DOMAIN_LABEL_MAX_CHARS,
): string {
  return label.length > max ? `${label.slice(0, max)}…` : label
}

/**
 * Whether `node` can take on a domain relationship by being dragged into a
 * region. Domain controllers define domains (they don't join others), root CAs
 * must stay out of any domain, and a VM must be configured first.
 *
 * A Linux product appliance *is* eligible — it just takes the integration
 * relationship rather than a membership (see `domainRelationshipKind`). It was
 * excluded outright while there was nothing for it to take on.
 */
export function isDomainEligible(
  node: Node<MachineData>,
  edges: Edge[],
): boolean {
  if (node.data.typeId === "domainController") return false
  if (!isConnectable(node.data)) return false
  if (
    node.data.typeId === "certificateAuthority" &&
    (node.data.config?.caType === "Root" || caTier(node.id, edges) === "root")
  )
    return false
  return true
}

/**
 * What dropping this node inside a domain's region means.
 *
 * Two different relationships share one gesture, because to the person drawing
 * it they are the same intent — "this machine belongs to that domain" — and the
 * canvas has exactly one way to express that. What differs is everything after:
 * a Windows machine joins the forest, and a Linux product appliance stays
 * outside it and gets DNS plus trust instead.
 */
export type DomainRelationshipKind = "membership" | "integration"

export function domainRelationshipKind(
  node: Node<MachineData>,
): DomainRelationshipKind {
  return templatePlatform(node.data.typeId) === "linux"
    ? "integration"
    : "membership"
}

/** The edge type a node's domain relationship is recorded as. */
export function domainRelationshipEdgeType(node: Node<MachineData>): EdgeType {
  return domainRelationshipKind(node) === "integration"
    ? EDGE_TYPE.productIntegration
    : EDGE_TYPE.domainJoin
}

/** The node's existing domain relationship edge, of whichever kind it uses. */
export function domainRelationshipEdge(
  node: Node<MachineData>,
  edges: Edge[],
): Edge | undefined {
  const kind = domainRelationshipEdgeType(node)
  return edges.find(
    (edge) => edge.source === node.id && edge.data?.edgeType === kind,
  )
}

/** Whether this node can take on `dc`'s domain relationship, whichever it is. */
export function domainRelationshipBlockReason(
  node: Node<MachineData>,
  dc: Node<MachineData>,
  edges: Edge[],
): string | null {
  return domainRelationshipKind(node) === "integration"
    ? productIntegrationBlockReason(node, dc, edges)
    : domainJoinBlockReason(node, dc, edges)
}

/**
 * Whether a Linux product appliance can be wired to this domain controller.
 *
 * A product does *not* join the domain — it stays outside the forest with no
 * computer account. What the relationship buys is the two things a Linux
 * appliance cannot arrange for itself: its service names in the domain's DNS
 * zone, and its (self-signed) certificate in every domain machine's root store,
 * so a browser on any lab node opens it without a warning.
 */
export function productIntegrationBlockReason(
  node: Node<MachineData>,
  dc: Node<MachineData>,
  edges: Edge[],
): string | null {
  if (!isConnectable(dc.data)) {
    return `Configure ${dc.data.name} before integrating with its domain.`
  }
  if (!isConnectable(node.data)) {
    return `Configure ${node.data.name} before integrating it with a domain.`
  }
  const current = edges.find(
    (edge) =>
      edge.source === node.id &&
      edge.data?.edgeType === EDGE_TYPE.productIntegration,
  )
  if (current?.target === dc.id) {
    return `${node.data.name} already integrates with ${domainLabel(dc)}.`
  }
  if (current) {
    return `${node.data.name} already integrates with another domain.`
  }
  return null
}

/** Explains whether a node can join a specific domain through either drag/drop or the accessible action. */
export function domainJoinBlockReason(
  node: Node<MachineData>,
  dc: Node<MachineData>,
  edges: Edge[],
): string | null {
  if (!isConnectable(dc.data)) {
    return `Configure ${dc.data.name} before using its domain.`
  }
  if (node.data.typeId === "domainController") {
    return "A domain controller defines its own boundary and cannot join another domain."
  }
  if (templatePlatform(node.data.typeId) === "linux") {
    // Reachable from the drag-into-a-domain-circle path, which is specifically
    // a *join*. The product's own relationship is drawn as an edge instead, and
    // saying so is more use than refusing without a next step.
    return `${node.data.name} is a Linux product server and cannot join a domain; connect it to ${domainLabel(dc)}'s domain controller to register its DNS and trust instead.`
  }
  if (!isConnectable(node.data)) {
    return `Configure ${node.data.name} before joining it to a domain.`
  }
  if (
    node.data.typeId === "certificateAuthority" &&
    (node.data.config?.caType === "Root" || caTier(node.id, edges) === "root")
  ) {
    return `${node.data.name} is an offline root CA and must remain outside Active Directory.`
  }
  const current = edges.find(
    (edge) =>
      edge.source === node.id && edge.data?.edgeType === EDGE_TYPE.domainJoin,
  )
  if (current?.target === dc.id) {
    return `${node.data.name} already belongs to ${domainLabel(dc)}.`
  }
  return null
}

/** Backend commands that the canonical domainJoin operation expands into for this role. */
/** The generated-operations preview for whichever relationship applies. */
export function domainRelationshipOperations(
  node: Node<MachineData>,
  dc: Node<MachineData>,
  nodes: Node<MachineData>[],
): string[] {
  if (domainRelationshipKind(node) === "integration") {
    // Every one of these runs inside the product's own provision op rather than
    // as a separate staged operation, which is why connecting a product stages
    // nothing on the canvas.
    return [
      `dns.apply_resources · A records in ${domainLabel(dc)}`,
      "certsecure.make_cert · self-signed TLS certificate",
      "cert.addstore · trust it on every domain machine",
    ]
  }
  return domainJoinOperations(node, dc, nodes)
}

export function domainJoinOperations(
  node: Node<MachineData>,
  dc: Node<MachineData>,
  nodes: Node<MachineData>[],
): string[] {
  const operations = [
    `dns.set_client · use ${dc.data.name}`,
    `domain.join · ${domainLabel(dc)}`,
    "system.reboot → domain.verify",
  ]

  if (
    node.data.typeId === "certificateAuthority" ||
    node.data.typeId === "webServer"
  ) {
    operations.push("dns.apply_resources → dns.verify · A/PTR")
  }
  if (node.data.typeId === "webServer") {
    operations.push("iis.setup_certenroll · share/ACL")
  }
  if (
    node.data.typeId === "client" &&
    nodes.some(
      (candidate) =>
        candidate.data.typeId === "certificateAuthority" &&
        caTier(candidate.id, []) !== "root" &&
        candidate.data.config?.caType !== "Root",
    )
  ) {
    operations.push("cert.enroll → cert.verify · Workstation")
  }
  return operations
}

const DOMAIN_HEALTH_PRIORITY: Record<ConnectionHealth, number> = {
  [CONNECTION_HEALTH.verified]: 0,
  [CONNECTION_HEALTH.planned]: 1,
  [CONNECTION_HEALTH.applying]: 2,
  [CONNECTION_HEALTH.degraded]: 3,
  [CONNECTION_HEALTH.broken]: 4,
}

function lifecycleConnectionHealth(data: MachineData): ConnectionHealth {
  switch (data.lifecycle) {
    case LIFECYCLE.deployed:
      return CONNECTION_HEALTH.verified
    case LIFECYCLE.staged:
      return CONNECTION_HEALTH.planned
    case LIFECYCLE.deploying:
    case LIFECYCLE.provisioning:
      return CONNECTION_HEALTH.applying
    case LIFECYCLE.drifted:
    case LIFECYCLE.destroying:
      return CONNECTION_HEALTH.degraded
    case LIFECYCLE.draft:
    case LIFECYCLE.failed:
      return CONNECTION_HEALTH.broken
  }
}

function leastHealthy(states: ConnectionHealth[]): ConnectionHealth {
  return states.reduce(
    (worst, state) =>
      DOMAIN_HEALTH_PRIORITY[state] > DOMAIN_HEALTH_PRIORITY[worst]
        ? state
        : worst,
    CONNECTION_HEALTH.verified,
  )
}

export interface DomainRegionSummary {
  memberCount: number
  forestLevel: string
  forestHealth: ConnectionHealth
  domainHealth: ConnectionHealth
}

/** Rim and nested-service state derived from the forest lifecycle and membership edges. */
export function domainRegionSummary(
  dc: Node<MachineData>,
  edges: Edge[],
): DomainRegionSummary {
  const forestHealth = lifecycleConnectionHealth(dc.data)
  const memberships = edges.filter(
    (edge) =>
      edge.target === dc.id && edge.data?.edgeType === EDGE_TYPE.domainJoin,
  )
  const membershipHealth = memberships.map((edge) => {
    const health = edge.data?.health as ConnectionHealth | undefined
    return health && health in DOMAIN_HEALTH_PRIORITY
      ? health
      : CONNECTION_HEALTH.planned
  })
  const reachHealth = leastHealthy([forestHealth, ...membershipHealth])

  return {
    memberCount: memberships.length,
    forestLevel: dc.data.config?.forestLevel ?? "Forest level pending",
    forestHealth,
    domainHealth: reachHealth,
  }
}

/**
 * The configured domain controller whose region contains `node`. When regions
 * overlap, the nearest DC wins so membership is unambiguous.
 */
export function findDomainForNode(
  node: Node<MachineData>,
  nodes: Node<MachineData>[],
  edges: Edge[],
): Node<MachineData> | null {
  const c = nodeCenter(node)
  let best: Node<MachineData> | null = null
  let bestDist = Infinity
  for (const dc of nodes) {
    if (dc.id === node.id) continue
    if (dc.data.typeId !== "domainController") continue
    if (!isConnectable(dc.data)) continue
    const dcc = nodeCenter(dc)
    const dist = Math.hypot(c.x - dcc.x, c.y - dcc.y)
    if (dist <= domainRadius(dc, nodes, edges) && dist < bestDist) {
      best = dc
      bestDist = dist
    }
  }
  return best
}

/**
 * The nearest domain circle touched by any part of a node. This is stricter
 * than membership's centre-point test and is used for nodes that must never
 * occupy a domain (offline roots and unconfigured machines).
 */
export function findDomainOverlappingNode(
  node: Node<MachineData>,
  nodes: Node<MachineData>[],
  edges: Edge[],
): Node<MachineData> | null {
  let best: Node<MachineData> | null = null
  let bestDist = Infinity
  for (const dc of nodes) {
    if (dc.id === node.id) continue
    if (dc.data.typeId !== "domainController" || !isConnectable(dc.data))
      continue
    const center = nodeCenter(dc)
    const centerDist = Math.hypot(
      nodeCenter(node).x - center.x,
      nodeCenter(node).y - center.y,
    )
    if (nodeOverlapsDomain(node, dc, nodes, edges) && centerDist < bestDist) {
      best = dc
      bestDist = centerDist
    }
  }
  return best
}

/** True when a node rectangle touches any part of one specific domain circle. */
export function nodeOverlapsDomain(
  node: Node<MachineData>,
  dc: Node<MachineData>,
  nodes: Node<MachineData>[],
  edges: Edge[],
): boolean {
  const rect = nodeRect(node)
  const center = nodeCenter(dc)
  const closestX = Math.max(rect.x, Math.min(center.x, rect.x + rect.w))
  const closestY = Math.max(rect.y, Math.min(center.y, rect.y + rect.h))
  return (
    Math.hypot(closestX - center.x, closestY - center.y) <=
    domainRadius(dc, nodes, edges)
  )
}

/** First non-member that cannot legally occupy a domain's current circle. */
export function domainRegionBlocker(
  dc: Node<MachineData>,
  nodes: Node<MachineData>[],
  edges: Edge[],
): { node: Node<MachineData>; reason: string } | null {
  for (const candidate of nodes) {
    if (candidate.id === dc.id) continue
    const isMember = edges.some(
      (edge) =>
        edge.source === candidate.id &&
        edge.target === dc.id &&
        edge.data?.edgeType === EDGE_TYPE.domainJoin,
    )
    if (isMember) continue
    if (!nodeOverlapsDomain(candidate, dc, nodes, edges)) continue
    const reason = domainJoinBlockReason(candidate, dc, edges)
    if (reason) return { node: candidate, reason }
  }
  return null
}

/**
 * A domain-join edge created by dropping a node into a region. Kept hidden —
 * the circle itself communicates membership — but still carries the
 * `domainJoin` edgeType so existing derived logic (badges, member counts)
 * continues to work unchanged.
 */
/**
 * The product-integration edge, drawn rather than hidden.
 *
 * `domainJoinEdge` is hidden because the domain circle already says
 * "membership" and a second line would be noise. This one stays visible on
 * purpose: it connects the same pair of shapes as a join and means something
 * different, and the labelled cyan line is what keeps a reader from assuming
 * the Linux box was domain-joined.
 */
export function productIntegrationEdge(
  source: string,
  target: string,
  staged = false,
): Edge {
  const visual = edgeStyle(EDGE_TYPE.productIntegration)
  return {
    id: `e-integration-${source}-${target}`,
    source,
    target,
    type: "capability",
    data: {
      edgeType: EDGE_TYPE.productIntegration,
      ports: connectionPorts(EDGE_TYPE.productIntegration),
      staged,
      health: staged ? CONNECTION_HEALTH.planned : CONNECTION_HEALTH.verified,
    },
    ...visual,
    style: staged ? { ...visual.style, opacity: 0.6 } : visual.style,
  }
}

export function domainJoinEdge(
  source: string,
  target: string,
  staged = false,
): Edge {
  const visual = edgeStyle(EDGE_TYPE.domainJoin)
  return {
    id: `e-domain-${source}-${target}`,
    source,
    target,
    type: "capability",
    hidden: true,
    data: {
      edgeType: EDGE_TYPE.domainJoin,
      ports: connectionPorts(EDGE_TYPE.domainJoin),
      staged,
      health: staged ? CONNECTION_HEALTH.planned : CONNECTION_HEALTH.verified,
    },
    ...visual,
    style: staged
      ? { ...visual.style, strokeDasharray: "6 4", opacity: 0.6 }
      : visual.style,
  }
}

// ---------------------------------------------------------------------------
// Node overlap prevention
// ---------------------------------------------------------------------------
//
// Nodes shouldn't be droppable on top of one another. `findOverlappingId` is
// used live during a drag (to flag the offending node), `nearestFreePosition`
// on drop (to relocate it).

const OVERLAP_GAP = 12

export interface Rect {
  x: number
  y: number
  w: number
  h: number
}

/** Bounding rect of a node in flow coordinates (position is the top-left corner). */
export function nodeRect(node: Node<MachineData>): Rect {
  const w = node.measured?.width ?? 160
  const h = node.measured?.height ?? 80
  return { x: node.position.x, y: node.position.y, w, h }
}

/** Axis-aligned bounding-box intersection, with an optional clearance gap. */
export function rectsOverlap(a: Rect, b: Rect, gap = 0): boolean {
  return (
    a.x < b.x + b.w + gap &&
    a.x + a.w + gap > b.x &&
    a.y < b.y + b.h + gap &&
    a.y + a.h + gap > b.y
  )
}

/** The id of the first node in `others` whose rect intersects `node`'s, if any. */
export function findOverlappingId(
  node: Node<MachineData>,
  others: Node<MachineData>[],
  gap = OVERLAP_GAP,
): string | null {
  const rect = nodeRect(node)
  for (const other of others) {
    if (other.id === node.id) continue
    if (rectsOverlap(rect, nodeRect(other), gap)) return other.id
  }
  return null
}

/**
 * Nearest position to `desired` (anchored there) where `node`'s rect clears
 * every rect in `others`. Searches outward in rings of increasing radius,
 * sampling a fixed number of angles per ring, so it's deterministic and
 * always terminates even on a densely packed canvas.
 */
export function nearestFreePosition(
  node: Node<MachineData>,
  others: Node<MachineData>[],
  desired: { x: number; y: number },
  gap = OVERLAP_GAP,
): { x: number; y: number } {
  const w = node.measured?.width ?? 160
  const h = node.measured?.height ?? 80
  const otherRects = others.filter((o) => o.id !== node.id).map(nodeRect)

  const fits = (pos: { x: number; y: number }) => {
    const rect: Rect = { x: pos.x, y: pos.y, w, h }
    return otherRects.every((o) => !rectsOverlap(rect, o, gap))
  }

  if (fits(desired)) return desired

  const STEP = 24
  const ANGLE_STEPS = 16
  const MAX_RADIUS = STEP * 40 // bounded search — always terminates

  for (let radius = STEP; radius <= MAX_RADIUS; radius += STEP) {
    for (let i = 0; i < ANGLE_STEPS; i++) {
      const angle = (i / ANGLE_STEPS) * Math.PI * 2
      const candidate = {
        x: desired.x + Math.cos(angle) * radius,
        y: desired.y + Math.sin(angle) * radius,
      }
      if (fits(candidate)) return candidate
    }
  }

  return desired // shouldn't realistically be reached
}

// ---------------------------------------------------------------------------
// Connection validation
// ---------------------------------------------------------------------------

export interface CanConnectResult {
  ok: boolean
  reason?: string
}

export function canConnect(
  sourceId: string,
  targetId: string,
  nodes: Node<MachineData>[],
  edges: Edge[],
): CanConnectResult {
  if (sourceId === targetId) {
    return { ok: false, reason: "Cannot connect a node to itself." }
  }

  const source = nodes.find((n) => n.id === sourceId)
  const target = nodes.find((n) => n.id === targetId)

  if (!source || !target) {
    return { ok: false, reason: "Node not found." }
  }

  if (!isConnectable(source.data)) {
    return {
      ok: false,
      reason: `"${source.data.name}" must be configured first.`,
    }
  }
  if (!isConnectable(target.data)) {
    return {
      ok: false,
      reason: `"${target.data.name}" must be configured first.`,
    }
  }

  if (target.data.typeId === "domainController") {
    const reason =
      templatePlatform(source.data.typeId) === "linux"
        ? productIntegrationBlockReason(source, target, edges)
        : domainJoinBlockReason(source, target, edges)
    return reason ? { ok: false, reason } : { ok: true }
  }

  const isCaToCa =
    source.data.typeId === "certificateAuthority" &&
    target.data.typeId === "certificateAuthority"
  const isCaToWebServer =
    source.data.typeId === "certificateAuthority" &&
    target.data.typeId === "webServer"

  if (!isCaToCa && !isCaToWebServer) {
    return {
      ok: false,
      reason:
        "Certificate Authorities can only connect to another CA or a Web Server.",
    }
  }

  const edgeType = inferEdgeType(source.data.typeId, target.data.typeId)

  if (
    edgeType === EDGE_TYPE.caHierarchy &&
    (source.data.config?.caType === "Issuing" ||
      caTier(sourceId, edges) === "issuing")
  ) {
    return { ok: false, reason: "3+ Tier PKI is not supported yet." }
  }

  const duplicate = edges.some(
    (e) =>
      (e.source === sourceId && e.target === targetId) ||
      (e.source === targetId && e.target === sourceId),
  )
  if (duplicate && edgeType !== EDGE_TYPE.webServerCert) {
    return {
      ok: false,
      reason: "A connection between these nodes already exists.",
    }
  }

  // Root CAs must not be domain-joined
  if (edgeType === EDGE_TYPE.domainJoin) {
    const sourceTier = caTier(sourceId, edges)
    if (
      source.data.typeId === "certificateAuthority" &&
      sourceTier === "root"
    ) {
      return {
        ok: false,
        reason: "Root CAs must not be domain-joined.",
      }
    }
  }

  // CA hierarchy must remain a tree: one parent per CA, no cycles
  if (edgeType === EDGE_TYPE.caHierarchy) {
    // Each CA can have at most one issuing parent
    const targetAlreadyHasParent = edges.some(
      (e) =>
        e.target === targetId && e.data?.edgeType === EDGE_TYPE.caHierarchy,
    )
    if (targetAlreadyHasParent) {
      return {
        ok: false,
        reason:
          "This CA already has an issuer — a CA can have only one parent.",
      }
    }

    // Prevent loops: the target must not already be an ancestor of the source
    if (isAncestor(targetId, sourceId, edges)) {
      return {
        ok: false,
        reason: "That would create a loop in the CA hierarchy.",
      }
    }
  }

  return { ok: true }
}

/**
 * Live gesture validation for labeled sockets. React Flow calls this while a
 * connection is still being dragged, so invalid roles, cycles, and second CA
 * parents resist the gesture before a drop can create an edge.
 */
export function canConnectServiceSockets(
  connection: ServiceSocketConnection,
  nodes: Node<MachineData>[],
  edges: Edge[],
): CanConnectResult {
  const sourceHandle = parseServiceSocketHandle(connection.sourceHandle)
  const targetHandle = parseServiceSocketHandle(connection.targetHandle)
  if (!sourceHandle || !targetHandle) {
    return {
      ok: false,
      reason: "Use a labeled service socket to create this relationship.",
    }
  }
  if (sourceHandle.socket !== targetHandle.socket) {
    return {
      ok: false,
      reason: "These service sockets provide different capabilities.",
    }
  }
  if (!serviceSocketEdgeType(connection, nodes)) {
    return {
      ok: false,
      reason: "That service socket is not supported by this destination.",
    }
  }
  const sourceSocket = sourceHandle.socket
  const duplicate = edges.some(
    (edge) =>
      edge.source === connection.source &&
      edge.target === connection.target &&
      edgeServiceSocket(edge) === sourceSocket,
  )
  if (duplicate) {
    return {
      ok: false,
      reason: `${SERVICE_SOCKET_GUIDANCE[sourceSocket].label} is already connected.`,
    }
  }
  return canConnect(connection.source, connection.target, nodes, edges)
}

/** Concrete unmet prerequisites shown in the live socket preview. */
export function connectionMissingRequirements(
  type: EdgeType,
  sourceId: string,
  targetId: string,
  nodes: Node<MachineData>[],
  edges: Edge[],
  serviceSocket: ServiceSocket = SERVICE_SOCKET.ocsp,
): string[] {
  if (type !== EDGE_TYPE.webServerCert) return []
  const source = nodes.find((node) => node.id === sourceId)
  const target = nodes.find((node) => node.id === targetId)
  if (!source || !target) return ["Both endpoints must still exist"]

  const missing: string[] = []
  const rootIssuer =
    source.data.config?.caType === "Root" || caTier(source.id, edges) === "root"
  if (rootIssuer) return missing
  if (!rootIssuer && !caParent(source.id, edges)) {
    missing.push(`${source.data.name} still needs a root CA parent`)
  }
  const sourceDomain = edges.find(
    (edge) =>
      edge.source === source.id && edge.data?.edgeType === EDGE_TYPE.domainJoin,
  )?.target
  const targetDomain = edges.find(
    (edge) =>
      edge.source === target.id && edge.data?.edgeType === EDGE_TYPE.domainJoin,
  )?.target
  if (!sourceDomain || sourceDomain !== targetDomain) {
    missing.push(
      `${source.data.name} and ${target.data.name} must share an AD domain`,
    )
  }
  if (
    serviceSocket === SERVICE_SOCKET.ocsp &&
    target.data.config?.enableOcsp === "Disabled"
  ) {
    missing.push(`${target.data.name} must enable Online Responder`)
  }
  return missing
}

// ---------------------------------------------------------------------------
// Edge visual style
// ---------------------------------------------------------------------------

export interface EdgeStyleProps {
  style: React.CSSProperties
  animated: boolean
  label: string
  labelStyle: React.CSSProperties
}

export interface EdgeStyleOptions {
  /**
   * Set when the source CA is a Root CA — dashes the line to mark that the
   * offline root's CDP/AIA is published to the web server indirectly (copied via
   * CA02), not over a live connection the way an online issuing CA publishes.
   */
  rootIssuer?: boolean
  serviceSocket?: ServiceSocket | null
}

export function edgeStyle(
  type: EdgeType,
  opts?: EdgeStyleOptions,
): EdgeStyleProps {
  switch (type) {
    case EDGE_TYPE.domainJoin:
      return {
        style: { stroke: "#3b82f6", strokeWidth: 2 },
        animated: false,
        label: "domain join",
        labelStyle: { fill: "#3b82f6", fontSize: 11 },
      }
    case EDGE_TYPE.caHierarchy:
      // A root issuer signs its subordinate offline: the CSR/cert cross the
      // air gap by hand (the backend relay), never a live link — dash the
      // edge and say so, matching the offline-root presentation.
      return {
        style: {
          stroke: "#f59e0b",
          strokeWidth: 2,
          ...(opts?.rootIssuer ? { strokeDasharray: "6 4" } : {}),
        },
        animated: false,
        label: opts?.rootIssuer
          ? "issues CA cert · offline relay"
          : "issues CA certificate",
        labelStyle: { fill: "#f59e0b", fontSize: 11 },
      }
    case EDGE_TYPE.webServerCert: {
      const ocsp = opts?.serviceSocket === SERVICE_SOCKET.ocsp
      const color = ocsp ? "#8b5cf6" : "#10b981"
      return {
        style: {
          stroke: color,
          strokeWidth: 2,
          ...(opts?.rootIssuer
            ? { strokeDasharray: "1 6", strokeLinecap: "round" }
            : {}),
        },
        animated: false,
        label: ocsp ? "enables OCSP" : "publishes CDP/AIA",
        labelStyle: { fill: color, fontSize: 11 },
      }
    }
    case EDGE_TYPE.productIntegration:
      // Distinct from the blue `domain join` in colour *and* label: the two
      // connect the same pair of shapes and mean different things, and reading
      // one as the other is the whole misconception this edge exists to avoid.
      return {
        style: { stroke: "#06b6d4", strokeWidth: 2, strokeDasharray: "4 3" },
        animated: false,
        label: "DNS + trust",
        labelStyle: { fill: "#06b6d4", fontSize: 11 },
      }
    case EDGE_TYPE.network:
      return {
        style: { stroke: "#94a3b8", strokeWidth: 1.5, strokeDasharray: "5 4" },
        animated: false,
        label: "",
        labelStyle: { fill: "#94a3b8", fontSize: 11 },
      }
  }
}
