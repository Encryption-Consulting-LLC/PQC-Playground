/**
 * Pre-configured PKI starter topology (the "Project Template" choice on the
 * project landing page).
 *
 * Rather than hand-craft nodes/edges/ops — which would have to re-implement
 * every invariant in `lib/topology.ts` and `lib/staging.ts` by hand — this
 * drives the real topology-store actions (`addNode` → `configureNode` →
 * `connect` → `applyDomainChanges`). That guarantees the result is internally
 * consistent and deploy-ready: valid CA-hierarchy tree, correct staged ops in
 * topological order, and a domain whose circle encloses its members.
 *
 * The caller (`store/projects.ts::addProjectFromTemplate`) runs this inside
 * `withSuppressedAutosave` and snapshots the working stores into the freshly
 * created project afterwards.
 *
 * The site-specific identities below (domain, NetBIOS, forest level, CA names,
 * CPS URL, lab admin password) are read from `VITE_PKI_*` env vars so they can
 * be changed per-deployment without editing code — see `frontend/.env.example`.
 *
 * Node positions are the HMHS reference lab's, translated as a block into
 * positive space so the whole topology sits inside `DEFAULT_VIEWPORT` rather
 * than off its top-left corner. They are laid out by hand rather than derived,
 * because the domain circle's radius follows its farthest member: the earlier
 * spacing sat the web server almost on top of the DC and pushed the issuing CA
 * against the rim, so the circle read as a blob rather than a boundary.
 */

import { DEFAULT_VIEWPORT, useTopologyStore } from "@/store/topology"
import { useStagingStore } from "@/store/staging"
import {
  domainLabel,
  domainRelationshipKind,
  serviceSocketHandleId,
} from "@/lib/topology"
import { SERVICE_SOCKET } from "@/constants/topology"
import { projectNetbiosPrefix } from "@/lib/projectNaming"
import type { DomainSyncChange } from "@/store/topology"

/** Reads a `VITE_*` env var, falling back to `fallback` when unset/blank. */
function envDefault(key: string, fallback: string): string {
  const v = (import.meta.env as Record<string, string | undefined>)[key]
  return typeof v === "string" && v.trim().length > 0 ? v : fallback
}

/**
 * Template defaults, env-overridable. The domain-admin password satisfies the
 * AD-complexity policy (`lib/passwordPolicy.ts`: ≥12 chars, ≥3 classes, no
 * "Administrator"/machine name) so the template deploys without edits — it's a
 * throwaway lab credential, meant to be changed for anything real.
 */
const PKI = {
  domainName: envDefault("VITE_PKI_DOMAIN_NAME", "encon.pki"),
  netbiosName: envDefault("VITE_PKI_NETBIOS_NAME", "ENCON"),
  forestLevel: envDefault("VITE_PKI_FOREST_LEVEL", "Windows Server 2016"),
  domainAdminPassword: envDefault(
    "VITE_PKI_DOMAIN_ADMIN_PASSWORD",
    "EcPkiLab#2026Key",
  ),
  rootCaCn: envDefault("VITE_PKI_ROOT_CA_CN", "EC-Root-CA"),
  issuingCaCn: envDefault("VITE_PKI_ISSUING_CA_CN", "EC-Issuing-CA"),
  cpsUrl: envDefault("VITE_PKI_CPS_URL", "http://pki.encon.pki/cps.txt"),
  keyAlgorithm: envDefault("VITE_PKI_KEY_ALGORITHM", "ML-DSA-87"),
}

/**
 * Resets the working topology + staging stores, then builds a two-tier ADCS
 * lab: an offline Root CA signing an enterprise Issuing CA, a Domain
 * Controller, and an IIS web server publishing CDP/AIA. The CA/web are wired
 * up and the online members domain-joined. Every VM is left `staged`, so the
 * project is one Deploy away from real clones.
 *
 * `netbiosPrefixed` seeds the DC's NetBIOS name with the per-project prefix that
 * keeps guest labs distinct in the shared pool. Passed in rather than read off
 * the auth store so this stays a pure store-builder; the caller
 * (`store/projects.ts`) knows the role.
 */
export function buildPkiTemplateIntoStores(
  projectId?: string,
  { netbiosPrefixed = true }: { netbiosPrefixed?: boolean } = {},
) {
  // Fresh slate — clear any graph the previous project left in the stores.
  useStagingStore.getState().loadOps([], null)
  useTopologyStore.getState().loadSnapshot([], [], {}, DEFAULT_VIEWPORT)

  // addNode auto-names and doesn't return the id, so read the just-appended
  // node back off the store before configuring it (which stages its createVm).
  const addConfigured = (
    typeId: string,
    name: string,
    position: { x: number; y: number },
    config?: Record<string, string>,
  ): string | null => {
    const store = useTopologyStore.getState()
    store.addNode(typeId, position)
    const node = useTopologyStore.getState().nodes.at(-1)
    if (!node) return null
    useTopologyStore.getState().renameNode(node.id, name)
    useTopologyStore.getState().configureNode(node.id, config)
    return node.id
  }

  const rootId = addConfigured(
    "certificateAuthority",
    "CA01",
    { x: 80, y: 60 },
    {
      caType: "Root",
      commonName: PKI.rootCaCn,
      keyAlgorithm: PKI.keyAlgorithm,
      validityYears: "20",
    },
  )
  const issuingId = addConfigured(
    "certificateAuthority",
    "CA02",
    { x: 441, y: 380 },
    {
      caType: "Issuing",
      commonName: PKI.issuingCaCn,
      keyAlgorithm: PKI.keyAlgorithm,
      validityYears: "10",
      cpsUrl: PKI.cpsUrl,
    },
  )
  const dcId = addConfigured(
    "domainController",
    "DC01",
    { x: 813, y: 494 },
    {
      domainName: PKI.domainName,
      netbiosName: netbiosPrefixed
        ? `${projectNetbiosPrefix(projectId ?? null)}${PKI.netbiosName}`
        : PKI.netbiosName,
      forestLevel: PKI.forestLevel,
      domainAdminPassword: PKI.domainAdminPassword,
    },
  )
  const webId = addConfigured(
    "webServer",
    "SRV1",
    { x: 989, y: 202 },
    {
      certEnrollPath: "C:\\CertEnroll",
      enableOcsp: "Enabled",
      ocspRefreshMinutes: "15",
    },
  )
  // Enrol the issuing CA and web server into the DC's domain (the offline root
  // stays out — roots must never be domain-joined). Membership is staged
  // before service relationships so the supplied project's persisted list
  // also reads in the semantic order the backend compiler will enforce.
  if (dcId) {
    const dc = useTopologyStore.getState().nodes.find((n) => n.id === dcId)
    if (dc) {
      const domainName = domainLabel(dc)
      const members = [issuingId, webId].filter((id): id is string => !!id)
      const changes: DomainSyncChange[] = members
        .map((nodeId): DomainSyncChange | null => {
          const node = useTopologyStore
            .getState()
            .nodes.find((n) => n.id === nodeId)
          if (!node) return null
          return {
            nodeId,
            nodeName: node.data.name,
            dcId,
            domainName,
            kind: domainRelationshipKind(node),
          }
        })
        .filter((c): c is DomainSyncChange => c !== null)
      useTopologyStore.getState().applyDomainChanges(changes)
    }
  }

  // Offline Root signs the Issuing CA (a dashed "manual transfer" edge).
  if (rootId && issuingId) {
    useTopologyStore.getState().connect({
      source: rootId,
      target: issuingId,
      sourceHandle: serviceSocketHandleId(SERVICE_SOCKET.issuance, "source"),
      targetHandle: serviceSocketHandleId(SERVICE_SOCKET.issuance, "target"),
    })
  }
  // The offline root's HTTP artifacts cross the air gap through the issuing
  // workflow; the dotted edge records that publication intent without adding
  // a live root-to-web deployment operation.
  if (rootId && webId) {
    useTopologyStore.getState().connect({
      source: rootId,
      target: webId,
      sourceHandle: serviceSocketHandleId(SERVICE_SOCKET.publication, "source"),
      targetHandle: serviceSocketHandleId(SERVICE_SOCKET.publication, "target"),
    })
  }
  // Issuing CA publishes CDP/AIA and provides OCSP through PKI web services.
  if (issuingId && webId) {
    useTopologyStore.getState().connect({
      source: issuingId,
      target: webId,
      sourceHandle: serviceSocketHandleId(SERVICE_SOCKET.publication, "source"),
      targetHandle: serviceSocketHandleId(SERVICE_SOCKET.publication, "target"),
    })
    useTopologyStore.getState().connect({
      source: issuingId,
      target: webId,
      sourceHandle: serviceSocketHandleId(SERVICE_SOCKET.ocsp, "source"),
      targetHandle: serviceSocketHandleId(SERVICE_SOCKET.ocsp, "target"),
    })
  }
}
